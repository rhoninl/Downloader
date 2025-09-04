# http_server.py
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import urllib.parse
import time
import threading
from typing import Tuple

from device_client import DeviceClient
from stream_hub import SSEHub


class DriverHTTPHandler(BaseHTTPRequestHandler):
    # Will be set by server bootstrap
    device: DeviceClient = None  # type: ignore
    hub: SSEHub = None  # type: ignore
    default_linear_velocity: float = 0.2

    server_version = "RobotDriverHTTP/1.0"

    def log_message(self, format: str, *args):
        try:
            msg = "%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args)
            self.server.log_lock.acquire()
            try:
                self.server.log_stream.write(msg)
                self.server.log_stream.flush()
            finally:
                self.server.log_lock.release()
        except Exception:
            pass

    def _send_json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _parse_query(self):
        parts = urllib.parse.urlsplit(self.path)
        return parts.path, urllib.parse.parse_qs(parts.query)

    def do_GET(self):
        path, qs = self._parse_query()
        if path == "/status":
            self.handle_status()
        elif path == "/snapshot":
            self.handle_snapshot()
        elif path == "/stream":
            self.handle_stream()
        elif path == "/move/forward":
            self.handle_move(direction="FORWARD", qs=qs)
        elif path == "/move/backward":
            self.handle_move(direction="BACKWARD", qs=qs)
        else:
            self.send_error(404, "Not Found")

    def handle_status(self):
        stats = self.device.get_stats()
        latest = self.device.get_latest()
        resp = {
            "connected": stats.get("connected", False),
            "uptime_sec": stats.get("uptime_sec", 0.0),
            "last_telemetry_ts": stats.get("last_telemetry_ts"),
            "samples_received": stats.get("samples_received", 0),
            "bytes_received": stats.get("bytes_received", 0),
            "last_error": stats.get("last_error"),
            "latest_keys": list(latest.keys()) if latest else [],
        }
        self._send_json(resp)

    def handle_snapshot(self):
        latest = self.device.get_latest()
        if latest is None:
            self._send_json({"error": "no data yet"}, status=503)
            return
        self._send_json(latest)

    def handle_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        client = self.hub.subscribe()
        # Send initial comment to establish SSE
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
        except Exception:
            self.hub.unsubscribe(client)
            return
        # Push last known snapshot immediately if any
        latest = self.device.get_latest()
        if latest is not None:
            try:
                payload = json.dumps(latest).encode("utf-8")
                self.wfile.write(b"data: "+payload+b"\n\n")
                self.wfile.flush()
            except Exception:
                self.hub.unsubscribe(client)
                return
        # Loop forwarding hub messages
        try:
            import queue as _q
            while True:
                try:
                    item = client.queue.get(timeout=15.0)
                except _q.Empty:
                    # heartbeat
                    try:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
                    continue
                if item == "__CLOSE__":
                    break
                try:
                    if item:
                        payload = item.encode("utf-8")
                        self.wfile.write(b"data: "+payload+b"\n\n")
                    else:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                except Exception:
                    break
        finally:
            self.hub.unsubscribe(client)

    def handle_move(self, direction: str, qs):
        lv = None
        try:
            if "linear_velocity" in qs and len(qs["linear_velocity"]) > 0:
                lv = float(qs["linear_velocity"][0])
        except Exception:
            lv = None
        if lv is None:
            lv = self.default_linear_velocity
        # Clamp to a sane range
        if direction == "FORWARD":
            lv = max(0.0, min(lv, 3.0))
        else:
            lv = max(0.0, min(lv, 3.0))
        vel = lv if direction == "FORWARD" else -lv
        ok = self.device.send_move(direction=direction, linear_velocity=abs(vel))
        status = 200 if ok else 503
        resp = {
            "ok": ok,
            "direction": direction.lower(),
            "linear_velocity": vel,
            "connected": self.device.is_connected(),
            "ts": time.time(),
        }
        self._send_json(resp, status=status)


class DriverHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: Tuple[str, int], RequestHandlerClass, bind_and_activate: bool = True):
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)
        self.log_lock = threading.Lock()
        import sys
        self.log_stream = sys.stderr
