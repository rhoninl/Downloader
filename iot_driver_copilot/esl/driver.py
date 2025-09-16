import json
import logging
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from config import load_config, Config


logger = logging.getLogger("esl_driver")


class SessionManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._token: Optional[str] = None
        self._session_id: Optional[int] = None
        self._last_login_ts: Optional[float] = None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self.cfg.device_username and self.cfg.device_password_md5:
            self._thread = threading.Thread(target=self._loop, name="SessionRefresh", daemon=True)
            self._thread.start()
        else:
            logger.info("SessionManager: DEVICE_USERNAME/DEVICE_PASSWORD_MD5 not set; will rely on client-provided token header")

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def get_token(self) -> Optional[str]:
        with self._lock:
            return self._token

    def invalidate_token(self):
        with self._lock:
            logger.info("SessionManager: invalidating token")
            self._token = None
            self._session_id = None

    def _loop(self):
        backoff = self.cfg.device_retry_backoff_initial
        while not self._stop_event.is_set():
            try:
                if self.get_token() is None:
                    ok = self._login_once()
                    if ok:
                        logger.info("SessionManager: login successful; token acquired")
                        backoff = self.cfg.device_retry_backoff_initial
                    else:
                        # Login failed; apply backoff
                        logger.error("SessionManager: login failed; retrying after backoff")
                        time.sleep(min(backoff, self.cfg.device_retry_backoff_max))
                        backoff = min(backoff * 2, self.cfg.device_retry_backoff_max)
                        continue
                # Sleep until refresh time or stop
                for _ in range(self.cfg.login_refresh_seconds):
                    if self._stop_event.is_set():
                        return
                    time.sleep(1)
                # Time to refresh
                ok = self._login_once()
                if ok:
                    logger.info("SessionManager: token refreshed")
                    backoff = self.cfg.device_retry_backoff_initial
                else:
                    logger.warning("SessionManager: token refresh failed; invalidating token")
                    self.invalidate_token()
            except Exception as e:
                logger.exception("SessionManager loop error: %s", e)
                time.sleep(1)

    def _login_once(self) -> bool:
        if not (self.cfg.device_username and self.cfg.device_password_md5):
            return False
        payload = {
            "username": self.cfg.device_username,
            "password": self.cfg.device_password_md5,
        }
        url = f"{self.cfg.device_base_url}/api/login"
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        try:
            status, body = http_json_request("POST", url, headers, payload, self.cfg.device_timeout_seconds)
            if status != 200:
                logger.error("Login HTTP status %s", status)
                return False
            if not isinstance(body, dict):
                logger.error("Login response not JSON object: %s", body)
                return False
            code = body.get("code")
            if code == 0:
                token = body.get("token")
                sess = body.get("session")
                if not token:
                    logger.error("Login succeeded but no token in response")
                    return False
                with self._lock:
                    self._token = token
                    self._session_id = sess
                    self._last_login_ts = time.time()
                logger.info("Logged in; session=%s", str(sess))
                return True
            else:
                logger.error("Login API code != 0: %s", body)
                return False
        except Exception as e:
            logger.error("Login exception: %s", e)
            return False


class DeviceClient:
    def __init__(self, cfg: Config, session_mgr: SessionManager):
        self.cfg = cfg
        self.session_mgr = session_mgr

    def post_with_retry(self, path: str, payload: dict, token: Optional[str]) -> Tuple[int, object]:
        # Returns (status_code, json_body)
        url = f"{self.cfg.device_base_url}{path}"
        attempt = 0
        backoff = self.cfg.device_retry_backoff_initial
        last_error: Optional[str] = None
        used_session_token = False
        if token is None:
            # try session manager token
            token = self.session_mgr.get_token()
            used_session_token = token is not None
        while True:
            headers = {"Content-Type": "application/json;charset=UTF-8"}
            if token:
                headers["token"] = token
            try:
                status, body = http_json_request("POST", url, headers, payload, self.cfg.device_timeout_seconds)
                # If unauthorized and we used session token, try to re-login once per attempt
                if status in (401, 403) and used_session_token:
                    logger.warning("Device auth failed (status %s), attempting re-login", status)
                    self.session_mgr.invalidate_token()
                    if self.session_mgr._login_once():  # direct call to refresh quickly
                        token = self.session_mgr.get_token()
                        used_session_token = token is not None
                        attempt += 1
                        if attempt > self.cfg.device_retry_max:
                            return status, body
                        continue
                # If 5xx, retry
                if status >= 500 and attempt < self.cfg.device_retry_max:
                    attempt += 1
                    sleep_for = min(backoff, self.cfg.device_retry_backoff_max)
                    logger.warning("Device HTTP %s; retrying in %.2fs (attempt %d/%d)", status, sleep_for, attempt, self.cfg.device_retry_max)
                    time.sleep(sleep_for)
                    backoff = min(backoff * 2, self.cfg.device_retry_backoff_max)
                    continue
                return status, body
            except (URLError, HTTPError) as e:
                last_error = str(e)
                if attempt < self.cfg.device_retry_max:
                    attempt += 1
                    sleep_for = min(backoff, self.cfg.device_retry_backoff_max)
                    logger.warning("Device request error: %s; retrying in %.2fs (attempt %d/%d)", last_error, sleep_for, attempt, self.cfg.device_retry_max)
                    time.sleep(sleep_for)
                    backoff = min(backoff * 2, self.cfg.device_retry_backoff_max)
                    continue
                logger.error("Device request failed after retries: %s", last_error)
                raise


# Thread-safe buffers for last operations
_last_flush = {"ts": None, "req": None, "resp": None}
_last_update = {"ts": None, "req": None, "resp": None}
_buf_lock = threading.RLock()


