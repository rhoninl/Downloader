import os
import threading
import cv2
import time
from flask import Flask, Response, request, jsonify

app = Flask(__name__)

# --- Environment Variables ---
CAMERA_INDEX = int(os.environ.get("USB_CAMERA_INDEX", 0))
HTTP_SERVER_HOST = os.environ.get("HTTP_SERVER_HOST", "0.0.0.0")
HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", 8000))
MJPEG_FPS = int(os.environ.get("MJPEG_FPS", 20))
CAMERA_WIDTH = int(os.environ.get("USB_CAMERA_WIDTH", 640))
CAMERA_HEIGHT = int(os.environ.get("USB_CAMERA_HEIGHT", 480))

# --- Streaming State ---
streaming_lock = threading.Lock()
streaming_state = {
    "active": False,
    "capture": None,
    "thread": None
}

def open_camera():
    cap = cv2.VideoCapture(CAMERA_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    return cap

def release_camera():
    if streaming_state["capture"] is not None:
        streaming_state["capture"].release()
        streaming_state["capture"] = None

def mjpeg_streamer():
    while True:
        with streaming_lock:
            if not streaming_state["active"] or streaming_state["capture"] is None:
                break
            cap = streaming_state["capture"]
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.01)
            continue
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        time.sleep(1.0 / MJPEG_FPS)

@app.route('/stream', methods=['GET'])
def get_stream():
    fmt = request.args.get('format', 'mjpeg').lower()
    with streaming_lock:
        if not streaming_state["active"]:
            return jsonify({"error": "Stream not started"}), 409
        if streaming_state["capture"] is None:
            return jsonify({"error": "Camera not initialized"}), 500

    if fmt == 'mjpeg':
        return Response(
            mjpeg_streamer(),
            mimetype='multipart/x-mixed-replace; boundary=frame'
        )
    else:
        return jsonify({"error": "Unsupported format"}), 400

@app.route('/stream', methods=['POST'])
def start_stream():
    with streaming_lock:
        if streaming_state["active"]:
            return jsonify({"status": "already streaming"}), 200
        cap = open_camera()
        if not cap.isOpened():
            return jsonify({"error": "Unable to open camera"}), 500
        streaming_state["active"] = True
        streaming_state["capture"] = cap

    return jsonify({"status": "stream started"}), 200

@app.route('/stream', methods=['DELETE'])
def stop_stream():
    with streaming_lock:
        if not streaming_state["active"]:
            return jsonify({"status": "already stopped"}), 200
        streaming_state["active"] = False
        release_camera()

    return jsonify({"status": "stream stopped"}), 200

if __name__ == "__main__":
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)