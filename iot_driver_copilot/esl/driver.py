import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


# ----------------------- Config -----------------------
class Config:
    def __init__(self) -> None:
        self.http_host = self._get_env("HTTP_HOST")
        self.http_port = self._get_int("HTTP_PORT")

        self.device_base_url = self._get_env("DEVICE_BASE_URL")
        self.device_login_path = self._get_env("DEVICE_LOGIN_PATH")
        self.device_flush_path = self._get_env("DEVICE_FLUSH_PATH")
        self.device_update_path = self._get_env("DEVICE_UPDATE_PATH")

        self.device_username = self._get_env("DEVICE_LOGIN_USERNAME")
        self.device_password = self._get_env("DEVICE_LOGIN_PASSWORD")

        self.device_timeout_sec = self._get_float("DEVICE_TIMEOUT_SECONDS")
        self.retry_max_attempts = self._get_int("RETRY_MAX_ATTEMPTS")
        self.backoff_initial_ms = self._get_int("BACKOFF_INITIAL_MS")
        self.backoff_max_ms = self._get_int("BACKOFF_MAX_MS")

        self.token_refresh_seconds = self._get_int("TOKEN_REFRESH_SECONDS")

        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    def _get_env(self, key: str) -> str:
        v = os.environ.get(key)
        if v is None or v == "":
            raise RuntimeError(f"Missing required environment variable: {key}")
        return v

    def _get_int(self, key: str) -> int:
        v = self._get_env(key)
        try:
            return int(v)
        except ValueError:
            raise RuntimeError(f"Invalid int for {key}: {v}")

    def _get_float(self, key: str) -> float:
        v = self._get_env(key)
        try:
            return float(v)
        except ValueError:
            raise RuntimeError(f"Invalid float for {key}: {v}")


# ----------------------- Shared State -----------------------
@dataclass
class SharedState:
    token: Optional[str] = None
    token_last_update_ts: Optional[float] = None
    last_login_msg: Optional[str] = None

    last_update_response: Optional[Dict[str, Any]] = None
    last_update_ts: Optional[float] = None

    last_flush_response: Optional[Dict[str, Any]] = None
    last_flush_ts: Optional[float] = None

    lock: threading.Lock = field(default_factory=threading.Lock)

    def set_token(self, token: str, msg: str = "OK") -> None:
        with self.lock:
            self.token = token
            self.token_last_update_ts = time.time()
            self.last_login_msg = msg

    def clear_token(self, msg: str = "") -> None:
        with self.lock:
            self.token = None
            self.last_login_msg = msg

    def get_token(self) -> Optional[str]:
        with self.lock:
            return self.token

    def set_last_update(self, resp: Dict[str, Any]) -> None:
        with self.lock:
            self.last_update_response = resp
            self.last_update_ts = time.time()

    def set_last_flush(self, resp: Dict[str, Any]) -> None:
        with self.lock:
            self.last_flush_response = resp
            self.last_flush_ts = time.time()


# ----------------------- HTTP Utilities -----------------------
def join_url(base: str, path: str) -> str:
    if not base.endswith('/') and not path.startswith('/'):
        return base + '/' + path
    return base + path if path.startswith('/') else urljoin(base, path)


def http_post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: float) -> Tuple[int, Dict[str, Any], bytes]:
    data = json.dumps(payload).encode('utf-8')
    req = Request(url=url, data=data, method='POST')
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.getcode()
    except HTTPError as e:
        body = e.read() if hasattr(e, 'read') else b''
        status = e.code
    except URLError as e:
        raise e

    parsed: Dict[str, Any] = {}
    if body:
        try:
            parsed = json.loads(body.decode('utf-8'))
        except Exception:
            parsed = {"raw": body.decode('utf-8', errors='replace')}
    return status, parsed, body


