import os
import threading
import cv2
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# Configuration from environment variables
SERVER_HOST = os.environ.get("SHIFU_HTTP_SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SHIFU_HTTP_SERVER_PORT", "8080"))
CAMERA_INDEX = int(os.environ.get("SHIFU_USB_CAMERA_INDEX", "0"))

# Global state
camera = None
camera_lock = threading.Lock()
streaming = False
frame = None
frame_lock = threading.Lock()
capture_thread = None
should_capture = False

def capture_frames():
    global frame, camera, should_capture
    while should_capture:
        with camera_lock:
            if camera is not None and camera.isOpened():
                ret, img = camera.read()
                if ret:
                    with frame_lock:
                        frame = img
            else:
                break

@app.route("/camera/start", methods=["POST"])
def start_camera():
    global camera, streaming, should_capture, capture_thread
    with camera_lock:
        if streaming:
            return jsonify({"status": "already started"}), 200
        camera = cv2.VideoCapture(CAMERA_INDEX)
        if not camera.isOpened():
            camera = None
            return jsonify({"error": "Cannot open camera"}), 500
        should_capture = True
        capture_thread = threading.Thread(target=capture_frames, daemon=True)
        capture_thread.start()
        streaming = True
    return jsonify({"status": "started"}), 200

@app.route("/camera/stop", methods=["POST"])
def stop_camera():
    global camera, streaming, should_capture, capture_thread
    with camera_lock:
        if not streaming:
            return jsonify({"status": "already stopped"}), 200
        should_capture = False
        if capture_thread is not None:
            capture_thread.join(timeout=2)
        if camera is not None:
            camera.release()
            camera = None
        streaming = False
    return jsonify({"status": "stopped"}), 200

def generate_mjpeg():
    boundary = "--frame"
    while True:
        if not streaming:
            break
        with frame_lock:
            if frame is None:
                continue
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            img_bytes = jpeg.tobytes()
        yield (
            b'%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n' % (boundary.encode(), len(img_bytes)) +
            img_bytes +
            b'\r\n'
        )

@app.route("/camera/stream", methods=["GET"])
def stream_camera():
    if not streaming:
        return jsonify({"error": "Camera stream is not started"}), 400
    return Response(
        generate_mjpeg(),
        mimetype='multipart/x-mixed-replace; boundary=--frame'
    )

if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)