import os
import cv2
from flask import Flask, Response, request, abort

# Configuration from environment variables
CAMERA_INDEX = int(os.environ.get('CAMERA_INDEX', 0))
HTTP_SERVER_HOST = os.environ.get('HTTP_SERVER_HOST', '0.0.0.0')
HTTP_SERVER_PORT = int(os.environ.get('HTTP_SERVER_PORT', 8000))
CAMERA_WIDTH = int(os.environ.get('CAMERA_WIDTH', 640))
CAMERA_HEIGHT = int(os.environ.get('CAMERA_HEIGHT', 480))
CAMERA_FPS = int(os.environ.get('CAMERA_FPS', 15))

app = Flask(__name__)

def get_camera():
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
    return cap

def generate_mjpeg():
    cap = get_camera()
    if cap is None:
        yield b"--frame\r\nContent-Type: text/plain\r\n\r\nCamera not found\r\n"
        return
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            frame_bytes = jpeg.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n'
                   b'Content-Length: ' + f"{len(frame_bytes)}".encode() + b'\r\n\r\n' +
                   frame_bytes + b'\r\n')
    finally:
        cap.release()

@app.route('/video', methods=['GET'])
def video_stream():
    return Response(generate_mjpeg(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)