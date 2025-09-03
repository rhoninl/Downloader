# http_server.py
import json
import time
import threading
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from typing import Optional

from device_client import DeviceClient


device_client_global: Optional[DeviceClient] = None
stream_heartbeat_s: int = 15


class Handler(BaseHTTPRequestHandler):
    server_version = "RobotDriverHTTP/1.0"

    def _send_json(self, code: int, payload: dict):
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, private")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> Optional[dict]:
        length = int(self.headers.get('Content-Length', '0') or '0')
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        if not raw:
            return None
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return None

    def log_message(self, format, *args):
        # Route to logging module instead of stderr
        logging.info("%s - - %s", self.address_string(), format % args)

    def do_GET(self):
        global device_client_global
        dc = device_client_global
        if dc is None:
            self._send_json(503, {"error": "device client not ready"})
            return

        path = urlparse(self.path).path
        if path == "/status":
            payload = dc.status_snapshot()
            self._send_json(200, payload)
            return
        elif path == "/snapshot":
            latest = dc.latest_json()
            if latest is None:
                self._send_json(204, {"note": "no data yet"})
                return
            self._send_json(200, latest)
            return
        elif path == "/stream":
            # Stream NDJSON lines; keep connection open
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, private")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            q = dc.subscribe()
            last_sent = time.time()
            try:
                while True:
                    try:
                        line = q.get(timeout=1.0)
                        # Send line with newline terminator
                        payload = (line + "\n").encode("utf-8")
                        self.wfile.write(payload)
                        try:
                            self.wfile.flush()
                        except Exception:
                            pass
                        last_sent = time.time()
                    except Exception:
                        # Timeout: send heartbeat to keep connection open periodically
                        if (time.time() - last_sent) >= stream_heartbeat_s:
                            hb = f"# ping {int(time.time())}\n".encode("utf-8")
                            try:
                                self.wfile.write(hb)
                                try:
                                    self.wfile.flush()
                                except Exception:
                                    pass
                                last_sent = time.time()
                            except Exception:
                                break
            except Exception:
                pass
            finally:
                dc.unsubscribe(q)
            return
        else:
            self._send_json(404, {"error": "not found"})
            return

    def do_POST(self):
        global device_client_global
        dc = device_client_global
        if dc is None:
            self._send_json(503, {"error": "device client not ready"})
            return
        path = urlparse(self.path).path
        if path == "/connect":
            ok = dc.cmd_connect()
            self._send_json(200 if ok else 500, {"ok": ok})
            return
        elif path == "/move/forward":
            body = self._read_json_body() or {}
            speed = body.get("speed")
            duration = body.get("duration")
            if speed is None:
                self._send_json(400, {"error": "missing speed"})
                return
            try:
                spd = float(speed)
            except Exception:
                self._send_json(400, {"error": "invalid speed"})
                return
            dur_val = None
            if duration is not None:
                try:
                    dur_val = float(duration)
                except Exception:
                    self._send_json(400, {"error": "invalid duration"})
                    return
            ok = dc.cmd_move_forward(spd, dur_val)
            self._send_json(200 if ok else 500, {"ok": ok})
            return
        elif path == "/move/backward":
            body = self._read_json_body() or {}
            speed = body.get("speed")
            duration = body.get("duration")
            if speed is None:
                self._send_json(400, {"error": "missing speed"})
                return
            try:
                spd = float(speed)
            except Exception:
                self._send_json(400, {"error": "invalid speed"})
                return
            dur_val = None
            if duration is not None:
                try:
                    dur_val = float(duration)
                except Exception:
                    self._send_json(400, {"error": "invalid duration"})
                    return
            ok = dc.cmd_move_backward(spd, dur_val)
            self._send_json(200 if ok else 500, {"ok": ok})
            return
        elif path == "/stop":
            ok = dc.cmd_stop()
            self._send_json(200 if ok else 500, {"ok": ok})
            return
        else:
            self._send_json(404, {"error": "not found"})
            return


class ServerThread(threading.Thread):
    def __init__(self, host: str, port: int):
        super().__init__(name="HTTPServerThread", daemon=True)
        self.httpd = ThreadingHTTPServer((host, port), Handler)

    def run(self):
        logging.info("HTTP server listening on %s:%d", *self.httpd.server_address)
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        except Exception as e:
            logging.error("HTTP server error: %s", e)

    def shutdown(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
