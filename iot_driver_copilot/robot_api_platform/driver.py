# driver.py
import signal
import sys
import threading
import time
import json

from config import load_config
from device_client import DeviceClient
from stream_hub import SSEHub
from http_server import DriverHTTPHandler, DriverHTTPServer


class Bridge:
    def __init__(self, dev: DeviceClient, hub: SSEHub):
        self.dev = dev
        self.hub = hub
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="TelemetryBridge", daemon=True)
        self._last_broadcast_ts = 0.0

    def start(self):
        self._stop.clear()
        if not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="TelemetryBridge", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self):
        while not self._stop.is_set():
            latest = self.dev.get_latest()
            if latest is not None:
                ts = latest.get("ts", 0.0)
                if ts and ts != self._last_broadcast_ts:
                    try:
                        payload = json.dumps(latest)
                        self.hub.broadcast(payload)
                        self._last_broadcast_ts = ts
                    except Exception:
                        pass
            else:
                # no data yet, send periodic heartbeat
                self.hub.heartbeat()
            time.sleep(0.2)


def main():
    cfg = load_config()

    dev = DeviceClient(
        host=cfg.device_host,
        port=cfg.device_port,
        connect_timeout=cfg.connect_timeout_sec,
        read_timeout=cfg.read_timeout_sec,
        reconnect_backoff=cfg.reconnect_backoff_sec,
        max_backoff=cfg.max_backoff_sec,
    )
    hub = SSEHub()
    bridge = Bridge(dev, hub)

    dev.start()
    bridge.start()

    DriverHTTPHandler.device = dev
    DriverHTTPHandler.hub = hub
    DriverHTTPHandler.default_linear_velocity = cfg.default_linear_velocity

    httpd = DriverHTTPServer((cfg.http_host, cfg.http_port), DriverHTTPHandler)

    stop_event = threading.Event()

    def handle_signal(signum, frame):
        try:
            httpd.shutdown()
        except Exception:
            pass
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    sys.stderr.write(f"Starting HTTP server on {cfg.http_host}:{cfg.http_port}\n")
    sys.stderr.flush()

    server_thread = threading.Thread(target=httpd.serve_forever, name="HTTPServer", daemon=True)
    server_thread.start()

    # Wait for stop
    try:
        while not stop_event.is_set():
            time.sleep(0.2)
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        bridge.stop()
        dev.stop()
        try:
            httpd.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
