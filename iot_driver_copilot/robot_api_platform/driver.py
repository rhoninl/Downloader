# driver.py
import json
import logging
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

from config import Config
from device_client import DeviceClient


logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger("Driver")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class RequestHandler(BaseHTTPRequestHandler):
    # Injected before server starts
    device: DeviceClient = None  # type: ignore
    config: Config = None  # type: ignore
    start_time: float = time.time()

    def log_message(self, format: str, *args):
        logger.info("%s - %s" % (self.address_string(), format % args))

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_query(self):
        o = urlparse(self.path)
        return o.path, parse_qs(o.query)

    def do_GET(self):
        path, qs = self._parse_query()
        if path == "/status":
            self._handle_status()
            return
        if path == "/snapshot":
            self._handle_snapshot()
            return
        if path == "/stream":
            self._handle_stream()
            return
        if path == "/move/forward":
            self._handle_move(direction="FORWARD")
            return
        if path == "/move/backward":
            self._handle_move(direction="BACKWARD")
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def _handle_status(self):
        payload = {
            "driver": {
                "uptime_sec": round(time.time() - self.start_time, 3),
                "http_host": self.config.http_host,
                "http_port": self.config.http_port,
            },
            "device": self.device.get_status(),
        }
        self._send_json(HTTPStatus.OK, payload)

    def _handle_snapshot(self):
        sample, ts, seq = self.device.get_latest_sample()
        if sample is None:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "error": "no sample available",
                "connected": self.device.is_connected(),
            })
            return
        payload = {
            "seq": seq,
            "ts": ts,
            "data": sample,
        }
        self._send_json(HTTPStatus.OK, payload)

    def _handle_stream(self):
        # Server-Sent Events streaming of telemetry
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        keepalive_interval = max(1.0, float(self.config.sse_keepalive_interval_sec))
        last_sent_seq = -1
        last_keepalive = time.time()

        try:
            while True:
                # Wait for new sample or timeout for keepalive
                with self.device._cond:  # Accessing condition for waiting
                    if self.device._sample_seq == last_sent_seq:
                        self.device._cond.wait(timeout=1.0)
                    sample, ts, seq = self.device.get_latest_sample()

                now = time.time()
                if sample is not None and seq != last_sent_seq:
                    event = {
                        "seq": seq,
                        "ts": ts,
                        "data": sample,
                    }
                    payload = json.dumps(event)
                    chunk = f"data: {payload}\n\n".encode("utf-8")
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    last_sent_seq = seq
                    last_keepalive = now
                elif (now - last_keepalive) >= keepalive_interval:
                    # Send a comment as keepalive
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except BrokenPipeError:
                        break
                    last_keepalive = now
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            logger.warning("Stream handler error: %s", e)

    def _handle_move(self, direction: str):
        # Parse query for linear_velocity
        _, qs = self._parse_query()
        vel = None
        if "linear_velocity" in qs and len(qs["linear_velocity"]) > 0:
            try:
                vel = float(qs["linear_velocity"][0])
            except ValueError:
                vel = None
        if vel is None:
            vel = float(self.config.default_linear_velocity)
        # Clamp velocity to a safe range [0.0, 1.0]
        if vel < 0.0:
            vel = 0.0
        if vel > 1.0:
            vel = 1.0

        try:
            self.device.send_move(direction, vel)
            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "direction": direction.lower(),
                "linear_velocity": vel,
            })
        except ConnectionError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "ok": False,
                "error": "device not connected",
            })
        except Exception as e:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False,
                "error": str(e),
            })


def main():
    cfg = Config.load_from_env()

    device = DeviceClient(
        host=cfg.device_host,
        port=cfg.device_port,
        connect_timeout_sec=cfg.connect_timeout_sec,
        read_timeout_sec=cfg.read_timeout_sec,
        reconnect_initial_delay_sec=cfg.reconnect_initial_delay_sec,
        reconnect_max_delay_sec=cfg.reconnect_max_delay_sec,
    )
    device.start()

    RequestHandler.device = device
    RequestHandler.config = cfg

    httpd = ThreadingHTTPServer((cfg.http_host, cfg.http_port), RequestHandler)

    shutdown_event = threading.Event()

    def handle_signal(signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        shutdown_event.set()
        # server.shutdown() is safe to call from another thread
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("HTTP server listening on %s:%d", cfg.http_host, cfg.http_port)
    try:
        httpd.serve_forever()
    finally:
        device.stop()
        httpd.server_close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
