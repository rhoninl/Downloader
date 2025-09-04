# driver.py
# Main HTTP server that proxies control to the robot device and exposes status, snapshot, and stream endpoints.

import json
import threading
import time
import signal
import sys
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, Optional, List

from config import load_config, DriverConfig
from device_client import DeviceHTTPClient


SERVICE_NAME = "robot-driver"
VERSION = "1.0.0"


class StatusStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.started_at = time.time()
        self.device_reachable = False
        self.last_command = None  # type: Optional[str]
        self.last_velocity = None  # type: Optional[float]
        self.last_update_ts = None  # type: Optional[float]
        self.commands_sent = 0
        self.errors = 0
        self.last_device_status_code = None  # type: Optional[int]
        self.last_device_response_body = None  # type: Optional[str]

    def update_reachability(self, reachable: bool) -> None:
        with self._lock:
            self.device_reachable = reachable
            self.last_update_ts = time.time()

    def record_command(self, cmd: str, vel: float, status_code: Optional[int], body: Optional[str]) -> None:
        with self._lock:
            self.last_command = cmd
            self.last_velocity = vel
            self.commands_sent += 1
            self.last_device_status_code = status_code
            self.last_device_response_body = body
            self.last_update_ts = time.time()

    def record_error(self) -> None:
        with self._lock:
            self.errors += 1
            self.last_update_ts = time.time()

    def snapshot(self, device_base_url: str) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            return {
                "service": SERVICE_NAME,
                "version": VERSION,
                "started_at": _iso8601(self.started_at),
                "uptime_s": round(now - self.started_at, 3),
                "device_base_url": device_base_url,
                "device_reachable": self.device_reachable,
                "last_command": self.last_command,
                "last_velocity": self.last_velocity,
                "last_device_status_code": self.last_device_status_code,
                "commands_sent": self.commands_sent,
                "errors": self.errors,
                "last_update": _iso8601(self.last_update_ts) if self.last_update_ts else None,
                "now": _iso8601(now),
            }


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: List["queue.Queue"] = []

    def subscribe(self):
        import queue
        q = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:
                pass


def _iso8601(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).isoformat() + "Z"


def log(msg: str, **fields: Any) -> None:
    ts = _iso8601(time.time())
    line = {"ts": ts, "msg": msg}
    line.update(fields)
    try:
        print(json.dumps(line), flush=True)
    except Exception:
        print(f"{ts} {msg} {fields}", flush=True)


