"""Small filesystem helpers shared by the web process and grading workers."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import os
import tempfile


@contextmanager
def locked(path):
    with open(path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextmanager
def replace_text(path):
    # The launcher creates symlinks in .active_lab. Replace their targets.
    path = Path(path).resolve()
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False,
        ) as stream:
            temp_name = stream.name
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
