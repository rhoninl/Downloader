import json
import logging
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request, error
from urllib.parse import urljoin

from config import load_config


class ThreadSafeState:
    def __init__(self):
        self._lock = threading.RLock()
        self.token = None
        self.session = None
        self.last_login_ts = None
        self.last_error = None
        self.last_flush_result = None
        self.last_data_result = None
        self.last_flush_ts = None
        self.last_data_ts = None

    def set_token(self, token, session):
        with self._lock:
            self.token = token
            self.session = session
            self.last_login_ts = time.time()
            self.last_error = None

    def clear_token(self, err_msg=None):
        with self._lock:
            self.token = None
            self.session = None
            if err_msg:
                self.last_error = err_msg

    def get_token(self):
        with self._lock:
            return self.token

    def set_last_flush(self, result):
        with self._lock:
            self.last_flush_result = result
            self.last_flush_ts = time.time()

    def set_last_data(self, result):
        with self._lock:
            self.last_data_result = result
            self.last_data_ts = time.time()


class TokenManager:
    def __init__(self, cfg, state: ThreadSafeState):
        self.cfg = cfg
        self.state = state
        self._stop = threading.Event()
        self._force_login = threading.Event()
        self._thread = threading.Thread(target=self._run, name="token-manager", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._force_login.set()
        self._thread.join(timeout=5)

    def force_relogin(self):
        self._force_login.set()

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            try:
                ok = self._login_once()
                if ok:
                    attempt = 0
                    logging.info("Token acquired; next refresh in %ss", self.cfg.token_refresh_interval_sec)
                    # Wait for refresh interval or forced relogin/stop
                    signaled = self._wait_with_events(self.cfg.token_refresh_interval_sec)
                    if signaled:
                        self._force_login.clear()
                        continue
                else:
                    # failed login
                    delay = min(self.cfg.retry_max_delay_ms, self.cfg.retry_base_delay_ms * (2 ** attempt)) / 1000.0
                    logging.warning("Login failed, retrying in %.2fs (attempt %d)", delay, attempt + 1)
                    if self._wait_with_events(delay):
                        self._force_login.clear()
                    attempt = min(attempt + 1, self.cfg.retry_max)
            except Exception as e:
                logging.exception("Unexpected error in token manager: %s", e)
                delay = min(self.cfg.retry_max_delay_ms, self.cfg.retry_base_delay_ms * (2 ** attempt)) / 1000.0
                if self._wait_with_events(delay):
                    self._force_login.clear()
                attempt = min(attempt + 1, self.cfg.retry_max)

    def _wait_with_events(self, timeout_sec: float) -> bool:
        # Wait until stop or force_login is set, or timeout
        end = time.time() + timeout_sec
        while time.time() < end:
            if self._stop.is_set() or self._force_login.is_set():
                return True
            time.sleep(0.1)
        return False

    def _login_once(self) -> bool:
        url = urljoin(self.cfg.device_base_url + '/', 'api/login')
        payload = {
            "username": self.cfg.device_username,
            "password": self.cfg.device_password_md5,
        }
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
        }
        data = json.dumps(payload).encode('utf-8')
        req = request.Request(url, data=data, headers=headers, method='POST')
        try:
            ctx = self.cfg.ssl_context()
            with request.urlopen(req, timeout=self.cfg.request_timeout_sec, context=ctx) as resp:
                body = resp.read()
                text = body.decode('utf-8', errors='replace')
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    logging.error("Login response is not JSON: %s", text)
                    self.state.clear_token("non-json login response")
                    return False
                if 'token' in obj and obj.get('code') in (0, None):
                    token = obj.get('token')
                    session = obj.get('session')
                    self.state.set_token(token, session)
                    logging.info("Logged in to device, session=%s", session)
                    return True
                else:
                    msg = obj.get('message') or obj.get('msg') or 'login failed'
                    logging.error("Login error: %s", msg)
                    self.state.clear_token(msg)
                    return False
        except error.HTTPError as e:
            text = e.read().decode('utf-8', errors='replace')
            logging.error("HTTPError during login: %s %s", e, text)
            self.state.clear_token(f"HTTPError {e.code}")
            return False
        except Exception as e:
            logging.error("Error during login: %s", e)
            self.state.clear_token(str(e))
            return False


