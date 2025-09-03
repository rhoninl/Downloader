# driver.py
import json
import logging
import signal
import threading
import time
from typing import Optional

from config import load_config
from data_store import DataStore
from device_client import DeviceClient
from http_server import make_server


class Collector(threading.Thread):
    def __init__(self, client: DeviceClient, store: DataStore, poll_interval_s: float, retry_backoff_s: float, retry_limit: int):
        super().__init__(name="collector", daemon=True)
        self._client = client
        self._store = store
        self._poll_interval_s = poll_interval_s
        self._retry_backoff_s = retry_backoff_s
        self._retry_limit = retry_limit
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        consecutive_errors = 0
        while not self._stop.is_set():
            if not self._store.is_connected():
                # Not connected; sleep briefly and retry
                time.sleep(min(self._poll_interval_s, 1.0))
                consecutive_errors = 0
                continue
            try:
                status = self._client.fetch_status()
                # Normalize sample shape
                sample = {
                    'ts': time.time(),
                    'device_status': status,
                }
                self._store.update_sample(sample)
                consecutive_errors = 0
                time.sleep(self._poll_interval_s)
            except Exception as e:
                consecutive_errors += 1
                self._store.set_error(str(e))
                logging.warning("Collector error (%d/%d): %s", consecutive_errors, self._retry_limit, e)
                if consecutive_errors >= self._retry_limit:
                    logging.error("Collector exceeded retry limit. Marking disconnected and attempting reconnect.")
                    self._store.set_connected(False)
                    consecutive_errors = 0
                    # Attempt auto-reconnect after backoff
                    if self._stop.wait(self._retry_backoff_s):
                        break
                    try:
                        self._client.connect()
                        self._store.set_connected(True)
                        logging.info("Auto-reconnect successful")
                    except Exception as e2:
                        self._store.set_error(str(e2))
                        logging.warning("Auto-reconnect failed: %s", e2)
                        # Wait more before next attempt
                        if self._stop.wait(self._retry_backoff_s):
                            break
                else:
                    if self._stop.wait(self._retry_backoff_s):
                        break


def main():
    cfg = load_config()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(threadName)s %(message)s'
    )

    store = DataStore()
    client = DeviceClient(cfg.device_base_url, cfg.device_timeout_s, cfg.device_username, cfg.device_password)

    collector = Collector(
        client=client,
        store=store,
        poll_interval_s=cfg.device_poll_interval_s,
        retry_backoff_s=cfg.device_retry_backoff_s,
        retry_limit=cfg.device_retry_limit,
    )

    httpd = make_server(cfg.http_host, cfg.http_port, store, client, cfg.device_poll_interval_s)

    stop_event = threading.Event()

    def handle_shutdown(signum, frame):
        logging.info("Received signal %s, shutting down...", signum)
        stop_event.set()
        try:
            httpd.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    logging.info("Starting collector thread")
    collector.start()

    logging.info("Starting HTTP server on %s:%s", cfg.http_host, cfg.http_port)
    server_thread = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    server_thread.start()

    # Wait for shutdown
    while not stop_event.is_set():
        time.sleep(0.2)

    # Graceful stop
    logging.info("Stopping collector")
    collector.stop()
    collector.join(timeout=5.0)
    logging.info("HTTP server stopped")


if __name__ == '__main__':
    main()
