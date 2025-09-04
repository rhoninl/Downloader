# driver.py
# Entry point for the Robot API Platform driver

import logging
import signal
import sys
import threading

from config import load_config
from device_client import DeviceClient
from http_server import run_http_server


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    )

    try:
        cfg = load_config()
    except Exception as e:
        logging.getLogger('driver').error("Configuration error: %s", e)
        sys.exit(2)

    dev = DeviceClient(
        base_url=cfg.robot_base_url,
        state_path=cfg.robot_state_path,
        cmd_move_path=cfg.robot_cmd_move_path,
        user=cfg.robot_user,
        passwd=cfg.robot_pass,
        timeout_s=cfg.robot_timeout_s,
        poll_interval_s=cfg.robot_poll_interval_s,
        retry_backoff_s=cfg.robot_retry_backoff_s,
    )
    dev.start()

    httpd = run_http_server(cfg.http_host, cfg.http_port, dev, cfg.default_linear_vel)

    stop_event = threading.Event()

    def _graceful_shutdown(signum, frame):
        logging.getLogger('driver').info("Signal %s received, shutting down...", signum)
        stop_event.set()
        # Stop HTTP server
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    try:
        httpd.serve_forever()
    finally:
        dev.stop()
        httpd.server_close()
        logging.getLogger('driver').info("Shutdown complete")


if __name__ == '__main__':
    main()
