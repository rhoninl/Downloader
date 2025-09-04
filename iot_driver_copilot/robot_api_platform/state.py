# state.py
import json
import threading
import time
from typing import Any, Dict, Optional

class DriverState:
    def __init__(self):
        self._lock = threading.Lock()
        self._start_time = time.time()
        self._connected = False
        self._last_check_ts = None  # epoch seconds
        self._last_check_ok_http = False
        self._last_http_status = None
        self._last_rtt_ms = 0.0
        self._total_checks = 0
        self._total_commands = 0
        self._command_failures = 0
        self._last_error = None
        self._last_command = None  # dict
        self._last_command_result = None  # dict
        self._current_motion = {"direction": "stopped", "linear_velocity": 0.0, "since": None}

    def update_connectivity(self, ok_tcp: bool, rtt_ms: float, ok_http: bool, http_status: Optional[int], error: Optional[str]):
        with self._lock:
            self._connected = ok_tcp
            self._last_rtt_ms = rtt_ms
            self._last_check_ts = time.time()
            self._last_check_ok_http = ok_http
            self._last_http_status = http_status
            self._total_checks += 1
            self._last_error = error if not ok_tcp else None

    def record_command(self, name: str, params: Dict[str, Any]):
        with self._lock:
            self._total_commands += 1
            self._last_command = {
                "name": name,
                "params": params,
                "ts": time.time(),
            }

    def record_command_result(self, status: int, body_snippet: str, rtt_ms: float, ok: bool):
        with self._lock:
            if not ok:
                self._command_failures += 1
            self._last_command_result = {
                "status": status,
                "body_snippet": body_snippet,
                "rtt_ms": rtt_ms,
                "ok": ok,
                "ts": time.time(),
            }

    def set_motion(self, direction: str, linear_velocity: float):
        with self._lock:
            self._current_motion = {
                "direction": direction,
                "linear_velocity": linear_velocity,
                "since": time.time(),
            }

    def stop_motion(self):
        with self._lock:
            self._current_motion = {
                "direction": "stopped",
                "linear_velocity": 0.0,
                "since": time.time(),
            }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            uptime = time.time() - self._start_time
            return {
                "uptime_sec": uptime,
                "connected_tcp": self._connected,
                "last_check_ts": self._last_check_ts,
                "last_http_ok": self._last_check_ok_http,
                "last_http_status": self._last_http_status,
                "last_rtt_ms": self._last_rtt_ms,
                "total_checks": self._total_checks,
                "total_commands": self._total_commands,
                "command_failures": self._command_failures,
                "last_error": self._last_error,
                "last_command": self._last_command,
                "last_command_result": self._last_command_result,
                "motion": self._current_motion,
            }

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.snapshot()).encode('utf-8')
