#include <arpa/inet.h>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <linux/videodev2.h>
#include <mutex>
#include <netinet/in.h>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <thread>
#include <unistd.h>
#include <vector>

#include "config.h"

// -------------------- Logging --------------------
enum class LogLevel { TRACE = 0, DEBUG = 1, INFO = 2, WARN = 3, ERROR = 4 };

static LogLevel GLOBAL_LOG_LEVEL = LogLevel::INFO;

static std::string nowString() {
    using namespace std::chrono;
    auto now = system_clock::now();
    auto t = system_clock::to_time_t(now);
    char buf[64];
    std::strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", std::localtime(&t));
    auto ms = duration_cast<milliseconds>(now.time_since_epoch()) % 1000;
    char out[80];
    std::snprintf(out, sizeof(out), "%s.%03lld", buf, (long long)ms.count());
    return std::string(out);
}

static void log(LogLevel lvl, const std::string &msg) {
    if ((int)lvl < (int)GLOBAL_LOG_LEVEL) return;
    const char *name = "INFO";
    switch (lvl) {
        case LogLevel::TRACE: name = "TRACE"; break;
        case LogLevel::DEBUG: name = "DEBUG"; break;
        case LogLevel::INFO: name = "INFO"; break;
        case LogLevel::WARN: name = "WARN"; break;
        case LogLevel::ERROR: name = "ERROR"; break;
    }
    std::cerr << nowString() << " [" << name << "] " << msg << std::endl;
}

// -------------------- UVC (V4L2) Device --------------------
struct Buffer {
    void *start = nullptr;
    size_t length = 0;
};

class UVCDevice {
public:
    UVCDevice(const Config &cfg)
        : devPath(cfg.uvc_device), width(cfg.uvc_width), height(cfg.uvc_height), fps(cfg.uvc_fps),
          readTimeoutMs(cfg.read_timeout_ms), retryBaseMs(cfg.retry_base_ms), retryMaxMs(cfg.retry_max_ms) {}

    ~UVCDevice() { stop(); }

    bool start() {
        std::lock_guard<std::mutex> lk(stateMutex);
        if (worker.joinable()) {
            log(LogLevel::INFO, "UVCDevice already started");
            return true;
        }
        stopFlag = false;
        worker = std::thread(&UVCDevice::run, this);
        log(LogLevel::INFO, "UVCDevice worker started");
        return true;
    }

    void stop() {
        {
            std::lock_guard<std::mutex> lk(stateMutex);
            stopFlag = true;
        }
        if (worker.joinable()) worker.join();
        cleanup();
    }

    bool isWorkerStarted() const {
        return worker.joinable();
    }

    bool isStreaming() const {
        return streaming.load();
    }

    // Wait for a new frame sequence different from lastSeq.
    // Returns false if stop requested; true if a new frame available.
    bool waitForFrame(uint64_t lastSeq, std::vector<uint8_t> &out, uint64_t &newSeq) {
        std::unique_lock<std::mutex> lk(frameMutex);
        frameCond.wait(lk, [&]{ return stopFlag || frameSeq != lastSeq; });
        if (stopFlag) return false;
        out = latestFrame;
        newSeq = frameSeq;
        return true;
    }

    // Non-blocking: copy the latest frame.
    bool getLatestFrame(std::vector<uint8_t> &out, uint64_t &seqOut) {
        std::lock_guard<std::mutex> lk(frameMutex);
        if (latestFrame.empty()) return false;
        out = latestFrame;
        seqOut = frameSeq;
        return true;
    }

    std::string lastUpdateString() const {
        std::lock_guard<std::mutex> lk(frameMutex);
        return lastUpdateStr;
    }

private:
    std::string devPath;
    int width;
    int height;
    int fps;
    int readTimeoutMs;
    int retryBaseMs;
    int retryMaxMs;

    int fd = -1;
    std::vector<Buffer> buffers;
    std::atomic<bool> streaming{false};
    std::atomic<bool> stopFlag{false};
    std::thread worker;
    std::mutex stateMutex;

    std::mutex frameMutex;
    std::condition_variable frameCond;
    std::vector<uint8_t> latestFrame;
    uint64_t frameSeq = 0;
    std::string lastUpdateStr;

