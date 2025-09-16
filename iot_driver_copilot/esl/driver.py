import json
import logging
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest
from urllib import error as urlerror
from urllib.parse import urljoin

from config import load_config, Config, ConfigError


# Global singletons will be initialized in main()
CONFIG: Config | None = None
TOKEN_MANAGER: "TokenManager" | None = None
LAST_STATE: "LastState" | None = None
HTTPD: ThreadingHTTPServer | None = None
STOP_EVENT = threading.Event()


class LastState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.last_flush_result: dict | None = None
        self.last_flush_ts: float | None = None
        self.last_data_result: dict | None = None
        self.last_data_ts: float | None = None
        self.last_login_ts: float | None = None

    def set_flush(self, result: dict) -> None:
        with self._lock:
            self.last_flush_result = result
            self.last_flush_ts = time.time()

    def set_data(self, result: dict) -> None:
        with self._lock:
            self.last_data_result = result
            self.last_data_ts = time.time()

    def set_login_ts(self) -> None:
        with self._lock:
            self.last_login_ts = time.time()


class TokenManager:
    def __init__(self, cfg: Config, last_state: LastState) -> None:
        self._cfg = cfg
        self._last_state = last_state
        self._token_lock = threading.Lock()
        self._token: str | None = None
        self._session: int | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._cfg.device_username and self._cfg.device_password:
            self._thread = threading.Thread(target=self._run, name="login-worker", daemon=True)
            self._thread.start()
            logging.info("TokenManager: background login thread started")
        else:
            logging.warning("TokenManager: DEVICE_USERNAME/PASSWORD not set; will rely on client-provided token header")

    def stop(self) -> None:
        # Nothing to do; thread checks STOP_EVENT
        pass

    def get_token(self) -> str | None:
        with self._token_lock:
            return self._token

    def set_token(self, token: str | None, session: int | None) -> None:
        with self._token_lock:
            self._token = token
            self._session = session

    def _run(self) -> None:
        backoff = self._cfg.backoff_initial
        while not STOP_EVENT.is_set():
            try:
                token, session = self._login()
                if token:
                    self.set_token(token, session)
                    self._last_state.set_login_ts()
                    logging.info("Login success; session=%s", session)
                    # On success, reset backoff and sleep until refresh interval or stop
                    backoff = self._cfg.backoff_initial
                    # Sleep in small chunks to be responsive to STOP_EVENT
                    slept = 0.0
                    interval = self._cfg.login_refresh_interval
                    while not STOP_EVENT.is_set() and slept < interval:
                        nap = min(0.5, interval - slept)
                        time.sleep(nap)
                        slept += nap
                    continue
                else:
                    logging.error("Login returned no token; will retry with backoff")
            except Exception as e:
                logging.error("Login failed: %s", e)
            # Backoff on error
            if STOP_EVENT.is_set():
                break
            logging.info("Reconnecting after %.2fs", backoff)
            time.sleep(backoff)
            backoff = min(self._cfg.backoff_max, backoff * self._cfg.backoff_factor)

    def _login(self) -> tuple[str | None, int | None]:
        payload = {
            "username": self._cfg.device_username,
            "password": self._cfg.device_password,
        }
        body = json.dumps(payload).encode("utf-8")
        url = urljoin(self._cfg.device_base_url.rstrip("/") + "/", "api/login")
        req = urlrequest.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json;charset=UTF-8")
        logging.info("Attempting login to %s", url)
        try:
            with urlrequest.urlopen(req, timeout=self._cfg.request_timeout) as resp:
                raw = resp.read()
                data = json.loads(raw.decode("utf-8", errors="replace"))
                token = data.get("token")
                session = data.get("session")
                if token:
                    return token, session
                return None, None
        except urlerror.HTTPError as e:
            msg = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Login HTTPError {e.code}: {msg}")
        except urlerror.URLError as e:
            raise RuntimeError(f"Login URLError: {e}")


