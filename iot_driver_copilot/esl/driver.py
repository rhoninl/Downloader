import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib import request, error


class Config:
    def __init__(self) -> None:
        # All configuration must come from environment variables
        self.http_host = self._get_env("HTTP_HOST")
        self.http_port = int(self._get_env("HTTP_PORT"))
        self.esl_base_url = self._get_env("ESL_BASE_URL").rstrip("/")
        self.esl_username = self._get_env("ESL_USERNAME")
        self.esl_password_md5 = self._get_env("ESL_PASSWORD_MD5")
        self.request_timeout_seconds = float(self._get_env("REQUEST_TIMEOUT_SECONDS"))
        self.retry_base_delay_seconds = float(self._get_env("RETRY_BASE_DELAY_SECONDS"))
        self.retry_max_delay_seconds = float(self._get_env("RETRY_MAX_DELAY_SECONDS"))
        self.login_refresh_seconds = float(self._get_env("LOGIN_REFRESH_SECONDS"))

    @staticmethod
    def _get_env(name: str) -> str:
        v = os.environ.get(name)
        if v is None or v == "":
            raise RuntimeError(f"Missing required environment variable: {name}")
        return v


class DeviceClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _http_json(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any]]:
        url = f"{self.cfg.esl_base_url}{path}"
        data_bytes = None
        req_headers = headers.copy() if headers else {}
        if body is not None:
            data_bytes = json.dumps(body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json;charset=UTF-8")

        req = request.Request(url=url, data=data_bytes, headers=req_headers, method=method)
        try:
            with request.urlopen(req, timeout=self.cfg.request_timeout_seconds) as resp:
                status = resp.getcode()
                raw = resp.read()
                # Some devices may return empty body
                if not raw:
                    return status, {}
                try:
                    return status, json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    return status, {"raw": raw.decode("utf-8", errors="replace")}
        except error.HTTPError as he:
            body = he.read()
            try:
                parsed = json.loads(body.decode("utf-8", errors="replace"))
            except Exception:
                parsed = {"error": body.decode("utf-8", errors="replace")}
            return he.code, parsed
        except Exception as e:
            raise e

    def login(self) -> Tuple[str, Optional[int]]:
        payload = {"username": self.cfg.esl_username, "password": self.cfg.esl_password_md5}
        status, resp = self._http_json("POST", "/api/login", body=payload)
        if status != 200:
            raise RuntimeError(f"Login HTTP status {status}: {resp}")
        # Expected: {"code":0, "message":"OK", "session":3, "token":"..."}
        if isinstance(resp, dict) and resp.get("code") == 0 and resp.get("token"):
            return str(resp.get("token")), resp.get("session")
        raise RuntimeError(f"Login failed: {resp}")

    def esl_flush(self, token: str, tag: str) -> Dict[str, Any]:
        headers = {"token": token, "Content-Type": "application/json;charset=UTF-8"}
        payload = {"tag": tag}
        status, resp = self._http_json("POST", "/api/esl/eslflush", body=payload, headers=headers)
        if status != 200:
            raise RuntimeError(f"eslflush HTTP status {status}: {resp}")
        return resp

    def esl_productalert(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"token": token, "Content-Type": "application/json;charset=UTF-8"}
        status, resp = self._http_json("POST", "/api/esl/productalert", body=payload, headers=headers)
        if status != 200:
            raise RuntimeError(f"productalert HTTP status {status}: {resp}")
        return resp


class SharedState:
    def __init__(self):
        self._lock = threading.RLock()
        self.token: Optional[str] = None
        self.session: Optional[int] = None
        self.token_updated_at: Optional[float] = None
        self.last_flush_resp: Optional[Dict[str, Any]] = None
        self.last_data_resp: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    def set_token(self, token: str, session: Optional[int]) -> None:
        with self._lock:
            self.token = token
            self.session = session
            self.token_updated_at = time.time()

    def clear_token(self) -> None:
        with self._lock:
            self.token = None
            self.session = None
            self.token_updated_at = None

    def get_token(self) -> Optional[str]:
        with self._lock:
            return self.token

    def set_last_flush_resp(self, resp: Dict[str, Any]) -> None:
        with self._lock:
            self.last_flush_resp = resp

    def set_last_data_resp(self, resp: Dict[str, Any]) -> None:
        with self._lock:
            self.last_data_resp = resp

    def set_last_error(self, err: str) -> None:
        with self._lock:
            self.last_error = err


class LoginWorker(threading.Thread):
    def __init__(self, cfg: Config, client: DeviceClient, state: SharedState, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.client = client
        self.state = state
        self.stop_event = stop_event

    def run(self) -> None:
        attempt = 0
        next_refresh = 0.0
        while not self.stop_event.is_set():
            now = time.time()
            token = self.state.get_token()
            # Determine if we need (re)login
            need_login = False
            if token is None:
                need_login = True
            else:
                # refresh if older than refresh interval
                # Defensive: re-login before expiry even if unknown
                with state._lock:
                    updated = state.token_updated_at or 0
                if now - updated >= self.cfg.login_refresh_seconds:
                    need_login = True

            if need_login:
                try:
                    logging.info("Attempting ESL login")
                    token, session = self.client.login()
                    self.state.set_token(token, session)
                    attempt = 0
                    next_refresh = time.time() + self.cfg.login_refresh_seconds
                    logging.info("ESL login success; session=%s token_set_at=%s", session, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
                except Exception as e:
                    attempt += 1
                    delay = min(self.cfg.retry_base_delay_seconds * (2 ** (attempt - 1)), self.cfg.retry_max_delay_seconds)
                    self.state.set_last_error(str(e))
                    logging.error("ESL login failed (attempt %d): %s; retrying in %.1fs", attempt, e, delay)
                    self.stop_event.wait(delay)
                    continue
            # Sleep until next check or stop
            sleep_for = 1.0
            if next_refresh:
                sleep_for = max(0.5, min(self.cfg.login_refresh_seconds / 4.0, 5.0))
            self.stop_event.wait(sleep_for)


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = DeviceClient(cfg)
        self.state = SharedState()
        self.stop_event = threading.Event()
        self.login_worker = LoginWorker(cfg, self.client, self.state, self.stop_event)
        # Guard to avoid concurrent active login from request threads
        self._login_lock = threading.Lock()

    def start(self):
        self.login_worker.start()

    def stop(self):
        self.stop_event.set()
        # No explicit logout API; just stop worker

    def ensure_token(self) -> str:
        token = self.state.get_token()
        if token:
            return token
        # Attempt a synchronous login once
        with self._login_lock:
            token = self.state.get_token()
            if token:
                return token
            try:
                logging.info("Synchronously logging in (on-demand)")
                token, session = self.client.login()
                self.state.set_token(token, session)
                return token
            except Exception as e:
                self.state.set_last_error(str(e))
                raise

    def handle_esl_flush(self, tag: str) -> Dict[str, Any]:
        token = self.ensure_token()
        # Try once, if token invalid then re-login and retry once
        try:
            resp = self.client.esl_flush(token, tag)
            # Device convention: code==0 success
            if isinstance(resp, dict) and resp.get("code") != 0:
                # Try re-login once
                logging.warning("eslflush non-zero code: %s; attempting re-login and retry", resp)
                self._force_relogin()
                token = self.ensure_token()
                resp = self.client.esl_flush(token, tag)
            self.state.set_last_flush_resp(resp)
            return resp
        except Exception as e:
            logging.error("eslflush error: %s", e)
            self.state.set_last_error(str(e))
            raise

    def handle_esl_data(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        token = self.ensure_token()
        try:
            resp = self.client.esl_productalert(token, payload)
            if isinstance(resp, dict) and resp.get("code") != 0:
                logging.warning("productalert non-zero code: %s; attempting re-login and retry", resp)
                self._force_relogin()
                token = self.ensure_token()
                resp = self.client.esl_productalert(token, payload)
            self.state.set_last_data_resp(resp)
            return resp
        except Exception as e:
            logging.error("productalert error: %s", e)
            self.state.set_last_error(str(e))
            raise

    def _force_relogin(self):
        with self._login_lock:
            self.state.clear_token()
            # Worker will re-login, but do it immediately here
            token, session = self.client.login()
            self.state.set_token(token, session)


APP: Optional[App] = None


class Handler(BaseHTTPRequestHandler):
    server_version = "ESLDriver/1.0"

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # for /esl/flush
        if self.path == "/esl/flush":
            self._handle_esl_flush()
        else:
            self.send_error(404, "Not Found")

    def do_PUT(self):  # for /esl/data
        if self.path == "/esl/data":
            self._handle_esl_data()
        else:
            self.send_error(404, "Not Found")

    def _read_json_body(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return None, "Invalid Content-Length"
        try:
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw:
                return {}, None
            return json.loads(raw.decode("utf-8")), None
        except json.JSONDecodeError:
            return None, "Invalid JSON body"
        except Exception as e:
            return None, str(e)

    def _handle_esl_flush(self):
        global APP
        if APP is None:
            self.send_error(500, "Server not initialized")
            return
        body, err = self._read_json_body()
        if err is not None:
            self._send_json(400, {"error": err})
            return
        tag = None
        if isinstance(body, dict):
            tag = body.get("tag")
        if not tag or not isinstance(tag, str):
            self._send_json(400, {"error": "Missing or invalid 'tag'"})
            return
        try:
            resp = APP.handle_esl_flush(tag)
            self._send_json(200, resp if isinstance(resp, dict) else {"data": resp})
        except Exception as e:
            self._send_json(502, {"error": str(e)})

    def _handle_esl_data(self):
        global APP
        if APP is None:
            self.send_error(500, "Server not initialized")
            return
        body, err = self._read_json_body()
        if err is not None:
            self._send_json(400, {"error": err})
            return
        if not isinstance(body, dict) or not body:
            self._send_json(400, {"error": "Body must be a non-empty JSON object"})
            return
        try:
            resp = APP.handle_esl_data(body)
            self._send_json(200, resp if isinstance(resp, dict) else {"data": resp})
        except Exception as e:
            self._send_json(502, {"error": str(e)})

    def log_message(self, format: str, *args: Any) -> None:
        logging.info("%s - - [%s] " + format, self.client_address[0], self.log_date_time_string(), *args)


def main():
    global APP
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = Config()
    except Exception as e:
        logging.error("Configuration error: %s", e)
        raise

    APP = App(cfg)
    APP.start()

    server = ThreadingHTTPServer((cfg.http_host, cfg.http_port), Handler)

    shutdown_event = threading.Event()

    def _signal_handler(signum, frame):
        logging.info("Received signal %s, shutting down...", signum)
        shutdown_event.set()
        APP.stop()
        server.shutdown()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logging.info("HTTP server starting on %s:%d", cfg.http_host, cfg.http_port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        logging.info("HTTP server stopped")


if __name__ == "__main__":
    main()