# ----------------------- Token Manager -----------------------
class TokenManager(threading.Thread):
    def __init__(self, cfg: Config, state: SharedState, shutdown_event: threading.Event) -> None:
        super().__init__(daemon=True)
        self.cfg = cfg
        self.state = state
        self.shutdown_event = shutdown_event

    def login_once(self) -> bool:
        url = join_url(self.cfg.device_base_url, self.cfg.device_login_path)
        payload = {"username": self.cfg.device_username, "password": self.cfg.device_password}
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        logging.info(f"Attempting login to device: {url}")
        try:
            status, parsed, _ = http_post_json(url, payload, headers, timeout=self.cfg.device_timeout_sec)
        except URLError as e:
            logging.error(f"Login URLError: {e}")
            return False
        except Exception as e:
            logging.exception(f"Login exception: {e}")
            return False

        code = parsed.get("code")
        token = parsed.get("token")
        msg = parsed.get("msg") or parsed.get("message") or f"HTTP {status}"

        if status == 200 and code == 0 and isinstance(token, str) and token:
            self.state.set_token(token, msg=msg)
            masked = token[:4] + "..." + token[-4:] if len(token) > 8 else "****"
            logging.info(f"Login successful. Token: {masked}")
            return True
        else:
            logging.error(f"Login failed. Status={status}, code={code}, msg={msg}")
            return False

    def run(self) -> None:
        delay_ms = self.cfg.backoff_initial_ms
        # Ensure initial login
        while not self.shutdown_event.is_set():
            ok = self.login_once()
            if ok:
                break
            sleep_s = min(delay_ms, self.cfg.backoff_max_ms) / 1000.0
            logging.warning(f"Login retry in {sleep_s:.2f}s")
            if self.shutdown_event.wait(timeout=sleep_s):
                return
            delay_ms = min(delay_ms * 2, self.cfg.backoff_max_ms)

        # Periodic refresh loop
        while not self.shutdown_event.is_set():
            # Sleep until refresh needed
            if self.shutdown_event.wait(timeout=self.cfg.token_refresh_seconds):
                break
            logging.info("Refreshing token...")
            ok = self.login_once()
            if not ok:
                # On refresh failure, attempt with backoff until success or shutdown
                delay_ms = self.cfg.backoff_initial_ms
                while not self.shutdown_event.is_set():
                    if self.login_once():
                        break
                    sleep_s = min(delay_ms, self.cfg.backoff_max_ms) / 1000.0
                    logging.warning(f"Token refresh retry in {sleep_s:.2f}s")
                    if self.shutdown_event.wait(timeout=sleep_s):
                        return
                    delay_ms = min(delay_ms * 2, self.cfg.backoff_max_ms)


