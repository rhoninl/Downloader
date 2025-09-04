# device_client.py
import cv2
import threading
import time
from typing import Optional, Tuple, Dict, Any
from datetime import datetime, timezone


def _now_ts() -> float:
    return time.time()


def _iso_utc(ts: Optional[float] = None) -> str:
    if ts is None:
        ts = _now_ts()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class CameraClient:
    def __init__(
        self,
        device_id: str,
        frame_width: Optional[int] = None,
        frame_height: Optional[int] = None,
        frame_rate: Optional[float] = None,
        jpeg_quality: Optional[int] = None,
        reconnect_min_ms: int = 500,
        reconnect_max_ms: int = 5000,
        read_timeout_ms: int = 2000,
    ) -> None:
        self.device_id_raw = device_id
        try:
            # if it's an integer index string, use int
            if device_id.strip().isdigit():
                self.device_index_or_path = int(device_id.strip())
            else:
                self.device_index_or_path = device_id
        except Exception:
            self.device_index_or_path = device_id

        self.frame_width = frame_width
        self.frame_height = frame_height
        self.frame_rate = frame_rate
        self.jpeg_quality = jpeg_quality
        self.reconnect_min_ms = reconnect_min_ms
        self.reconnect_max_ms = reconnect_max_ms
        self.read_timeout_ms = read_timeout_ms

        # runtime state
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._running = False
        self._connected = False
        self._cap: Optional[cv2.VideoCapture] = None

        self._lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None
        self._latest_ts: Optional[float] = None

        self._frames = 0
        self._errors = 0
        self._started_at: Optional[float] = None
        self._last_connect_ts: Optional[float] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="CameraIngest", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._thread = None
        self._running = False
        self._connected = False
        with self._lock:
            # keep the last frame in memory even after stop; do not clear
            pass

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def has_frame(self) -> bool:
        with self._lock:
            return self._latest_jpeg is not None

    def get_latest_jpeg(self) -> Tuple[Optional[bytes], Optional[float]]:
        with self._lock:
            return self._latest_jpeg, self._latest_ts

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            latest_ts = self._latest_ts
        started_at = self._started_at
        uptime = None
        if started_at is not None:
            uptime = max(0.0, _now_ts() - started_at)
        fps = None
        if self._started_at and self._frames > 0:
            dur = max(1e-6, _now_ts() - self._started_at)
            fps = self._frames / dur
        return {
            "device_id": str(self.device_index_or_path),
            "connected": self._connected,
            "running": self.is_running(),
            "started_at": _iso_utc(started_at) if started_at else None,
            "uptime_sec": uptime,
            "last_frame_ts": _iso_utc(latest_ts) if latest_ts else None,
            "frames_received": self._frames,
            "approx_fps": fps,
            "errors": self._errors,
            "capture_settings": {
                "frame_width": self.frame_width,
                "frame_height": self.frame_height,
                "frame_rate": self.frame_rate,
                "jpeg_quality": self.jpeg_quality,
            },
        }

    def _configure_capture(self, cap: cv2.VideoCapture) -> None:
        # Apply settings if provided
        if self.frame_width is not None:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.frame_width))
        if self.frame_height is not None:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.frame_height))
        if self.frame_rate is not None and self.frame_rate > 0:
            cap.set(cv2.CAP_PROP_FPS, float(self.frame_rate))

    def _encode_jpeg(self, frame) -> Optional[bytes]:
        params = []
        if self.jpeg_quality is not None:
            params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.jpeg_quality)]
        ok, buf = cv2.imencode(".jpg", frame, params)
        if not ok:
            return None
        return bytes(buf)

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        try:
            cap = cv2.VideoCapture(self.device_index_or_path)
            if not cap or not cap.isOpened():
                return None
            self._configure_capture(cap)
            return cap
        except Exception:
            return None

    def _run(self) -> None:
        self._running = True
        self._started_at = _now_ts()
        backoff_ms = self.reconnect_min_ms
        print(f"[camera] ingest loop starting for device {self.device_index_or_path}")
        while not self._stop_evt.is_set():
            self._connected = False
            cap = self._open_capture()
            if cap is None or not cap.isOpened():
                self._errors += 1
                print(f"[camera] failed to open device {self.device_index_or_path}, retry in {backoff_ms}ms")
                self._sleep_ms(backoff_ms)
                backoff_ms = min(self.reconnect_max_ms, max(self.reconnect_min_ms, int(backoff_ms * 2)))
                continue

            self._cap = cap
            self._last_connect_ts = _now_ts()
            self._connected = True
            backoff_ms = self.reconnect_min_ms
            print(f"[camera] connected to device {self.device_index_or_path}")

            last_frame_time = _now_ts()
            while not self._stop_evt.is_set():
                try:
                    ok, frame = cap.read()
                except Exception:
                    ok = False
                    frame = None
                if not ok or frame is None:
                    self._errors += 1
                    print("[camera] read failed, will reconnect")
                    break

                now = _now_ts()
                # If the camera stalls, enforce a read timeout
                if (now - last_frame_time) * 1000.0 > self.read_timeout_ms:
                    self._errors += 1
                    print("[camera] read timeout, will reconnect")
                    break

                jpeg = self._encode_jpeg(frame)
                if jpeg is not None:
                    with self._lock:
                        self._latest_jpeg = jpeg
                        self._latest_ts = now
                    self._frames += 1
                    last_frame_time = now
                # No explicit sleep; rely on camera/frame_rate settings

            # Cleanup and try to reconnect
            try:
                if self._cap is not None:
                    self._cap.release()
            except Exception:
                pass
            self._cap = None
            self._connected = False
            if not self._stop_evt.is_set():
                print(f"[camera] disconnected, retry in {backoff_ms}ms")
                self._sleep_ms(backoff_ms)
                backoff_ms = min(self.reconnect_max_ms, max(self.reconnect_min_ms, int(backoff_ms * 2)))

        # Exiting
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
        self._cap = None
        self._connected = False
        self._running = False
        print("[camera] ingest loop stopped")

    def _sleep_ms(self, ms: int) -> None:
        time.sleep(max(0.0, ms / 1000.0))
