import json
import logging
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from config import Config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(threadName)s %(message)s'
)


def _join_url(base, path):
    if base.endswith('/'):
        base = base[:-1]
    if not path.startswith('/'):
        path = '/' + path
    return base + path


class DeviceClient:
    def __init__(self, cfg: Config):
        self.base_url = cfg.DEVICE_BASE_URL
        self.username = cfg.DEVICE_USERNAME
        self.password_md5 = cfg.DEVICE_PASSWORD_MD5
        self.timeout = cfg.DEVICE_TIMEOUT_SEC
        self.retry_max = cfg.DEVICE_RETRY_MAX
        self.backoff_initial_ms = cfg.DEVICE_BACKOFF_INITIAL_MS
        self.backoff_max_ms = cfg.DEVICE_BACKOFF_MAX_MS
        self.login_refresh_sec = cfg.DEVICE_LOGIN_REFRESH_SEC

        self._token = None
        self._token_lock = threading.RLock()
        self._last_login_ts = 0.0
        self._stop_event = threading.Event()
        self._login_thread = threading.Thread(target=self._login_keeper, name='LoginKeeper', daemon=True)

        # Buffers for latest operations
        self._buf_lock = threading.RLock()
        self.last_update_payload = None
        self.last_update_result = None
        self.last_refresh_payload = None
        self.last_refresh_result = None

    def start(self):
        logging.info("Starting DeviceClient login keeper thread")
        self._login_thread.start()

    def stop(self):
        logging.info("Stopping DeviceClient login keeper thread")
        self._stop_event.set()
        self._login_thread.join(timeout=5)

    def _login_keeper(self):
        # Background loop to ensure token exists and is refreshed periodically
        backoff_ms = self.backoff_initial_ms
        while not self._stop_event.is_set():
            try:
                now = time.time()
                need_login = False
                with self._token_lock:
                    if not self._token:
                        need_login = True
                    elif now - self._last_login_ts >= self.login_refresh_sec:
                        need_login = True

                if need_login:
                    self._perform_login()
                    backoff_ms = self.backoff_initial_ms
                # Sleep until next check or stop
                # Use small sleep to be responsive to stop
                for _ in range(10):
                    if self._stop_event.wait(1.0):
                        break
                continue
            except Exception as e:
                logging.error(f"Login keeper error: {e}")
                # Exponential backoff on unexpected error
                if self._stop_event.wait(backoff_ms / 1000.0):
                    break
                backoff_ms = min(self.backoff_max_ms, backoff_ms * 2)

    def _perform_login(self):
        # Retry with exponential backoff
        backoff_ms = self.backoff_initial_ms
        for attempt in range(1, self.retry_max + 1):
            try:
                url = _join_url(self.base_url, '/api/login')
                payload = {"username": self.username, "password": self.password_md5}
                data = json.dumps(payload).encode('utf-8')
                headers = {
                    'Content-Type': 'application/json;charset=UTF-8'
                }
                req = urlrequest.Request(url, data=data, headers=headers, method='POST')
                logging.info(f"Attempting device login (attempt {attempt})")
                with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode('utf-8')
                    obj = json.loads(body) if body else {}
                    if isinstance(obj, dict) and obj.get('code') == 0:
                        token = obj.get('token')
                        if not token:
                            raise RuntimeError("Login succeeded but no token returned")
                        with self._token_lock:
                            self._token = token
                            self._last_login_ts = time.time()
                        logging.info("Device login successful; token acquired")
                        return
                    else:
                        msg = obj.get('message') or obj.get('msg') or str(obj)
                        raise RuntimeError(f"Device login failed: code={obj.get('code')} msg={msg}")
            except (HTTPError, URLError, TimeoutError) as e:
                logging.warning(f"Device login network error: {e}")
            except Exception as e:
                logging.warning(f"Device login error: {e}")
            # backoff
            if attempt < self.retry_max:
                time.sleep(backoff_ms / 1000.0)
                backoff_ms = min(self.backoff_max_ms, backoff_ms * 2)
        raise RuntimeError("Device login exhausted retries")

    def ensure_logged_in(self):
        with self._token_lock:
            token_exists = self._token is not None
        if not token_exists:
            logging.info("No device token cached; logging in")
            self._perform_login()

    def _device_request(self, path, method='POST', json_body=None, need_token=True):
        # Generic request with retries and exponential backoff
        backoff_ms = self.backoff_initial_ms
        last_err = None
        for attempt in range(1, self.retry_max + 1):
            try:
                if need_token:
                    self.ensure_logged_in()
                url = _join_url(self.base_url, path)
                data = None
                headers = {
                    'Content-Type': 'application/json;charset=UTF-8'
                }
                if json_body is not None:
                    data = json.dumps(json_body).encode('utf-8')
                if need_token:
                    with self._token_lock:
                        token = self._token
                    headers['token'] = token
                req = urlrequest.Request(url, data=data, headers=headers, method=method)
                with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode('utf-8')
                    status = resp.getcode()
                    if status >= 400:
                        raise RuntimeError(f"Device HTTP error status: {status}")
                    obj = json.loads(body) if body else {}
                    if isinstance(obj, dict) and obj.get('code') == 0:
                        return obj
                    else:
                        # token might be invalid; try to re-login once by clearing token and retrying next loop
                        msg = obj.get('message') or obj.get('msg') or str(obj)
                        last_err = RuntimeError(f"Device API error: code={obj.get('code')} msg={msg}")
                        # Invalidate token and retry
                        with self._token_lock:
                            self._token = None
                        raise last_err
            except (HTTPError, URLError, TimeoutError) as e:
                last_err = e
                logging.warning(f"Device request network error (attempt {attempt}): {e}")
            except Exception as e:
                last_err = e
                logging.warning(f"Device request error (attempt {attempt}): {e}")
            if attempt < self.retry_max:
                time.sleep(backoff_ms / 1000.0)
                backoff_ms = min(self.backoff_max_ms, backoff_ms * 2)
        # Exhausted
        raise last_err if last_err else RuntimeError("Unknown device request failure")

    def update_product(self, payload: dict):
        # Store payload for buffer
        with self._buf_lock:
            self.last_update_payload = payload
        result = self._device_request('/api/esl/productalert', method='POST', json_body=payload, need_token=True)
        with self._buf_lock:
            self.last_update_result = {"ts": time.time(), "result": result}
        logging.info("Product data updated on device")
        return result

    def refresh_esl(self, tag: str):
        payload = {"tag": tag}
        with self._buf_lock:
            self.last_refresh_payload = payload
        result = self._device_request('/api/esl/eslflush', method='POST', json_body=payload, need_token=True)
        with self._buf_lock:
            self.last_refresh_result = {"ts": time.time(), "result": result}
        logging.info(f"ESL refresh triggered for tag={tag}")
        return result


