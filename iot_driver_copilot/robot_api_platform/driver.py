import json
import logging
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

from config import load_config
from device_client import DeviceClient


class MoveHandler(BaseHTTPRequestHandler):
    cfg = None
    client = None

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path not in ("/move/forward", "/move/backward"):
            self._send_json(404, {"error": "not found"})
            return

        qs = parse_qs(parsed.query or "")
        v_str = None
        if "linear_velocity" in qs and len(qs["linear_velocity"]) > 0:
            v_str = qs["linear_velocity"][0]
        linear_velocity = None
        if v_str is not None:
            try:
                linear_velocity = float(v_str)
            except Exception:
                self._send_json(400, {"error": "invalid linear_velocity"})
                return
        elif self.cfg.default_linear_velocity is not None:
            linear_velocity = float(self.cfg.default_linear_velocity)
        else:
            self._send_json(400, {"error": "missing linear_velocity and DEFAULT_LINEAR_VELOCITY not set"})
            return

        if path == "/move/backward":
            linear_velocity = -abs(linear_velocity)
        else:
            linear_velocity = abs(linear_velocity)

        result = self.client.send_velocity(linear_velocity, 0.0)
        ok = 200 <= int(result.get("http_status", 0)) < 300
        last_sample, last_ts = self.client.get_last_sample()
        resp = {
            "ok": bool(ok),
            "linear_velocity": linear_velocity,
            "device_http_status": result.get("http_status"),
            "device_body": result.get("body") if ok else None,
            "device_error": result.get("error") if not ok else None,
            "last_update_utc": datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat() if last_ts else None,
            "connected": self.client.is_connected(),
        }
        self._send_json(200 if ok else 502, resp)

    def log_message(self, format: str, *args):
        # Route BaseHTTPRequestHandler logs to logging module
        logging.info("%s - - %s", self.client_address[0], format % args)


def main():
    cfg = load_config()

    # Configure logging
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")

    client = DeviceClient(cfg)
    client.start()

    MoveHandler.cfg = cfg
    MoveHandler.client = client

    server = ThreadingHTTPServer((cfg.http_host, cfg.http_port), MoveHandler)
    server.daemon_threads = True

    shutdown_event = threading.Event()

    def handle_signal(signum, frame):
        logging.info("Received signal %s, shutting down...", signum)
        shutdown_event.set()
        # Stop accepting new requests and shutdown serve_forever loop
        threading.Thread(target=server.shutdown, name="server-shutdown", daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logging.info("HTTP server starting on %s:%s", cfg.http_host, cfg.http_port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        logging.info("HTTP server stopping...")
        client.stop()
        server.server_close()
        logging.info("Shutdown complete")


if __name__ == "__main__":
    main()
