"""Advisory process locks with the same call contract on Windows and Unix."""
import os

if os.name != "nt":
    from fcntl import flock, LOCK_EX, LOCK_NB, LOCK_UN
else:
    import errno
    import msvcrt
    import time

    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def flock(handle, operation):
        fd = handle if isinstance(handle, int) else handle.fileno()
        position = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            while True:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK if operation & LOCK_UN else msvcrt.LK_NBLCK, 1)
                    return
                except OSError as error:
                    if operation & LOCK_UN or error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if operation & LOCK_NB:
                        raise BlockingIOError(errno.EAGAIN, "File is locked") from error
                    time.sleep(0.05)
        finally:
            os.lseek(fd, position, os.SEEK_SET)
