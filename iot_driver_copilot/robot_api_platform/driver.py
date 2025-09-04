import json
import logging
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer
from urllib import request, error
import base64

from config import load_config


class TelemetryBuffer:
    def __init__(self):
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._latest_obj: dict | None = None
        self._latest_raw: str | None = None
        self._last_update_ts: float | None = None
        self._update_count: int = 0

    def update(self, obj: dict, raw: str):
        with self._cond:
            self._latest_obj = obj
            self._latest_raw = raw
            self._last_update_ts = time.time()
            self._update_count += 1
            self._cond.notify_all()

    def get_snapshot(self) -> tuple[dict | None, str | None, float | None, int]:
        with self._lock:
            return self._latest_obj, self._latest_raw, self._last_update_ts, self._update_count

    def wait_for_update(self, last_seen_count: int, timeout: float | None = None) -> int:
        with self._cond:
            if last_seen_count != self._update_count:
                return self._update_count
            self._cond.wait(timeout=timeout)
            return self._update_count


class DriverState:
    def __init__(self):
        self._lock = threading.RLock()
        self.connected: bool = False
        self.last_error: str | None = None
        self.samples_received: int = 0
        self.consecutive_errors: int = 0
        self.current_backoff: float = 0.0
        self.last_update_ts: float | None = None

    def on_success(self):
        with self._lock:
            if not self.connected:
                logging.info("Device connection established")
            self.connected = True
            self.consecutive_errors = 0
            self.current_backoff = 0.0
            self.samples_received += 1
            self.last_error = None
            self.last_update_ts = time.time()

    def on_error(self, err: Exception | str, backoff: float):
        with self._lock:
            if self.connected:
                logging.warning("Device connection lost: %s", err)
            self.connected = False
            self.consecutive_errors += 1
            self.current_backoff = backoff
            self.last_error = str(err)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "connected": self.connected,
                "samples_received": self.samples_received,
                "consecutive_errors": self.consecutive_errors,
                "current_backoff_sec": round(self.current_backoff, 3),
                "last_error": self.last_error,
                "last_update_unix": self.last_update_ts,
                "last_update_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.last_update_ts)) if self.last_update_ts else None,
            }


class Collector(threading.Thread):
    def __init__(self, cfg, buf: TelemetryBuffer, state: DriverState, stop_evt: threading.Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.buf = buf
        self.state = state
        self.stop_evt = stop_evt

    def _build_request(self) -> request.Request:
        req = request.Request(self.cfg.device_poll_url)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "SmartDonkey-Driver/1.0")
        if self.cfg.auth_bearer_token:
            req.add_header("Authorization", f"Bearer {self.cfg.auth_bearer_token}")
        elif self.cfg.basic_auth_user and self.cfg.basic_auth_pass:
            token = f"{self.cfg.basic_auth_user}:{self.cfg.basic_auth_pass}".encode("utf-8")
            b64 = base64.b64encode(token).decode("ascii")
            req.add_header("Authorization", f"Basic {b64}")
        return req

    def run(self):
        backoff = self.cfg.backoff_initial_sec
        first_log = True
        while not self.stop_evt.is_set():
            try:
                if first_log:
                    logging.info("Polling device: %s", self.cfg.device_poll_url)
                    first_log = False
                req = self._build_request()
                with request.urlopen(req, timeout=self.cfg.http_timeout_sec) as resp:
                    ctype = resp.headers.get("Content-Type", "").lower()
                    raw_bytes = resp.read()
                    raw_text = raw_bytes.decode("utf-8", errors="replace").strip()
                    if "application/json" not in ctype and not (raw_text.startswith("{") or raw_text.startswith("[")):
                        raise ValueError(f"Unexpected content-type: {ctype}")
                    try:
                        obj = json.loads(raw_text) if raw_text else {}
                    except json.JSONDecodeError as je:
                        raise ValueError(f"Invalid JSON from device: {je}")

                    self.buf.update(obj, json.dumps(obj, separators=(",", ":")))
                    self.state.on_success()
                    backoff = self.cfg.backoff_initial_sec

                # Sleep the configured poll interval or until stop
                if self.stop_evt.wait(self.cfg.poll_interval_sec):
                    break

            except Exception as exc:  # noqa: BLE001
                logging.error("Polling error: %s", exc)
                self.state.on_error(exc, backoff)
                # Exponential backoff with cap
                if self.stop_evt.wait(backoff):
                    break
                backoff = min(backoff * 2.0, self.cfg.backoff_max_sec)


class DriverHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "SmartDonkeyDriver/1.0"

    # References to shared objects will be attached to the server instance
    # as attributes: buf, state, stop_evt, cfg

    def log_message(self, format: str, *args):
        logging.info("%s - - %s", self.address_string(), format % args)

    def _send_json(self, obj: dict, status=HTTPStatus.OK):
        payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        if self.path == "/status":
            self.handle_status()
        elif self.path == "/snapshot":
            self.handle_snapshot()
        elif self.path == "/stream":
            self.handle_stream()
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def handle_status(self):
        # Compose status including driver and device state
        obj, _raw, last_ts, _cnt = self.server.buf.get_snapshot()
        status = self.server.state.snapshot()
        status.update({
            "has_snapshot": obj is not None,
            "device_poll_url": self.server.cfg.device_poll_url,
        })
        self._send_json(status)

    def handle_snapshot(self):
        obj, raw, _last_ts, _cnt = self.server.buf.get_snapshot()
        if raw is None:
            # No data yet: return empty JSON object
            self._send_json({})
        else:
            payload = raw.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def handle_stream(self):
        # Server-Sent Events streaming of telemetry JSON
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # Send an initial comment to open the stream
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
        except Exception:
            return

        last_seen = 0
        # Send the last snapshot immediately if present
        _obj, raw, _ts, cnt = self.server.buf.get_snapshot()
        if raw is not None:
            try:
                data = raw.replace("\n", "")
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                last_seen = cnt
            except Exception:
                return

        heartbeat_sec = 15.0
        while not self.server.stop_evt.is_set():
            try:
                new_count = self.server.buf.wait_for_update(last_seen, timeout=heartbeat_sec)
                if new_count == last_seen:
                    # Heartbeat
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                # There is a new sample
                _obj, raw, _ts, cnt = self.server.buf.get_snapshot()
                if raw is None:
                    continue
                data = raw.replace("\n", "")
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                last_seen = cnt
            except (BrokenPipeError, ConnectionResetError):
                break
            except Exception as exc:  # noqa: BLE001
                logging.error("Stream error: %s", exc)
                break


class ThreadingHTTPServer(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    buf = TelemetryBuffer()
    state = DriverState()
    stop_evt = threading.Event()

    collector = Collector(cfg, buf, state, stop_evt)
    collector.start()

    def handle_signal(signum, frame):  # noqa: ARG001
        logging.info("Signal %s received, shutting down...", signum)
        stop_evt.set()
        try:
            httpd.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    class Server(ThreadingHTTPServer):
        pass

    Server.buf = buf
    Server.state = state
    Server.stop_evt = stop_evt
    Server.cfg = cfg

    global httpd  # for signal handler access
    httpd = Server((cfg.http_host, cfg.http_port), DriverHTTPRequestHandler)

    logging.info("HTTP server listening on %s:%d", cfg.http_host, cfg.http_port)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        stop_evt.set()
        logging.info("Stopping background collector...")
        collector.join(timeout=5.0)
        logging.info("Shutdown complete.")


if __name__ == "__main__":
    main()
