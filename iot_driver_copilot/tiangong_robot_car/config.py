import os
from dataclasses import dataclass


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if val is None or val == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _env_float(name: str) -> float:
    return float(_require_env(name))


def _env_int(name: str) -> int:
    return int(_require_env(name))


@dataclass(frozen=True)
class Config:
    # HTTP server
    http_host: str
    http_port: int

    # Device HTTP configuration
    device_base_url: str
    device_telemetry_path: str
    device_cmd_vel_path: str

    # Network and polling
    http_timeout_sec: float
    poll_interval_sec: float

    # Retry/backoff
    retry_base_delay_sec: float
    retry_max_delay_sec: float

    # Defaults
    default_speed: float

    @staticmethod
    def load() -> "Config":
        http_host = _require_env("HTTP_HOST")
        http_port = _env_int("HTTP_PORT")

        device_base_url = _require_env("DEVICE_BASE_URL").strip()
        device_telemetry_path = _require_env("DEVICE_TELEMETRY_PATH").strip()
        device_cmd_vel_path = _require_env("DEVICE_CMD_VEL_PATH").strip()

        http_timeout_sec = _env_float("HTTP_TIMEOUT_SEC")
        poll_interval_sec = _env_float("POLL_INTERVAL_SEC")

        retry_base_delay_sec = _env_float("RETRY_BASE_DELAY_SEC")
        retry_max_delay_sec = _env_float("RETRY_MAX_DELAY_SEC")

        default_speed = _env_float("DEFAULT_SPEED")

        return Config(
            http_host=http_host,
            http_port=http_port,
            device_base_url=device_base_url,
            device_telemetry_path=device_telemetry_path,
            device_cmd_vel_path=device_cmd_vel_path,
            http_timeout_sec=http_timeout_sec,
            poll_interval_sec=poll_interval_sec,
            retry_base_delay_sec=retry_base_delay_sec,
            retry_max_delay_sec=retry_max_delay_sec,
            default_speed=default_speed,
        )
