"""
A simple cross-process lock for a list file.

ytdl (downloader), the web UI and the Channel Manager can all rewrite the same
list file. The lock is a small '<list>.lock' file created exclusively, so only
one process changes a list at a time. A lock older than STALE_SECONDS is
assumed to be left behind by a crash and is removed.
"""
import os
import time
from contextlib import contextmanager
from pathlib import Path

STALE_SECONDS = 60
WAIT_SECONDS = 15


@contextmanager
def list_lock(listfile):
    lock = Path(listfile).with_suffix(".lock")
    deadline = time.time() + WAIT_SECONDS
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > STALE_SECONDS:
                    lock.unlink()          # left behind by a crash
                    continue
            except OSError:
                continue
            if time.time() > deadline:
                raise TimeoutError(f"{Path(listfile).name} is busy (lock: {lock.name})")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
