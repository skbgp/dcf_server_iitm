"""Build a Bubblewrap command and supervise its output and process group."""
from functools import lru_cache
from pathlib import Path
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time

MAX_OUTPUT_BYTES = 1024 * 1024
_LIMITS = Path(__file__).with_name("sandbox_limits.py").resolve()
DEVELOPMENT_MODE = os.environ.get("DCF_ALLOW_UNSANDBOXED") == "1"


class OutputLimitExceeded(Exception):
    pass


def bubblewrap():
    command = [
        "bwrap", "--unshare-all", "--unshare-user", "--disable-userns",
        "--die-with-parent", "--cap-drop", "ALL", "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LC_ALL", "C",
    ]
    for directory in ("/usr", "/bin", "/lib", "/lib64"):
        if os.path.isdir(directory):
            command += ["--ro-bind", directory, directory]
    return command + ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]


@lru_cache(maxsize=1)
def require_sandbox():
    if DEVELOPMENT_MODE:
        return
    if not sys.platform.startswith("linux") or not shutil.which("bwrap"):
        raise RuntimeError("Linux and Bubblewrap are required for grading")
    if os.geteuid() == 0:
        raise RuntimeError("Run the grader as a dedicated non-root user")
    try:
        probe = run_bounded(bubblewrap() + ["g++", "--version"], timeout=10, memory_mib=512)
    except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded) as exc:
        raise RuntimeError("Bubblewrap preflight could not run") from exc
    if probe.returncode:
        raise RuntimeError(f"Bubblewrap preflight failed: {probe.stderr.decode(errors='replace').strip()}")


def run_bounded(command, *, timeout, memory_mib, cwd=None, stdin=None):
    args = [sys.executable, str(_LIMITS), str(memory_mib * 1024 * 1024), *command]
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        args, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, start_new_session=True,
    ) as process:
        try:
            streams = {process.stdout: bytearray(), process.stderr: bytearray()}
            size = 0
            with selectors.DefaultSelector() as selector:
                for stream in streams:
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    events = selector.select(remaining)
                    if not events:
                        raise subprocess.TimeoutExpired(command, timeout)
                    for key, _ in events:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        size += len(chunk)
                        if size > MAX_OUTPUT_BYTES:
                            raise OutputLimitExceeded()
                        streams[key.fileobj].extend(chunk)
            process.wait(timeout=max(0, deadline - time.monotonic()))
            return subprocess.CompletedProcess(
                command, process.returncode, bytes(streams[process.stdout]), bytes(streams[process.stderr])
            )
        finally:
            # A descendant can keep pipes or resources open after its parent exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
