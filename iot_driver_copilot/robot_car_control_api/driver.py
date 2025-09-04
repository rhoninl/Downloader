import json
import logging
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

from config import load_config
from device_client import DeviceHTTPClient


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("driver")


class TelemetryBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict | None = None
        self._last_ts: float | None = None

    def update(self, data: dict) -> None:
        with self._lock:
            self._last = data
            self._last_ts = time.time()

    def snapshot(self) -> tuple[dict | None, float | None]:
        with self._lock:
            return self._last, self._last_ts


class Collector(threading.Thread):
    def __init__(self, client: DeviceHTTPClient, buf: TelemetryBuffer, stop_event: threading.Event) -> None:
        super().__init__(daemon=True)
        self.client = client
        self.buf = buf
        self.stop_event = stop_event
        self.cfg = load_config()

    def run(self) -> None:
        backoff_ms = self.cfg.backoff_initial_ms
        connected = False
        attempt = 0
        while not self.stop_event.is_set():
            try:
                data = self.client.get_status()
                self.buf.update(data)
                if not connected:
                    connected = True
                    attempt = 0
                    backoff_ms = self.cfg.backoff_initial_ms
                    logger.info("Device connected. Telemetry updated.")
                else:
                    logger.debug("Telemetry updated.")
                # Sleep normal poll interval
                self._sleep(self.cfg.poll_interval_s)
            except Exception as e:
                if connected:
                    logger.warning("Device disconnected or status error: %s", e)
                else:
                    logger.warning("Device status retry %d failed: %s", attempt + 1, e)
                connected = False
                attempt += 1
                # Exponential backoff with cap
                self._sleep(backoff_ms / 1000.0)
                backoff_ms = min(self.cfg.backoff_max_ms, max(self.cfg.backoff_initial_ms, backoff_ms * 2))

    def _sleep(self, seconds: float) -> None:
        # Sleep in small increments to be responsive to stop_event
        end = time.time() + seconds
        while not self.stop_event.is_set() and time.time() < end:
            time.sleep(min(0.1, max(0.0, end - time.time())))


class ControlState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending_timer: threading.Timer | None = None

    def schedule_stop(self, delay_s: float, stop_fn) -> None:
        with self._lock:
            self.cancel_timer_locked()
            if delay_s > 0:
                t = threading.Timer(delay_s, stop_fn)
                t.daemon = True
                self._pending_timer = t
                t.start()

    def cancel_timer(self) -> None:
        with self._lock:
            self.cancel_timer_locked()

    def cancel_timer_locked(self) -> None:
        if self._pending_timer is not None:
            try:
                self._pending_timer.cancel()
            except Exception:
                pass
            self._pending_timer = None


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "RobotDriver/1.0"

    # Shared references injected at server creation time
    client: DeviceHTTPClient = None  # type: ignore
    ctrl_state: ControlState = None  # type: ignore
    cfg = load_config()

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_body_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {}

    def _get_params(self) -> dict:
        params = {}
        # Query string
        q = parse_qs(urlparse(self.path).query)
        for k, v in q.items():
            if len(v) > 0:
                params[k] = v[0]
        # JSON body
        body = self._parse_body_json()
        if isinstance(body, dict):
            params.update(body)
        return params

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/move/forward":
                self._handle_move(forward=True)
            elif path == "/move/backward":
                self._handle_move(forward=False)
            elif path == "/stop":
                self._handle_stop()
            else:
                self._send_json(404, {"error": "not_found"})
        except Exception as e:
            logging.exception("Handler error")
            self._send_json(500, {"error": str(e)})

    def log_message(self, fmt: str, *args) -> None:
        # Integrate with logging
        logger.info("%s - - %s", self.client_address[0], fmt % args)

    def _handle_move(self, forward: bool) -> None:
        params = self._get_params()
        # speed can be provided as float; default from env
        try:
            speed = float(params.get("speed", self.cfg.default_speed))
        except Exception:
            speed = self.cfg.default_speed
        if speed < 0:
            speed = -speed
        lin_vel = speed if forward else -speed

        duration_ms = None
        if "duration" in params:
            # allow seconds if float, or ms if int-like with suffix
            try:
                duration_ms = int(float(params.get("duration")) * 1000.0)
            except Exception:
                duration_ms = None
        elif "duration_ms" in params:
            try:
                duration_ms = int(params.get("duration_ms"))
            except Exception:
                duration_ms = None
        else:
            duration_ms = self.cfg.default_duration_ms or 0

        # Send command to device
        res = Handler.client.send_move(lin_vel, duration_ms if duration_ms and duration_ms > 0 else None)

        # If duration provided, schedule a stop at driver level for safety
        if duration_ms and duration_ms > 0:
            delay_s = max(0.0, duration_ms / 1000.0)
            Handler.ctrl_state.schedule_stop(delay_s, self._background_stop)
            logger.info("Scheduled stop in %.3f s", delay_s)
        else:
            Handler.ctrl_state.cancel_timer()

        last_sample, last_ts = self.server.buf.snapshot()  # type: ignore[attr-defined]
        self._send_json(200, {
            "result": "moving_forward" if forward else "moving_backward",
            "linear_velocity": lin_vel,
            "duration_ms": duration_ms or 0,
            "device_response": res,
            "last_update_ts": last_ts,
            "last_sample": last_sample,
        })

    def _background_stop(self) -> None:
        try:
            Handler.client.send_stop()
            logger.info("Auto stop executed after duration elapsed")
        except Exception as e:
            logger.error("Auto stop failed: %s", e)

    def _handle_stop(self) -> None:
        Handler.ctrl_state.cancel_timer()
        res = Handler.client.send_stop()
        last_sample, last_ts = self.server.buf.snapshot()  # type: ignore[attr-defined]
        self._send_json(200, {
            "result": "stopped",
            "device_response": res,
            "last_update_ts": last_ts,
            "last_sample": last_sample,
        })


def run() -> None:
    cfg = load_config()
    client = DeviceHTTPClient()
    buf = TelemetryBuffer()
    stop_event = threading.Event()

    collector = Collector(client, buf, stop_event)
    collector.start()

    Handler.client = client
    Handler.ctrl_state = ControlState()

    class Server(ThreadingHTTPServer):
        pass

    httpd = Server((cfg.http_host, cfg.http_port), Handler)
    # Inject buffer for handler access
    httpd.buf = buf  # type: ignore[attr-defined]

    def shutdown(signum=None, frame=None):  # noqa: ARG001
        logger.info("Shutting down driver (signal: %s)", signum)
        stop_event.set()
        Handler.ctrl_state.cancel_timer()
        # Gracefully stop HTTP server
        try:
            httpd.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("HTTP server starting on %s:%d", cfg.http_host, cfg.http_port)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        logger.info("HTTP server stopped. Waiting for collector...")
        stop_event.set()
        collector.join(timeout=5.0)
        logger.info("Driver exited.")


if __name__ == "__main__":
    run()
