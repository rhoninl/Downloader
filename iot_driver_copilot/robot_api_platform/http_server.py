# http_server.py
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from config import Config
from device_client import DeviceClient
from state import DriverState

logger = logging.getLogger(__name__)

# Globals set by driver.py
CFG: Config = None  # type: ignore
CLIENT: DeviceClient = None  # type: ignore
STATE: DriverState = None  # type: ignore
SHUTDOWN_EVENT: threading.Event = None  # type: ignore


def _send_json(handler: BaseHTTPRequestHandler, obj: dict, status: int = 200):
    body = json.dumps(obj).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(body)


def _bad_request(handler: BaseHTTPRequestHandler, msg: str):
    _send_json(handler, {"error": msg}, status=400)


class DriverHandler(BaseHTTPRequestHandler):
    server_version = 'RobotCarDriver/1.0'

    def log_message(self, format, *args):  # noqa: A003 - override
        logger.info("%s - - %s", self.address_string(), format % args)

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == '/status':
                return self.handle_status()
            if path == '/snapshot':
                return self.handle_snapshot()
            if path == '/stream':
                return self.handle_stream(parsed)
            if path == '/move/forward':
                return self.handle_move('forward', parsed)
            if path == '/move/backward':
                return self.handle_move('backward', parsed)
            # Not found
            _send_json(self, {"error": "not found"}, status=404)
        except Exception as e:  # noqa: BLE001
            logger.exception('Handler error: %s', e)
            _send_json(self, {"error": str(e)}, status=500)

    def handle_status(self):
        snap = STATE.snapshot()
        _send_json(self, {
            "ok": True,
            "state": snap,
        })

    def handle_snapshot(self):
        snap = STATE.snapshot()
        _send_json(self, snap)

    def handle_stream(self, parsed):
        qs = parse_qs(parsed.query or '')
        try:
            interval_ms = int(qs.get('interval_ms', [str(CFG.stream_interval_ms)])[0])
        except ValueError:
            interval_ms = CFG.stream_interval_ms
        if interval_ms < 100:
            interval_ms = 100
        # Prepare SSE headers
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        # Stream loop
        try:
            while not SHUTDOWN_EVENT.is_set():
                data = json.dumps(STATE.snapshot())
                chunk = f"event: snapshot\n" f"data: {data}\n\n"
                self.wfile.write(chunk.encode('utf-8'))
                self.wfile.flush()
                time.sleep(interval_ms / 1000.0)
        except (BrokenPipeError, ConnectionResetError):
            logger.info('Client disconnected from /stream')
        except Exception as e:  # noqa: BLE001
            logger.warning('Stream error: %s', e)

    def handle_move(self, direction: str, parsed):
        qs = parse_qs(parsed.query or '')
        if 'linear_velocity' not in qs:
            return _bad_request(self, 'missing linear_velocity')
        try:
            lv = float(qs['linear_velocity'][0])
        except ValueError:
            return _bad_request(self, 'linear_velocity must be float')
        if lv < 0:
            return _bad_request(self, 'linear_velocity must be >= 0')

        STATE.record_command(f'move_{direction}', {"linear_velocity": lv})
        # Call device native API via DeviceClient
        try:
            if direction == 'forward':
                status, body, rtt = CLIENT.move_forward(lv)
            else:
                status, body, rtt = CLIENT.move_backward(lv)
            ok = 200 <= status < 300
            snippet = body.decode('utf-8', errors='ignore')[:512] if body else ''
            STATE.record_command_result(status, snippet, rtt, ok)
            if ok:
                STATE.set_motion(direction, lv)
            resp = {
                "ok": ok,
                "device_status": status,
                "device_rtt_ms": rtt,
                "device_response_snippet": snippet,
                "motion": STATE.snapshot().get('motion'),
            }
            _send_json(self, resp, status=200 if ok else 502)
        except Exception as e:  # noqa: BLE001
            STATE.record_command_result(0, str(e)[:512], 0.0, False)
            _send_json(self, {"ok": False, "error": str(e)}, status=502)


class DriverHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def create_server(cfg: Config, client: DeviceClient, state: DriverState, shutdown_event: threading.Event) -> DriverHTTPServer:
    global CFG, CLIENT, STATE, SHUTDOWN_EVENT
    CFG = cfg
    CLIENT = client
    STATE = state
    SHUTDOWN_EVENT = shutdown_event

    server = DriverHTTPServer((cfg.http_host, cfg.http_port), DriverHandler)
    return server
