# server.py
import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from telemetry import Telemetry
from device_client import DeviceClient


# Globals will be initialized by driver.py before serving
TELEMETRY: Optional[Telemetry] = None
DEVICE: Optional[DeviceClient] = None
STREAM_INTERVAL_SEC: float = 1.0
STOP_EVENT: Optional[threading.Event] = None


class DriverRequestHandler(BaseHTTPRequestHandler):
    server_version = "RobotDriver/1.0"

    def log_message(self, format: str, *args):
        logging.info("%s - - %s", self.address_string(), format % args)

    def _send_json(self, obj, status=HTTPStatus.OK):
        data = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get('Content-Length', '0') or '0')
        body = b''
        if length > 0:
            body = self.rfile.read(length)
        if not body:
            return {}
        try:
            return json.loads(body.decode('utf-8'))
        except Exception:
            return {}

    def do_GET(self):
        global TELEMETRY, STREAM_INTERVAL_SEC, STOP_EVENT
        if self.path == '/status':
            snap = TELEMETRY.snapshot()
            self._send_json(snap, status=HTTPStatus.OK)
            return
        if self.path == '/snapshot':
            snap = TELEMETRY.snapshot()
            self._send_json(snap, status=HTTPStatus.OK)
            return
        if self.path == '/stream':
            # SSE streaming
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            try:
                while not STOP_EVENT.is_set():
                    snap = TELEMETRY.snapshot()
                    payload = json.dumps(snap)
                    chunk = f"data: {payload}\n\n".encode('utf-8')
                    self.wfile.write(chunk)
                    try:
                        self.wfile.flush()
                    except Exception:
                        break
                    time.sleep(STREAM_INTERVAL_SEC)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        # Not found
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self):
        global TELEMETRY, DEVICE
        # Route by path
        if self.path == '/connect':
            TELEMETRY.record_command('connect')
            try:
                status, body = DEVICE.connect()
                if 200 <= status < 300:
                    TELEMETRY.set_connected(True)
                    self._send_json({'ok': True, 'status': status})
                else:
                    TELEMETRY.record_error(f"connect failed: {status}")
                    self._send_json({'ok': False, 'status': status, 'body': self._safe_body(body)}, status=status)
            except Exception as e:
                TELEMETRY.record_error(f"connect exception: {e}")
                self._send_json({'ok': False, 'error': str(e)}, status=HTTPStatus.BAD_GATEWAY)
            return

        if self.path == '/stop':
            TELEMETRY.record_command('stop')
            try:
                status, body = DEVICE.stop()
                if 200 <= status < 300:
                    TELEMETRY.stop_moving()
                    self._send_json({'ok': True, 'status': status})
                else:
                    TELEMETRY.record_error(f"stop failed: {status}")
                    self._send_json({'ok': False, 'status': status, 'body': self._safe_body(body)}, status=status)
            except Exception as e:
                TELEMETRY.record_error(f"stop exception: {e}")
                self._send_json({'ok': False, 'error': str(e)}, status=HTTPStatus.BAD_GATEWAY)
            return

        if self.path == '/move/forward':
            body = self._read_json()
            speed = body.get('speed')
            duration = body.get('duration') if 'duration' in body else None
            if speed is None:
                self._send_json({'ok': False, 'error': 'speed is required'}, status=HTTPStatus.BAD_REQUEST)
                return
            TELEMETRY.record_command('move_forward')
            try:
                status, resp_body = DEVICE.move_forward(speed=float(speed), duration=float(duration) if duration is not None else None)
                if 200 <= status < 300:
                    TELEMETRY.set_moving(direction='forward', speed=float(speed), duration=float(duration) if duration is not None else None)
                    self._send_json({'ok': True, 'status': status})
                else:
                    TELEMETRY.record_error(f"move_forward failed: {status}")
                    self._send_json({'ok': False, 'status': status, 'body': self._safe_body(resp_body)}, status=status)
            except Exception as e:
                TELEMETRY.record_error(f"move_forward exception: {e}")
                self._send_json({'ok': False, 'error': str(e)}, status=HTTPStatus.BAD_GATEWAY)
            return

        if self.path == '/move/backward':
            body = self._read_json()
            speed = body.get('speed')
            duration = body.get('duration') if 'duration' in body else None
            if speed is None:
                self._send_json({'ok': False, 'error': 'speed is required'}, status=HTTPStatus.BAD_REQUEST)
                return
            TELEMETRY.record_command('move_backward')
            try:
                status, resp_body = DEVICE.move_backward(speed=float(speed), duration=float(duration) if duration is not None else None)
                if 200 <= status < 300:
                    TELEMETRY.set_moving(direction='backward', speed=float(speed), duration=float(duration) if duration is not None else None)
                    self._send_json({'ok': True, 'status': status})
                else:
                    TELEMETRY.record_error(f"move_backward failed: {status}")
                    self._send_json({'ok': False, 'status': status, 'body': self._safe_body(resp_body)}, status=status)
            except Exception as e:
                TELEMETRY.record_error(f"move_backward exception: {e}")
                self._send_json({'ok': False, 'error': str(e)}, status=HTTPStatus.BAD_GATEWAY)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def _safe_body(self, body: bytes):
        try:
            return json.loads(body.decode('utf-8'))
        except Exception:
            try:
                return body.decode('utf-8', errors='replace')
            except Exception:
                return ''


def make_server(host: str, port: int, telemetry: Telemetry, device: DeviceClient, stream_interval_sec: float, stop_event: threading.Event) -> ThreadingHTTPServer:
    global TELEMETRY, DEVICE, STREAM_INTERVAL_SEC, STOP_EVENT
    TELEMETRY = telemetry
    DEVICE = device
    STREAM_INTERVAL_SEC = stream_interval_sec
    STOP_EVENT = stop_event
    httpd = ThreadingHTTPServer((host, port), DriverRequestHandler)
    return httpd
