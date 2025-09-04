# device_client.py
import socket
import threading
import time
import json
import random
from typing import Optional, Dict, Any


class DeviceClient:
    def __init__(
        self,
        host: str,
        port: int,
        connect_timeout: float = 5.0,
        read_timeout: float = 5.0,
        reconnect_backoff: float = 1.0,
        max_backoff: float = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.reconnect_backoff = reconnect_backoff
        self.max_backoff = max_backoff

        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="DeviceClientLoop", daemon=True)

        self._latest_sample: Optional[Dict[str, Any]] = None
        self._latest_lock = threading.Lock()

        self._connected = False
        self._connected_lock = threading.Lock()

        self._stats_lock = threading.Lock()
        self._samples_received = 0
        self._bytes_received = 0
        self._last_error: Optional[str] = None
        self._last_telemetry_ts: Optional[float] = None
        self._start_time = time.time()

    def start(self) -> None:
        self._stop_event.clear()
        if not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="DeviceClientLoop", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            with self._sock_lock:
                if self._sock:
                    try:
                        self._sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                    self._sock = None
        except Exception:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def is_connected(self) -> bool:
        with self._connected_lock:
            return self._connected

    def _set_connected(self, val: bool) -> None:
        with self._connected_lock:
            self._connected = val

    def get_latest(self) -> Optional[Dict[str, Any]]:
        with self._latest_lock:
            return dict(self._latest_sample) if self._latest_sample is not None else None

    def get_stats(self) -> Dict[str, Any]:
        with self._stats_lock:
            stats = {
                "samples_received": self._samples_received,
                "bytes_received": self._bytes_received,
                "last_error": self._last_error,
                "last_telemetry_ts": self._last_telemetry_ts,
                "uptime_sec": time.time() - self._start_time,
            }
        stats["connected"] = self.is_connected()
        return stats

    def send_move(self, direction: str, linear_velocity: float) -> bool:
        # direction: 'FORWARD' or 'BACKWARD'
        cmd = f"CMD MOVE {direction} v={linear_velocity:.3f}\n"
        data = cmd.encode("utf-8")
        with self._sock_lock:
            if not self._sock:
                self._set_last_error("not connected")
                return False
            try:
                self._sock.sendall(data)
                return True
            except Exception as e:
                self._set_last_error(f"send_move failed: {e}")
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
                self._set_connected(False)
                return False

    def _set_last_error(self, err: str) -> None:
        with self._stats_lock:
            self._last_error = err

    def _run(self) -> None:
        backoff = self.reconnect_backoff
        while not self._stop_event.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
                sock.settimeout(self.read_timeout)
                with self._sock_lock:
                    self._sock = sock
                self._set_connected(True)
                backoff = self.reconnect_backoff
                self._read_loop(sock)
            except Exception as e:
                self._set_last_error(str(e))
                self._set_connected(False)
                with self._sock_lock:
                    if self._sock:
                        try:
                            self._sock.close()
                        except Exception:
                            pass
                        self._sock = None
                if self._stop_event.is_set():
                    break
                sleep_time = min(self.max_backoff, backoff * (1.0 + random.random()))
                time.sleep(sleep_time)
                backoff = min(self.max_backoff, backoff * 2)

    def _read_loop(self, sock: socket.socket) -> None:
        # Use buffered file for line-based reading
        f = sock.makefile("r", encoding="utf-8", newline="\n")
        try:
            while not self._stop_event.is_set():
                line = f.readline()
                if not line:
                    raise ConnectionError("device closed connection")
                self._on_line(line)
        finally:
            try:
                f.close()
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    def _on_line(self, line: str) -> None:
        ts = time.time()
        b = len(line.encode("utf-8", errors="ignore"))
        with self._stats_lock:
            self._bytes_received += b
        line = line.strip()
        if not line:
            return
        sample: Optional[Dict[str, Any]] = None
        # Try parse as JSON first
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                sample = obj
        except Exception:
            pass
        if sample is None:
            # Try parse key=value pairs separated by commas or spaces
            parts = [p for seg in line.split(",") for p in seg.strip().split()]
            kv: Dict[str, Any] = {}
            for part in parts:
                if "=" in part:
                    k, v = part.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    # Try convert to number
                    try:
                        if v.lower() in ("true", "false"):
                            val: Any = v.lower() == "true"
                        elif "." in v or "e" in v.lower():
                            val = float(v)
                        else:
                            val = int(v)
                    except Exception:
                        val = v
                    kv[k] = val
            if kv:
                sample = kv
        if sample is None:
            # Last resort wrap raw line
            sample = {"raw": line}
        sample["ts"] = ts
        with self._latest_lock:
            self._latest_sample = sample
        with self._stats_lock:
            self._samples_received += 1
            self._last_telemetry_ts = ts
