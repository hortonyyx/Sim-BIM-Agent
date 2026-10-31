"""OS-backed exclusive file locks with the same busy error on every platform.

Portalocker uses flock on POSIX and LockFileEx on Windows. A lock remains held
until explicitly unlocked or its handle closes; it is never a no-op fallback.
"""

import errno

import portalocker

LOCK_EX = portalocker.LOCK_EX
LOCK_NB = portalocker.LOCK_NB
LOCK_UN = portalocker.LOCK_UN


def flock(file, operation):
    if operation == LOCK_UN:
        portalocker.unlock(file)
        return
    try:
        portalocker.lock(file, operation)
    except portalocker.exceptions.AlreadyLocked as exc:
        raise BlockingIOError(errno.EAGAIN, "file is already locked") from exc