class ESLHandler(BaseHTTPRequestHandler):
    server_version = "ESLDriver/1.0"

    def _send_json(self, status: int, obj: dict) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _bad_request(self, message: str) -> None:
        logging.warning("400 Bad Request: %s", message)
        self._send_json(400, {"error": message})

    def _unauthorized(self, message: str) -> None:
        logging.warning("401 Unauthorized: %s", message)
        self._send_json(401, {"error": message})

    def _method_not_allowed(self) -> None:
        self._send_json(405, {"error": "Method Not Allowed"})

    def _not_found(self) -> None:
        self._send_json(404, {"error": "Not Found"})

    def _read_json_body(self) -> dict | None:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return {}
        try:
            length = int(length_header)
        except ValueError:
            self._bad_request("Invalid Content-Length")
            return None
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._bad_request("Invalid JSON body")
            return None

    def do_POST(self) -> None:
        if self.path == "/esl/flush":
            self._handle_esl_flush()
        else:
            self._not_found()

    def do_PUT(self) -> None:
        if self.path == "/esl/data":
            self._handle_esl_data()
        else:
            self._not_found()

    def log_message(self, format: str, *args) -> None:
        # Route server logs through logging module
        logging.info("%s - - %s", self.client_address[0], format % args)

    def _resolve_token(self) -> str | None:
        # Priority: client-provided token header -> background token
        token = self.headers.get("token")
        if token:
            return token
        if TOKEN_MANAGER is not None:
            return TOKEN_MANAGER.get_token()
        return None

    def _device_request(self, path: str, body_dict: dict, token: str) -> tuple[int, dict]:
        assert CONFIG is not None
        url = urljoin(CONFIG.device_base_url.rstrip("/") + "/", path.lstrip("/"))
        body = json.dumps(body_dict).encode("utf-8")
        req = urlrequest.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json;charset=UTF-8")
        req.add_header("token", token)
        logging.info("Forwarding to device: %s", url)
        try:
            with urlrequest.urlopen(req, timeout=CONFIG.request_timeout) as resp:
                raw = resp.read()
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:
                    data = {"raw": raw.decode("utf-8", errors="replace")}
                return resp.status, data
        except urlerror.HTTPError as e:
            payload = e.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(payload)
            except Exception:
                data = {"error": payload}
            return e.code, data
        except urlerror.URLError as e:
            logging.error("Device connection error: %s", e)
            return 502, {"error": f"Device connection error: {e}"}

    def _handle_esl_flush(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        tag = body.get("tag") if isinstance(body, dict) else None
        if not tag:
            self._bad_request("Missing required field: tag")
            return
        token = self._resolve_token()
        if not token:
            self._unauthorized("No token available. Provide 'token' header or configure DEVICE_USERNAME/PASSWORD for auto-login.")
            return
        status, data = self._device_request("api/esl/eslflush", {"tag": tag}, token)
        if LAST_STATE is not None and status < 500:
            LAST_STATE.set_flush(data if isinstance(data, dict) else {"data": data})
        self._send_json(status, data if isinstance(data, dict) else {"data": data})

    def _handle_esl_data(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._bad_request("JSON body must be an object")
            return
        token = self._resolve_token()
        if not token:
            self._unauthorized("No token available. Provide 'token' header or configure DEVICE_USERNAME/PASSWORD for auto-login.")
            return
        status, data = self._device_request("api/esl/productalert", body, token)
        if LAST_STATE is not None and status < 500:
            LAST_STATE.set_data(data if isinstance(data, dict) else {"data": data})
        self._send_json(status, data if isinstance(data, dict) else {"data": data})


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def start_http_server(cfg: Config) -> ThreadingHTTPServer:
    server_address = (cfg.http_host, cfg.http_port)
    httpd = ThreadingHTTPServer(server_address, ESLHandler)
    logging.info("HTTP server listening on %s:%s", cfg.http_host, cfg.http_port)
    return httpd


def shutdown(signum=None, frame=None):
    logging.info("Shutting down (signal: %s)", signum)
    STOP_EVENT.set()
    if HTTPD is not None:
        # This will unblock serve_forever
        HTTPD.shutdown()
    logging.info("Shutdown signal processed")


def main() -> None:
    global CONFIG, TOKEN_MANAGER, LAST_STATE, HTTPD
    setup_logging()

    try:
        CONFIG = load_config()
    except ConfigError as e:
        logging.error("Configuration error: %s", e)
        raise SystemExit(2)

    LAST_STATE = LastState()
    TOKEN_MANAGER = TokenManager(CONFIG, LAST_STATE)
    TOKEN_MANAGER.start()

    # Setup signals for graceful shutdown
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    HTTPD = start_http_server(CONFIG)
    try:
        HTTPD.serve_forever(poll_interval=0.5)
    finally:
        logging.info("HTTP server stopped")


if __name__ == "__main__":
    main()