# ----------------------- Device Bridge -----------------------
class DeviceBridge:
    def __init__(self, cfg: Config, state: SharedState):
        self.cfg = cfg
        self.state = state

    def _do_device_post(self, path: str, payload: Dict[str, Any], with_token: bool = True) -> Tuple[int, Dict[str, Any]]:
        url = join_url(self.cfg.device_base_url, path)
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        if with_token:
            token = self.state.get_token()
            if not token:
                raise RuntimeError("No token available; device not connected")
            headers["token"] = token

        attempts = 0
        delay_ms = self.cfg.backoff_initial_ms
        last_status = 0
        last_parsed: Dict[str, Any] = {}
        while attempts < self.cfg.retry_max_attempts:
            attempts += 1
            try:
                status, parsed, _ = http_post_json(url, payload, headers, timeout=self.cfg.device_timeout_sec)
                last_status, last_parsed = status, parsed
                # Device returns JSON with code==0 on success
                if status == 200 and isinstance(parsed, dict):
                    code = parsed.get("code")
                    if code == 0:
                        return status, parsed
                # Non-success: fall through to retry logic
                logging.warning(f"Device POST non-success (attempt {attempts}/{self.cfg.retry_max_attempts}), status={status}, body={parsed}")
            except URLError as e:
                logging.error(f"Device POST URLError (attempt {attempts}/{self.cfg.retry_max_attempts}): {e}")
            except Exception as e:
                logging.exception(f"Device POST exception (attempt {attempts}/{self.cfg.retry_max_attempts}): {e}")

            if attempts >= self.cfg.retry_max_attempts:
                break
            time.sleep(min(delay_ms, self.cfg.backoff_max_ms) / 1000.0)
            delay_ms = min(delay_ms * 2, self.cfg.backoff_max_ms)
        return last_status, last_parsed

    def flush(self, tag: str) -> Tuple[int, Dict[str, Any]]:
        payload = {"tag": tag}
        status, parsed = self._do_device_post(self.cfg.device_flush_path, payload, with_token=True)
        if status == 200 and isinstance(parsed, dict):
            self.state.set_last_flush(parsed)
        return status, parsed

    def update(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        status, parsed = self._do_device_post(self.cfg.device_update_path, payload, with_token=True)
        if status == 200 and isinstance(parsed, dict):
            self.state.set_last_update(parsed)
        return status, parsed


# ----------------------- HTTP Server -----------------------
class Handler(BaseHTTPRequestHandler):
    cfg: Config = None  # type: ignore
    state: SharedState = None  # type: ignore
    bridge: DeviceBridge = None  # type: ignore

    def _send_json(self, code: int, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        length = self.headers.get('Content-Length')
        if not length:
            return None, "Missing Content-Length"
        try:
            n = int(length)
        except ValueError:
            return None, "Invalid Content-Length"
        try:
            data = self.rfile.read(n)
            payload = json.loads(data.decode('utf-8'))
            if not isinstance(payload, dict):
                return None, "JSON body must be an object"
            return payload, None
        except json.JSONDecodeError:
            return None, "Invalid JSON"
        except Exception as e:
            return None, f"Error reading body: {e}"

    def log_message(self, format: str, *args: Any) -> None:
        logging.info("%s - - [%s] " + format, self.address_string(), self.log_date_time_string(), *args)

    def do_POST(self) -> None:
        try:
            if self.path == "/esl/flush":
                self.handle_esl_flush()
                return
            if self.path == "/esl/update":
                self.handle_esl_update()
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not Found"})
        except Exception as e:
            logging.exception(f"Unhandled exception in handler: {e}")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal Server Error"})

    def handle_esl_flush(self) -> None:
        payload, err = self._read_json()
        if err:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": err})
            return
        tag = payload.get("tag") if payload else None
        if not isinstance(tag, str) or not tag:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Field 'tag' (string) is required"})
            return
        token = self.state.get_token()
        if not token:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Device not connected (token not available)"})
            return
        logging.info(f"Flushing ESL tag={tag}")
        status, parsed = self.bridge.flush(tag)
        if status == 200 and isinstance(parsed, dict):
            self._send_json(HTTPStatus.OK, parsed)
        else:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "Device call failed", "device_status": status, "device_body": parsed})

    def handle_esl_update(self) -> None:
        payload, err = self._read_json()
        if err:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": err})
            return
        if not isinstance(payload, dict) or len(payload) == 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Non-empty JSON object is required"})
            return
        token = self.state.get_token()
        if not token:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Device not connected (token not available)"})
            return
        logging.info(f"Updating ESL data with fields: {list(payload.keys())}")
        status, parsed = self.bridge.update(payload)
        if status == 200 and isinstance(parsed, dict):
            self._send_json(HTTPStatus.OK, parsed)
        else:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "Device call failed", "device_status": status, "device_body": parsed})


# ----------------------- Main -----------------------
shutdown_event = threading.Event()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def main() -> None:
    cfg = Config()
    setup_logging(cfg.log_level)

    state = SharedState()
    bridge = DeviceBridge(cfg, state)

    Handler.cfg = cfg
    Handler.state = state
    Handler.bridge = bridge

    server = ThreadingHTTPServer((cfg.http_host, cfg.http_port), Handler)

    token_thread = TokenManager(cfg, state, shutdown_event)
    token_thread.start()

    def handle_sig(signum, frame):  # type: ignore
        logging.info(f"Received signal {signum}, shutting down...")
        shutdown_event.set()
        try:
            server.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    logging.info(f"HTTP server listening on {cfg.http_host}:{cfg.http_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        try:
            server.server_close()
        except Exception:
            pass
        token_thread.join(timeout=5.0)
        logging.info("Shutdown complete")


if __name__ == "__main__":
    main()
