import os
import threading
import time
import cv2
from flask import Flask, Response, request, jsonify

# Configuration from environment variables
DEVICE_INDEX = int(os.environ.get('DEVICE_INDEX', '0'))  # Default first camera
HTTP_HOST = os.environ.get('HTTP_HOST', '0.0.0.0')
HTTP_PORT = int(os.environ.get('HTTP_PORT', '8080'))
STREAM_FPS = float(os.environ.get('STREAM_FPS', '20'))  # Adjustable FPS for streaming
STREAM_FORMAT = os.environ.get('STREAM_FORMAT', 'mjpeg').lower()  # Options: mjpeg (default)
JPEG_QUALITY = int(os.environ.get('JPEG_QUALITY', '80'))

# Flask app
app = Flask(__name__)

# Shared camera state
class CameraStreamState:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = False
        self.camera = None
        self.last_frame = None
        self.capture_thread = None
        self.running = False

    def start(self):
        with self.lock:
            if self.active:
                return True
            try:
                camera = cv2.VideoCapture(DEVICE_INDEX)
                if not camera.isOpened():
                    return False
                self.camera = camera
                self.active = True
                self.running = True
                self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
                self.capture_thread.start()
                return True
            except Exception:
                self.active = False
                return False

    def _capture_loop(self):
        while self.running:
            if self.camera:
                ret, frame = self.camera.read()
                if ret:
                    self.last_frame = frame
            time.sleep(1.0 / STREAM_FPS)

    def stop(self):
        with self.lock:
            self.running = False
            self.active = False
            if self.camera:
                try:
                    self.camera.release()
                except Exception:
                    pass
                self.camera = None
            self.last_frame = None

    def get_frame(self):
        with self.lock:
            return self.last_frame

    def is_active(self):
        with self.lock:
            return self.active

camera_state = CameraStreamState()

@app.route('/commands/start', methods=['POST'])
def start_stream():
    started = camera_state.start()
    if started:
        return jsonify({"status": "stream_started"}), 200
    else:
        return jsonify({"error": "Failed to start camera stream"}), 500

@app.route('/commands/stop', methods=['POST'])
def stop_stream():
    camera_state.stop()
    return jsonify({"status": "stream_stopped"}), 200

def gen_mjpeg():
    while camera_state.is_active():
        frame = camera_state.get_frame()
        if frame is not None:
            ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
            if ret:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        time.sleep(1.0 / STREAM_FPS)
    # Stream ended
    yield b''

@app.route('/stream', methods=['GET'])
def stream_video():
    if not camera_state.is_active():
        return jsonify({"error": "Stream not started. POST /commands/start first."}), 400

    fmt = request.args.get('format', STREAM_FORMAT).lower()
    if fmt == 'mjpeg':
        return Response(gen_mjpeg(),
                        mimetype='multipart/x-mixed-replace; boundary=frame')
    else:
        return jsonify({"error": "Unsupported stream format"}), 400

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)