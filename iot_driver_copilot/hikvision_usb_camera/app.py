# app.py
import signal
import sys
import time

from config import load_config
from device_client import CameraClient
from http_server import run_http_server


def main():
    cfg = load_config()

    camera = CameraClient(
        device_id=cfg.camera_device,
        frame_width=cfg.frame_width,
        frame_height=cfg.frame_height,
        frame_rate=cfg.frame_rate,
        jpeg_quality=cfg.jpeg_quality,
        reconnect_min_ms=cfg.reconnect_min_ms,
        reconnect_max_ms=cfg.reconnect_max_ms,
        read_timeout_ms=cfg.read_timeout_ms,
    )

    httpd, http_thread = run_http_server(cfg.http_host, cfg.http_port, camera, cfg.stream_fps_limit)

    if cfg.auto_connect:
        camera.start()

    stop_requested = False

    def handle_signal(signum, frame):
        nonlocal stop_requested
        if not stop_requested:
            print(f"[main] received signal {signum}, shutting down...")
            stop_requested = True
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                camera.stop()
            except Exception:
                pass

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while not stop_requested:
            time.sleep(0.5)
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
        print("[main] exited")


if __name__ == "__main__":
    main()
