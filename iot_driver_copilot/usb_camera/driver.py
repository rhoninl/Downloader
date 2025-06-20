import os
import cv2
import threading
import time
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# Configuration from environment variables
CAMERA_DEVICE = os.environ.get("CAMERA_DEVICE", "0")
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))

# Camera control state
camera_lock = threading.Lock()
camera_streaming = False
camera_capture = None

def open_camera():
    global camera_capture
    device_index = 0
    try:
        device_index = int(CAMERA_DEVICE)
    except ValueError:
        device_index = CAMERA_DEVICE
    camera_capture = cv2.VideoCapture(device_index)
    # Try to set a reasonable resolution (optional)
    camera_capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera_capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    return camera_capture.isOpened()

def release_camera():
    global camera_capture
    if camera_capture is not None:
        camera_capture.release()
        camera_capture = None

def generate_mjpeg():
    global camera_capture, camera_streaming
    while True:
        with camera_lock:
            if not camera_streaming or camera_capture is None:
                break
            ret, frame = camera_capture.read()
        if not ret:
            time.sleep(0.05)
            continue
        # MJPEG encode
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        frame_bytes = jpeg.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.04)  # Approx 25 FPS

@app.route('/camera/start', methods=['POST'])
def start_camera():
    global camera_streaming, camera_capture
    with camera_lock:
        if camera_streaming:
            return jsonify({"result": "already started"}), 200
        opened = open_camera()
        if not opened:
            return jsonify({"result": "fail", "error": "unable to open camera"}), 500
        camera_streaming = True
    return jsonify({"result": "started"}), 200

@app.route('/camera/stop', methods=['POST'])
def stop_camera():
    global camera_streaming
    with camera_lock:
        if not camera_streaming:
            return jsonify({"result": "already stopped"}), 200
        camera_streaming = False
        release_camera()
    return jsonify({"result": "stopped"}), 200

@app.route('/camera/stream', methods=['GET'])
def stream_camera():
    global camera_streaming, camera_capture
    with camera_lock:
        if not camera_streaming or camera_capture is None:
            return jsonify({"result": "fail", "error": "camera not streaming"}), 503
    return Response(generate_mjpeg(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)