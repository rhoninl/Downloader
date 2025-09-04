# driver.py
import logging
import signal
import sys
import threading
import time

from config import Config
from device_client import DeviceClient
from http_server import create_server
from state import DriverState

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('driver')


def connectivity_worker(cfg: Config, client: DeviceClient, state: DriverState, stop_event: threading.Event):
    interval = max(100, cfg.connect_check_interval_ms) / 1000.0
    logger.info('Connectivity worker started with interval %.3fs', interval)
    while not stop_event.is_set():
        # Prefer HTTP HEAD check; fallback to TCP port check
        ok_http, http_status, rtt_http, err_http = client.head_root()
        if ok_http:
            state.update_connectivity(True, rtt_http, True, http_status, None)
        else:
            ok_tcp, rtt_tcp, err_tcp = client.port_reachable()
            state.update_connectivity(ok_tcp, rtt_tcp, False, None, err_tcp or err_http)
        stop_event.wait(interval)
    logger.info('Connectivity worker stopped')


def main():
    cfg = Config()
    state = DriverState()
    client = DeviceClient(cfg)

    shutdown_event = threading.Event()

    # Start background connectivity worker
    worker_thread = threading.Thread(target=connectivity_worker, args=(cfg, client, state, shutdown_event), name='connectivity', daemon=True)
    worker_thread.start()

    # Create HTTP server
    server = create_server(cfg, client, state, shutdown_event)

    def handle_signal(signum, frame):  # noqa: ANN001
        logger.info('Received signal %s, shutting down...', signum)
        shutdown_event.set()
        # Shutdown HTTP server
        try:
            server.shutdown()
        except Exception as e:  # noqa: BLE001
            logger.warning('Error during server shutdown: %s', e)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info('HTTP server starting on %s:%d', cfg.http_host, cfg.http_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        handle_signal('KeyboardInterrupt', None)  # type: ignore[arg-type]
    finally:
        # Wait for background worker to finish
        worker_thread.join(timeout=cfg.shutdown_grace_sec)
        logger.info('Shutdown complete')


if __name__ == '__main__':
    main()
