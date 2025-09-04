import logging
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

import cv2  # Requires opencv-python

from config import load_config, Config


class FrameBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._frame_bytes: Optional[bytes] = None
        self._timestamp: float = 0.0
        self._seq: int = 0

    def set_frame(self, data: bytes, ts: Optional[float] = None) -> None:
        with self._cond:
            self._frame_bytes = data
            self._timestamp = time.time() if ts is None else ts
            self._seq += 1
            self._cond.notify_all()

    def get_latest(self) -> Tuple[Optional[bytes], float, int]:
        with self._lock:
            return self._frame_bytes, self._timestamp, self._seq

    def wait_for_newer(self, last_seq: int, timeout: Optional[float]) -> Tuple[Optional[bytes], float, int]:
        with self._cond:
            if timeout is not None:
                end = time.monotonic() + timeout
            while self._seq <= last_seq or self._frame_bytes is None:
                remaining = None
                if timeout is not None:
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        break
                self._cond.wait(timeout=remaining)
            return self._frame_bytes, self._timestamp, self._seq


class CameraWorker(threading.Thread):
    daemon = True

    def __init__(self, config: Config, buffer: FrameBuffer, stop_event: threading.Event, logger: logging.Logger) -> None:
        super().__init__(name="camera-worker")
        self.config = config
        self.buffer = buffer
        self.stop_event = stop_event
        self.logger = logger
        self._connected = threading.Event()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def run(self) -> None:
        base_delay = max(1, self.config.reconnect_base_delay_ms) / 1000.0
        max_delay = max(base_delay, self.config.reconnect_max_delay_ms / 1000.0)
        backoff = base_delay

        while not self.stop_event.is_set():
            cap = self._open_capture()
            if cap is None or not cap.isOpened():
                self._connected.clear()
                self.logger.warning("Camera open failed. Retrying in %.2fs", backoff)
                self._sleep_with_stop(backoff)
                backoff = min(backoff * 2.0, max_delay)
                continue

            # Configure capture
            self._configure_capture(cap)

            # Initial frame within connect timeout
            if not self._grab_initial_frame(cap):
                try:
                    cap.release()
                except Exception:
                    pass
                self._connected.clear()
                self.logger.warning("Failed to receive initial frame within %.2fs. Retrying in %.2fs", self.config.connect_timeout_sec, backoff)
                self._sleep_with_stop(backoff)
                backoff = min(backoff * 2.0, max_delay)
                continue

            # We are connected
            self._connected.set()
            self.logger.info("Camera connected. Streaming frames...")
            backoff = base_delay

            last_success = time.monotonic()
            try:
                while not self.stop_event.is_set():
                    ok, frame = cap.read()
                    now = time.monotonic()
                    if not ok or frame is None:
                        # If we've exceeded read timeout without a good frame, force reconnect
                        if (now - last_success) > self.config.read_timeout_sec:
                            raise RuntimeError("Frame read timeout exceeded")
                        # brief sleep to avoid hot loop
                        time.sleep(0.005)
                        continue

                    # Encode to JPEG
                    try:
                        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.config.jpeg_quality)]
                        ret, buf = cv2.imencode('.jpg', frame, encode_params)
                        if not ret or buf is None:
                            self.logger.error("JPEG encode failed")
                            continue
                        self.buffer.set_frame(bytes(buf))
                        last_success = now
                    except Exception as e:
                        self.logger.exception("JPEG encoding error: %s", e)
                        # continue reading; if persistent, read timeout will trigger

            except Exception as e:
                self.logger.warning("Stream loop ended due to: %s", e)
            finally:
                try:
                    cap.release()
                except Exception:
                    pass
                self._connected.clear()
                self.logger.info("Camera disconnected. Will attempt reconnect in %.2fs", backoff)
                self._sleep_with_stop(backoff)
                backoff = min(backoff * 2.0, max_delay)

        self.logger.info("Camera worker stopped")

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        dev = self.config.camera_device.strip()
        api_preference = 0
        index: Optional[int] = None

        # Determine if device is numeric index or a /dev/videoX path
        if dev.isdigit():
            try:
                index = int(dev)
            except Exception:
                index = None
        elif dev.startswith('/dev/video'):
            # Try to parse trailing digits
            try:
                idx_str = ''.join(ch for ch in dev if ch.isdigit())
                if idx_str:
                    index = int(idx_str[-1])
            except Exception:
                index = None

        try:
            if index is not None:
                cap = cv2.VideoCapture(index, api_preference)
            else:
                cap = cv2.VideoCapture(dev, api_preference)
            return cap
        except Exception as e:
            self.logger.error("Exception opening camera %s: %s", dev, e)
            return None

    def _configure_capture(self, cap: cv2.VideoCapture) -> None:
        try:
            # Try set MJPG for better USB throughput
            fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            cap.set(cv2.CAP_PROP_FOURCC, fourcc)
        except Exception:
            pass
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.config.cap_width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.config.cap_height))
            cap.set(cv2.CAP_PROP_FPS, float(self.config.cap_fps))
        except Exception as e:
            self.logger.warning("Error setting capture properties: %s", e)

    def _grab_initial_frame(self, cap: cv2.VideoCapture) -> bool:
        start = time.monotonic()
        while not self.stop_event.is_set():
            ok, frame = cap.read()
            if ok and frame is not None:
                try:
                    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.config.jpeg_quality)]
                    ret, buf = cv2.imencode('.jpg', frame, encode_params)
                    if ret and buf is not None:
                        self.buffer.set_frame(bytes(buf))
                        return True
                except Exception as e:
                    self.logger.error("Initial JPEG encode failed: %s", e)

            if (time.monotonic() - start) >= self.config.connect_timeout_sec:
                return False
            time.sleep(0.01)
        return False

    def _sleep_with_stop(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while not self.stop_event.is_set() and time.monotonic() < end:
            time.sleep(0.05)


class MJPEGHandler(BaseHTTPRequestHandler):
    # Injected class-level references
    frame_buffer: FrameBuffer = None  # type: ignore
    stop_event: threading.Event = None  # type: ignore
    config: Config = None  # type: ignore
    logger: logging.Logger = None  # type: ignore
    worker: CameraWorker = None  # type: ignore

    server_version = "HikUSBHTTP/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # Route http.server logs into our logger
        try:
            self.logger.info("%s - - " + fmt, self.address_string(), *args)
        except Exception:
            pass

    def _send_plain(self, status: int, body: str) -> None:
        data = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def do_GET(self) -> None:
        if self.path != "/stream":
            self._send_plain(HTTPStatus.NOT_FOUND, "Not Found")
            return

        # Wait for first frame up to timeout
        start_wait = time.monotonic()
        last_seq = -1
        frame, ts, seq = self.frame_buffer.wait_for_newer(last_seq, timeout=self.config.stream_start_timeout_sec)
        if frame is None:
            msg = "Camera not connected or no frames available. Try again later."
            self.logger.warning("/stream start timeout after %.2fs", time.monotonic() - start_wait)
            self._send_plain(HTTPStatus.SERVICE_UNAVAILABLE, msg)
            return

        boundary = b"frame"
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=%s' % boundary.decode('ascii'))
            self.end_headers()
        except Exception as e:
            self.logger.warning("Failed to send stream headers: %s", e)
            return

        # Stream loop
        last_seq = seq - 1  # ensure we send the first available frame
        try:
            while not self.stop_event.is_set():
                frame, ts, seq = self.frame_buffer.wait_for_newer(last_seq, timeout=max(1.0, 2.0))
                if frame is None:
                    continue

                last_seq = seq
                try:
                    self.wfile.write(b"--" + boundary + b"\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(b"Content-Length: " + str(len(frame)).encode('ascii') + b"\r\n\r\n")
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    break
                except Exception as e:
                    self.logger.warning("Stream write error: %s", e)
                    break
        finally:
            try:
                # Close connection
                self.wfile.flush()
            except Exception:
                pass


def setup_logging(level_str: str) -> logging.Logger:
    lvl = getattr(logging, level_str.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format='%(asctime)s %(levelname)s %(threadName)s %(message)s',
        stream=sys.stdout,
    )
    return logging.getLogger("hik-usb-driver")


def main() -> None:
    config = load_config()
    logger = setup_logging(config.log_level)

    stop_event = threading.Event()
    buffer = FrameBuffer()

    worker = CameraWorker(config, buffer, stop_event, logger)
    worker.start()

    handler_class = MJPEGHandler
    handler_class.frame_buffer = buffer
    handler_class.stop_event = stop_event
    handler_class.config = config
    handler_class.logger = logger
    handler_class.worker = worker

    server = ThreadingHTTPServer((config.http_host, config.http_port), handler_class)

    def handle_signal(signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        stop_event.set()
        try:
            server.shutdown()
        except Exception:
            pass

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("HTTP server listening on %s:%d", config.http_host, config.http_port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        handle_signal(signal.SIGINT, None)
    finally:
        stop_event.set()
        try:
            server.server_close()
        except Exception:
            pass
        try:
            worker.join(timeout=5.0)
        except Exception:
            pass
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