    void run() {
        int backoffMs = retryBaseMs;
        while (!stopFlag) {
            if (!openAndConfigure()) {
                log(LogLevel::ERROR, "Failed to open/configure UVC device; retry in " + std::to_string(backoffMs) + " ms");
                sleepMs(backoffMs);
                backoffMs = std::min(backoffMs * 2, retryMaxMs);
                continue;
            }
            backoffMs = retryBaseMs; // reset on success
            log(LogLevel::INFO, "UVC device configured; starting capture loop");

            if (!streamOn()) {
                log(LogLevel::ERROR, "VIDIOC_STREAMON failed; retry in " + std::to_string(backoffMs) + " ms");
                cleanup();
                sleepMs(backoffMs);
                backoffMs = std::min(backoffMs * 2, retryMaxMs);
                continue;
            }
            streaming.store(true);
            captureLoop();
            streaming.store(false);
            streamOff();
            cleanup();

            if (!stopFlag) {
                log(LogLevel::WARN, "Capture loop ended unexpectedly; retry in " + std::to_string(backoffMs) + " ms");
                sleepMs(backoffMs);
                backoffMs = std::min(backoffMs * 2, retryMaxMs);
            }
        }
        log(LogLevel::INFO, "UVCDevice worker exiting");
    }

    bool openAndConfigure() {
        cleanup();
        fd = ::open(devPath.c_str(), O_RDWR | O_NONBLOCK, 0);
        if (fd < 0) {
            log(LogLevel::ERROR, "Cannot open device: " + devPath + ", errno=" + std::to_string(errno));
            return false;
        }

        v4l2_capability cap{};
        if (ioctl(fd, VIDIOC_QUERYCAP, &cap) < 0) {
            log(LogLevel::ERROR, "VIDIOC_QUERYCAP failed, errno=" + std::to_string(errno));
            ::close(fd); fd = -1; return false;
        }
        if (!(cap.capabilities & V4L2_CAP_VIDEO_CAPTURE)) {
            log(LogLevel::ERROR, "Device does not support video capture");
            ::close(fd); fd = -1; return false;
        }
        if (!(cap.capabilities & V4L2_CAP_STREAMING)) {
            log(LogLevel::ERROR, "Device does not support streaming I/O");
            ::close(fd); fd = -1; return false;
        }

        v4l2_format fmt{};
        fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        fmt.fmt.pix.width = width;
        fmt.fmt.pix.height = height;
        fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG; // Require MJPEG to avoid software encoding
        fmt.fmt.pix.field = V4L2_FIELD_ANY;
        if (ioctl(fd, VIDIOC_S_FMT, &fmt) < 0) {
            log(LogLevel::ERROR, "VIDIOC_S_FMT (MJPEG) failed. Ensure your camera supports MJPEG. errno=" + std::to_string(errno));
            ::close(fd); fd = -1; return false;
        }
        std::stringstream ss; ss << "Format set: " << fmt.fmt.pix.width << "x" << fmt.fmt.pix.height;
        log(LogLevel::INFO, ss.str());

        v4l2_streamparm parm{};
        parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        parm.parm.capture.timeperframe.numerator = 1;
        parm.parm.capture.timeperframe.denominator = fps;
        if (ioctl(fd, VIDIOC_S_PARM, &parm) < 0) {
            log(LogLevel::WARN, "VIDIOC_S_PARM failed; FPS might not be set. errno=" + std::to_string(errno));
        } else {
            int denom = parm.parm.capture.timeperframe.denominator;
            int numer = parm.parm.capture.timeperframe.numerator;
            if (denom > 0 && numer > 0) {
                std::stringstream fpss; fpss << "FPS set to approx " << (double)denom / (double)numer;
                log(LogLevel::INFO, fpss.str());
            }
        }

        v4l2_requestbuffers req{};
        req.count = 4;
        req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        req.memory = V4L2_MEMORY_MMAP;
        if (ioctl(fd, VIDIOC_REQBUFS, &req) < 0) {
            log(LogLevel::ERROR, "VIDIOC_REQBUFS failed, errno=" + std::to_string(errno));
            ::close(fd); fd = -1; return false;
        }
        if (req.count < 2) {
            log(LogLevel::ERROR, "Insufficient buffer memory on device");
            ::close(fd); fd = -1; return false;
        }

        buffers.resize(req.count);
        for (unsigned int i = 0; i < req.count; ++i) {
            v4l2_buffer buf{};
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            buf.index = i;
            if (ioctl(fd, VIDIOC_QUERYBUF, &buf) < 0) {
                log(LogLevel::ERROR, "VIDIOC_QUERYBUF failed, errno=" + std::to_string(errno));
                ::close(fd); fd = -1; return false;
            }

            void *start = mmap(NULL, buf.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, buf.m.offset);
            if (start == MAP_FAILED) {
                log(LogLevel::ERROR, "mmap failed, errno=" + std::to_string(errno));
                ::close(fd); fd = -1; return false;
            }
            buffers[i].start = start;
            buffers[i].length = buf.length;
        }

        for (unsigned int i = 0; i < req.count; ++i) {
            v4l2_buffer buf{};
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            buf.index = i;
            if (ioctl(fd, VIDIOC_QBUF, &buf) < 0) {
                log(LogLevel::ERROR, "VIDIOC_QBUF failed, errno=" + std::to_string(errno));
                ::close(fd); fd = -1; return false;
            }
        }

        return true;
    }

