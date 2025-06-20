import os
import threading
import time
import cv2
from flask import Flask, Response, jsonify, stream_with_context

app = Flask(__name__)

# Configuration from environment variables
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", 8080))

# Global camera control
camera_lock = threading.Lock()
camera = None
camera_active = False
capture_thread = None
last_frame = None

def open_camera():
    global camera, camera_active
    if camera is None:
        camera = cv2.VideoCapture(CAMERA_INDEX)
        if not camera.isOpened():
            camera.release()
            camera = None
            raise RuntimeError("Cannot open USB Camera")
    camera_active = True

def close_camera():
    global camera, camera_active
    camera_active = False
    if camera is not None:
        camera.release()
        camera = None

def get_frame():
    global camera, last_frame
    if camera is not None and camera_active:
        ret, frame = camera.read()
        if not ret:
            return None
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            return None
        last_frame = jpeg.tobytes()
        return last_frame
    return last_frame

def stream_mjpeg():
    while camera_active:
        frame = get_frame()
        if frame is None:
            continue
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        time.sleep(0.04)  # ~25 FPS
    # When camera is stopped, end the stream
    yield (b"--frame\r\n\r\n")

@app.route("/camera/start", methods=["POST"])
def start_camera():
    with camera_lock:
        if not camera_active:
            try:
                open_camera()
            except Exception as e:
                return jsonify({"success": False, "message": str(e)}), 500
        return jsonify({"success": True, "message": "Camera started."})

@app.route("/camera/stop", methods=["POST"])
def stop_camera():
    with camera_lock:
        close_camera()
        return jsonify({"success": True, "message": "Camera stopped."})

@app.route("/camera/stream", methods=["GET"])
def camera_stream():
    with camera_lock:
        if not camera_active:
            try:
                open_camera()
            except Exception as e:
                return jsonify({"success": False, "message": str(e)}), 500
        return Response(stream_with_context(stream_mjpeg()), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)