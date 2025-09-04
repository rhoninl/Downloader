# stream_hub.py
import threading
import queue
import time
from typing import List


class SSEClient:
    def __init__(self, max_buffer: int = 256) -> None:
        self.queue: "queue.Queue[str]" = queue.Queue(maxsize=max_buffer)
        self.active = True

    def close(self) -> None:
        self.active = False
        try:
            self.queue.put_nowait("__CLOSE__")
        except Exception:
            pass


class SSEHub:
    def __init__(self) -> None:
        self._clients: List[SSEClient] = []
        self._lock = threading.Lock()

    def subscribe(self) -> SSEClient:
        client = SSEClient()
        with self._lock:
            self._clients.append(client)
        return client

    def unsubscribe(self, client: SSEClient) -> None:
        with self._lock:
            try:
                self._clients.remove(client)
            except ValueError:
                pass
        client.close()

    def broadcast(self, data: str) -> None:
        # Non-blocking fan-out with drop-oldest policy
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            if not c.active:
                continue
            try:
                c.queue.put_nowait(data)
            except queue.Full:
                try:
                    _ = c.queue.get_nowait()
                except Exception:
                    pass
                try:
                    c.queue.put_nowait(data)
                except Exception:
                    pass

    def heartbeat(self) -> None:
        self.broadcast("")
