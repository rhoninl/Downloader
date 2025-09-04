# driver.py
import json
import signal
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from config import Config
from util import Logger
from device_client import RobotClient


START_TIME = time.time()
CFG = Config()
LOG = Logger(CFG.log_level)
CLIENT = RobotClient(CFG, LOG)
SHUTDOWN = threading.Event()


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "RobotDriver/1.0"

    def _set_common_headers(self, code=200, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        # For SSE streaming, the connection is kept alive by default

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        qs = parse_qs(parsed.query)
        try:
            if path == "/status":
                self._handle_status()
            elif path == "/snapshot":
                self._handle_snapshot()
            elif path == "/stream":
                self._handle_stream()
            elif path == "/move/forward":
                self._handle_move(direction="forward", qs=qs)
            elif path == "/move/backward":
                self._handle_move(direction="backward", qs=qs)
            else:
                self._set_common_headers(404)
                self.end_headers()
                self.wfile.write(_json_bytes({"error": "not found"}))
        except Exception as e:
            LOG.error(f"Handler error for {path}: {e}")
            try:
                self._set_common_headers(500)
                self.end_headers()
                self.wfile.write(_json_bytes({"error": str(e)}))
            except Exception:
                pass

    def log_message(self, format, *args):
        # Route server logs through our logger
        LOG.info("HTTP " + (format % args))

    def _handle_status(self):
        latest, latest_ts, tel_count, tel_errs, tel_connected, tel_last_err = CLIENT.telemetry_snapshot()
        ctrl = CLIENT.control_snapshot()
        body = {
            "uptime_sec": round(time.time() - START_TIME, 3),
            "device": {
                "host": CFG.device_host,
                "control_port": CFG.device_ctrl_port,
                "telemetry_port": CFG.device_tel_port,
            },
            "telemetry": {
                "connected": tel_connected,
                "samples": tel_count,
                "errors": tel_errs,
                "last_error": tel_last_err,
                "last_sample_ts": latest_ts,
            },
            "control": ctrl,
            "http": {"host": CFG.http_host, "port": CFG.http_port},
            "now": time.time(),
        }
        self._set_common_headers(200, "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(_json_bytes(body))

    def _handle_snapshot(self):
        latest = CLIENT.latest_telemetry()
        if latest is None:
            self._set_common_headers(204, "application/json; charset=utf-8")
            self.end_headers()
            return
        self._set_common_headers(200, "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(_json_bytes(latest))

    def _handle_stream(self):
        # Server-Sent Events streaming of telemetry updates
        q = CLIENT.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.flush()

            last_ping = time.time()
            while not SHUTDOWN.is_set():
                try:
                    # Wait up to 10s for new data
                    item = q.get(timeout=10.0)
                    payload = json.dumps(item, separators=(",", ":"))
                    data = f"event: update\ndata: {payload}\n\n".encode("utf-8")
                    self.wfile.write(data)
                    self.wfile.flush()
                    last_ping = time.time()
                except Exception:
                    # Timeout or write error; send a keepalive comment if needed
                    now = time.time()
                    if now - last_ping > 15:
                        try:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            last_ping = now
                        except Exception:
                            break
        finally:
            CLIENT.unsubscribe(q)

    def _handle_move(self, direction: str, qs):
        # GET parameter: linear_velocity (float)
        lv = None
        if "linear_velocity" in qs and qs["linear_velocity"]:
            try:
                lv = float(qs["linear_velocity"][0])
            except ValueError:
                self._set_common_headers(400)
                self.end_headers()
                self.wfile.write(_json_bytes({"error": "invalid linear_velocity"}))
                return
        else:
            lv = CFG.linear_velocity_default

        try:
            if direction == "forward":
                resp = CLIENT.move_forward(lv)
            else:
                resp = CLIENT.move_backward(lv)
        except Exception as e:
            self._set_common_headers(502)
            self.end_headers()
            self.wfile.write(_json_bytes({"ok": False, "error": str(e)}))
            return

        self._set_common_headers(200)
        self.end_headers()
        self.wfile.write(_json_bytes({
            "ok": True,
            "direction": direction,
            "linear_velocity": lv,
            "device_response": resp,
        }))


def _graceful_shutdown(signum, frame):
    LOG.warn(f"Received signal {signum}, shutting down...")
    SHUTDOWN.set()
    try:
        httpd.shutdown()
    except Exception:
        pass
    CLIENT.stop()


if __name__ == "__main__":
    CLIENT.start()

    server_address = (CFG.http_host, CFG.http_port)
    httpd = ThreadingHTTPServer(server_address, Handler)

    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    LOG.info(f"HTTP server listening on http://{CFG.http_host}:{CFG.http_port}")
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        CLIENT.stop()
        LOG.info("HTTP server stopped")
