# data_store.py
import threading
import time
from typing import Any, Dict, Optional


class DataStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._latest_sample: Optional[Dict[str, Any]] = None
        self._last_update_ts: Optional[float] = None
        self._samples_received: int = 0
        self._errors: int = 0
        self._connected: bool = False
        self._connect_time: Optional[float] = None
        self._start_time: float = time.time()
        self._last_error: Optional[str] = None

    def set_connected(self, val: bool) -> None:
        with self._lock:
            if self._connected != val:
                self._connected = val
                self._connect_time = time.time() if val else None
                self._cond.notify_all()

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def set_error(self, msg: str) -> None:
        with self._lock:
            self._errors += 1
            self._last_error = msg

    def update_sample(self, sample: Dict[str, Any]) -> None:
        with self._lock:
            self._latest_sample = sample
            self._last_update_ts = time.time()
            self._samples_received += 1
            self._cond.notify_all()

    def get_snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return {
                'sample': self._latest_sample,
                'last_update_ts': self._last_update_ts,
                'samples_received': self._samples_received,
            } if self._latest_sample is not None else None

    def wait_for_update(self, timeout: float) -> bool:
        with self._lock:
            return self._cond.wait(timeout=timeout)

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'connected': self._connected,
                'uptime_s': time.time() - self._start_time,
                'connected_since_ts': self._connect_time,
                'last_update_ts': self._last_update_ts,
                'samples_received': self._samples_received,
                'errors': self._errors,
                'last_error': self._last_error,
            }
