# http_server.py
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from typing import Optional

from device_client import CameraClient, _iso_utc


class CameraHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "HikUSBHTTP/1.0"

    # camera_client will be injected via server context
    @property
    def camera(self) -> CameraClient:
        return self.server.camera  # type: ignore[attr-defined]

    @property
    def stream_fps_limit(self) -> Optional[float]:
        return getattr(self.server, "stream_fps_limit", None)  # type: ignore[attr-defined]

    def log_message(self, format, *args):
        # include client address and time
        try:
            msg = "%s - - [%s] %s\n" % (self.address_string(), _iso_utc(), format % args)
        except Exception:
            msg = format % args + "\n"
        try:
            self.wfile.write(msg.encode("utf-8"))
        except Exception:
            pass

    def _send_json(self, obj, code=200):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def _send_text(self, text: str, code=200):
        payload = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/status":
            self._handle_status()
        elif self.path == "/snapshot":
            self._handle_snapshot()
        elif self.path == "/stream":
            self._handle_stream()
        else:
            # Minimal landing info
            self._send_text("OK", code=200)

    def do_POST(self):
        if self.path == "/connect":
            self._handle_connect()
        elif self.path == "/disconnect":
            self._handle_disconnect()
        else:
            self._send_text("Not Found", code=404)

    def _handle_status(self):
        status = self.camera.get_status()
        self._send_json(status)

    def _handle_connect(self):
        if not self.camera.is_running():
            self.camera.start()
        self._send_json({"ok": True, "message": "connecting", "status": self.camera.get_status()})

    def _handle_disconnect(self):
        if self.camera.is_running():
            self.camera.stop()
        self._send_json({"ok": True, "message": "disconnected", "status": self.camera.get_status()})

    def _handle_snapshot(self):
        if not self.camera.is_running():
            self._send_json({"error": "not connected; call /connect"}, code=503)
            return
        data, ts = self.camera.get_latest_jpeg()
        if not data:
            self._send_json({"error": "no frame available yet"}, code=503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        if ts:
            self.send_header("X-Timestamp", _iso_utc(ts))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _handle_stream(self):
        if not self.camera.is_running():
            self._send_json({"error": "not connected; call /connect"}, code=503)
            return
        # Send MJPEG multipart stream
        boundary = "frame"
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()

        last_sent_ts = 0.0
        min_interval = 0.0
        if self.stream_fps_limit and self.stream_fps_limit > 0:
            min_interval = 1.0 / float(self.stream_fps_limit)

        try:
            while True:
                if not self.camera.is_running():
                    break
                data, ts = self.camera.get_latest_jpeg()
                if not data:
                    time.sleep(0.05)
                    continue
                now = time.time()
                if min_interval > 0 and (now - last_sent_ts) < min_interval:
                    time.sleep(max(0.0, min_interval - (now - last_sent_ts)))
                # Write part
                self.wfile.write(bytes(f"--{boundary}\r\n", "utf-8"))
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(bytes(f"Content-Length: {len(data)}\r\n", "utf-8"))
                if ts:
                    self.wfile.write(bytes(f"X-Timestamp: {_iso_utc(ts)}\r\n", "utf-8"))
                self.wfile.write(b"\r\n")
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                try:
                    self.wfile.flush()
                except Exception:
                    pass
                last_sent_ts = time.time()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            # swallow any other errors to finish response cleanly
            pass


class CameraHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, camera: CameraClient, stream_fps_limit: Optional[float] = None):
        super().__init__(server_address, RequestHandlerClass)
        self.camera = camera
        self.stream_fps_limit = stream_fps_limit
        # Make server socket reuse address for quick restarts
        self.allow_reuse_address = True


def run_http_server(host: str, port: int, camera: CameraClient, stream_fps_limit: Optional[float] = None):
    httpd = CameraHTTPServer((host, port), CameraHTTPRequestHandler, camera, stream_fps_limit)

    def _serve():
        print(f"[http] serving on http://{host}:{port}")
        try:
            httpd.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        print("[http] server stopping")

    t = threading.Thread(target=_serve, name="HTTPServer", daemon=True)
    t.start()
    return httpd, t