class DeviceProxy:
    def __init__(self, cfg, state: ThreadSafeState, token_mgr: TokenManager):
        self.cfg = cfg
        self.state = state
        self.token_mgr = token_mgr

    def _do_request(self, method: str, path: str, body_obj: dict, need_token: bool = True) -> tuple[int, dict | str]:
        # Ensure token availability if needed
        token = self.state.get_token() if need_token else None
        if need_token and not token:
            # Trigger a relogin and wait up to configured timeout
            self.token_mgr.force_relogin()
            deadline = time.time() + self.cfg.token_wait_timeout_sec
            while time.time() < deadline:
                token = self.state.get_token()
                if token:
                    break
                time.sleep(0.1)
            if not token:
                return 503, {"error": "token_unavailable", "message": "Device token not available"}

        url = urljoin(self.cfg.device_base_url + '/', path.lstrip('/'))
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
        }
        if need_token and token:
            headers["token"] = token

        data_bytes = json.dumps(body_obj).encode('utf-8') if body_obj is not None else None
        req = request.Request(url, data=data_bytes, headers=headers, method=method)
        ctx = self.cfg.ssl_context()

        try:
            with request.urlopen(req, timeout=self.cfg.request_timeout_sec, context=ctx) as resp:
                status = resp.getcode()
                text = resp.read().decode('utf-8', errors='replace')
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    obj = text
                return status, obj
        except error.HTTPError as e:
            text = e.read().decode('utf-8', errors='replace')
            # If unauthorized, force a relogin once and retry
            if e.code in (401, 403) and need_token:
                logging.warning("Auth error (%s). Forcing relogin and retrying once.", e.code)
                self.token_mgr.force_relogin()
                # small wait for token
                deadline = time.time() + min(2, self.cfg.token_wait_timeout_sec)
                while time.time() < deadline:
                    if self.state.get_token():
                        break
                    time.sleep(0.1)
                # Retry once
                return self._do_request(method, path, body_obj, need_token=True)
            logging.error("Device HTTPError %s: %s", e.code, text)
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                obj = {"error": "device_http_error", "status": e.code, "body": text}
            return e.code, obj
        except Exception as e:
            logging.error("Device request error: %s", e)
            return 502, {"error": "device_request_failed", "message": str(e)}


class ESLHandler(BaseHTTPRequestHandler):
    # Shared components set by server initialization
    proxy: DeviceProxy = None
    state: ThreadSafeState = None

    def _send_json(self, status: int, obj):
        body = json.dumps(obj).encode('utf-8') if not isinstance(obj, (bytes, bytearray)) else obj
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode('utf-8'))
        except json.JSONDecodeError:
            return None

    def log_message(self, format, *args):
        # Bridge to logging
        logging.info("%s - - %s", self.address_string(), format % args)

    def do_POST(self):
        if self.path == '/esl/flush':
            body = self._read_json()
            if not isinstance(body, dict) or 'tag' not in body or not body['tag']:
                return self._send_json(400, {"error": "bad_request", "message": "JSON body with 'tag' is required"})
            status, obj = self.proxy._do_request('POST', '/api/esl/eslflush', {"tag": str(body['tag'])}, need_token=True)
            if 200 <= status < 300:
                ESLHandler.state.set_last_flush(obj)
                return self._send_json(200, obj)
            else:
                return self._send_json(502 if status >= 500 else status, obj)
        else:
            self.send_error(404, "Not Found")

    def do_PUT(self):
        if self.path == '/esl/data':
            body = self._read_json()
            if not isinstance(body, dict):
                return self._send_json(400, {"error": "bad_request", "message": "JSON body is required"})
            # Forward body as-is to device
            status, obj = self.proxy._do_request('POST', '/api/esl/productalert', body, need_token=True)
            if 200 <= status < 300:
                ESLHandler.state.set_last_data(obj)
                return self._send_json(200, obj)
            else:
                return self._send_json(502 if status >= 500 else status, obj)
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        # Only explicitly required endpoints are implemented
        self.send_error(405, "Method Not Allowed")

    def do_DELETE(self):
        self.send_error(405, "Method Not Allowed")


class Server:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = ThreadSafeState()
        self.token_mgr = TokenManager(cfg, self.state)
        self.proxy = DeviceProxy(cfg, self.state, self.token_mgr)
        self.httpd = ThreadingHTTPServer((cfg.http_host, cfg.http_port), ESLHandler)
        ESLHandler.proxy = self.proxy
        ESLHandler.state = self.state

    def start(self):
        logging.info("Starting token manager...")
        self.token_mgr.start()
        logging.info("HTTP server listening on %s:%s", self.cfg.http_host, self.cfg.http_port)
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self):
        logging.info("Shutting down HTTP server...")
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        logging.info("Stopping token manager...")
        try:
            self.token_mgr.stop()
        except Exception:
            pass
        logging.info("Shutdown complete.")


def main():
    cfg = load_config()
    logging.basicConfig(level=cfg.log_level, format='[%(asctime)s] %(levelname)s: %(message)s')

    srv = Server(cfg)

    # Graceful shutdown on SIGTERM
    def handle_sigterm(signum, frame):
        logging.info("Received signal %s, shutting down...", signum)
        srv.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    srv.start()


if __name__ == '__main__':
    main()
