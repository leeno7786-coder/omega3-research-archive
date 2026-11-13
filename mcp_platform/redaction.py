"""Bounded, content-redacting stderr sink for local MCP transports."""

from __future__ import annotations

import codecs
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from core.paths import OMEGA_ROOT

_LINE_LIMIT = 2_048
_DEFAULT_TAIL_LIMIT = 64 * 1024
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE)
_PRIVATE_KEY_END = re.compile(r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE)
_AUTHORIZATION = re.compile(r"(?i)\bauthorization\s*[:=]\s*[^\r\n]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:code|oauth[_-]?code|refresh[_-]?token|access[_-]?token|client[_-]?secret)"
    r"\s*[:=]\s*(?:['\"]?)[^\s,'\";&]+(?:['\"]?)"
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])(?:[A-Z]:\\[^\r\n\t<>|\"?*]+)")
_POSIX_PATH = re.compile(r"(?<![\w:])/(?:[^\s/]+/)*[^\s]*")


class RedactingTextSink:
    """A write-only-style text sink that retains only a bounded redacted tail."""

    encoding = "utf-8"

    def __init__(
        self,
        *,
        omega_root: Path = OMEGA_ROOT,
        max_bytes: int = _DEFAULT_TAIL_LIMIT,
    ) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._lock = threading.RLock()
        self._omega_root = str(Path(omega_root).resolve()).casefold()
        self._max_bytes = max_bytes
        self._tail = ""
        self._in_private_key = False
        self._line_chars = 0
        self._pending_line = ""
        self._discarding_line = False
        self._marker_window = ""

    def writable(self) -> bool:
        return True

    def flush(self) -> None:
        return None

    def write(self, value: Any) -> int:
        text = value if type(value) is str else "<redacted:non-text>"
        with self._lock:
            for character in text:
                if self._discarding_line:
                    if character in "\r\n":
                        self._append_redacted("<redacted:oversize-line>\n")
                        self._discarding_line = False
                        self._marker_window = ""
                    else:
                        self._track_discarded_marker(character)
                    continue
                if character in "\r\n":
                    self._append_redacted(self._redact(self._pending_line) + "\n")
                    self._pending_line = ""
                    continue
                if len(self._pending_line) >= 8_192:
                    for buffered in self._pending_line:
                        self._track_discarded_marker(buffered)
                    self._pending_line = ""
                    self._discarding_line = True
                    self._track_discarded_marker(character)
                    continue
                self._pending_line += character
        return len(text)

    def finish(self) -> None:
        """Commit one final unterminated logical line after the byte stream ends."""
        with self._lock:
            if self._discarding_line:
                self._append_redacted("<redacted:oversize-line>")
            elif self._pending_line:
                self._append_redacted(self._redact(self._pending_line))
            self._pending_line = ""
            self._discarding_line = False
            self._marker_window = ""

    def _track_discarded_marker(self, character: str) -> None:
        self._marker_window = (self._marker_window + character)[-64:]
        if self._in_private_key:
            if _PRIVATE_KEY_END.search(self._marker_window):
                self._in_private_key = False
                self._marker_window = ""
        elif _PRIVATE_KEY_BEGIN.search(self._marker_window):
            self._in_private_key = True
            self._marker_window = ""

    def _append_redacted(self, redacted: str) -> None:
        bounded_parts: list[str] = []
        for segment in redacted.splitlines(keepends=True):
            content = segment.rstrip("\r\n")
            ending = segment[len(content) :]
            remaining = max(0, _LINE_LIMIT - self._line_chars)
            accepted = content[:remaining]
            bounded_parts.append(accepted)
            self._line_chars += len(accepted)
            if ending:
                bounded_parts.append("\n")
                self._line_chars = 0
        bounded = "".join(bounded_parts)
        self._tail = _bounded_utf8_tail(self._tail + bounded, self._max_bytes)

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def getvalue(self) -> str:
        with self._lock:
            return self._tail

    def _redact(self, text: str) -> str:
        text = self._redact_private_key_stream(text)
        text = _PRIVATE_KEY.sub("<redacted:private-key>", text)
        text = _AUTHORIZATION.sub("Authorization: <redacted:authorization>", text)
        text = _BEARER.sub("Bearer <redacted:token>", text)
        text = _SECRET_ASSIGNMENT.sub("<redacted:credential>", text)
        text = _EMAIL.sub("<redacted:identity>", text)
        text = _WINDOWS_PATH.sub(self._redact_path, text)
        return _POSIX_PATH.sub(self._redact_path, text)

    def _redact_private_key_stream(self, text: str) -> str:
        retained: list[str] = []
        remaining = text
        while remaining:
            if self._in_private_key:
                end = _PRIVATE_KEY_END.search(remaining)
                if end is None:
                    return "".join(retained)
                self._in_private_key = False
                remaining = remaining[end.end() :]
                continue
            begin = _PRIVATE_KEY_BEGIN.search(remaining)
            if begin is None:
                retained.append(remaining)
                break
            retained.append(remaining[: begin.start()])
            retained.append("<redacted:private-key>")
            end = _PRIVATE_KEY_END.search(remaining, begin.end())
            if end is None:
                self._in_private_key = True
                break
            remaining = remaining[end.end() :]
        return "".join(retained)

    def _redact_path(self, match: re.Match[str]) -> str:
        candidate = match.group(0).rstrip(".,;:)")
        suffix = match.group(0)[len(candidate) :]
        try:
            normalized = str(Path(candidate).resolve()).casefold()
        except (OSError, RuntimeError, ValueError):
            return "<redacted:path>" + suffix
        if normalized == self._omega_root or normalized.startswith(self._omega_root + "\\"):
            return candidate + suffix
        return "<redacted:path>" + suffix


def _bounded_utf8_tail(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    tail = encoded[-limit:]
    while tail:
        try:
            return tail.decode("utf-8")
        except UnicodeDecodeError as exc:
            tail = tail[exc.start + 1 :]
    return ""


class RedactingStderrCapture:
    """OS-handle stderr target drained into one bounded redacting sink."""

    _JOIN_TIMEOUT_S = 2.0

    def __init__(
        self,
        sink: RedactingTextSink,
        *,
        shutdown_timeout_seconds: float = 4.0,
    ) -> None:
        if type(shutdown_timeout_seconds) not in {int, float} or shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._sink = sink
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._lock = threading.Lock()
        self._read_fd, self._write_fd = os.pipe()
        os.set_blocking(self._read_fd, False)
        os.set_inheritable(self._write_fd, True)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def fileno(self) -> int:
        with self._lock:
            if self._write_fd < 0:
                raise ValueError("stderr_capture_closed")
            return self._write_fd

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("stderr_capture_already_started")
            thread = threading.Thread(
                target=self._drain,
                name=f"omega-mcp-stderr-{id(self):x}",
                daemon=False,
            )
            self._thread = thread
        try:
            thread.start()
        except BaseException:
            self._close_fd("_write_fd")
            self._close_fd("_read_fd")
            raise

    def close_parent_writer(self) -> None:
        self._close_fd("_write_fd")

    def close(self) -> None:
        deadline = time.monotonic() + self._shutdown_timeout_seconds
        self.close_parent_writer()
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            self._close_fd("_read_fd")
            self._sink.finish()
            return
        if thread is threading.current_thread():
            raise RuntimeError("stderr_capture_cleanup_failed")
        thread.join(min(self._JOIN_TIMEOUT_S, max(0.0, deadline - time.monotonic())))
        if thread.is_alive():
            self._close_fd("_read_fd")
            thread.join(min(self._JOIN_TIMEOUT_S, max(0.0, deadline - time.monotonic())))
        if thread.is_alive():
            raise RuntimeError("stderr_capture_cleanup_failed")

    def thread_alive(self) -> bool:
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def _close_fd(self, attribute: str) -> None:
        with self._lock:
            descriptor = getattr(self, attribute)
            setattr(self, attribute, -1)
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _drain(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        with self._lock:
            read_fd = self._read_fd
        empty_after_stop = 0
        try:
            while read_fd >= 0:
                try:
                    chunk = os.read(read_fd, 4096)
                except BlockingIOError:
                    if self._stop.is_set():
                        empty_after_stop += 1
                        if empty_after_stop >= 2:
                            break
                    self._stop.wait(0.01)
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                empty_after_stop = 0
                decoded = decoder.decode(chunk)
                if decoded:
                    self._sink.write(decoded)
            final = decoder.decode(b"", final=True)
            if final:
                self._sink.write(final)
        finally:
            self._sink.finish()
            self._close_fd("_read_fd")
