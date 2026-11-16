import errno
import os
from pathlib import Path


class InstanceAlreadyRunningError(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self._file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
        except OSError as error:
            lock_file.close()
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(
                error, "winerror", None
            ) == 33:
                raise InstanceAlreadyRunningError(
                    "Another Twitch Live Organizer instance is already running."
                ) from error
            raise

        self._file = lock_file
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(str(os.getpid()).encode("ascii"))
            lock_file.flush()
        except Exception:
            self.release()
            raise

    def release(self):
        if self._file is None:
            return

        lock_file = self._file
        self._file = None
        try:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        self.release()
