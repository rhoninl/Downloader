# device_client.py
import socket
import time
import json
import threading
import select
import queue
import logging
from typing import Optional, List, Tuple


class DeviceClient:
    def __init__(self,
                 host: str,
                 port: int,
                 connect_timeout_ms: int,
                 read_timeout_ms: int,
                 backoff_min_ms: int,
                 backoff_max_ms: int):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout_ms / 1000.0
        self.read_timeout = read_timeout_ms / 1000.0
        self.backoff_min = backoff_min_ms / 1000.0
        self.backoff_max = backoff_max_ms / 1000.0

        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Outgoing command queue
        self._out_q: "queue.Queue[bytes]" = queue.Queue(maxsize=256)

        # Telemetry latest data
        self._latest_lock = threading.Lock()
        self._latest_json: Optional[dict] = None
        self._last_update_ts: Optional[float] = None

        # Subscribers for streaming telemetry (one queue per client)
        self._subs_lock = threading.Lock()
        self._subs: List[Tuple["queue.Queue[str]", float]] = []  # (queue, created_time)

        # Metrics
        self._start_time = time.time()
        self._connected = False
        self._recv_count = 0
        self._reconnects = 0
        self._bytes_in = 0
        self._bytes_out = 0
        self._last_error: Optional[str] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run_loop, name="DeviceClientLoop", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        with self._sock_lock:
            try:
                if self._sock:
                    self._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                if self._sock:
                    self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._thread:
            self._thread.join(timeout=5)

    # Public API
    def ensure_connect(self) -> bool:
        return self._connected

    def send_command(self, cmd: str) -> bool:
        data = (cmd.rstrip("\n") + "\n").encode("utf-8")
        try:
            self._out_q.put_nowait(data)
            return True
        except queue.Full:
            logging.warning("Command queue full, dropping command: %s", cmd)
            return False

    def subscribe(self, max_buffer: int = 256) -> "queue.Queue[str]":
        q: "queue.Queue[str]" = queue.Queue(maxsize=max_buffer)
        with self._subs_lock:
            self._subs.append((q, time.time()))
        return q

    def unsubscribe(self, q: "queue.Queue[str]"):
        with self._subs_lock:
            self._subs = [(qq, t0) for (qq, t0) in self._subs if qq is not q]

    # Status helpers
    def status_snapshot(self) -> dict:
        with self._latest_lock:
            latest = self._latest_json.copy() if isinstance(self._latest_json, dict) else None
            last_update_ts = self._last_update_ts
        return {
            "connected": self._connected,
            "uptime_sec": time.time() - self._start_time,
            "last_update_ts": last_update_ts,
            "recv_count": self._recv_count,
            "reconnects": self._reconnects,
            "bytes_in": self._bytes_in,
            "bytes_out": self._bytes_out,
            "last_error": self._last_error,
            "device": {
                "host": self.host,
                "port": self.port,
            },
            "latest_available": latest is not None,
        }

    def latest_json(self) -> Optional[dict]:
        with self._latest_lock:
            return self._latest_json.copy() if isinstance(self._latest_json, dict) else None

    # Internal helpers
    def _connect_sock(self) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.connect_timeout)
        try:
            s.connect((self.host, self.port))
            s.setblocking(False)
            with self._sock_lock:
                if self._sock:
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                self._sock = s
            self._connected = True
            logging.info("Connected to device %s:%d", self.host, self.port)
            return True
        except Exception as e:
            self._last_error = f"connect: {e}"
            logging.warning("Connect failed: %s", e)
            try:
                s.close()
            except Exception:
                pass
            return False

    def _close_sock(self):
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
        if self._connected:
            logging.info("Disconnected from device")
        self._connected = False

    def _broadcast(self, line: str):
        # Deliver to all subscribers, dropping oldest if their queue is full
        with self._subs_lock:
            subs_copy = list(self._subs)
        for (q, _t0) in subs_copy:
            try:
                q.put_nowait(line)
            except queue.Full:
                try:
                    _ = q.get_nowait()  # drop oldest
                except Exception:
                    pass
                try:
                    q.put_nowait(line)
                except Exception:
                    pass

    def _send_pending(self, writable: bool):
        if not writable:
            return
        with self._sock_lock:
            s = self._sock
        if s is None:
            return
        # Try to send up to N queued messages per writable event
        for _ in range(32):
            try:
                data = self._out_q.get_nowait()
            except queue.Empty:
                return
            try:
                sent = s.send(data)
                if sent < len(data):
                    # Not all sent: put back remaining
                    remaining = data[sent:]
                    self._bytes_out += sent
                    # Requeue remaining at front by creating a new queue (simple fallback)
                    # To avoid complexity, we'll try again next loop by placing at head
                    tmp = queue.Queue(maxsize=self._out_q.maxsize)
                    tmp.put_nowait(remaining)
                    while True:
                        try:
                            tmp.put_nowait(self._out_q.get_nowait())
                        except queue.Empty:
                            break
                    self._out_q = tmp
                    return
                else:
                    self._bytes_out += sent
            except BlockingIOError:
                # Can't send now; put back and exit
                try:
                    # Put back at head: rebuild queue
                    tmp = queue.Queue(maxsize=self._out_q.maxsize)
                    tmp.put_nowait(data)
                    while True:
                        try:
                            tmp.put_nowait(self._out_q.get_nowait())
                        except queue.Empty:
                            break
                    self._out_q = tmp
                except Exception:
                    pass
                return
            except Exception as e:
                self._last_error = f"send: {e}"
                logging.error("Send error: %s", e)
                # Drop this data and break; socket will likely be closed on next loop
                return

    def _run_loop(self):
        backoff = self.backoff_min
        buf = bytearray()
        while not self._stop_evt.is_set():
            if not self._connected:
                if not self._connect_sock():
                    self._reconnects += 1
                    time.sleep(backoff)
                    backoff = min(self.backoff_max, max(self.backoff_min, backoff * 1.5))
                    continue
                else:
                    backoff = self.backoff_min
            # Connected: select for readability/writability
            with self._sock_lock:
                s = self._sock
            if s is None:
                self._connected = False
                continue
            try:
                rlist, wlist, xlist = select.select([s], [s] if not self._out_q.empty() else [], [s], self.read_timeout)
            except Exception as e:
                self._last_error = f"select: {e}"
                logging.error("Select error: %s", e)
                self._close_sock()
                continue

            if xlist:
                self._last_error = "exceptional socket state"
                logging.error("Exceptional socket state")
                self._close_sock()
                continue

            try:
                self._send_pending(writable=len(wlist) > 0)
            except Exception as e:
                logging.error("_send_pending error: %s", e)

            if rlist:
                try:
                    chunk = s.recv(4096)
                    if not chunk:
                        # Connection closed by peer
                        logging.warning("Device closed connection")
                        self._close_sock()
                        continue
                    self._bytes_in += len(chunk)
                    buf.extend(chunk)
                    # Process lines
                    while True:
                        nl = buf.find(b"\n")
                        if nl == -1:
                            break
                        line = buf[:nl]
                        del buf[:nl + 1]
                        line_str = line.decode("utf-8", errors="ignore").strip()
                        if not line_str:
                            continue
                        # Parse as JSON if possible; otherwise wrap as text
                        parsed = None
                        try:
                            parsed = json.loads(line_str)
                        except Exception:
                            parsed = {"raw": line_str}
                        with self._latest_lock:
                            self._latest_json = parsed
                            self._last_update_ts = time.time()
                        self._recv_count += 1
                        # Broadcast NDJSON line (serialize parsed)
                        try:
                            out_line = json.dumps(parsed, separators=(",", ":"))
                        except Exception:
                            out_line = json.dumps({"raw": line_str}, separators=(",", ":"))
                        self._broadcast(out_line)
                except BlockingIOError:
                    pass
                except Exception as e:
                    self._last_error = f"recv: {e}"
                    logging.error("Receive error: %s", e)
                    self._close_sock()
                    continue

        # Cleanup
        self._close_sock()

    # High-level convenience commands (native protocol strings)
    def cmd_connect(self) -> bool:
        # Many devices require a connect/arm command; provide a simple textual protocol
        # We'll emit a CONNECT command to the device
        return self.send_command("CONNECT")

    def cmd_move_forward(self, speed: float, duration: Optional[float]) -> bool:
        if duration is None:
            return self.send_command(f"MOVE FORWARD {speed}")
        return self.send_command(f"MOVE FORWARD {speed} {duration}")

    def cmd_move_backward(self, speed: float, duration: Optional[float]) -> bool:
        if duration is None:
            return self.send_command(f"MOVE BACKWARD {speed}")
        return self.send_command(f"MOVE BACKWARD {speed} {duration}")

    def cmd_stop(self) -> bool:
        return self.send_command("STOP")
