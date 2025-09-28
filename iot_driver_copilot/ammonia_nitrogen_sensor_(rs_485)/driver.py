import os
import threading
import time
import logging
from typing import Dict, Optional, List

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

from pymodbus.client import ModbusSerialClient

from config import (
    HTTP_HOST,
    HTTP_PORT,
    SERIAL_PORT,
    MODBUS_BAUD,
    MODBUS_ADDRESS,
    MODBUS_STOPBITS,
    MODBUS_BYTESIZE,
    MODBUS_PARITY,
    MODBUS_TIMEOUT_S,
    SAMPLE_INTERVAL_MS,
    RETRY_MAX_ATTEMPTS,
    RETRY_BASE_DELAY_MS,
    RETRY_MAX_DELAY_MS,
    AMMONIA_REG,
    PH_REG,
    TEMP_REG,
    AMMONIA_OFFSET_REG,
    PH_OFFSET_REG,
    AMMONIA_FUNC,
    PH_FUNC,
    TEMP_FUNC,
    AMMONIA_OFFSET_FUNC,
    PH_OFFSET_FUNC,
    AMMONIA_SCALE,
    PH_SCALE,
    TEMP_SCALE,
    AMMONIA_OFFSET_SCALE,
    PH_OFFSET_SCALE,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("ammonia_nitrogen_driver")

app = FastAPI()

ALLOWED_FIELDS = {
    "ammonia_ppm",
    "ph",
    "temperature_c",
    "ammonia_offset",
    "ph_offset",
    "device_address",
    "baud_rate",
}

class LatestBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: Dict[str, float] = {}
        self._timestamp: Optional[float] = None

    def update(self, data: Dict[str, float]):
        with self._lock:
            self._data = data
            self._timestamp = time.time()

    def read(self) -> Optional[Dict[str, float]]:
        with self._lock:
            if not self._data:
                return None
            # Return a shallow copy to avoid race conditions
            return dict(self._data)

    def last_update(self) -> Optional[float]:
        with self._lock:
            return self._timestamp

latest_buffer = LatestBuffer()
stop_event = threading.Event()


def _read_register(client: ModbusSerialClient, unit: int, address: int, func: str) -> Optional[int]:
    try:
        if func.lower() == "input":
            rr = client.read_input_registers(address=address, count=1, unit=unit)
        elif func.lower() == "holding":
            rr = client.read_holding_registers(address=address, count=1, unit=unit)
        else:
            logger.error(f"Unsupported function type: {func}")
            return None
        if rr is None or hasattr(rr, 'isError') and rr.isError():
            logger.error(f"Modbus read error at address {address} func {func}")
            return None
        if not hasattr(rr, 'registers') or rr.registers is None or len(rr.registers) < 1:
            logger.error(f"No registers returned for address {address}")
            return None
        return rr.registers[0]
    except Exception as e:
        logger.error(f"Exception reading register {address} func {func}: {e}")
        return None


def _collect_loop():
    backoff_attempt = 0
    attempts_left = RETRY_MAX_ATTEMPTS
    client = None

    while not stop_event.is_set():
        try:
            if client is None:
                client = ModbusSerialClient(
                    method='rtu',
                    port=SERIAL_PORT,
                    baudrate=MODBUS_BAUD,
                    stopbits=MODBUS_STOPBITS,
                    bytesize=MODBUS_BYTESIZE,
                    parity=MODBUS_PARITY,
                    timeout=MODBUS_TIMEOUT_S,
                )

            if not client.connect():
                logger.warning("Failed to connect to Modbus device. Retrying with backoff...")
                client.close()
                client = None
                raise ConnectionError("connect failed")

            # Read measurements
            sample: Dict[str, float] = {}

            # ammonia_ppm
            if AMMONIA_REG is not None and AMMONIA_FUNC is not None and AMMONIA_SCALE is not None:
                raw = _read_register(client, MODBUS_ADDRESS, AMMONIA_REG, AMMONIA_FUNC)
                if raw is not None:
                    sample["ammonia_ppm"] = raw * AMMONIA_SCALE

            # ph
            if PH_REG is not None and PH_FUNC is not None and PH_SCALE is not None:
                raw = _read_register(client, MODBUS_ADDRESS, PH_REG, PH_FUNC)
                if raw is not None:
                    sample["ph"] = raw * PH_SCALE

            # temperature_c
            if TEMP_REG is not None and TEMP_FUNC is not None and TEMP_SCALE is not None:
                raw = _read_register(client, MODBUS_ADDRESS, TEMP_REG, TEMP_FUNC)
                if raw is not None:
                    sample["temperature_c"] = raw * TEMP_SCALE

            # ammonia_offset
            if AMMONIA_OFFSET_REG is not None and AMMONIA_OFFSET_FUNC is not None and AMMONIA_OFFSET_SCALE is not None:
                raw = _read_register(client, MODBUS_ADDRESS, AMMONIA_OFFSET_REG, AMMONIA_OFFSET_FUNC)
                if raw is not None:
                    sample["ammonia_offset"] = raw * AMMONIA_OFFSET_SCALE

            # ph_offset
            if PH_OFFSET_REG is not None and PH_OFFSET_FUNC is not None and PH_OFFSET_SCALE is not None:
                raw = _read_register(client, MODBUS_ADDRESS, PH_OFFSET_REG, PH_OFFSET_FUNC)
                if raw is not None:
                    sample["ph_offset"] = raw * PH_OFFSET_SCALE

            if sample:
                latest_buffer.update(sample)
                logger.info(f"Collected sample; last update at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(latest_buffer.last_update() or time.time()))}")

            # Reset backoff after successful cycle
            backoff_attempt = 0
            attempts_left = RETRY_MAX_ATTEMPTS

            # Sleep until next sample
            stop_event.wait(SAMPLE_INTERVAL_MS / 1000.0)
        except Exception as e:
            logger.error(f"Collection error: {e}")
            attempts_left -= 1
            delay_ms = min(RETRY_BASE_DELAY_MS * (2 ** backoff_attempt), RETRY_MAX_DELAY_MS)
            backoff_attempt += 1
            logger.info(f"Retrying in {delay_ms} ms; attempts left: {attempts_left}")
            stop_event.wait(delay_ms / 1000.0)
            if attempts_left <= 0:
                logger.error("Max retry attempts reached. Will continue retrying with capped backoff.")
                attempts_left = RETRY_MAX_ATTEMPTS

    # Cleanup
    try:
        if client is not None:
            client.close()
            logger.info("Modbus client closed.")
    except Exception:
        pass


@app.on_event("startup")
def on_startup():
    logger.info("Starting collection loop...")
    t = threading.Thread(target=_collect_loop, name="collector", daemon=True)
    t.start()


@app.on_event("shutdown")
def on_shutdown():
    logger.info("Shutting down...")
    stop_event.set()


@app.get("/readings")
def get_readings(fields: Optional[str] = Query(default=None, description="Comma-separated fields to return")):
    data = latest_buffer.read()
    if data is None:
        raise HTTPException(status_code=503, detail="No data available yet")

    # Default fields
    if fields is None or fields.strip() == "":
        requested = ["ammonia_ppm", "ph", "temperature_c"]
    else:
        requested = [f.strip() for f in fields.split(",") if f.strip()]
        for f in requested:
            if f not in ALLOWED_FIELDS:
                raise HTTPException(status_code=400, detail=f"Invalid field: {f}")

    result: Dict[str, float] = {}
    for f in requested:
        if f in ("device_address", "baud_rate"):
            # Provide config-based fields
            if f == "device_address":
                result[f] = MODBUS_ADDRESS
            elif f == "baud_rate":
                result[f] = MODBUS_BAUD
        else:
            if f in data:
                result[f] = data[f]
            else:
                # Field requested but not available yet (e.g., optional offsets not configured)
                result[f] = None

    return JSONResponse(content=result)


@app.get("/ph")
def get_ph():
    data = latest_buffer.read()
    if data is None or "ph" not in data:
        raise HTTPException(status_code=503, detail="No pH data available yet")
    return JSONResponse(content={"ph": data["ph"]})


if __name__ == "__main__":
    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT)