class DriverHTTPRequestHandler(BaseHTTPRequestHandler):
    # These class variables are set at server start
    device_client: DeviceClient = None
    auth_token: str = None

    def _read_json(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return None
        data = self.rfile.read(length)
        try:
            return json.loads(data.decode('utf-8'))
        except Exception:
            self.send_error(400, "Invalid JSON body")
            return None

    def _require_auth(self):
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            self._send_json(401, {"error": "Unauthorized", "message": "Missing Bearer token"})
            return False
        token = auth.split(' ', 1)[1].strip()
        if token != self.auth_token:
            self._send_json(401, {"error": "Unauthorized", "message": "Invalid token"})
            return False
        return True

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json;charset=UTF-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        if self.path != '/update':
            self.send_error(404, "Not Found")
            return
        if not self._require_auth():
            return
        payload = self._read_json()
        if payload is None:
            return
        # Forward to device
        try:
            result = self.device_client.update_product(payload)
            self._send_json(200, {"status": "ok", "device": result})
        except Exception as e:
            logging.error(f"/update error: {e}")
            self._send_json(502, {"error": "DeviceError", "message": str(e)})

    def do_POST(self):
        if self.path != '/refresh':
            self.send_error(404, "Not Found")
            return
        if not self._require_auth():
            return
        payload = self._read_json()
        if payload is None:
            return
        tag = payload.get('tag') if isinstance(payload, dict) else None
        if not tag or not isinstance(tag, str):
            self._send_json(400, {"error": "BadRequest", "message": "Field 'tag' is required and must be string"})
            return
        try:
            result = self.device_client.refresh_esl(tag)
            self._send_json(200, {"status": "ok", "device": result})
        except Exception as e:
            logging.error(f"/refresh error: {e}")
            self._send_json(502, {"error": "DeviceError", "message": str(e)})

    def log_message(self, format, *args):
        # Redirect BaseHTTPRequestHandler logs to logging module
        logging.info("%s - %s" % (self.address_string(), format % args))


def run_server():
    cfg = Config()
    device_client = DeviceClient(cfg)
    device_client.start()

    # Prepare handler with shared references
    def handler_factory(*args, **kwargs):
        h = DriverHTTPRequestHandler(*args, **kwargs)
        return h

    DriverHTTPRequestHandler.device_client = device_client
    DriverHTTPRequestHandler.auth_token = cfg.AUTH_TOKEN

    server = ThreadingHTTPServer((cfg.HTTP_HOST, cfg.HTTP_PORT), DriverHTTPRequestHandler)

    stop_event = threading.Event()

    def handle_signal(signum, frame):
        logging.info(f"Received signal {signum}; shutting down...")
        stop_event.set()
        # server.shutdown is blocking; call in separate thread to avoid deadlocks
        threading.Thread(target=server.shutdown, name='HTTPShutdown').start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logging.info(f"HTTP server listening on {cfg.HTTP_HOST}:{cfg.HTTP_PORT}")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        logging.info("HTTP server stopped")
        device_client.stop()


if __name__ == '__main__':
    try:
        run_server()
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)
