import json
import logging
import signal
import sys
import threading
import time
import hashlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict, Any
from urllib import request, error
from urllib.parse import urljoin

from config import config


logging.basicConfig(
    level=config.log_level,
    format='%(asctime)s %(levelname)s %(threadName)s %(message)s',
)
logger = logging.getLogger(__name__)


class DeviceState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.token: Optional[str] = None
        self.session: Optional[int] = None
        self.last_login_ts: Optional[float] = None
        self.last_login_error: Optional[str] = None
        self.last_update_ts: Optional[float] = None
        # Buffers for latest requests/responses
        self.last_flush_req: Optional[Dict[str, Any]] = None
        self.last_flush_resp: Optional[Dict[str, Any]] = None
        self.last_data_req: Optional[Dict[str, Any]] = None
        self.last_data_resp: Optional[Dict[str, Any]] = None

    def set_token(self, token: Optional[str], session: Optional[int]) -> None:
        with self._lock:
            self.token = token
            self.session = session
            self.last_login_ts = time.time() if token else None

    def set_login_error(self, err: Optional[str]) -> None:
        with self._lock:
            self.last_login_error = err

    def set_last_update(self) -> None:
        with self._lock:
            self.last_update_ts = time.time()

    def set_last_flush(self, req: Dict[str, Any], resp: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self.last_flush_req = req
            self.last_flush_resp = resp

    def set_last_data(self, req: Dict[str, Any], resp: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self.last_data_req = req
            self.last_data_resp = resp

    def get_token(self) -> Optional[str]:
        with self._lock:
            return self.token


STATE = DeviceState()
STOP_EVENT = threading.Event()


class DeviceClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def _http_json(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        url = self.base_url + path
        data_bytes = None
        if body is not None:
            data_bytes = json.dumps(body).encode('utf-8')
        req = request.Request(url=url, data=data_bytes, method=method)
        req.add_header('Content-Type', 'application/json;charset=UTF-8')
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                charset = resp.headers.get_content_charset() or 'utf-8'
                resp_bytes = resp.read()
                text = resp_bytes.decode(charset, errors='replace')
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    logger.error('Non-JSON response from device: %s', text[:200])
                    raise
        except error.HTTPError as e:
            msg = e.read().decode('utf-8', errors='replace')
            logger.error('Device HTTPError %s %s: %s', e.code, url, msg)
            raise
        except error.URLError as e:
            logger.error('Device URLError %s: %s', url, getattr(e, 'reason', e))
            raise

    def login(self, username: str, password_md5: str) -> Dict[str, Any]:
        payload = {"username": username, "password": password_md5}
        logger.info('Attempting device login for user=%s', username)
        return self._http_json('POST', '/api/login', body=payload)

    def product_alert(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"token": token}
        return self._http_json('POST', '/api/esl/productalert', body=payload, headers=headers)

    def esl_flush(self, token: str, tag: str) -> Dict[str, Any]:
        headers = {"token": token}
        payload = {"tag": tag}
        return self._http_json('POST', '/api/esl/eslflush', body=payload, headers=headers)


CLIENT = DeviceClient(config.device_base_url, config.request_timeout)


def compute_md5_hex(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()


class TokenManager(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name='TokenManager', daemon=True)
        self.base_delay = config.retry_base_delay
        self.max_backoff = config.retry_max_backoff
        self.refresh_interval = config.token_refresh_interval

    def run(self) -> None:
        delay = self.base_delay
        while not STOP_EVENT.is_set():
            username = config.device_username
            pw_md5 = config.device_password_md5
            if not pw_md5 and config.device_password:
                pw_md5 = compute_md5_hex(config.device_password)

            if username and pw_md5:
                try:
                    resp = CLIENT.login(username, pw_md5)
                    # Expecting: {"code":0, "message":"OK", "session":..., "token":"..."}
                    token = resp.get('token')
                    session = resp.get('session')
                    code = resp.get('code')
                    if token and (code == 0 or code is None):
                        STATE.set_token(token, session)
                        STATE.set_login_error(None)
                        logger.info('Device login succeeded; session=%s', str(session))
                        delay = self.base_delay
                        # Wait until refresh interval or stop
                        STOP_EVENT.wait(self.refresh_interval)
                        continue
                    else:
                        msg = resp.get('message') or resp.get('msg') or 'Unknown login response'
                        STATE.set_login_error(str(msg))
                        STATE.set_token(None, None)
                        logger.warning('Device login failed: %s', msg)
                except Exception as e:
                    STATE.set_login_error(str(e))
                    STATE.set_token(None, None)
                    logger.error('Device login exception: %s', e)
                # Backoff on failure
                sleep_for = min(delay, self.max_backoff)
                logger.info('Retrying login in %.2fs', sleep_for)
                STOP_EVENT.wait(sleep_for)
                delay = min(delay * 2.0, self.max_backoff)
            else:
                # No credentials; idle until refresh interval
                logger.debug('No device credentials provided; token manager idle')
                STOP_EVENT.wait(self.refresh_interval)


def parse_json_body(handler: BaseHTTPRequestHandler) -> Optional[Dict[str, Any]]:
    try:
        length_str = handler.headers.get('Content-Length')
        if not length_str:
            return {}
        length = int(length_str)
        data = handler.rfile.read(length) if length > 0 else b''
        if not data:
            return {}
        return json.loads(data.decode('utf-8'))
    except json.JSONDecodeError:
        send_json(handler, 400, {"error": "Invalid JSON body"})
        return None
    except Exception as e:
        logger.exception('Error reading request body')
        send_json(handler, 400, {"error": f"Failed to read request body: {e}"})
        return None


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    server_version = 'ESLDriver/1.0'

    def do_POST(self) -> None:
        if self.path == '/esl/flush':
            self._handle_esl_flush()
            return
        send_json(self, 404, {"error": "Not found"})

    def do_PUT(self) -> None:
        if self.path == '/esl/data':
            self._handle_esl_data()
            return
        send_json(self, 404, {"error": "Not found"})

    def log_message(self, format: str, *args) -> None:
        logger.info("%s - - %s", self.client_address[0], format % args)

    def _get_effective_token(self) -> Optional[str]:
        # Prefer token from client header; fallback to managed token
        token_hdr = self.headers.get('token')
        if token_hdr:
            return token_hdr
        return STATE.get_token()

    def _handle_esl_flush(self) -> None:
        body = parse_json_body(self)
        if body is None:
            return
        tag = body.get('tag') if isinstance(body, dict) else None
        if not isinstance(tag, str) or not tag:
            send_json(self, 400, {"error": "Missing or invalid 'tag'"})
            return
        token = self._get_effective_token()
        if not token:
            send_json(self, 401, {"error": "Missing token. Provide 'token' header or configure credentials for auto-login."})
            return
        logger.info('ESL flush request for tag=%s', tag)
        try:
            resp = CLIENT.esl_flush(token, tag)
            STATE.set_last_flush({"tag": tag}, resp)
            STATE.set_last_update()
            send_json(self, 200, resp)
        except error.HTTPError as he:
            if he.code in (401, 403) and not self.headers.get('token'):
                # Try re-login once and retry
                if self._attempt_relogin():
                    try:
                        resp2 = CLIENT.esl_flush(STATE.get_token() or '', tag)
                        STATE.set_last_flush({"tag": tag}, resp2)
                        STATE.set_last_update()
                        send_json(self, 200, resp2)
                        return
                    except Exception as e2:
                        logger.error('Retry flush failed: %s', e2)
            logger.error('Flush failed: %s', he)
            send_json(self, 502, {"error": f"Device error: HTTP {he.code}"})
        except Exception as e:
            logger.error('Flush exception: %s', e)
            send_json(self, 502, {"error": f"Device exception: {e}"})

    def _handle_esl_data(self) -> None:
        body = parse_json_body(self)
        if body is None:
            return
        if not isinstance(body, dict):
            send_json(self, 400, {"error": "JSON body must be an object"})
            return
        token = self._get_effective_token()
        if not token:
            send_json(self, 401, {"error": "Missing token. Provide 'token' header or configure credentials for auto-login."})
            return
        logger.info('ESL data update request')
        try:
            resp = CLIENT.product_alert(token, body)
            STATE.set_last_data(body, resp)
            STATE.set_last_update()
            send_json(self, 200, resp)
        except error.HTTPError as he:
            if he.code in (401, 403) and not self.headers.get('token'):
                if self._attempt_relogin():
                    try:
                        resp2 = CLIENT.product_alert(STATE.get_token() or '', body)
                        STATE.set_last_data(body, resp2)
                        STATE.set_last_update()
                        send_json(self, 200, resp2)
                        return
                    except Exception as e2:
                        logger.error('Retry data update failed: %s', e2)
            logger.error('Data update failed: %s', he)
            send_json(self, 502, {"error": f"Device error: HTTP {he.code}"})
        except Exception as e:
            logger.error('Data update exception: %s', e)
            send_json(self, 502, {"error": f"Device exception: {e}"})

    def _attempt_relogin(self) -> bool:
        username = config.device_username
        pw_md5 = config.device_password_md5
        if not pw_md5 and config.device_password:
            pw_md5 = compute_md5_hex(config.device_password)
        if not (username and pw_md5):
            logger.warning('Cannot relogin: missing credentials')
            return False
        try:
            resp = CLIENT.login(username, pw_md5)
            token = resp.get('token')
            session = resp.get('session')
            code = resp.get('code')
            if token and (code == 0 or code is None):
                STATE.set_token(token, session)
                STATE.set_login_error(None)
                logger.info('Re-login succeeded; session=%s', session)
                return True
            logger.warning('Re-login failed: %s', resp)
            return False
        except Exception as e:
            logger.error('Re-login exception: %s', e)
            return False


def serve() -> None:
    srv = ThreadingHTTPServer((config.http_host, config.http_port), Handler)

    def shutdown(signum=None, frame=None):
        logger.info('Shutting down (signal %s)', str(signum))
        STOP_EVENT.set()
        try:
            srv.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    token_mgr = TokenManager()
    token_mgr.start()

    logger.info('HTTP server listening on %s:%s', config.http_host, config.http_port)
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        STOP_EVENT.set()
        logger.info('Waiting for TokenManager to exit...')
        token_mgr.join(timeout=10.0)
        logger.info('Shutdown complete')


if __name__ == '__main__':
    try:
        serve()
    except Exception as e:
        logger.error('Fatal error: %s', e)
        sys.exit(1)
