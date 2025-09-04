# device_client.py
import socket
import threading
import time
import json
import queue
import random
from typing import Optional, Tuple, Dict, Any


class SubscriptionManager:
    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=100)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            self._subs.discard(q)

    def publish(self, item: Any):
        # Non-blocking publish, drop if subscriber is slow
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                # Drop the oldest item and try again to avoid buildup
                try:
                    q.get_nowait()
                    q.put_nowait(item)
                except Exception:
                    pass


class TelemetryState:
    def __init__(self):
        self.latest: Optional[Dict[str, Any]] = None
        self.latest_ts: Optional[float] = None
        self.sample_count: int = 0
        self.error_count: int = 0
        self.connected: bool = False
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()

    def update(self, sample: Dict[str, Any]):
        with self._lock:
            self.latest = sample
            self.latest_ts = time.time()
            self.sample_count += 1
            self.connected = True

    def set_connected(self, value: bool):
        with self._lock:
            self.connected = value

    def set_error(self, err: str):
        with self._lock:
            self.error_count += 1
            self.last_error = err
            self.connected = False

    def snapshot(self) -> Tuple[Optional[Dict[str, Any]], Optional[float], int, int, bool, Optional[str]]:
        with self._lock:
            return self.latest, self.latest_ts, self.sample_count, self.error_count, self.connected, self.last_error


class ControlState:
    def __init__(self):
        self.connected: bool = False
        self.cmd_count: int = 0
        self.error_count: int = 0
        self.last_error: Optional[str] = None
        self.last_cmd: Optional[str] = None
        self.last_cmd_ts: Optional[float] = None
        self._lock = threading.Lock()

    def set_connected(self, v: bool):
        with self._lock:
            self.connected = v

    def record_cmd(self, cmd: str):
        with self._lock:
            self.cmd_count += 1
            self.last_cmd = cmd
            self.last_cmd_ts = time.time()

    def record_error(self, err: str):
        with self._lock:
            self.error_count += 1
            self.last_error = err
            self.connected = False

    def snapshot(self):
        with self._lock:
            return {
                "connected": self.connected,
                "cmd_count": self.cmd_count,
                "error_count": self.error_count,
                "last_error": self.last_error,
                "last_cmd": self.last_cmd,
                "last_cmd_ts": self.last_cmd_ts,
            }