    bool streamOn() {
        int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        if (ioctl(fd, VIDIOC_STREAMON, &type) < 0) {
            return false;
        }
        return true;
    }

    void streamOff() {
        if (fd >= 0) {
            int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            ioctl(fd, VIDIOC_STREAMOFF, &type);
        }
    }

    void captureLoop() {
        auto lastLog = std::chrono::steady_clock::now();
        while (!stopFlag) {
            fd_set fds;
            FD_ZERO(&fds);
            FD_SET(fd, &fds);
            struct timeval tv{};
            tv.tv_sec = readTimeoutMs / 1000;
            tv.tv_usec = (readTimeoutMs % 1000) * 1000;
            int r = select(fd + 1, &fds, NULL, NULL, &tv);
            if (r < 0) {
                if (errno == EINTR) continue;
                log(LogLevel::ERROR, std::string("select error: ") + std::strerror(errno));
                break; // error -> restart
            } else if (r == 0) {
                log(LogLevel::DEBUG, "Frame read timeout");
                continue;
            }

            v4l2_buffer buf{};
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            if (ioctl(fd, VIDIOC_DQBUF, &buf) < 0) {
                if (errno == EAGAIN) {
                    continue;
                }
                log(LogLevel::ERROR, std::string("VIDIOC_DQBUF error: ") + std::strerror(errno));
                break; // error -> restart
            }

            if (buf.index >= buffers.size()) {
                log(LogLevel::ERROR, "Buffer index out of range");
                break;
            }

            // Copy MJPEG frame
            {
                std::lock_guard<std::mutex> lk(frameMutex);
                latestFrame.assign((uint8_t*)buffers[buf.index].start, (uint8_t*)buffers[buf.index].start + buf.bytesused);
                frameSeq++;
                lastUpdateStr = nowString();
            }
            frameCond.notify_all();

            // Log last-update timestamps periodically
            auto now = std::chrono::steady_clock::now();
            if (now - lastLog >= std::chrono::seconds(1)) {
                log(LogLevel::DEBUG, std::string("Last frame timestamp: ") + lastUpdateStr + ", size=" + std::to_string(latestFrame.size()));
                lastLog = now;
            }

            if (ioctl(fd, VIDIOC_QBUF, &buf) < 0) {
                log(LogLevel::ERROR, std::string("VIDIOC_QBUF error: ") + std::strerror(errno));
                break;
            }
        }
        log(LogLevel::WARN, "Ending capture loop");
    }

    void cleanup() {
        if (fd >= 0) {
            // unmap buffers
            for (auto &b : buffers) {
                if (b.start && b.length) {
                    munmap(b.start, b.length);
                }
            }
            buffers.clear();
            ::close(fd);
            fd = -1;
        }
    }

    static void sleepMs(int ms) {
        std::this_thread::sleep_for(std::chrono::milliseconds(ms));
    }
};

