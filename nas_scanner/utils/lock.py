import os
import sys

class LockError(Exception):
    """Exception raised when lock acquisition fails."""
    pass

class ScannerLock:
    def __init__(self, lock_file_path):
        self.lock_file_path = os.path.abspath(lock_file_path)
        self.fd = None

    def acquire(self):
        """
        Acquire the lock. Raises LockError if another process holds the lock,
        or if we cannot open/create the lock file.
        """
        try:
            self.fd = os.open(self.lock_file_path, os.O_CREAT | os.O_WRONLY)
        except Exception as e:
            raise LockError(f"Could not open/create lock file {self.lock_file_path}: {e}")

        if os.name == 'nt':
            import msvcrt
            try:
                # Lock 1 byte at the start of the file. LK_NBLCK is non-blocking lock.
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            except (IOError, OSError):
                self.close()
                raise LockError("Another instance of the scanner is already running (failed to acquire lock).")
        else:
            import fcntl
            try:
                # Exclusive non-blocking lock
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError):
                self.close()
                raise LockError("Another instance of the scanner is already running (failed to acquire lock).")
        
        # Write current process ID to the lock file for diagnostic purposes
        try:
            os.ftruncate(self.fd, 0)
            os.write(self.fd, f"{os.getpid()}\n".encode('utf-8'))
        except Exception:
            pass

    def release(self):
        if self.fd is not None:
            if os.name == 'nt':
                import msvcrt
                try:
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                except Exception:
                    pass
            self.close()
            try:
                os.remove(self.lock_file_path)
            except Exception:
                pass

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
