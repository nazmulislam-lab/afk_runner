"""Helpers shared by the isolated (cloud) providers."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import uuid
from typing import Callable, List, Optional

from ..display import BoundedTail
from ..sandbox_provider import ExecResult


def shell_escape(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def stdin_temp_path() -> str:
    return f"/tmp/sandcastle-stdin-{uuid.uuid4()}"


def with_stdin_file(command: str, stdin_path: str) -> str:
    """Feed a previously uploaded file as stdin, removing it first."""
    return f"{{ rm -f {stdin_path}; {command}\n}} < {stdin_path}"


class LineSink(io.RawIOBase):
    """A writable binary stream that splits into lines for ``on_line`` and
    keeps a bounded tail (or the whole text when ``max_chars`` is None)."""

    def __init__(self, on_line: Optional[Callable[[str], None]], max_chars: Optional[int], separator: str = "\n") -> None:
        super().__init__()
        self._on_line = on_line
        self._whole: List[str] = []
        self._tail = BoundedTail(max_chars, separator) if max_chars is not None else None
        self._partial = ""
        self._decoder_buf = b""

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:  # type: ignore[override]
        data = bytes(b) if not isinstance(b, (bytes, bytearray)) else bytes(b)
        self.push_text(self._decode(data))
        return len(data)

    def _decode(self, data: bytes) -> str:
        buf = self._decoder_buf + data
        try:
            text = buf.decode("utf-8")
            self._decoder_buf = b""
            return text
        except UnicodeDecodeError as e:
            if e.start >= len(buf) - 3:
                self._decoder_buf = buf[e.start :]
                return buf[: e.start].decode("utf-8", "replace")
            self._decoder_buf = b""
            return buf.decode("utf-8", "replace")

    def push_text(self, text: str) -> None:
        if self._tail is None:
            self._whole.append(text)
            return
        if self._on_line is None:
            self._tail.push(text)
            return
        parts = (self._partial + text).split("\n")
        self._partial = parts.pop()
        for line in parts:
            self._tail.push(line)
            self._on_line(line)

    def finish(self) -> None:
        if self._decoder_buf:
            self.push_text(self._decoder_buf.decode("utf-8", "replace"))
            self._decoder_buf = b""
        if self._partial and self._tail is not None and self._on_line is not None:
            self._tail.push(self._partial)
            self._on_line(self._partial)
            self._partial = ""

    def text(self) -> str:
        return "".join(self._whole) if self._tail is None else str(self._tail)


def tar_directory(archive: str, d: str) -> None:
    r = subprocess.run(["tar", "-czf", archive, "-C", d, "."], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"tar exited with {r.returncode}: {r.stderr.strip()}")


def walk_files(d: str) -> List[str]:
    out = []
    for root, _dirs, files in os.walk(d):
        for f in files:
            out.append(os.path.join(root, f))
    return out


def make_tarball(d: str) -> bytes:
    fd, path = tempfile.mkstemp(prefix="sandcastle-copyin-", suffix=".tar.gz")
    os.close(fd)
    try:
        tar_directory(path, d)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def require(module: str, install: str):
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as e:
        raise ImportError(f"This sandbox provider needs the '{install}' package: pip install {install}") from e


def result(stdout: str, stderr: str, code: Optional[int]) -> ExecResult:
    return ExecResult(stdout, stderr, 1 if code is None or code < 0 else int(code))
