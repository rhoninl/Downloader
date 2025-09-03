# telemetry.py
import threading
import time
from typing import Optional, Dict, Any


class Telemetry:
    def __init__(self):
        self._lock = threading.RLock()
        now = time.time()
        self.start_time = now
        self.last_update_ts = now
        self.connected = False
        self.device_reachable = None  # None unknown, True/False known
        self.last_command = None  # type: Optional[str]
        self.last_error = None  # type: Optional[str]
        self.commands_sent = 0
        self.commands_failed = 0
        # Motion state
        self.moving = False
        self.direction = None  # 'forward' or 'backward' or None
        self.speed = 0.0
        self.expected_stop_ts = None  # type: Optional[float]

    def record_command(self, name: str):
        with self._lock:
            self.commands_sent += 1
            self.last_command = name
            self.last_update_ts = time.time()

    def record_error(self, err_msg: str):
        with self._lock:
            self.commands_failed += 1
            self.last_error = err_msg
            self.last_update_ts = time.time()

    def set_connected(self, is_connected: bool):
        with self._lock:
            self.connected = is_connected
            self.last_update_ts = time.time()

    def set_device_reachable(self, reachable: Optional[bool]):
        with self._lock:
            self.device_reachable = reachable
            self.last_update_ts = time.time()

    def set_moving(self, direction: str, speed: float, duration: Optional[float]):
        with self._lock:
            self.moving = True
            self.direction = direction
            self.speed = float(speed)
            if duration is not None:
                self.expected_stop_ts = time.time() + float(duration)
            else:
                self.expected_stop_ts = None
            self.last_update_ts = time.time()

    def stop_moving(self):
        with self._lock:
            self.moving = False
            self.direction = None
            self.speed = 0.0
            self.expected_stop_ts = None
            self.last_update_ts = time.time()

    def tick(self):
        with self._lock:
            now = time.time()
            if self.moving and self.expected_stop_ts is not None and now >= self.expected_stop_ts:
                # Assume the device stopped after duration elapsed
                self.moving = False
                self.direction = None
                self.speed = 0.0
                self.expected_stop_ts = None
            # Update timestamp to reflect a heartbeat
            self.last_update_ts = now

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            return {
                'uptime_s': now - self.start_time,
                'server_time_s': now,
                'connected': self.connected,
                'device_reachable': self.device_reachable,
                'last_command': self.last_command,
                'last_error': self.last_error,
                'commands_sent': self.commands_sent,
                'commands_failed': self.commands_failed,
                'motion': {
                    'moving': self.moving,
                    'direction': self.direction,
                    'speed_mps': self.speed,
                    'expected_stop_time_s': self.expected_stop_ts,
                },
                'last_update_ts_s': self.last_update_ts,
            }
