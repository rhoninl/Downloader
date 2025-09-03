# config.py
import os
import sys
from typing import Optional


class ConfigError(Exception):
    pass


def _get_env(name: str, required: bool = True, cast: Optional[type] = None) -> Optional[object]:
    val = os.getenv(name)
    if val is None or val == "":
        if required:
            raise ConfigError(f"Missing required environment variable: {name}")
        return None
    if cast is None:
        return val
    try:
        if cast is bool:
            low = val.strip().lower()
            if low in ("1", "true", "yes", "on"): return True
            if low in ("0", "false", "no", "off"): return False
            raise ValueError(f"Invalid boolean for {name}: {val}")
        return cast(val)
    except Exception as e:
        raise ConfigError(f"Invalid value for {name}: {val} ({e})")


class Config:
    def __init__(self):
        # HTTP Server
        self.http_host: str = _get_env("HTTP_HOST", required=True, cast=str)  # e.g., 0.0.0.0
        self.http_port: int = _get_env("HTTP_PORT", required=True, cast=int)  # e.g., 8080

        # Device connection
        self.device_host: str = _get_env("DEVICE_HOST", required=True, cast=str)
        self.device_port: int = _get_env("DEVICE_PORT", required=True, cast=int)

        # Timeouts and retry/backoff settings (milliseconds)
        self.connect_timeout_ms: int = _get_env("CONNECT_TIMEOUT_MS", required=True, cast=int)
        self.read_timeout_ms: int = _get_env("READ_TIMEOUT_MS", required=True, cast=int)
        self.retry_backoff_min_ms: int = _get_env("RETRY_BACKOFF_MS_MIN", required=True, cast=int)
        self.retry_backoff_max_ms: int = _get_env("RETRY_BACKOFF_MS_MAX", required=True, cast=int)

        # Streaming heartbeat seconds (to keep HTTP connections alive when idle)
        self.stream_heartbeat_s: int = _get_env("STREAM_HEARTBEAT_S", required=True, cast=int)

    def dump(self) -> dict:
        return {
            "http_host": self.http_host,
            "http_port": self.http_port,
            "device_host": self.device_host,
            "device_port": self.device_port,
            "connect_timeout_ms": self.connect_timeout_ms,
            "read_timeout_ms": self.read_timeout_ms,
            "retry_backoff_min_ms": self.retry_backoff_min_ms,
            "retry_backoff_max_ms": self.retry_backoff_max_ms,
            "stream_heartbeat_s": self.stream_heartbeat_s,
        }


def load_config() -> Config:
    try:
        return Config()
    except ConfigError as e:
        sys.stderr.write(str(e) + "\n")
        sys.exit(2)
