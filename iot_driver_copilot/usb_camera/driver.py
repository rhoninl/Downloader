import os
import cv2
import threading
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
STREAM_FORMAT = os.environ.get("STREAM_FORMAT", "MJPEG").upper()  # MJPEG only for browser

# Global camera state
camera_lock = threading.Lock()
camera = None
streaming = False

def open_camera():
    global camera
    with camera_lock:
        if camera is None:
            camera = cv2.VideoCapture(CAMERA_INDEX)
            # If H.264 is needed, configure the camera here (browser only supports MJPEG natively)
        return camera.isOpened()

def release_camera():
    global camera
    with camera_lock:
        if camera is not None:
            camera.release()
            camera = None

def gen_mjpeg():
    global camera
    while streaming:
        with camera_lock:
            if camera is None or not camera.isOpened():
                break
            ret, frame = camera.read()
        if not ret:
            continue
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        frame_bytes = jpeg.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
    # Stream end

@app.route('/cam/start', methods=['POST'])
def start_stream():
    global streaming
    if streaming:
        return jsonify({"status": "already streaming"}), 200
    ok = open_camera()
    if not ok:
        return jsonify({"status": "failed", "reason": "camera open failed"}), 500
    streaming = True
    return jsonify({"status": "started"}), 200

@app.route('/cam/stop', methods=['POST'])
def stop_stream():
    global streaming
    if not streaming:
        return jsonify({"status": "already stopped"}), 200
    streaming = False
    release_camera()
    return jsonify({"status": "stopped"}), 200

@app.route('/cam/stream', methods=['GET'])
def stream():
    global streaming
    if not streaming:
        return jsonify({"status": "error", "reason": "stream not started"}), 400
    if STREAM_FORMAT == "MJPEG":
        return Response(gen_mjpeg(), mimetype='multipart/x-mixed-replace; boundary=frame')
    else:
        return jsonify({"status": "error", "reason": "unsupported format"}), 400

if __name__ == '__main__':
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)