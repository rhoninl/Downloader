# http_server.py
# HTTP server exposing status/snapshot/stream and movement control

import json
import logging
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Callable


class DriverHTTPHandler(BaseHTTPRequestHandler):
    # Will be set by factory
    device_client = None
    default_linear_vel = 0.2

    server_version = "RobotDriver/1.0"

    def log_message(self, format, *args):
        logging.getLogger('HTTP').info("%s - - %s", self.address_string(), format % args)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status=200, content_type='text/plain; charset=utf-8'):
        body = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/status':
            status = self.device_client.get_status()
            self._send_json(status)
            return
        if path == '/snapshot':
            latest = self.device_client.get_latest()
            if latest is None:
                self._send_json({'error': 'no data yet'}, status=503)
            else:
                self._send_json({'ts': time.time(), 'data': latest})
            return
        if path == '/stream':
            self._handle_sse_stream()
            return
        if path == '/move/forward':
            self._handle_move(forward=True)
            return
        if path == '/move/backward':
            self._handle_move(forward=False)
            return
        # Default 404
        self._send_json({'error': 'not found', 'path': path}, status=404)

    def _handle_move(self, forward: bool):
        q = parse_qs(urlparse(self.path).query)
        lv = None
        if 'linear_velocity' in q and len(q['linear_velocity']) > 0:
            try:
                lv = float(q['linear_velocity'][0])
            except ValueError:
                return self._send_json({'ok': False, 'error': 'invalid linear_velocity'}, status=400)
        if lv is None:
            lv = float(self.default_linear_vel)
        if not forward:
            lv = -abs(lv)
        else:
            lv = abs(lv)
        result = self.device_client.move_velocity(linear_velocity=lv, angular_velocity=0.0)
        if result.get('ok'):
            self._send_json({'ok': True, 'linear_velocity': lv, 'ts': time.time(), 'device_response': result.get('device_response')})
        else:
            self._send_json({'ok': False, 'linear_velocity': lv, 'error': result.get('error'), 'ts': time.time()}, status=502)

    def _handle_sse_stream(self):
        # Server-Sent Events stream of latest telemetry
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        last_sent_count = -1
        keepalive_interval = 15.0
        next_ka = time.monotonic() + keepalive_interval

        # We'll wait on the device_client's condition to send updates when new data arrives
        cv = self.device_client.condition
        try:
            while True:
                with cv:
                    # Wait for either new sample or timeout for keep-alive
                    now_mono = time.monotonic()
                    timeout = max(0.1, next_ka - now_mono)
                    cv.wait(timeout=timeout)
                    status = self.device_client.get_status()
                    samples_count = status.get('samples_count', 0)
                    latest = self.device_client.get_latest()
                if latest is not None and samples_count != last_sent_count:
                    event = {
                        'ts': time.time(),
                        'data': latest,
                    }
                    payload = json.dumps(event)
                    msg = f"event: telemetry\n" f"data: {payload}\n\n"
                    self.wfile.write(msg.encode('utf-8'))
                    self.wfile.flush()
                    last_sent_count = samples_count
                    next_ka = time.monotonic() + keepalive_interval
                else:
                    # time for keepalive?
                    if time.monotonic() >= next_ka:
                        self.wfile.write(b":\n\n")  # comment as keep-alive
                        self.wfile.flush()
                        next_ka = time.monotonic() + keepalive_interval
        except (BrokenPipeError, ConnectionResetError):
            # client disconnected
            return


def run_http_server(host: str, port: int, device_client, default_linear_vel: float) -> ThreadingHTTPServer:
    handler_cls = DriverHTTPHandler
    handler_cls.device_client = device_client
    handler_cls.default_linear_vel = default_linear_vel

    httpd = ThreadingHTTPServer((host, port), handler_cls)
    logging.getLogger('HTTP').info("HTTP server listening on %s:%d", host, port)
    return httpd
