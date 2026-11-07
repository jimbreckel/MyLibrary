"""File utility functions for preserving timestamps and filesystem attributes."""

from __future__ import annotations
import os
import logging
from pathlib import Path
import shutil
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def preserve_file_ownership_and_mode(src: Path, dst: Path) -> None:
    """Copy POSIX permission bits (chmod) and UID/GID ownership (chown) from src to dst."""
    try:
        shutil.copymode(src, dst)
    except Exception as e:
        logger.debug("Failed copying file mode from %s to %s: %s", src.name, dst.name, e)

    if hasattr(os, "chown"):
        try:
            st = src.stat()
            os.chown(dst, st.st_uid, st.st_gid)
        except Exception as e:
            logger.debug("Failed preserving file ownership (uid/gid) for %s: %s", dst.name, e)


def resolve_mirrored_destination(
    file_path: Path,
    dest_root: Path,
    scan_root: Optional[Path] = None,
) -> Path:
    """
    Compute a destination path inside `dest_root` that mirrors `file_path`'s relative
    subfolder path under `scan_root` (e.g. Books/SciFi/Dune.epub -> Books_bad/bak/SciFi/Dune.epub).
    """
    resolved_file = Path(file_path).resolve()
    resolved_dest = Path(dest_root).resolve()
    if scan_root:
        resolved_scan = Path(scan_root).resolve()
        if resolved_scan.is_file():
            resolved_scan = resolved_scan.parent
        try:
            rel = resolved_file.relative_to(resolved_scan)
            return resolved_dest / rel
        except ValueError:
            pass
    return resolved_dest / resolved_file.name


def get_file_timestamps(path: Path) -> Tuple[float, float, float]:
    """Return (atime, mtime, ctime) for a given file."""
    st = path.stat()
    ctime = getattr(st, "st_ctime", st.st_mtime)
    return st.st_atime, st.st_mtime, ctime


def restore_file_timestamps(path: Path, timestamps: Tuple[float, float, float]) -> None:
    """
    Restore access time, modification time, and creation time (on Windows)
    to keep file attributes identical after in-place updates.
    """
    atime, mtime, ctime = timestamps
    try:
        # 1. Restore modification and access times
        os.utime(path, (atime, mtime))
    except Exception as e:
        logger.debug("Failed restoring utime for %s: %s", path.name, e)

    # 2. On Windows, restore creation time using kernel32.SetFileTime
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            FILE_WRITE_ATTRIBUTES = 0x0100
            OPEN_EXISTING = 3
            FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

            handle = ctypes.windll.kernel32.CreateFileW(
                str(path.resolve()),
                FILE_WRITE_ATTRIBUTES,
                0,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
            if handle != -1 and handle != 0xFFFFFFFF:
                try:
                    # Windows FILETIME is 100-nanosecond intervals since Jan 1, 1601 UTC
                    c_time_int = int((ctime + 11644473600) * 10000000)
                    c_filetime = wintypes.FILETIME(c_time_int & 0xFFFFFFFF, c_time_int >> 32)
                    ctypes.windll.kernel32.SetFileTime(handle, ctypes.byref(c_filetime), None, None)
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
        except Exception as win_err:
            logger.debug("Failed restoring Windows creation time for %s: %s", path.name, win_err)
