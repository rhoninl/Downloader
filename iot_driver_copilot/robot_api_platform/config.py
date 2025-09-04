import os
import sys
from typing import Optional

class Config:
    def __init__(self):
        # HTTP server configuration
        self.http_host = self._require('HTTP_HOST')
        self.http_port = self._require_int('HTTP_PORT')

        # Device base URL (e.g., http://robot.local:8080)
        self.device_base_url = self._require('DEVICE_BASE_URL')

        # Device command endpoint paths (relative to base)
        self.device_move_path = self._require('DEVICE_MOVE_PATH')  # e.g., /api/commands/move_velocity

        # Optional device status path for polling latest state
        self.device_status_path = os.getenv('DEVICE_STATUS_PATH')  # e.g., /api/state

        # Authentication (optional): Bearer token or Basic auth
        self.device_bearer_token = os.getenv('DEVICE_BEARER_TOKEN')
        self.device_basic_user = os.getenv('DEVICE_BASIC_USER')
        self.device_basic_pass = os.getenv('DEVICE_BASIC_PASS')

        # Timeouts and retries
        self.request_timeout_sec = self._require_float('REQUEST_TIMEOUT_SEC')
        self.cmd_max_retries = self._require_int('CMD_MAX_RETRIES')

        # Polling config only required if status path is provided
        if self.device_status_path:
            self.poll_interval_sec = self._require_float('POLL_INTERVAL_SEC')
            self.retry_backoff_init_ms = self._require_int('RETRY_BACKOFF_INIT_MS')
            self.retry_backoff_max_ms = self._require_int('RETRY_BACKOFF_MAX_MS')
        else:
            self.poll_interval_sec = None
            self.retry_backoff_init_ms = None
            self.retry_backoff_max_ms = None

    def _require(self, key: str) -> str:
        val = os.getenv(key)
        if val is None or val == '':
            print(f"Missing required env var: {key}", file=sys.stderr)
            sys.exit(1)
        return val

    def _require_int(self, key: str) -> int:
        val = self._require(key)
        try:
            return int(val)
        except ValueError:
            print(f"Env var {key} must be an integer", file=sys.stderr)
            sys.exit(1)

    def _require_float(self, key: str) -> float:
        val = self._require(key)
        try:
            return float(val)
        except ValueError:
            print(f"Env var {key} must be a float", file=sys.stderr)
            sys.exit(1)

CONFIG: Optional[Config] = None

def get_config() -> Config:
    global CONFIG
    if CONFIG is None:
        CONFIG = Config()
    return CONFIG
