"""Anonymous-pipe (--remote-debugging-pipe) CDP transport for Chromium."""
from __future__ import annotations

from collections.abc import Callable
import json
import os
import threading
from typing import Any


class CDPError(RuntimeError):
    """Raised when Chromium returns a CDP protocol error."""

    def __init__(self, method: str, error: dict[str, Any]) -> None:
        self.method = method
        self.code = error.get("code")
        self.error_message = error.get("message", str(error))
        self.data = error.get("data")
        detail = f"{method} failed [{self.code}]: {self.error_message}"
        if self.data:
            detail += f" ({self.data})"
        super().__init__(detail)


class _PendingCall:
    __slots__ = ("method", "event", "result", "error")

    def __init__(self, method: str) -> None:
        self.method = method
        self.event = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: Exception | None = None


EventCallback = Callable[[str | None, str, dict[str, Any]], None]


class PipeCDPConnection:
    """Thread-safe NUL-delimited JSON CDP client over Chromium's fd 3 & fd 4."""

    def __init__(self, read_fd: int, write_fd: int) -> None:
        self._read_fd = read_fd
        self._write_fd = write_fd
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, _PendingCall] = {}
        self._subscribers: list[EventCallback] = []
        self._closed = False
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="chromium-cdp-pipe-reader", daemon=True
        )
        self._reader_thread.start()

    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        with self._state_lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._state_lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        with self._state_lock:
            if self._closed:
                raise RuntimeError(f"CDP pipe is closed while sending {method}")
            self._next_id += 1
            msg_id = self._next_id
            call = _PendingCall(method)
            self._pending[msg_id] = call

        payload: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        if session_id is not None:
            payload["sessionId"] = session_id

        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\x00"
        try:
            with self._write_lock:
                view = memoryview(encoded)
                while view:
                    written = os.write(self._write_fd, view)
                    view = view[written:]
        except OSError as exc:
            with self._state_lock:
                self._pending.pop(msg_id, None)
            raise RuntimeError(f"Failed writing CDP command {method}: {exc}") from exc

        if not call.event.wait(timeout=timeout):
            with self._state_lock:
                self._pending.pop(msg_id, None)
            raise TimeoutError(f"CDP command {method} timed out after {timeout:.1f}s")

        if call.error is not None:
            raise call.error
        return call.result or {}

    def _reader_loop(self) -> None:
        buf = bytearray()
        try:
            while True:
                chunk = os.read(self._read_fd, 65536)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    zero_idx = buf.find(0)
                    if zero_idx < 0:
                        break
                    raw = bytes(buf[:zero_idx])
                    del buf[: zero_idx + 1]
                    if not raw:
                        continue
                    self._dispatch_raw(raw)
        except OSError:
            pass
        finally:
            self._fail_all(RuntimeError("Chromium CDP pipe closed"))

    def _dispatch_raw(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            return

        msg_id = msg.get("id")
        if msg_id is not None:
            with self._state_lock:
                call = self._pending.pop(msg_id, None)
            if call is not None:
                if "error" in msg:
                    call.error = CDPError(call.method, msg["error"])
                else:
                    call.result = msg.get("result") or {}
                call.event.set()
            return

        method = msg.get("method")
        if not method:
            return
        session_id = msg.get("sessionId")
        params = msg.get("params") or {}
        with self._state_lock:
            callbacks = list(self._subscribers)
        for cb in callbacks:
            try:
                cb(session_id, method, params)
            except Exception:
                pass

    def _fail_all(self, exc: Exception) -> None:
        with self._state_lock:
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for call in pending:
            call.error = exc
            call.event.set()

    def close(self) -> None:
        self._fail_all(RuntimeError("CDP connection closed"))
        for fd_attr in ("_write_fd", "_read_fd"):
            fd = getattr(self, fd_attr, -1)
            if fd >= 0:
                setattr(self, fd_attr, -1)
                try:
                    os.close(fd)
                except OSError:
                    pass
