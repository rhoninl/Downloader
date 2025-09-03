# driver.py
import logging
import signal
import threading
import time
from typing import Optional

from config import get_config
from device_client import DeviceClient
from telemetry import Telemetry
from server import make_server


class BackgroundWorker(threading.Thread):
    def __init__(self, telemetry: Telemetry, stop_event: threading.Event, interval_sec: float):
        super().__init__(daemon=True)
        self.telemetry = telemetry
        self.stop_event = stop_event
        self.interval_sec = interval_sec

    def run(self):
        logging.info("Background worker started")
        while not self.stop_event.is_set():
            try:
                self.telemetry.tick()
            except Exception as e:
                logging.warning("Background tick error: %s", e)
            # Sleep in small chunks to be more responsive to shutdown
            remaining = self.interval_sec
            while remaining > 0 and not self.stop_event.is_set():
                step = min(0.2, remaining)
                time.sleep(step)
                remaining -= step
        logging.info("Background worker stopped")


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = get_config()

    telemetry = Telemetry()

    device = DeviceClient(
        base_url=cfg.device_base_url,
        timeout_sec=cfg.device_timeout_sec,
        retry_max=cfg.retry_max,
        retry_backoff_sec=cfg.retry_backoff_sec,
        username=cfg.device_username,
        password=cfg.device_password,
    )

    stop_event = threading.Event()

    bg = BackgroundWorker(telemetry=telemetry, stop_event=stop_event, interval_sec=cfg.stream_interval_sec)
    bg.start()

    httpd = make_server(cfg.http_host, cfg.http_port, telemetry, device, cfg.stream_interval_sec, stop_event)

    def shutdown(signum=None, frame=None):
        logging.info("Shutdown signal received: %s", signum)
        stop_event.set()
        # Close server
        try:
            httpd.shutdown()
        except Exception as e:
            logging.warning("Error during server shutdown: %s", e)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logging.info("HTTP server starting on %s:%s", cfg.http_host, cfg.http_port)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        logging.info("HTTP server stopped")
        stop_event.set()
        bg.join(timeout=5.0)


if __name__ == '__main__':
    main()
