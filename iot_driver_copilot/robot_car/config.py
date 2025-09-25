import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Config:
    http_host: str
    http_port: int

    device_base_url: str
    device_set_velocity_path: str
    device_telemetry_path: str

    request_timeout_secs: float
    poll_interval_secs: float
    backoff_min_secs: float
    backoff_max_secs: float

    max_speed_mps: float

    device_bearer_token: Optional[str]
    device_username: Optional[str]
    device_password: Optional[str]


class ConfigError(Exception):
    pass


def _require_env(name: str) -> str:
    v = os.getenv(name)
    if v is None or v == "":
        raise ConfigError(f"Missing required environment variable: {name}")
    return v


def _to_int(name: str, v: str) -> int:
    try:
        return int(v)
    except Exception:
        raise ConfigError(f"Invalid int for {name}: {v}")


def _to_float(name: str, v: str) -> float:
    try:
        return float(v)
    except Exception:
        raise ConfigError(f"Invalid float for {name}: {v}")


def _normalize_base_url(url: str) -> str:
    # Ensure no trailing slash
    return url[:-1] if url.endswith('/') else url


def _normalize_path(path: str) -> str:
    # Ensure leading slash and no trailing slash (server/device paths typically don't require trailing slash)
    if not path.startswith('/'):
        path = '/' + path
    if len(path) > 1 and path.endswith('/'):
        path = path[:-1]
    return path


def load_config() -> Config:
    http_host = _require_env("HTTP_HOST")
    http_port = _to_int("HTTP_PORT", _require_env("HTTP_PORT"))

    device_base_url = _normalize_base_url(_require_env("DEVICE_BASE_URL"))
    device_set_velocity_path = _normalize_path(_require_env("DEVICE_SET_VELOCITY_PATH"))
    device_telemetry_path = _normalize_path(_require_env("DEVICE_TELEMETRY_PATH"))

    request_timeout_secs = _to_float("REQUEST_TIMEOUT_SECS", _require_env("REQUEST_TIMEOUT_SECS"))
    poll_interval_secs = _to_float("POLL_INTERVAL_SECS", _require_env("POLL_INTERVAL_SECS"))
    backoff_min_secs = _to_float("BACKOFF_MIN_SECS", _require_env("BACKOFF_MIN_SECS"))
    backoff_max_secs = _to_float("BACKOFF_MAX_SECS", _require_env("BACKOFF_MAX_SECS"))

    max_speed_mps = _to_float("MAX_SPEED_MPS", _require_env("MAX_SPEED_MPS"))

    device_bearer_token = os.getenv("DEVICE_BEARER_TOKEN")
    device_username = os.getenv("DEVICE_USERNAME")
    device_password = os.getenv("DEVICE_PASSWORD")

    # If username provided, password must also be provided (and vice versa)
    if (device_username and not device_password) or (device_password and not device_username):
        raise ConfigError("DEVICE_USERNAME and DEVICE_PASSWORD must be both set or both unset")

    return Config(
        http_host=http_host,
        http_port=http_port,
        device_base_url=device_base_url,
        device_set_velocity_path=device_set_velocity_path,
        device_telemetry_path=device_telemetry_path,
        request_timeout_secs=request_timeout_secs,
        poll_interval_secs=poll_interval_secs,
        backoff_min_secs=backoff_min_secs,
        backoff_max_secs=backoff_max_secs,
        max_speed_mps=max_speed_mps,
        device_bearer_token=device_bearer_token,
        device_username=device_username,
        device_password=device_password,
    )
