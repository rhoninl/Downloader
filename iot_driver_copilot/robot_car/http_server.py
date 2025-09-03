# http_server.py
import io
import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from data_store import DataStore
from device_client import DeviceClient


class DriverRequestHandler(BaseHTTPRequestHandler):
    server_version = "RobotDriver/1.0"

    # References injected by server factory
    data_store: DataStore = None  # type: ignore
    device_client: DeviceClient = None  # type: ignore
    poll_interval_s: float = 1.0  # type: ignore

    def _set_json_headers(self, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode('utf-8')
        self._set_json_headers(status)
        self.wfile.write(body)

    def _read_json_body(self) -> Optional[dict]:
        length = int(self.headers.get('Content-Length', '0'))
        if length <= 0:
            return None
        data = self.rfile.read(length)
        try:
            return json.loads(data.decode('utf-8'))
        except Exception:
            return None

    def do_GET(self):
        try:
            if self.path == '/status':
                status = self.data_store.get_status()
                status['device_last_http_code'] = self.device_client.last_status_code
                self._send_json(status)
                return
            if self.path == '/snapshot':
                snap = self.data_store.get_snapshot()
                if snap is None:
                    self._send_json({'error': 'no data yet'}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                self._send_json(snap)
                return
            if self.path == '/stream':
                self._handle_stream()
                return
            self._send_json({'error': 'not found'}, status=HTTPStatus.NOT_FOUND)
        except Exception as e:
            logging.exception("Unhandled error in GET %s: %s", self.path, e)
            self._send_json({'error': 'internal error'}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_stream(self):
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        # Flush headers
        try:
            # Send initial comment to open the stream
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            last_sent_ts = 0.0
            while True:
                # Wait for new sample or timeout to send keepalive
                notified = self.data_store.wait_for_update(timeout=self.poll_interval_s)
                snap = self.data_store.get_snapshot()
                if snap is not None and (notified or (snap['last_update_ts'] or 0) > last_sent_ts):
                    payload = json.dumps(snap)
                    chunk = f"event: update\ndata: {payload}\n\n".encode('utf-8')
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    last_sent_ts = snap['last_update_ts'] or last_sent_ts
                else:
                    # Keepalive comment
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except ConnectionResetError:
            logging.info("SSE client disconnected (reset)")
        except BrokenPipeError:
            logging.info("SSE client disconnected (broken pipe)")
        except Exception as e:
            logging.warning("SSE stream error: %s", e)

    def do_POST(self):
        try:
            if self.path == '/connect':
                resp = self.device_client.connect()
                self.data_store.set_connected(True)
                self._send_json({'ok': True, 'device': resp})
                return
            if self.path == '/move/forward':
                body = self._read_json_body() or {}
                speed = body.get('speed')
                duration = body.get('duration')
                if speed is None:
                    self._send_json({'error': 'missing speed'}, status=HTTPStatus.BAD_REQUEST)
                    return
                resp = self.device_client.move_forward(float(speed), float(duration) if duration is not None else None)
                self._send_json({'ok': True, 'device': resp})
                return
            if self.path == '/move/backward':
                body = self._read_json_body() or {}
                speed = body.get('speed')
                duration = body.get('duration')
                if speed is None:
                    self._send_json({'error': 'missing speed'}, status=HTTPStatus.BAD_REQUEST)
                    return
                resp = self.device_client.move_backward(float(speed), float(duration) if duration is not None else None)
                self._send_json({'ok': True, 'device': resp})
                return
            if self.path == '/stop':
                resp = self.device_client.stop()
                self._send_json({'ok': True, 'device': resp})
                return
            self._send_json({'error': 'not found'}, status=HTTPStatus.NOT_FOUND)
        except Exception as e:
            logging.exception("Unhandled error in POST %s: %s", self.path, e)
            self._send_json({'error': 'internal error'}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    # Silence default logging; use logging module instead
    def log_message(self, format: str, *args) -> None:
        logging.info("%s - - [%s] " + format, self.address_string(), self.log_date_time_string(), *args)


class DriverHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def make_server(host: str, port: int, data_store: DataStore, device_client: DeviceClient, poll_interval_s: float) -> DriverHTTPServer:
    handler_cls = DriverRequestHandler
    handler_cls.data_store = data_store
    handler_cls.device_client = device_client
    handler_cls.poll_interval_s = poll_interval_s

    httpd = DriverHTTPServer((host, port), handler_cls)
    return httpd