class RobotClient:
    def __init__(self, config, logger):
        self.cfg = config
        self.log = logger
        self._ctrl_sock: Optional[socket.socket] = None
        self._ctrl_lock = threading.Lock()
        self.ctrl_state = ControlState()

        self._stop_event = threading.Event()
        self.telemetry_state = TelemetryState()
        self._tel_thread: Optional[threading.Thread] = None
        self._tel_sock: Optional[socket.socket] = None
        self._tel_lock = threading.Lock()
        self._subs = SubscriptionManager()
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        self._tel_thread = threading.Thread(target=self._telemetry_loop, name="telemetry", daemon=True)
        self._tel_thread.start()
        self.log.info("RobotClient started telemetry loop")

    def stop(self):
        self._stop_event.set()
        # Close sockets to unblock loops
        with self._tel_lock:
            if self._tel_sock:
                try:
                    self._tel_sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self._tel_sock.close()
                except Exception:
                    pass
                self._tel_sock = None
        with self._ctrl_lock:
            if self._ctrl_sock:
                try:
                    self._ctrl_sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self._ctrl_sock.close()
                except Exception:
                    pass
                self._ctrl_sock = None
        if self._tel_thread and self._tel_thread.is_alive():
            self._tel_thread.join(timeout=5)
        self.log.info("RobotClient stopped")

    # Subscription for streaming telemetry
    def subscribe(self) -> queue.Queue:
        return self._subs.subscribe()

    def unsubscribe(self, q: queue.Queue):
        self._subs.unsubscribe(q)

    def latest_telemetry(self) -> Optional[Dict[str, Any]]:
        latest, _, _, _, _, _ = self.telemetry_state.snapshot()
        return latest

    def telemetry_snapshot(self):
        return self.telemetry_state.snapshot()

    def control_snapshot(self):
        return self.ctrl_state.snapshot()

    def _connect_control_locked(self):
        # Caller must hold _ctrl_lock
        if self._ctrl_sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.cfg.connect_timeout)
        self.log.info(f"Connecting control to {self.cfg.device_host}:{self.cfg.device_ctrl_port}")
        s.connect((self.cfg.device_host, self.cfg.device_ctrl_port))
        s.settimeout(self.cfg.read_timeout)
        self._ctrl_sock = s
        self.ctrl_state.set_connected(True)
        self.log.info("Control connection established")

    def _send_command(self, cmd: str) -> str:
        with self._ctrl_lock:
            try:
                if self._ctrl_sock is None:
                    self._connect_control_locked()
                assert self._ctrl_sock is not None
                data = (cmd + "\n").encode("utf-8")
                self._ctrl_sock.sendall(data)
                # Read a single-line response
                buf = bytearray()
                end = time.time() + self.cfg.read_timeout
                while time.time() < end:
                    try:
                        chunk = self._ctrl_sock.recv(1)
                        if not chunk:
                            raise ConnectionError("control socket closed")
                        if chunk == b"\n":
                            break
                        buf.extend(chunk)
                    except socket.timeout:
                        continue
                resp = buf.decode("utf-8", errors="replace").strip()
                self.ctrl_state.record_cmd(cmd)
                return resp or "OK"
            except Exception as e:
                self.ctrl_state.record_error(str(e))
                # Drop and close socket on error
                if self._ctrl_sock is not None:
                    try:
                        self._ctrl_sock.close()
                    except Exception:
                        pass
                self._ctrl_sock = None
                raise

    def move_forward(self, linear_velocity: float) -> str:
        if not isinstance(linear_velocity, (int, float)):
            raise ValueError("linear_velocity must be a number")
        cmd = f"MOVE FORWARD {linear_velocity:.3f}"
        return self._send_command(cmd)

    def move_backward(self, linear_velocity: float) -> str:
        if not isinstance(linear_velocity, (int, float)):
            raise ValueError("linear_velocity must be a number")
        cmd = f"MOVE BACKWARD {linear_velocity:.3f}"
        return self._send_command(cmd)

    def _telemetry_loop(self):
        # Connect to telemetry stream and continuously ingest JSON lines
        backoff = self.cfg.reconnect_delay_min
        while not self._stop_event.is_set():
            try:
                with self._tel_lock:
                    if self._tel_sock is not None:
                        try:
                            self._tel_sock.close()
                        except Exception:
                            pass
                        self._tel_sock = None
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(self.cfg.connect_timeout)
                    self.log.info(f"Connecting telemetry to {self.cfg.device_host}:{self.cfg.device_tel_port}")
                    s.connect((self.cfg.device_host, self.cfg.device_tel_port))
                    s.settimeout(self.cfg.read_timeout)
                    self._tel_sock = s
                self.telemetry_state.set_connected(True)
                self.log.info("Telemetry connection established")
                backoff = self.cfg.reconnect_delay_min

                buf = bytearray()
                while not self._stop_event.is_set():
                    try:
                        chunk = self._tel_sock.recv(1)
                        if not chunk:
                            raise ConnectionError("telemetry socket closed")
                        if chunk == b"\n":
                            line = buf.decode("utf-8", errors="replace").strip()
                            buf.clear()
                            if not line:
                                continue
                            try:
                                sample = json.loads(line)
                            except json.JSONDecodeError:
                                # attempt to parse a simple key=value format as fallback
                                sample = {"raw": line}
                            # Ensure timestamp
                            if isinstance(sample, dict) and "ts" not in sample:
                                sample["ts"] = time.time()
                            self.telemetry_state.update(sample)
                            # Publish to subscribers (non-blocking)
                            self._subs.publish(sample)
                        else:
                            buf.extend(chunk)
                    except socket.timeout:
                        # keep alive
                        continue
                # loop breaks due to stop event
            except Exception as e:
                err = str(e)
                self.telemetry_state.set_error(err)
                self.log.warn(f"Telemetry error: {err}")
                # Close socket and back off before retrying
                with self._tel_lock:
                    if self._tel_sock is not None:
                        try:
                            self._tel_sock.close()
                        except Exception:
                            pass
                        self._tel_sock = None
                if self._stop_event.is_set():
                    break
                # Exponential backoff with cap
                time.sleep(backoff)
                backoff = min(self.cfg.reconnect_delay_max, backoff * 2 * (0.5 + random.random()))
        self.log.info("Telemetry loop exiting")
