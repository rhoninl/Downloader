import os


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if val is None or val == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _optional_int(name: str):
    val = os.getenv(name)
    if val is None or val == "":
        return None
    return int(val)


def _optional_float(name: str):
    val = os.getenv(name)
    if val is None or val == "":
        return None
    return float(val)


def _optional_str(name: str):
    val = os.getenv(name)
    if val is None or val == "":
        return None
    return val

# HTTP server configuration
HTTP_HOST = _require_env("HTTP_HOST")
HTTP_PORT = int(_require_env("HTTP_PORT"))

# Modbus RTU configuration
SERIAL_PORT = _require_env("SERIAL_PORT")  # e.g., /dev/ttyUSB0 or COM3
MODBUS_BAUD = int(_require_env("MODBUS_BAUD"))
MODBUS_ADDRESS = int(_require_env("MODBUS_ADDRESS"))
MODBUS_STOPBITS = int(_require_env("MODBUS_STOPBITS"))  # e.g., 1
MODBUS_BYTESIZE = int(_require_env("MODBUS_BYTESIZE"))  # e.g., 8
MODBUS_PARITY = _require_env("MODBUS_PARITY")  # 'N', 'E', or 'O'
MODBUS_TIMEOUT_S = float(_require_env("MODBUS_TIMEOUT_S"))  # seconds

# Collection loop
SAMPLE_INTERVAL_MS = int(_require_env("SAMPLE_INTERVAL_MS"))
RETRY_MAX_ATTEMPTS = int(_require_env("RETRY_MAX_ATTEMPTS"))
RETRY_BASE_DELAY_MS = int(_require_env("RETRY_BASE_DELAY_MS"))
RETRY_MAX_DELAY_MS = int(_require_env("RETRY_MAX_DELAY_MS"))

# Register map for measurements (addresses are device-specific)
AMMONIA_REG = _optional_int("AMMONIA_REG")
PH_REG = _optional_int("PH_REG")
TEMP_REG = _optional_int("TEMP_REG")

AMMONIA_FUNC = _optional_str("AMMONIA_FUNC")  # 'input' or 'holding'
PH_FUNC = _optional_str("PH_FUNC")
TEMP_FUNC = _optional_str("TEMP_FUNC")

AMMONIA_SCALE = _optional_float("AMMONIA_SCALE")
PH_SCALE = _optional_float("PH_SCALE")
TEMP_SCALE = _optional_float("TEMP_SCALE")

# Optional offsets/config registers
AMMONIA_OFFSET_REG = _optional_int("AMMONIA_OFFSET_REG")
PH_OFFSET_REG = _optional_int("PH_OFFSET_REG")

AMMONIA_OFFSET_FUNC = _optional_str("AMMONIA_OFFSET_FUNC")
PH_OFFSET_FUNC = _optional_str("PH_OFFSET_FUNC")

AMMONIA_OFFSET_SCALE = _optional_float("AMMONIA_OFFSET_SCALE")
PH_OFFSET_SCALE = _optional_float("PH_OFFSET_SCALE")
