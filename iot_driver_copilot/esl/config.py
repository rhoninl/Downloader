import os
import sys
from dataclasses import dataclass


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if val is None or val == "":
        sys.stderr.write(f"Missing required environment variable: {name}\n")
        sys.exit(1)
    return val


def _require_int(name: str) -> int:
    v = _require_env(name)
    try:
        return int(v)
    except ValueError:
        sys.stderr.write(f"Environment variable {name} must be an integer, got: {v}\n")
        sys.exit(1)


def _require_float(name: str) -> float:
    v = _require_env(name)
    try:
        return float(v)
    except ValueError:
        sys.stderr.write(f"Environment variable {name} must be a number, got: {v}\n")
        sys.exit(1)


@dataclass(frozen=True)
class Config:
    http_host: str
    http_port: int
    device_base_url: str
    device_username: str
    device_password: str
    request_timeout_seconds: float
    backoff_initial_seconds: float
    backoff_max_seconds: float


def load_config() -> Config:
    return Config(
        http_host=_require_env("HTTP_HOST"),
        http_port=_require_int("HTTP_PORT"),
        device_base_url=_require_env("DEVICE_BASE_URL"),
        device_username=_require_env("DEVICE_USERNAME"),
        device_password=_require_env("DEVICE_PASSWORD"),
        request_timeout_seconds=_require_float("REQUEST_TIMEOUT_SECONDS"),
        backoff_initial_seconds=_require_float("BACKOFF_INITIAL_SECONDS"),
        backoff_max_seconds=_require_float("BACKOFF_MAX_SECONDS"),
    )