// -------------------- HTTP Server --------------------
class HttpServer {
public:
    HttpServer(const Config &cfg, UVCDevice &dev) : cfg(cfg), device(dev) {}
    ~HttpServer() { stop(); }

    bool start() {
        serverSock = ::socket(AF_INET, SOCK_STREAM, 0);
        if (serverSock < 0) {
            log(LogLevel::ERROR, "Failed to create socket: " + std::string(std::strerror(errno)));
            return false;
        }
        int opt = 1;
        setsockopt(serverSock, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(cfg.http_port);
        if (inet_pton(AF_INET, cfg.http_host.c_str(), &addr.sin_addr) != 1) {
            log(LogLevel::ERROR, "Invalid HTTP_HOST address: " + cfg.http_host);
            ::close(serverSock);
            serverSock = -1;
            return false;
        }
        if (bind(serverSock, (sockaddr*)&addr, sizeof(addr)) < 0) {
            log(LogLevel::ERROR, std::string("bind failed: ") + std::strerror(errno));
            ::close(serverSock);
            serverSock = -1;
            return false;
        }
        if (listen(serverSock, 16) < 0) {
            log(LogLevel::ERROR, std::string("listen failed: ") + std::strerror(errno));
            ::close(serverSock);
            serverSock = -1;
            return false;
        }
        running = true;
        acceptThread = std::thread(&HttpServer::acceptLoop, this);
        log(LogLevel::INFO, "HTTP server listening on " + cfg.http_host + ":" + std::to_string(cfg.http_port));
        return true;
    }

    void stop() {
        running = false;
        if (serverSock >= 0) {
            ::shutdown(serverSock, SHUT_RDWR);
            ::close(serverSock);
            serverSock = -1;
        }
        if (acceptThread.joinable()) acceptThread.join();
    }

private:
    Config cfg;
    UVCDevice &device;
    int serverSock = -1;
    std::atomic<bool> running{false};
    std::thread acceptThread;

    static bool recvLine(int sock, std::string &line) {
        line.clear();
        char c;
        while (true) {
            ssize_t n = recv(sock, &c, 1, 0);
            if (n <= 0) return false;
            if (c == '\r') {
                // expect '\n'
                n = recv(sock, &c, 1, 0);
                if (n <= 0) return false;
                if (c == '\n') break;
            } else if (c == '\n') {
                break;
            } else {
                line.push_back(c);
            }
        }
        return true;
    }

    static bool sendAll(int sock, const void *data, size_t len) {
        const uint8_t *p = (const uint8_t *)data;
        size_t sent = 0;
        while (sent < len) {
            ssize_t n = send(sock, p + sent, len - sent, 0);
            if (n <= 0) return false;
            sent += (size_t)n;
        }
        return true;
    }

    void acceptLoop() {
        while (running) {
            sockaddr_in caddr{};
            socklen_t clen = sizeof(caddr);
            int csock = accept(serverSock, (sockaddr*)&caddr, &clen);
            if (csock < 0) {
                if (errno == EINTR) continue;
                if (!running) break;
                log(LogLevel::WARN, std::string("accept failed: ") + std::strerror(errno));
                continue;
            }
            std::thread(&HttpServer::handleClient, this, csock).detach();
        }
    }

    void handleClient(int csock) {
        // Parse HTTP request
        std::string requestLine;
        if (!recvLine(csock, requestLine)) { ::close(csock); return; }
        log(LogLevel::DEBUG, "Request: " + requestLine);

        std::string method, path, version;
        {
            std::istringstream iss(requestLine);
            iss >> method >> path >> version;
        }
        // Read and discard headers
        std::string headerLine;
        while (recvLine(csock, headerLine)) {
            if (headerLine.empty()) break;
        }

        if (method == "POST" && path == "/connect") {
            handleConnect(csock);
        } else if (method == "GET" && path == "/stream") {
            handleStream(csock);
        } else {
            std::string body = "{\"error\":\"not_found\"}";
            std::ostringstream oss;
            oss << "HTTP/1.1 404 Not Found\r\n"
                << "Content-Type: application/json\r\n"
                << "Content-Length: " << body.size() << "\r\n"
                << "Connection: close\r\n\r\n"
                << body;
            std::string resp = oss.str();
            sendAll(csock, resp.data(), resp.size());
            ::close(csock);
        }
    }

    void handleConnect(int csock) {
        device.start();
        std::string body = std::string("{\"status\":\"ok\",\"message\":\"camera initialized\",\"device\":\"") + cfg.uvc_device + "\",\"width\":" + std::to_string(cfg.uvc_width) + ",\"height\":" + std::to_string(cfg.uvc_height) + ",\"fps\":" + std::to_string(cfg.uvc_fps) + "}";
        std::ostringstream oss;
        oss << "HTTP/1.1 200 OK\r\n"
            << "Content-Type: application/json\r\n"
            << "Content-Length: " << body.size() << "\r\n"
            << "Connection: close\r\n\r\n"
            << body;
        std::string resp = oss.str();
        sendAll(csock, resp.data(), resp.size());
        ::close(csock);
    }

    void handleStream(int csock) {
        if (!device.isWorkerStarted()) {
            std::string body = "{\"error\":\"not_connected\",\"message\":\"call /connect first\"}";
            std::ostringstream oss;
            oss << "HTTP/1.1 409 Conflict\r\n"
                << "Content-Type: application/json\r\n"
                << "Content-Length: " << body.size() << "\r\n"
                << "Connection: close\r\n\r\n"
                << body;
            std::string resp = oss.str();
            sendAll(csock, resp.data(), resp.size());
            ::close(csock);
            return;
        }

        std::ostringstream hdr;
        hdr << "HTTP/1.1 200 OK\r\n"
            << "Cache-Control: no-cache\r\n"
            << "Pragma: no-cache\r\n"
            << "Connection: close\r\n"
            << "Content-Type: multipart/x-mixed-replace; boundary=" << cfg.stream_boundary << "\r\n\r\n";
        std::string header = hdr.str();
        if (!sendAll(csock, header.data(), header.size())) { ::close(csock); return; }

        uint64_t lastSeq = 0;
        while (true) {
            std::vector<uint8_t> frame;
            uint64_t newSeq = 0;
            if (!device.waitForFrame(lastSeq, frame, newSeq)) break; // stop requested
            lastSeq = newSeq;
            if (frame.empty()) continue;

            std::ostringstream partHdr;
            partHdr << "--" << cfg.stream_boundary << "\r\n"
                    << "Content-Type: image/jpeg\r\n"
                    << "Content-Length: " << frame.size() << "\r\n\r\n";
            std::string ph = partHdr.str();
            if (!sendAll(csock, ph.data(), ph.size())) break;
            if (!sendAll(csock, frame.data(), frame.size())) break;
            const char *crlf = "\r\n";
            if (!sendAll(csock, crlf, 2)) break;
        }
        ::close(csock);
        log(LogLevel::INFO, "Stream client disconnected");
    }
};

// -------------------- Signal handling --------------------
static std::atomic<bool> MAIN_RUNNING{true};

static void sigHandler(int) {
    MAIN_RUNNING = false;
}

// -------------------- Main --------------------
int main() {
    Config cfg;
    if (!Config::loadFromEnv(cfg)) {
        std::cerr << "Missing or invalid environment variables. See README.md for required configuration." << std::endl;
        return 1;
    }

    // Set log level
    if (cfg.log_level == "TRACE") GLOBAL_LOG_LEVEL = LogLevel::TRACE;
    else if (cfg.log_level == "DEBUG") GLOBAL_LOG_LEVEL = LogLevel::DEBUG;
    else if (cfg.log_level == "INFO") GLOBAL_LOG_LEVEL = LogLevel::INFO;
    else if (cfg.log_level == "WARN") GLOBAL_LOG_LEVEL = LogLevel::WARN;
    else GLOBAL_LOG_LEVEL = LogLevel::ERROR;

    std::signal(SIGINT, sigHandler);
    std::signal(SIGTERM, sigHandler);

    UVCDevice dev(cfg);
    HttpServer server(cfg, dev);
    if (!server.start()) {
        return 1;
    }

    // Main loop waits for signal
    while (MAIN_RUNNING) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    log(LogLevel::INFO, "Shutting down...");
    server.stop();
    dev.stop();
    log(LogLevel::INFO, "Shutdown complete");
    return 0;
}