class HealthPinger(threading.Thread):
    def __init__(self, cfg: DriverConfig, status: StatusStore, stop_event: threading.Event, bus: EventBus):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.status = status
        self.stop_event = stop_event
        self.bus = bus
        self._host, self._port = self._extract_host_port(cfg.device_base_url)

    def _extract_host_port(self, base_url: str):
        from urllib.parse import urlparse
        p = urlparse(base_url)
        host = p.hostname or ""
        if p.port:
            port = p.port
        else:
            port = 443 if (p.scheme or "http") == "https" else 80
        return host, port

    def run(self) -> None:
        log("health_pinger_start", host=self._host, port=self._port)
        last_state: Optional[bool] = None
        while not self.stop_event.is_set():
            reachable = self._try_connect(self._host, self._port, timeout=self.cfg.request_timeout)
            if reachable != last_state:
                self.status.update_reachability(reachable)
                self.bus.publish({
                    "type": "reachability",
                    "reachable": reachable,
                    "ts": _iso8601(time.time()),
                })
                log("device_reachability_change", reachable=reachable)
                last_state = reachable
            # emit periodic heartbeat event for streaming clients
            self.bus.publish({
                "type": "heartbeat",
                "reachable": reachable,
                "ts": _iso8601(time.time()),
            })
            self.stop_event.wait(self.cfg.ping_interval)
        log("health_pinger_stop")

    def _try_connect(self, host: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False


class RequestHandler(BaseHTTPRequestHandler):
    # Shared references will be set by server setup
    cfg: DriverConfig = None  # type: ignore
    client: DeviceHTTPClient = None  # type: ignore
    status_store: StatusStore = None  # type: ignore
    bus: EventBus = None  # type: ignore
    stop_event: threading.Event = None  # type: ignore

    server_version = f"{SERVICE_NAME}/{VERSION}"

    def _send_json(self, obj: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _parse_float_param(self, qs: Dict[str, Any], name: str) -> Optional[float]:
        vals = qs.get(name)
        if not vals:
            return None
        try:
            return float(vals[0])
        except Exception:
            return None

    def log_message(self, format: str, *args) -> None:  # noqa: A003 (shadow-builtin)
        # Override to integrate with our JSON logger
        log("http", remote=self.address_string(), method=self.command, path=self.path, proto=self.request_version, msg=format % args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query or "")

        if path == "/status":
            snap = self.status_store.snapshot(self.cfg.device_base_url)
            self._send_json(snap)
            return

        if path == "/snapshot":
            snap = self.status_store.snapshot(self.cfg.device_base_url)
            # provide a lightweight snapshot field for convenience
            self._send_json({"snapshot": snap})
            return

        if path == "/stream":
            self._handle_stream()
            return

        if path == "/move/forward":
            self._handle_move("forward", qs)
            return

        if path == "/move/backward":
            self._handle_move("backward", qs)
            return

        self.send_error(404, "Not Found")

    def _handle_move(self, direction: str, qs: Dict[str, Any]) -> None:
        lv = self._parse_float_param(qs, "linear_velocity")
        if lv is None:
            self._send_json({"error": "missing or invalid linear_velocity"}, status=400)
            return
        try:
            if direction == "forward":
                status_code, body_bytes, url = self.client.move_forward(lv)
            else:
                status_code, body_bytes, url = self.client.move_backward(lv)
            body_text = _safe_decode(body_bytes)
            self.status_store.record_command(direction, lv, status_code, body_text)
            event = {
                "type": "command",
                "direction": direction,
                "linear_velocity": lv,
                "device_status": status_code,
                "ts": _iso8601(time.time()),
            }
            self.bus.publish(event)
            log("command_sent", direction=direction, linear_velocity=lv, device_status=status_code, device_url=url)
            self._send_json({
                "ok": True,
                "direction": direction,
                "linear_velocity": lv,
                "device_status": status_code,
                "device_response": body_text,
            })
        except Exception as e:
            self.status_store.record_error()
            log("command_error", error=str(e), direction=direction)
            self._send_json({"ok": False, "error": str(e)}, status=500)

    def _handle_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = self.bus.subscribe()
        try:
            # Send an initial snapshot event
            snap = self.status_store.snapshot(self.cfg.device_base_url)
            self._sse_send({"type": "snapshot", "data": snap})

            last_heartbeat = time.time()
            heartbeat_interval = self.cfg.stream_heartbeat_interval

            while not self.stop_event.is_set():
                try:
                    item = q.get(timeout=1.0)
                except Exception:
                    item = None
                if item is not None:
                    self._sse_send(item)
                now = time.time()
                if now - last_heartbeat >= heartbeat_interval:
                    # Send comment as heartbeat to keep connection alive
                    self._sse_comment("keep-alive")
                    last_heartbeat = now
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.bus.unsubscribe(q)

    def _sse_send(self, data: Dict[str, Any]) -> None:
        payload = json.dumps(data)
        out = f"data: {payload}\n\n".encode("utf-8")
        try:
            self.wfile.write(out)
            self.wfile.flush()
        except Exception:
            raise

    def _sse_comment(self, comment: str) -> None:
        line = f": {comment}\n\n".encode("utf-8")
        try:
            self.wfile.write(line)
            self.wfile.flush()
        except Exception:
            raise


def _safe_decode(b: Optional[bytes]) -> Optional[str]:
    if b is None:
        return None
    try:
        return b.decode("utf-8", errors="replace")
    except Exception:
        return None


def run_server():
    cfg = load_config()

    if not cfg.device_base_url:
        log("fatal_missing_config", missing="DEVICE_BASE_URL")
        sys.stderr.write("DEVICE_BASE_URL is required\n")
        sys.stderr.flush()
        sys.exit(2)

    status = StatusStore()
    bus = EventBus()
    stop_event = threading.Event()

    client = DeviceHTTPClient(
        base_url=cfg.device_base_url,
        username=cfg.device_username,
        password=cfg.device_password,
        timeout=cfg.request_timeout,
        max_retries=cfg.max_retries,
        backoff_initial=cfg.retry_backoff_initial,
        backoff_max=cfg.retry_backoff_max,
    )

    # Health pinger in background
    pinger = HealthPinger(cfg, status, stop_event, bus)
    pinger.start()

    # Bind shared refs into handler
    RequestHandler.cfg = cfg
    RequestHandler.client = client
    RequestHandler.status_store = status
    RequestHandler.bus = bus
    RequestHandler.stop_event = stop_event

    httpd = ThreadingHTTPServer((cfg.http_host, cfg.http_port), RequestHandler)

    def _graceful_shutdown(signum, frame):  # noqa: ARG001
        log("signal_received", signum=signum)
        stop_event.set()
        # Initiate server shutdown in another thread to avoid deadlock
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    log("server_start", host=cfg.http_host, port=cfg.http_port)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        log("server_stopping")
        stop_event.set()
        # Allow background threads to notice stop
        time.sleep(cfg.shutdown_grace)
        try:
            httpd.server_close()
        except Exception:
            pass
        log("server_stopped")


if __name__ == "__main__":
    run_server()
