import os
import threading
import io
import time
import http.server
import socketserver
import urllib.parse
import base64

import requests
import cv2
import numpy as np

# -------------------- Environment Variables --------------------
DEVICE_IP = os.environ.get("DEVICE_IP", "192.168.1.64")
DEVICE_RTSP_PORT = int(os.environ.get("DEVICE_RTSP_PORT", "554"))
DEVICE_HTTP_PORT = int(os.environ.get("DEVICE_HTTP_PORT", "80"))
DEVICE_USERNAME = os.environ.get("DEVICE_USERNAME", "admin")
DEVICE_PASSWORD = os.environ.get("DEVICE_PASSWORD", "12345")
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))

# -------------------- RTSP URL Construction --------------------
RTSP_URL = f"rtsp://{DEVICE_USERNAME}:{DEVICE_PASSWORD}@{DEVICE_IP}:{DEVICE_RTSP_PORT}/Streaming/Channels/101"

# -------------------- MJPEG Streaming Implementation --------------------
# Each client connection gets its own VideoStreamer instance.
class VideoStreamer:
    def __init__(self):
        self.vc = cv2.VideoCapture(RTSP_URL)
        self.lock = threading.Lock()
        self.running = True

    def frames(self):
        while self.running:
            with self.lock:
                ret, frame = self.vc.read()
                if not ret:
                    # Try to reconnect
                    self.vc.release()
                    time.sleep(0.5)
                    self.vc = cv2.VideoCapture(RTSP_URL)
                    continue
                # Encode frame as JPEG
                ret, jpeg = cv2.imencode('.jpg', frame)
                if not ret:
                    continue
                yield jpeg.tobytes()
            time.sleep(0.04)  # ~25fps

    def stop(self):
        with self.lock:
            self.running = False
            self.vc.release()

# -------------------- HTTP Request Handler --------------------
class HikvisionHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    # For streaming control
    streamer_threads = {}
    streamer_objs = {}

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path == '/video/stream':
            self.handle_video_stream()
        elif parsed_path.path == '/camera/snapshot':
            self.handle_camera_snapshot()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path == '/video/stream/stop':
            self.handle_stream_stop()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')

    def handle_video_stream(self):
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.end_headers()
        streamer = VideoStreamer()
        client_id = id(self)
        self.streamer_objs[client_id] = streamer
        try:
            for jpeg in streamer.frames():
                self.wfile.write(b'--frame\r\n')
                self.wfile.write(b'Content-Type: image/jpeg\r\n')
                self.wfile.write(b'Content-Length: %d\r\n\r\n' % len(jpeg))
                self.wfile.write(jpeg)
                self.wfile.write(b'\r\n')
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            streamer.stop()
            del self.streamer_objs[client_id]

    def handle_camera_snapshot(self):
        # Hikvision HTTP snapshot API (JPEG)
        snapshot_url = f"http://{DEVICE_IP}:{DEVICE_HTTP_PORT}/ISAPI/Streaming/channels/101/picture"
        try:
            resp = requests.get(snapshot_url, auth=(DEVICE_USERNAME, DEVICE_PASSWORD), timeout=3)
            if resp.status_code == 200 and resp.headers.get('Content-Type', '').startswith('image/jpeg'):
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(len(resp.content)))
                self.end_headers()
                self.wfile.write(resp.content)
            else:
                self.send_response(502)
                self.end_headers()
                self.wfile.write(b'Camera did not return image')
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b'Could not connect to camera snapshot API')

    def handle_stream_stop(self):
        client_id = id(self)
        streamer = self.streamer_objs.get(client_id)
        if streamer:
            streamer.stop()
            del self.streamer_objs[client_id]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'Stream stopped')
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'No active stream for this client')

    def log_message(self, format, *args):
        # Suppress default logging to keep output clean
        pass

# -------------------- Server Initialization --------------------
class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def run_server():
    with ThreadingHTTPServer((SERVER_HOST, SERVER_PORT), HikvisionHTTPRequestHandler) as httpd:
        print(f"Hikvision IP Camera driver HTTP server running at http://{SERVER_HOST}:{SERVER_PORT}")
        httpd.serve_forever()

if __name__ == '__main__':
    run_server()