# util.py
import sys
import time
import threading


class Logger:
    LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

    def __init__(self, level: str = "INFO"):
        self.level = self.LEVELS.get(level.upper(), 20)
        self._lock = threading.Lock()

    def _log(self, lvl: str, msg: str):
        if self.LEVELS.get(lvl, 100) < self.level:
            return
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        line = f"{ts}Z [{lvl}] {msg}\n"
        with self._lock:
            sys.stderr.write(line)
            try:
                sys.stderr.flush()
            except Exception:
                pass

    def debug(self, msg: str):
        self._log("DEBUG", msg)

    def info(self, msg: str):
        self._log("INFO", msg)

    def warn(self, msg: str):
        self._log("WARN", msg)

    def error(self, msg: str):
        self._log("ERROR", msg)
