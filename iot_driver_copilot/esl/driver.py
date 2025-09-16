import json
import logging
import signal
import sys
import threading
import time
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from config import load_config


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)


class DeviceClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float,
                 backoff_initial: float, backoff_max: float):
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.timeout = timeout
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max

        self._token = None
        self._session = None
        self._last_login_ts = None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._bg_thread = threading.Thread(target=self._login_background_loop, name="login-maintainer", daemon=True)

        # For logging/inspection
        self._last_device_update_ts = None

    def start(self):
        logging.info("Starting device client background login loop")
        self._bg_thread.start()

    def stop(self):
        logging.info("Stopping device client background login loop")
        self._stop_event.set()
        self._bg_thread.join(timeout=5)

    def _login_background_loop(self):
        backoff = self.backoff_initial
        first_attempt = True
        while not self._stop_event.is_set():
            try:
                ok = self._login_once()
                if ok:
                    backoff = self.backoff_initial
                    # After a successful login, wait until asked to stop or a token is invalidated on demand
                    # We sleep in small intervals to allow responsive shutdown
                    for _ in range(60 * 60):  # up to ~1 hour idle wait
                        if self._stop_event.is_set():
                            break
                        time.sleep(1)
                    continue
            except Exception as e:
                logging.error(f"Unexpected error in login loop: {e}")
            # on failure
            if first_attempt:
                logging.warning("Initial login failed; will retry with exponential backoff")
                first_attempt = False
            if self._stop_event.wait(backoff):
                break
            backoff = min(self.backoff_max, backoff * 2)

    def _login_once(self) -> bool:
        url = f"{self.base_url}/api/login"
        payload = {"username": self.username, "password": self.password}
        body = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json;charset=UTF-8")
        start = time.time()
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except (HTTPError, URLError, socket.timeout) as e:
            logging.error(f"Login HTTP error: {e}")
            return False
        except Exception as e:
            logging.error(f"Login unexpected error: {e}")
            return False
        elapsed = (time.time() - start) * 1000.0
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            logging.error(f"Login response not JSON: {e}")
            return False
        code = data.get("code")
        token = data.get("token")
        session = data.get("session")
        if code == 0 and token:
            with self._lock:
                self._token = token
                self._session = session
                self._last_login_ts = time.time()
            logging.info(f"Device login successful in {elapsed:.1f}ms; session={session}")
            return True
        else:
            logging.error(f"Login failed: {data}")
            return False

    def get_token(self):
        with self._lock:
            return self._token

    def invalidate_token(self):
        with self._lock:
            self._token = None
            self._session = None
            self._last_login_ts = None

    def _do_request(self, path: str, payload: dict, method: str = "POST", token_override: str | None = None):
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json;charset=UTF-8")
        token = token_override if token_override else self.get_token()
        if token:
            req.add_header("token", token)
        start = time.time()
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.getcode()
        except HTTPError as e:
            try:
                raw = e.read()
            except Exception:
                raw = b""
            status = e.code
        except (URLError, socket.timeout) as e:
            raise ConnectionError(f"Device request error: {e}")
        elapsed = (time.time() - start) * 1000.0
        try:
            data = json.loads(raw.decode("utf-8") if raw else "{}")
        except Exception:
            data = {"raw": (raw.decode("utf-8", errors="replace") if raw else "")}
        logging.info(f"Device request {path} completed in {elapsed:.1f}ms status={status}")
        return status, data

    def flush(self, tag: str, token_override: str | None = None):
        payload = {"tag": tag}
        status, data = self._do_request("/api/esl/eslflush", payload, method="POST", token_override=token_override)
        if data.get("code") == 0:
            self._last_device_update_ts = time.time()
        elif status == 401 or data.get("message") == "Unauthorized":
            self.invalidate_token()
        return status, data

    def update_data(self, payload: dict, token_override: str | None = None):
        status, data = self._do_request("/api/esl/productalert", payload, method="POST", token_override=token_override)
        if data.get("code") == 0:
            self._last_device_update_ts = time.time()
        elif status == 401 or data.get("message") == "Unauthorized":
            self.invalidate_token()
        return status, data


class Handler(BaseHTTPRequestHandler):
    device_client: DeviceClient = None  # injected

    server_version = "esl-driver/1.0"

    def log_message(self, format, *args):
        logging.info("%s - - %s" % (self.address_string(), format % args))

    def _read_json(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length == 0:
            return None
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return None

    def _send_json(self, status: int, obj: dict):
        payload = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path == "/esl/flush":
            self._handle_esl_flush()
        else:
            self._send_json(404, {"error": "not_found"})

    def do_PUT(self):
        if self.path == "/esl/data":
            self._handle_esl_data()
        else:
            self._send_json(404, {"error": "not_found"})

    def _handle_esl_flush(self):
        body = self._read_json()
        if not body or 'tag' not in body or not isinstance(body['tag'], str) or not body['tag']:
            self._send_json(400, {"error": "invalid_request", "detail": "JSON body with non-empty 'tag' required"})
            return
        incoming_token = self.headers.get('token')
        token_to_use = incoming_token or self.device_client.get_token()
        if not token_to_use:
            self._send_json(401, {"error": "unauthorized", "detail": "missing token and not logged in"})
            return
        try:
            status, data = self.device_client.flush(body['tag'], token_override=incoming_token)
        except ConnectionError as e:
            logging.error(f"Flush connection error: {e}")
            self._send_json(502, {"error": "bad_gateway", "detail": str(e)})
            return
        # Map device response
        http_status = 200 if data.get('code') == 0 else 502
        self._send_json(http_status, data)

    def _handle_esl_data(self):
        body = self._read_json()
        if not body or not isinstance(body, dict):
            self._send_json(400, {"error": "invalid_request", "detail": "JSON body required"})
            return
        incoming_token = self.headers.get('token')
        token_to_use = incoming_token or self.device_client.get_token()
        if not token_to_use:
            self._send_json(401, {"error": "unauthorized", "detail": "missing token and not logged in"})
            return
        try:
            status, data = self.device_client.update_data(body, token_override=incoming_token)
        except ConnectionError as e:
            logging.error(f"Update data connection error: {e}")
            self._send_json(502, {"error": "bad_gateway", "detail": str(e)})
            return
        http_status = 200 if data.get('code') == 0 else 502
        self._send_json(http_status, data)


def run():
    cfg = load_config()
    client = DeviceClient(
        base_url=cfg.device_base_url,
        username=cfg.device_username,
        password=cfg.device_password,
        timeout=cfg.request_timeout_seconds,
        backoff_initial=cfg.backoff_initial_seconds,
        backoff_max=cfg.backoff_max_seconds,
    )
    client.start()

    Handler.device_client = client

    httpd = ThreadingHTTPServer((cfg.http_host, cfg.http_port), Handler)

    shutdown_event = threading.Event()

    def handle_signal(signum, frame):
        logging.info(f"Received signal {signum}, shutting down...")
        shutdown_event.set()
        # Trigger server shutdown from another thread
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logging.info(f"HTTP server listening on {cfg.http_host}:{cfg.http_port}")
    try:
        httpd.serve_forever()
    finally:
        client.stop()
        httpd.server_close()
        logging.info("Server stopped")


if __name__ == "__main__":
    run()
