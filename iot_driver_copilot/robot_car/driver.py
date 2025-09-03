# driver.py
import logging
import signal
import sys
import time

from config import load_config
from device_client import DeviceClient
from http_server import ServerThread, device_client_global, stream_heartbeat_s


def main():
    cfg = load_config()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')

    # Create and start device client
    dc = DeviceClient(
        host=cfg.device_host,
        port=cfg.device_port,
        connect_timeout_ms=cfg.connect_timeout_ms,
        read_timeout_ms=cfg.read_timeout_ms,
        backoff_min_ms=cfg.retry_backoff_min_ms,
        backoff_max_ms=cfg.retry_backoff_max_ms,
    )
    dc.start()

    # Expose device client to HTTP handlers
    global device_client_global
    device_client_global = dc

    # Configure heartbeat interval for stream endpoint
    global stream_heartbeat_s
    stream_heartbeat_s = cfg.stream_heartbeat_s

    # Start HTTP server
    srv = ServerThread(cfg.http_host, cfg.http_port)
    srv.start()

    stop = False

    def handle_signal(signum, frame):
        nonlocal stop
        logging.info("Received signal %s, shutting down...", signum)
        stop = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while not stop:
            time.sleep(0.5)
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            dc.stop()
        except Exception:
            pass
        logging.info("Shutdown complete")


if __name__ == '__main__':
    main()
