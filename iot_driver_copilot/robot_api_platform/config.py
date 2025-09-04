import os
from dataclasses import dataclass
from typing import Optional


class ConfigError(Exception):
    pass


def _get_required_env(name: str) -> str:
    val = os.environ.get(name)
    if val is None or val == "":
        raise ConfigError(f"Missing required environment variable: {name}")
    return val


def _get_optional_env(name: str) -> Optional[str]:
    val = os.environ.get(name)
    if val is None or val == "":
        return None
    return val


@dataclass
class Config:
    http_host: str
    http_port: int
    device_status_url: str
    device_move_velocity_url: str
    request_timeout: float
    poll_interval: float
    backoff_min: float
    backoff_max: float
    auth_token: Optional[str]
    username: Optional[str]
    password: Optional[str]


def load_config() -> Config:
    http_host = _get_required_env("HTTP_HOST")
    http_port = int(_get_required_env("HTTP_PORT"))
    device_status_url = _get_required_env("DEVICE_STATUS_URL")
    device_move_velocity_url = _get_required_env("DEVICE_MOVE_VELOCITY_URL")
    request_timeout = float(_get_required_env("REQUEST_TIMEOUT_SECONDS"))
    poll_interval = float(_get_required_env("POLL_INTERVAL_SECONDS"))
    backoff_min = float(_get_required_env("RETRY_BACKOFF_MIN_SECONDS"))
    backoff_max = float(_get_required_env("RETRY_BACKOFF_MAX_SECONDS"))

    auth_token = _get_optional_env("DEVICE_AUTH_TOKEN")
    username = _get_optional_env("DEVICE_USERNAME")
    password = _get_optional_env("DEVICE_PASSWORD")

    return Config(
        http_host=http_host,
        http_port=http_port,
        device_status_url=device_status_url,
        device_move_velocity_url=device_move_velocity_url,
        request_timeout=request_timeout,
        poll_interval=poll_interval,
        backoff_min=backoff_min,
        backoff_max=backoff_max,
        auth_token=auth_token,
        username=username,
        password=password,
    )
