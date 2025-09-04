# config.py
# Configuration management for the robot driver

import os
from dataclasses import dataclass
from typing import Optional


def _get_env_str(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    val = os.getenv(name, default)
    if required and (val is None or val.strip() == ""):
        raise ValueError(f"Missing required environment variable: {name}")
    return val


def _get_env_float(name: str, default: Optional[float] = None, required: bool = False) -> Optional[float]:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        if required and default is None:
            raise ValueError(f"Missing required environment variable: {name}")
        return default
    try:
        return float(val)
    except ValueError:
        raise ValueError(f"Invalid float for {name}: {val}")


def _get_env_int(name: str, default: Optional[int] = None, required: bool = False) -> Optional[int]:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        if required and default is None:
            raise ValueError(f"Missing required environment variable: {name}")
        return default
    try:
        return int(val)
    except ValueError:
        raise ValueError(f"Invalid int for {name}: {val}")


@dataclass
class Config:
    # HTTP server config
    http_host: str
    http_port: int

    # Device access config (HTTP/REST)
    robot_base_url: str
    robot_state_path: str
    robot_cmd_move_path: str

    robot_user: Optional[str]
    robot_pass: Optional[str]

    # Networking behavior
    robot_timeout_s: float
    robot_poll_interval_s: float
    robot_retry_backoff_s: float

    # Default control parameters
    default_linear_vel: float


def load_config() -> Config:
    return Config(
        http_host=_get_env_str("HTTP_HOST", "0.0.0.0"),
        http_port=_get_env_int("HTTP_PORT", 8000) or 8000,
        robot_base_url=_get_env_str("ROBOT_BASE_URL", required=True),
        robot_state_path=_get_env_str("ROBOT_STATE_PATH", "/state"),
        robot_cmd_move_path=_get_env_str("ROBOT_CMD_MOVE_PATH", "/cmd/move_velocity"),
        robot_user=_get_env_str("ROBOT_USER"),
        robot_pass=_get_env_str("ROBOT_PASS"),
        robot_timeout_s=_get_env_float("ROBOT_TIMEOUT_S", 5.0) or 5.0,
        robot_poll_interval_s=_get_env_float("ROBOT_POLL_INTERVAL_S", 0.5) or 0.5,
        robot_retry_backoff_s=_get_env_float("ROBOT_RETRY_BACKOFF_S", 2.0) or 2.0,
        default_linear_vel=_get_env_float("DEFAULT_LINEAR_VEL", 0.2) or 0.2,
    )
