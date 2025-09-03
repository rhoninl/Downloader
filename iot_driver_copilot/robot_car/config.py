# config.py
import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class Config:
    http_host: str
    http_port: int
    device_base_url: str
    device_timeout_sec: float
    retry_max: int
    retry_backoff_sec: float
    stream_interval_sec: float
    device_username: str | None = None
    device_password: str | None = None


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if val is None or val == "":
        raise ValueError(f"Missing required environment variable: {name}")
    return val


def get_config() -> Config:
    http_host = _require_env("HTTP_HOST")
    http_port = int(_require_env("HTTP_PORT"))

    device_base_url = _require_env("DEVICE_BASE_URL")
    # Basic validation
    parsed = urlparse(device_base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("DEVICE_BASE_URL must be a valid URL, e.g., http://192.168.1.10:8080")

    device_timeout_sec = float(_require_env("DEVICE_TIMEOUT_SEC"))
    retry_max = int(_require_env("RETRY_MAX"))
    retry_backoff_sec = float(_require_env("RETRY_BACKOFF_SEC"))
    stream_interval_sec = float(_require_env("STREAM_INTERVAL_SEC"))

    device_username = os.environ.get("DEVICE_USERNAME")
    device_password = os.environ.get("DEVICE_PASSWORD")
    if (device_username and not device_password) or (device_password and not device_username):
        raise ValueError("If using device auth, both DEVICE_USERNAME and DEVICE_PASSWORD must be set")

    return Config(
        http_host=http_host,
        http_port=http_port,
        device_base_url=device_base_url,
        device_timeout_sec=device_timeout_sec,
        retry_max=retry_max,
        retry_backoff_sec=retry_backoff_sec,
        stream_interval_sec=stream_interval_sec,
        device_username=device_username,
        device_password=device_password,
    )
