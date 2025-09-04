import json
import logging
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib import request, parse, error as urlerror

from config import Config


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def join_url(base: str, path: str) -> str:
    base = base.rstrip("/")
    path = path if path.startswith("/") else "/" + path
    return base + path


class DeviceClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.telemetry_url = join_url(cfg.device_base_url, cfg.device_telemetry_path)
        self.cmd_vel_url = join_url(cfg.device_base_url, cfg.device_cmd_vel_path)

    def get_telemetry(self) -> Dict[str, Any]:
        req = request.Request(self.telemetry_url, method="GET")
        with request.urlopen(req, timeout=self.cfg.http_timeout_sec) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            data = resp.read().decode(charset)
            return json.loads(data)

    def send_velocity(self, linear: float, angular: float = 0.0) -> Dict[str, Any]:
        payload = {
            "linear_velocity": float(linear),
            "angular_velocity": float(angular),
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.cmd_vel_url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=self.cfg.http_timeout_sec) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            data = resp.read().decode(charset)
            if not data:
                return {"status": "ok"}
            try:
                return json.loads(data)
            except Exception:
                return {"raw_response": data}

    def stop(self) -> Dict[str, Any]:
        return self.send_velocity(0.0, 0.0)


class TelemetryPoller:
    def __init__(self, client: DeviceClient, cfg: Config):
        self.client = client
        self.cfg = cfg
        self._thread = threading.Thread(target=self._run, name="telemetry-poller", daemon=True)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: Dict[str, Any] = {}
        self._last_update_ts: Optional[float] = None

    def start(self):
        logging.info("Starting telemetry poller")
        self._thread.start()

    def stop(self):
        logging.info("Stopping telemetry poller")
        self._stop.set()
        self._thread.join(timeout=self.cfg.http_timeout_sec + 2.0)

    def get_latest(self) -> Dict[str, Any]:
        with self._lock:
            out = dict(self._latest) if self._latest else {}
            if self._last_update_ts is not None:
                out["_last_update_ts"] = self._last_update_ts
            return out

    def _run(self):
        backoff_attempts = 0
        while not self._stop.is_set():
            try:
                data = self.client.get_telemetry()
                now = time.time()
                with self._lock:
                    self._latest = data if isinstance(data, dict) else {"data": data}
                    self._last_update_ts = now
                if backoff_attempts > 0:
                    logging.info("Telemetry reconnected after %d attempt(s)", backoff_attempts)
                backoff_attempts = 0
                time.sleep(self.cfg.poll_interval_sec)
            except (urlerror.URLError, urlerror.HTTPError, TimeoutError) as e:
                backoff_attempts += 1
                delay = min(self.cfg.retry_max_delay_sec, self.cfg.retry_base_delay_sec * (2 ** (backoff_attempts - 1)))
                logging.warning(
                    "Telemetry error: %s; attempt=%d; backing off for %.2fs",
                    getattr(e, 'reason', str(e)), backoff_attempts, delay,
                )
                if self._stop.wait(delay):
                    break
            except Exception as e:
                backoff_attempts += 1
                delay = min(self.cfg.retry_max_delay_sec, self.cfg.retry_base_delay_sec * (2 ** (backoff_attempts - 1)))
                logging.exception("Unexpected telemetry error: %s; backing off for %.2fs", e, delay)
                if self._stop.wait(delay):
                    break


