# device_client.py
import json
import logging
import socket
import threading
import time
from typing import Optional, Tuple, Any, Dict


class DeviceClient:
    """
    Device client that talks to the robot car using a simple TCP line protocol.
    Outbound commands (ASCII lines):
      - MOVE FORWARD <velocity>\n
      - MOVE BACKWARD <velocity>\n
    Inbound telemetry (JSON Lines): one JSON object per line, e.g.
      {"ts": 1712345678.123, "speed": 0.50, "battery": 87}
    """

    def __init__(self,
                 host: str,
                 port: int,
                 connect_timeout_sec: float,
                 read_timeout_sec: float,
                 reconnect_initial_delay_sec: float,
                 reconnect_max_delay_sec: float):
        self.host = host
        self.port = port
        self.connect_timeout_sec = connect_timeout_sec
        self.read_timeout_sec = read_timeout_sec
        self.reconnect_initial_delay_sec = reconnect_initial_delay_sec
        self.reconnect_max_delay_sec = reconnect_max_delay_sec

        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._connected = False
        self._last_error: Optional[str] = None

        self._latest_sample: Optional[Dict[str, Any]] = None
        self._latest_sample_ts: Optional[float] = None
        self._sample_seq: int = 0

        self._cond = threading.Condition()

        self._total_samples = 0
        self._reconnects = 0
        self._start_time = time.time()

        self._logger = logging.getLogger("DeviceClient")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="device-client", daemon=True)
        self._thread.start()
        self._logger.info("Device client started background thread")

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
        if self._thread:
            self._thread.join(timeout=5)
        with self._cond:
            self._cond.notify_all()
        self._logger.info("Device client stopped")

    def is_connected(self) -> bool:
        return self._connected

    def uptime(self) -> float:
        return time.time() - self._start_time

    def get_status(self) -> Dict[str, Any]:
        return {
            "connected": self._connected,
            "host": self.host,
            "port": self.port,
            "uptime_sec": round(self.uptime(), 3),
            "last_error": self._last_error,
            "last_sample_ts": self._latest_sample_ts,
            "total_samples": self._total_samples,
            "reconnects": self._reconnects,
        }

    def get_latest_sample(self) -> Tuple[Optional[Dict[str, Any]], Optional[float], int]:
        # Returns (sample, ts, seq)
        return self._latest_sample, self._latest_sample_ts, self._sample_seq

    def send_move(self, direction: str, velocity: float) -> None:
        if direction not in ("FORWARD", "BACKWARD"):
            raise ValueError("Invalid direction")
        line = f"MOVE {direction} {velocity:.3f}\n".encode("utf-8")
        with self._sock_lock:
            if not self._sock or not self._connected:
                raise ConnectionError("Device not connected")
            try:
                self._sock.sendall(line)
                self._logger.info("Sent command: %s", line.decode().strip())
            except Exception as e:
                self._last_error = str(e)
                self._logger.error("Failed to send command: %s", e)
                raise

    def _run(self) -> None:
        backoff = self.reconnect_initial_delay_sec
        while not self._stop_event.is_set():
            try:
                self._connect()
                self._read_loop()
            except Exception as e:
                self._last_error = str(e)
                self._logger.warning("Device connection/read error: %s", e)
            finally:
                self._set_connected(False)
                self._close_sock()

            if self._stop_event.is_set():
                break

            # Backoff before reconnect
            self._reconnects += 1
            delay = min(backoff, self.reconnect_max_delay_sec)
            self._logger.info("Reconnecting in %.2f sec (attempt %d)", delay, self._reconnects)
            self._stop_event.wait(delay)
            backoff = min(backoff * 2.0, self.reconnect_max_delay_sec)

    def _connect(self) -> None:
        self._logger.info("Connecting to device %s:%d", self.host, self.port)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.connect_timeout_sec)
        s.connect((self.host, self.port))
        s.settimeout(self.read_timeout_sec)
        with self._sock_lock:
            self._sock = s
        self._set_connected(True)
        self._logger.info("Connected to device")

        # Optional greeting or sync could be added here if protocol requires

    def _read_loop(self) -> None:
        buf = bytearray()
        last_data_time = time.time()
        while not self._stop_event.is_set():
            with self._sock_lock:
                s = self._sock
            if not s:
                raise ConnectionError("Socket closed")
            try:
                data = s.recv(4096)
                if not data:
                    raise ConnectionError("Device closed the connection")
                buf.extend(data)
                last_data_time = time.time()

                while True:
                    nl = buf.find(b"\n")
                    if nl == -1:
                        break
                    line = buf[:nl]
                    del buf[:nl + 1]
                    line = line.strip()
                    if not line:
                        continue
                    self._handle_line(line)

            except socket.timeout:
                # No data within read timeout; we can send a keepalive or just continue
                now = time.time()
                # If no data for an extended period, consider it a fault to trigger reconnect
                if now - last_data_time > max(self.read_timeout_sec * 3.0, 10.0):
                    raise TimeoutError("No telemetry received for extended period")
                continue
            except Exception:
                raise

    def _handle_line(self, line: bytes) -> None:
        try:
            text = line.decode("utf-8", errors="replace")
            sample = json.loads(text)
        except json.JSONDecodeError:
            # Non-JSON line; log and ignore
            self._logger.debug("Ignoring non-JSON line: %s", line[:200])
            return
        ts = sample.get("ts", time.time())
        with self._cond:
            self._latest_sample = sample
            self._latest_sample_ts = float(ts)
            self._total_samples += 1
            self._sample_seq += 1
            self._cond.notify_all()
        self._logger.debug("Ingested sample #%d", self._sample_seq)

    def _set_connected(self, value: bool) -> None:
        self._connected = value
        with self._cond:
            self._cond.notify_all()

    def _close_sock(self) -> None:
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
