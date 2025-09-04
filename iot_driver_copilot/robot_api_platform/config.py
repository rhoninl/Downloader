import os
import sys
from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    http_host: str
    http_port: int

    device_base_url: str
    device_telemetry_path: str
    device_velocity_cmd_path: str

    telemetry_url: str
    velocity_cmd_url: str

    default_linear_velocity: Optional[float]

    http_timeout_sec: float
    poll_interval_sec: float
    retry_initial_backoff_sec: float
    retry_max_backoff_sec: float

    log_level: str

    device_auth_token: Optional[str]
    device_username: Optional[str]
    device_password: Optional[str]


def _getenv_required(key: str) -> str:
    val = os.getenv(key)
    if val is None or val == "":
        sys.stderr.write(f"Missing required environment variable: {key}\n")
        sys.exit(2)
    return val


def _getenv_optional(key: str) -> Optional[str]:
    val = os.getenv(key)
    if val is None or val == "":
        return None
    return val


def _to_int(name: str, val: str) -> int:
    try:
        return int(val)
    except Exception:
        sys.stderr.write(f"Invalid int for {name}: {val}\n")
        sys.exit(2)


def _to_float(name: str, val: str) -> float:
    try:
        return float(val)
    except Exception:
        sys.stderr.write(f"Invalid float for {name}: {val}\n")
        sys.exit(2)


def _to_optional_float(name: str, val: Optional[str]) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        sys.stderr.write(f"Invalid float for {name}: {val}\n")
        sys.exit(2)


def load_config() -> Config:
    http_host = _getenv_required("HTTP_HOST")
    http_port = _to_int("HTTP_PORT", _getenv_required("HTTP_PORT"))

    device_base_url = _getenv_required("DEVICE_BASE_URL").rstrip("/")
    device_telemetry_path = _getenv_required("DEVICE_TELEMETRY_PATH")
    if not device_telemetry_path.startswith("/"):
        device_telemetry_path = "/" + device_telemetry_path
    device_velocity_cmd_path = _getenv_required("DEVICE_VELOCITY_CMD_PATH")
    if not device_velocity_cmd_path.startswith("/"):
        device_velocity_cmd_path = "/" + device_velocity_cmd_path

    telemetry_url = f"{device_base_url}{device_telemetry_path}"
    velocity_cmd_url = f"{device_base_url}{device_velocity_cmd_path}"

    default_linear_velocity = _to_optional_float(
        "DEFAULT_LINEAR_VELOCITY", _getenv_optional("DEFAULT_LINEAR_VELOCITY")
    )

    http_timeout_sec = _to_float("HTTP_TIMEOUT_SEC", _getenv_required("HTTP_TIMEOUT_SEC"))
    poll_interval_sec = _to_float("POLL_INTERVAL_SEC", _getenv_required("POLL_INTERVAL_SEC"))
    retry_initial_backoff_sec = _to_float(
        "RETRY_INITIAL_BACKOFF_SEC", _getenv_required("RETRY_INITIAL_BACKOFF_SEC")
    )
    retry_max_backoff_sec = _to_float(
        "RETRY_MAX_BACKOFF_SEC", _getenv_required("RETRY_MAX_BACKOFF_SEC")
    )

    log_level = _getenv_required("LOG_LEVEL").upper()

    device_auth_token = _getenv_optional("DEVICE_AUTH_TOKEN")
    device_username = _getenv_optional("DEVICE_USERNAME")
    device_password = _getenv_optional("DEVICE_PASSWORD")

    return Config(
        http_host=http_host,
        http_port=http_port,
        device_base_url=device_base_url,
        device_telemetry_path=device_telemetry_path,
        device_velocity_cmd_path=device_velocity_cmd_path,
        telemetry_url=telemetry_url,
        velocity_cmd_url=velocity_cmd_url,
        default_linear_velocity=default_linear_velocity,
        http_timeout_sec=http_timeout_sec,
        poll_interval_sec=poll_interval_sec,
        retry_initial_backoff_sec=retry_initial_backoff_sec,
        retry_max_backoff_sec=retry_max_backoff_sec,
        log_level=log_level,
        device_auth_token=device_auth_token,
        device_username=device_username,
        device_password=device_password,
    )