class Mover:
    def __init__(self, client: DeviceClient):
        self.client = client
        self._lock = threading.Lock()
        self._stop_timer: Optional[threading.Timer] = None

    def _cancel_timer_locked(self):
        if self._stop_timer is not None:
            try:
                self._stop_timer.cancel()
            except Exception:
                pass
            self._stop_timer = None

    def move(self, linear: float, duration: Optional[float]) -> Dict[str, Any]:
        with self._lock:
            self._cancel_timer_locked()
            logging.info("Sending velocity command linear=%.3f duration=%s", linear, str(duration))
            resp = self.client.send_velocity(linear, 0.0)
            if duration is not None and duration > 0:
                def _send_stop():
                    try:
                        logging.info("Auto-stopping after duration %.3fs", duration)
                        self.client.stop()
                    except Exception as e:
                        logging.error("Failed to auto-stop: %s", e)
                t = threading.Timer(duration, _send_stop)
                t.daemon = True
                t.start()
                self._stop_timer = t
            return resp

    def cancel(self):
        with self._lock:
            self._cancel_timer_locked()
            try:
                self.client.stop()
            except Exception as e:
                logging.error("Failed to send stop on cancel: %s", e)


class RequestHandler(BaseHTTPRequestHandler):
    cfg: Config = None  # type: ignore
    client: DeviceClient = None  # type: ignore
    poller: TelemetryPoller = None  # type: ignore
    mover: Mover = None  # type: ignore

    def _send_json(self, code: int, payload: Dict[str, Any]):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logging.info("%s - - %s", self.address_string(), fmt %% args)

    def do_GET(self):
        try:
            parsed = parse.urlparse(self.path)
            path = parsed.path
            q = parse.parse_qs(parsed.query)

            if path == "/move/forward":
                self._handle_move(forward=True, q=q)
                return
            if path == "/move/backward":
                self._handle_move(forward=False, q=q)
                return

            self._send_json(404, {"error": "not_found"})
        except Exception as e:
            logging.exception("Request handling error: %s", e)
            self._send_json(500, {"error": "internal_error", "message": str(e)})

    def _handle_move(self, forward: bool, q: Dict[str, Any]):
        speed_str = q.get("speed", [None])[0]
        duration_str = q.get("duration", [None])[0]

        try:
            if speed_str is None or speed_str == "":
                speed = float(self.cfg.default_speed)
            else:
                speed = float(speed_str)
        except ValueError:
            self._send_json(400, {"error": "invalid_speed"})
            return

        try:
            duration = None if duration_str in (None, "") else float(duration_str)
            if duration is not None and duration < 0:
                raise ValueError("duration must be >= 0")
        except ValueError:
            self._send_json(400, {"error": "invalid_duration"})
            return

        linear = speed if forward else -abs(speed)
        try:
            cmd_resp = self.mover.move(linear=linear, duration=duration)
            telem = self.poller.get_latest()
            self._send_json(200, {
                "status": "ok",
                "action": "forward" if forward else "backward",
                "requested_speed": speed,
                "effective_linear": linear,
                "duration": duration,
                "device_response": cmd_resp,
                "telemetry": telem,
                "timestamp": time.time(),
            })
        except (urlerror.URLError, urlerror.HTTPError, TimeoutError) as e:
            self._send_json(502, {"error": "device_unreachable", "message": getattr(e, 'reason', str(e))})
        except Exception as e:
            self._send_json(500, {"error": "command_failed", "message": str(e)})


def run():
    cfg = Config.load()
    client = DeviceClient(cfg)
    poller = TelemetryPoller(client, cfg)
    mover = Mover(client)

    RequestHandler.cfg = cfg
    RequestHandler.client = client
    RequestHandler.poller = poller
    RequestHandler.mover = mover

    server = ThreadingHTTPServer((cfg.http_host, cfg.http_port), RequestHandler)

    stop_event = threading.Event()

    def _shutdown(signum=None, frame=None):
        if not stop_event.is_set():
            logging.info("Shutting down (signal: %s)", str(signum))
            stop_event.set()
            try:
                server.shutdown()
            except Exception:
                pass

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    poller.start()

    logging.info("HTTP server starting on %s:%d", cfg.http_host, cfg.http_port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        logging.info("HTTP server stopping")
        mover.cancel()
        poller.stop()
        try:
            server.server_close()
        except Exception:
            pass
        logging.info("Shutdown complete")


if __name__ == "__main__":
    run()