def http_json_request(method: str, url: str, headers: dict, payload: Optional[dict], timeout: float) -> Tuple[int, object]:
    data_bytes = None
    if method in ("POST", "PUT") and payload is not None:
        data_bytes = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url=url, data=data_bytes, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            body_bytes = resp.read()
            body_text = body_bytes.decode("utf-8") if body_bytes else ""
            try:
                body_json = json.loads(body_text) if body_text else {}
            except json.JSONDecodeError:
                body_json = {"raw": body_text}
            return status, body_json
    except HTTPError as e:
        body = e.read().decode("utf-8") if hasattr(e, 'read') else ""
        try:
            body_json = json.loads(body) if body else {"error": str(e)}
        except json.JSONDecodeError:
            body_json = {"error": str(e), "raw": body}
        return e.code, body_json


def json_response(handler: BaseHTTPRequestHandler, status: int, obj: object):
    payload = json.dumps(obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json;charset=UTF-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class ESLHandler(BaseHTTPRequestHandler):
    server_version = "ESLDriver/1.0"

    def _read_json(self) -> Tuple[Optional[dict], Optional[str]]:
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return None, "Invalid Content-Length"
        if length <= 0:
            return {}, None
        try:
            raw = self.rfile.read(length)
        except Exception as e:
            return None, f"Failed to read body: {e}"
        try:
            obj = json.loads(raw.decode('utf-8')) if raw else {}
            if not isinstance(obj, dict):
                return None, "JSON body must be an object"
            return obj, None
        except json.JSONDecodeError as e:
            return None, f"Invalid JSON: {e}"

    def log_message(self, format: str, *args):
        logger.info("%s - %s", self.address_string(), format % args)

    def do_POST(self):
        if self.path == "/esl/flush":
            self.handle_esl_flush()
        else:
            json_response(self, 404, {"error": "Not Found"})

    def do_PUT(self):
        if self.path == "/esl/data":
            self.handle_esl_update()
        else:
            json_response(self, 404, {"error": "Not Found"})

    def handle_esl_flush(self):
        body, err = self._read_json()
        if err:
            json_response(self, 400, {"error": err})
            return
        tag = None if body is None else body.get("tag")
        if not tag or not isinstance(tag, str):
            json_response(self, 400, {"error": "'tag' is required and must be a non-empty string"})
            return
        # Prefer client-provided token header; fall back to session token
        token = self.headers.get('token') or app.session_mgr.get_token()
        if token is None and not (app.cfg.device_username and app.cfg.device_password_md5):
            json_response(self, 401, {"error": "token header required or configure DEVICE_USERNAME/DEVICE_PASSWORD_MD5 for auto-login"})
            return
        payload = {"tag": tag}
        try:
            status, resp = app.device_client.post_with_retry("/api/esl/eslflush", payload, token)
        except Exception as e:
            logger.error("/esl/flush device call failed: %s", e)
            json_response(self, 502, {"error": "device request failed", "detail": str(e)})
            return
        with _buf_lock:
            _last_flush["ts"] = time.time()
            _last_flush["req"] = payload
            _last_flush["resp"] = resp
        if status == 200:
            json_response(self, 200, resp)
        else:
            json_response(self, 502, {"error": "device error", "status": status, "body": resp})

    def handle_esl_update(self):
        body, err = self._read_json()
        if err:
            json_response(self, 400, {"error": err})
            return
        if body is None or not isinstance(body, dict):
            json_response(self, 400, {"error": "JSON body is required"})
            return
        token = self.headers.get('token') or app.session_mgr.get_token()
        if token is None and not (app.cfg.device_username and app.cfg.device_password_md5):
            json_response(self, 401, {"error": "token header required or configure DEVICE_USERNAME/DEVICE_PASSWORD_MD5 for auto-login"})
            return
        try:
            status, resp = app.device_client.post_with_retry("/api/esl/productalert", body, token)
        except Exception as e:
            logger.error("/esl/data device call failed: %s", e)
            json_response(self, 502, {"error": "device request failed", "detail": str(e)})
            return
        with _buf_lock:
            _last_update["ts"] = time.time()
            _last_update["req"] = body
            _last_update["resp"] = resp
        if status == 200:
            json_response(self, 200, resp)
        else:
            json_response(self, 502, {"error": "device error", "status": status, "body": resp})


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session_mgr = SessionManager(cfg)
        self.device_client = DeviceClient(cfg, self.session_mgr)
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None

    def start(self):
        # Start session manager
        self.session_mgr.start()
        # Start HTTP server
        self.httpd = ThreadingHTTPServer((self.cfg.http_host, self.cfg.http_port), ESLHandler)
        self._server_thread = threading.Thread(target=self.httpd.serve_forever, name="HTTPServer", daemon=True)
        self._server_thread.start()
        logger.info("HTTP server started at http://%s:%d", self.cfg.http_host, self.cfg.http_port)

    def stop(self):
        logger.info("Shutting down HTTP server...")
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5)
        self.session_mgr.stop()
        logger.info("Shutdown complete")


# Global app reference for handler access
app: App


def setup_logging(level: int):
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main():
    try:
        cfg = load_config()
    except Exception as e:
        logging.basicConfig(level=logging.INFO, stream=sys.stdout)
        logging.getLogger("esl_driver").error("Configuration error: %s", e)
        sys.exit(2)

    setup_logging(cfg.log_level)
    logger.info("Starting ESL driver")

    global app
    app = App(cfg)

    def handle_signal(signum, frame):
        logger.info("Received signal %s; shutting down...", signum)
        app.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        app.start()
        # Keep main thread alive
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        handle_signal(signal.SIGINT, None)
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        try:
            app.stop()
        finally:
            sys.exit(1)


if __name__ == "__main__":
    main()
