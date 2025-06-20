import os
import threading
import time
from flask import Flask, Response, jsonify
import cv2

# Environment variables for configuration
CAMERA_INDEX = int(os.environ.get('CAMERA_INDEX', '0'))
HTTP_HOST = os.environ.get('HTTP_HOST', '0.0.0.0')
HTTP_PORT = int(os.environ.get('HTTP_PORT', '8080'))
VIDEO_WIDTH = int(os.environ.get('VIDEO_WIDTH', '640'))
VIDEO_HEIGHT = int(os.environ.get('VIDEO_HEIGHT', '480'))
VIDEO_FPS = int(os.environ.get('VIDEO_FPS', '15'))

app = Flask(__name__)

# Shared state for camera and streaming
class CameraState:
    def __init__(self):
        self.capture = None
        self.is_streaming = False
        self.lock = threading.Lock()

    def start_capture(self):
        with self.lock:
            if self.capture is not None and self.capture.isOpened():
                return True
            self.capture = cv2.VideoCapture(CAMERA_INDEX)
            if not self.capture.isOpened():
                self.capture = None
                return False
            self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_WIDTH)
            self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_HEIGHT)
            self.capture.set(cv2.CAP_PROP_FPS, VIDEO_FPS)
            self.is_streaming = True
            return True

    def stop_capture(self):
        with self.lock:
            self.is_streaming = False
            if self.capture is not None:
                self.capture.release()
                self.capture = None

    def read_frame(self):
        with self.lock:
            if self.capture is not None and self.capture.isOpened():
                ret, frame = self.capture.read()
                if ret:
                    return frame
            return None

    def is_started(self):
        with self.lock:
            return self.is_streaming and self.capture is not None and self.capture.isOpened()

camera_state = CameraState()

def mjpeg_generator():
    while camera_state.is_started():
        frame = camera_state.read_frame()
        if frame is None:
            time.sleep(0.05)
            continue
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        time.sleep(1.0 / VIDEO_FPS)
    # On stop, yield empty to signal disconnect
    yield b''

@app.route('/start', methods=['GET'])
def start_video_stream():
    success = camera_state.start_capture()
    if success:
        return jsonify({'status': 'started'}), 200
    else:
        return jsonify({'status': 'error', 'message': 'Could not start camera capture'}), 500

@app.route('/video', methods=['GET'])
def video_feed():
    if not camera_state.is_started():
        started = camera_state.start_capture()
        if not started:
            return jsonify({'status': 'error', 'message': 'Camera not available'}), 500
    return Response(mjpeg_generator(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stop', methods=['GET'])
def stop_video_stream():
    camera_state.stop_capture()
    return jsonify({'status': 'stopped'}), 200

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)