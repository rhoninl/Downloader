# config.py
import os
import sys
from urllib.parse import urlparse

class Config:
    def __init__(self):
        self.http_host = os.getenv('HTTP_HOST', '0.0.0.0')
        try:
            self.http_port = int(os.getenv('HTTP_PORT', '8000'))
        except ValueError:
            print('Invalid HTTP_PORT, must be int', file=sys.stderr)
            sys.exit(1)

        self.device_base_url = os.getenv('DEVICE_BASE_URL')
        if not self.device_base_url:
            print('DEVICE_BASE_URL is required', file=sys.stderr)
            sys.exit(1)

        self.device_username = os.getenv('DEVICE_USERNAME')
        self.device_password = os.getenv('DEVICE_PASSWORD')

        # Timeouts and retry
        try:
            self.request_timeout_sec = float(os.getenv('REQUEST_TIMEOUT_SEC', '5.0'))
        except ValueError:
            print('Invalid REQUEST_TIMEOUT_SEC', file=sys.stderr)
            sys.exit(1)

        try:
            self.retry_max_attempts = int(os.getenv('RETRY_MAX_ATTEMPTS', '3'))
        except ValueError:
            print('Invalid RETRY_MAX_ATTEMPTS', file=sys.stderr)
            sys.exit(1)

        try:
            self.retry_backoff_ms = int(os.getenv('RETRY_BACKOFF_MS', '500'))
        except ValueError:
            print('Invalid RETRY_BACKOFF_MS', file=sys.stderr)
            sys.exit(1)

        try:
            self.stream_interval_ms = int(os.getenv('STREAM_INTERVAL_MS', '1000'))
        except ValueError:
            print('Invalid STREAM_INTERVAL_MS', file=sys.stderr)
            sys.exit(1)

        try:
            self.connect_check_interval_ms = int(os.getenv('CONNECT_CHECK_INTERVAL_MS', '1000'))
        except ValueError:
            print('Invalid CONNECT_CHECK_INTERVAL_MS', file=sys.stderr)
            sys.exit(1)

        try:
            self.shutdown_grace_sec = float(os.getenv('SHUTDOWN_GRACE_SEC', '5.0'))
        except ValueError:
            print('Invalid SHUTDOWN_GRACE_SEC', file=sys.stderr)
            sys.exit(1)

        self._parsed = urlparse(self.device_base_url)
        if self._parsed.scheme not in ('http', 'https'):
            print('DEVICE_BASE_URL must be http or https', file=sys.stderr)
            sys.exit(1)
        if not self._parsed.hostname:
            print('DEVICE_BASE_URL must include host', file=sys.stderr)
            sys.exit(1)

    @property
    def device_scheme(self):
        return self._parsed.scheme

    @property
    def device_host(self):
        return self._parsed.hostname

    @property
    def device_port(self):
        if self._parsed.port:
            return self._parsed.port
        return 443 if self._parsed.scheme == 'https' else 80

    @property
    def device_base_path(self):
        # Ensure base path has no trailing slash for joining
        p = self._parsed.path or ''
        if p.endswith('/'):
            p = p[:-1]
        return p

    def join_device_path(self, path: str) -> str:
        # Create absolute path combining base path and provided path
        base = self.device_base_path
        if not path.startswith('/'):
            path = '/' + path
        if base:
            return base + path
        return path
