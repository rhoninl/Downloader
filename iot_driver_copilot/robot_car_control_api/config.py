import os


def _get_str(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.getenv(name, default)
    if required and (val is None or val == ""):
        raise ValueError(f"Missing required env var: {name}")
    return val


def _get_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v == "":
        return float(default)
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"Invalid float for {name}: {v}")


def _get_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return int(default)
    try:
        return int(v)
    except ValueError:
        raise ValueError(f"Invalid int for {name}: {v}")


class Config:
    def __init__(self) -> None:
        # Driver HTTP server
        self.http_host: str = _get_str("HTTP_HOST", "0.0.0.0") or "0.0.0.0"
        self.http_port: int = _get_int("HTTP_PORT", 8080)

        # Device connection (HTTP native protocol)
        self.device_base_url: str = _get_str("DEVICE_BASE_URL", required=True)  # e.g., http://192.168.1.50
        self.device_timeout_s: float = _get_float("DEVICE_TIMEOUT_SECONDS", 3.0)

        # Device API paths (joined with base URL)
        self.device_move_cmd_path: str = _get_str("DEVICE_MOVE_CMD_PATH", "/api/move") or "/api/move"
        self.device_stop_cmd_path: str | None = _get_str("DEVICE_STOP_CMD_PATH", None, required=False)
        self.device_status_path: str = _get_str("DEVICE_STATUS_PATH", "/api/status") or "/api/status"

        # Auth (choose token or basic; both may be empty)
        self.auth_token: str | None = _get_str("DEVICE_AUTH_TOKEN", None)
        self.basic_username: str | None = _get_str("DEVICE_BASIC_USERNAME", None)
        self.basic_password: str | None = _get_str("DEVICE_BASIC_PASSWORD", None)

        # Collection loop
        self.poll_interval_s: float = _get_float("POLL_INTERVAL_SECONDS", 1.0)
        self.backoff_initial_ms: int = _get_int("RETRY_BACKOFF_INITIAL_MS", 500)
        self.backoff_max_ms: int = _get_int("RETRY_BACKOFF_MAX_MS", 10000)

        # Driver motion defaults
        self.default_speed: float = _get_float("DEFAULT_SPEED", 0.2)  # positive m/s
        self.default_duration_ms: int = _get_int("DEFAULT_DURATION_MS", 0)  # 0 => continuous until /stop


CONFIG = None


def load_config() -> Config:
    global CONFIG
    if CONFIG is None:
        CONFIG = Config()
    return CONFIG
