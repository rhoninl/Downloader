import json
import logging
import signal
import sys
import threading
import time
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request, error, parse
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from config import get_config

# Global shared state buffer
class StateBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._state: Optional[Dict[str, Any]] = None
        self._ts: Optional[float] = None

    def update(self, state: Dict[str, Any]):
        with self._lock:
            self._state = state
            self._ts = time.time()

    def get(self):
        with self._lock:
            return (None if self._state is None else dict(self._state), self._ts)

state_buffer = StateBuffer()
stop_event = threading.Event()

class DeviceClient:
    def __init__(self, base_url: str, move_path: str, timeout_sec: float,
                 bearer_token: Optional[str] = None,
                 basic_user: Optional[str] = None,
                 basic_pass: Optional[str] = None):
        self.base_url = base_url.rstrip('/')
        self.move_path = move_path
        self.timeout = timeout_sec
        self.bearer_token = bearer_token
        self.basic_user = basic_user
        self.basic_pass = basic_pass

    def _full_url(self, path: str) -> str:
        if not path.startswith('/'):
            path = '/' + path
        return self.base_url + path

    def _headers(self) -> Dict[str, str]:
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': 'SmartDonkey-Driver/1.0'
        }
        if self.bearer_token:
            headers['Authorization'] = f'Bearer {self.bearer_token}'
        elif self.basic_user is not None and self.basic_pass is not None:
            token = base64.b64encode(f"{self.basic_user}:{self.basic_pass}".encode('utf-8')).decode('ascii')
            headers['Authorization'] = f'Basic {token}'
        return headers

    def post_move_velocity(self, linear_velocity: float, angular_velocity: float = 0.0) -> Dict[str, Any]:
        payload = {
            'linear_velocity': linear_velocity,
            'angular_velocity': angular_velocity
        }
        data = json.dumps(payload).encode('utf-8')
        req = request.Request(self._full_url(self.move_path), data=data, headers=self._headers(), method='POST')
        with request.urlopen(req, timeout=self.timeout) as resp:
            resp_data = resp.read()
            if resp_data:
                try:
                    return json.loads(resp_data.decode('utf-8'))
                except Exception:
                    return {'raw': resp_data.decode('utf-8', errors='replace')}
            return {}

    def get_status(self, status_path: str) -> Dict[str, Any]:
        req = request.Request(self._full_url(status_path), headers=self._headers(), method='GET')
        with request.urlopen(req, timeout=self.timeout) as resp:
            resp_data = resp.read()
            if not resp_data:
                return {}
            return json.loads(resp_data.decode('utf-8'))


def iso_now(ts: Optional[float] = None) -> str:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def poller_thread(cfg, client: DeviceClient):
    if not cfg.device_status_path:
        logging.info('Status polling disabled (DEVICE_STATUS_PATH not set).')
        return

    backoff_ms = cfg.retry_backoff_init_ms
    logging.info('Starting background polling of device state.')
    while not stop_event.is_set():
        try:
            state = client.get_status(cfg.device_status_path)
            state_buffer.update(state)
            logging.info('Polled device state at %s', iso_now())
            # On success, reset backoff and wait for poll interval or until stop
            backoff_ms = cfg.retry_backoff_init_ms
            # Wait using stop_event to allow fast shutdown
            if stop_event.wait(cfg.poll_interval_sec):
                break
        except error.HTTPError as e:
            logging.error('HTTP error while polling: %s %s', e.code, e.reason)
        except error.URLError as e:
            logging.error('URL error while polling: %s', getattr(e, 'reason', e))
        except Exception as e:
            logging.exception('Unexpected error while polling: %s', e)

        # On error, apply exponential backoff
        sleep_s = backoff_ms / 1000.0
        logging.info('Retrying poll in %.3f s (backoff=%d ms)', sleep_s, backoff_ms)
        if stop_event.wait(sleep_s):
            break
        backoff_ms = min(backoff_ms * 2, cfg.retry_backoff_max_ms)


class DriverHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = 'SmartDonkeyDriver/1.0'

    def _send_json(self, status_code: int, obj: Dict[str, Any]):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logging.info("%s - - %s", self.address_string(), format % args)

    def do_GET(self):
        cfg = self.server.cfg  # type: ignore[attr-defined]
        client = self.server.client  # type: ignore[attr-defined]
        parsed = parse.urlparse(self.path)
        path = parsed.path
        qs = parse.parse_qs(parsed.query)

        if path == '/move/forward':
            self._handle_move(client, cfg, qs, direction='forward')
            return
        if path == '/move/backward':
            self._handle_move(client, cfg, qs, direction='backward')
            return

        self._send_json(404, {'error': 'not_found'})

    def _handle_move(self, client: DeviceClient, cfg, qs: Dict[str, Any], direction: str):
        # Expect linear_velocity as query param
        lv_vals = qs.get('linear_velocity')
        if not lv_vals or len(lv_vals) == 0:
            self._send_json(400, {'error': 'missing_parameter', 'parameter': 'linear_velocity'})
            return
        try:
            lv = float(lv_vals[0])
        except ValueError:
            self._send_json(400, {'error': 'invalid_parameter', 'parameter': 'linear_velocity'})
            return

        # Enforce sign based on direction
        if direction == 'forward':
            if lv <= 0:
                self._send_json(400, {'error': 'invalid_value', 'detail': 'linear_velocity must be > 0 for forward'})
                return
            cmd_lv = lv
        else:  # backward
            if lv <= 0:
                self._send_json(400, {'error': 'invalid_value', 'detail': 'linear_velocity must be > 0 for backward'})
                return
            cmd_lv = -lv

        attempts = 0
        last_err = None
        while attempts < cfg.cmd_max_retries:
            attempts += 1
            try:
                resp = client.post_move_velocity(linear_velocity=cmd_lv, angular_velocity=0.0)
                logging.info('Sent move_velocity (dir=%s, lv=%.3f) on attempt %d', direction, cmd_lv, attempts)
                last_state, last_ts = state_buffer.get()
                self._send_json(200, {
                    'status': 'ok',
                    'direction': direction,
                    'linear_velocity': lv,
                    'device_response': resp,
                    'last_state': last_state,
                    'last_state_ts': (iso_now(last_ts) if last_ts else None),
                    'timestamp': iso_now(),
                    'attempts': attempts
                })
                return
            except error.HTTPError as e:
                last_err = {'code': e.code, 'reason': e.reason}
                logging.error('HTTP error sending command: %s %s', e.code, e.reason)
            except error.URLError as e:
                last_err = {'error': str(getattr(e, 'reason', e))}
                logging.error('URL error sending command: %s', getattr(e, 'reason', e))
            except Exception as e:
                last_err = {'error': str(e)}
                logging.exception('Unexpected error sending command')

            # Simple short backoff between command retries
            time.sleep(0.2 * attempts)

        self._send_json(502, {
            'status': 'error',
            'direction': direction,
            'linear_velocity': lv,
            'error': last_err,
            'timestamp': iso_now()
        })


def run_server():
    cfg = get_config()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    client = DeviceClient(
        base_url=cfg.device_base_url,
        move_path=cfg.device_move_path,
        timeout_sec=cfg.request_timeout_sec,
        bearer_token=cfg.device_bearer_token,
        basic_user=cfg.device_basic_user,
        basic_pass=cfg.device_basic_pass,
    )

    # Start background poller if configured
    poll_thread = threading.Thread(target=poller_thread, args=(cfg, client), name='poller', daemon=True)
    poll_thread.start()

    class _Server(ThreadingHTTPServer):
        def __init__(self, server_address, RequestHandlerClass):
            super().__init__(server_address, RequestHandlerClass)
            self.cfg = cfg
            self.client = client

    httpd = _Server((cfg.http_host, cfg.http_port), DriverHTTPRequestHandler)

    def shutdown_handler(signum, frame):
        logging.info('Signal %s received, shutting down...', signum)
        stop_event.set()
        try:
            httpd.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, shutdown_handler)
    # SIGTERM may not be available on some platforms (e.g., Windows)
    try:
        signal.signal(signal.SIGTERM, shutdown_handler)
    except Exception:
        pass

    logging.info('HTTP server starting on %s:%d', cfg.http_host, cfg.http_port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        shutdown_handler('KeyboardInterrupt', None)
    finally:
        logging.info('HTTP server stopped. Waiting for poller to finish...')
        stop_event.set()
        if poll_thread.is_alive():
            poll_thread.join(timeout=5.0)
        logging.info('Shutdown complete.')


if __name__ == '__main__':
    run_server()
