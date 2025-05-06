import os
import threading
import queue
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import base64
import requests
import cv2
import numpy as np

# Configuration from environment variables
CAMERA_IP = os.environ.get('CAMERA_IP')
CAMERA_RTSP_PORT = int(os.environ.get('CAMERA_RTSP_PORT', '554'))
CAMERA_USERNAME = os.environ.get('CAMERA_USERNAME')
CAMERA_PASSWORD = os.environ.get('CAMERA_PASSWORD')
CAMERA_HTTP_PORT = int(os.environ.get('CAMERA_HTTP_PORT', '80'))

SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', '8080'))

RTSP_PATH = os.environ.get('CAMERA_RTSP_PATH', '/Streaming/Channels/101')
# Default RTSP url: rtsp://user:pass@ip:port/path

SNAPSHOT_PATH = os.environ.get('CAMERA_SNAPSHOT_PATH', '/ISAPI/Streaming/channels/101/picture')

STREAM_FPS = int(os.environ.get('STREAM_FPS', '15'))

# Global stream state
streaming_clients = set()
stream_queue = queue.Queue(maxsize=10)
streaming_active = threading.Event()
video_thread = None
video_thread_lock = threading.Lock()

def get_rtsp_url():
    user_pass = ''
    if CAMERA_USERNAME and CAMERA_PASSWORD:
        user_pass = f'{CAMERA_USERNAME}:{CAMERA_PASSWORD}@'
    return f'rtsp://{user_pass}{CAMERA_IP}:{CAMERA_RTSP_PORT}{RTSP_PATH}'

def get_snapshot_url():
    return f'http://{CAMERA_IP}:{CAMERA_HTTP_PORT}{SNAPSHOT_PATH}'

def fetch_snapshot():
    url = get_snapshot_url()
    auth = (CAMERA_USERNAME, CAMERA_PASSWORD) if (CAMERA_USERNAME and CAMERA_PASSWORD) else None
    resp = requests.get(url, auth=auth, timeout=10)
    resp.raise_for_status()
    return resp.content

def video_capture_worker():
    rtsp_url = get_rtsp_url()
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        streaming_active.clear()
        return
    last_frame_time = 0
    try:
        while streaming_active.is_set():
            ret, frame = cap.read()
            if not ret:
                break
            now = time.time()
            if STREAM_FPS > 0 and now - last_frame_time < 1.0 / STREAM_FPS:
                time.sleep(max(0, (1.0 / STREAM_FPS) - (now - last_frame_time)))
                continue
            last_frame_time = now
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            try:
                stream_queue.put(jpeg.tobytes(), block=False)
            except queue.Full:
                pass  # drop frame if queue is full
    finally:
        cap.release()
        streaming_active.clear()
        with stream_queue.mutex:
            stream_queue.queue.clear()

def start_video_stream():
    global video_thread
    with video_thread_lock:
        if video_thread is None or not video_thread.is_alive():
            streaming_active.set()
            video_thread = threading.Thread(target=video_capture_worker, daemon=True)
            video_thread.start()

def stop_video_stream():
    streaming_active.clear()
    with video_thread_lock:
        if video_thread is not None:
            video_thread.join(timeout=1)
    with stream_queue.mutex:
        stream_queue.queue.clear()

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class HikvisionHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/video/stream':
            self.handle_video_stream()
        elif self.path == '/camera/snapshot':
            self.handle_snapshot()
        else:
            self.send_error(404, 'Not Found')

    def do_POST(self):
        if self.path == '/video/stream/stop':
            self.handle_stream_stop()
        else:
            self.send_error(404, 'Not Found')

    def handle_video_stream(self):
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.end_headers()
        client_id = id(self)
        streaming_clients.add(client_id)
        try:
            start_video_stream()
            while streaming_active.is_set():
                try:
                    frame = stream_queue.get(timeout=5)
                except queue.Empty:
                    continue
                self.wfile.write(b'--frame\r\n')
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
                self.wfile.write(b'\r\n')
                if not self.is_client_connected():
                    break
        except Exception:
            pass
        finally:
            streaming_clients.discard(client_id)
            if not streaming_clients:
                stop_video_stream()

    def handle_snapshot(self):
        try:
            img_bytes = fetch_snapshot()
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(img_bytes)))
            self.end_headers()
            self.wfile.write(img_bytes)
        except Exception as e:
            self.send_error(500, f'Failed to fetch snapshot: {str(e)}')

    def handle_stream_stop(self):
        streaming_clients.clear()
        stop_video_stream()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"status":"stream stopped"}')

    def log_message(self, format, *args):
        pass

    def is_client_connected(self):
        try:
            self.wfile.flush()
            return True
        except Exception:
            return False

def main():
    server = ThreadedHTTPServer((SERVER_HOST, SERVER_PORT), HikvisionHandler)
    print(f"Hikvision IP Camera HTTP driver is running at http://{SERVER_HOST}:{SERVER_PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_video_stream()
        server.server_close()

if __name__ == '__main__':
    main()