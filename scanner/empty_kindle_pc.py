#!/usr/bin/env python3
"""
Empty Kindle for Windows Local Downloaded Books (`Classic\\Content` & `book_asset.db`)
=====================================================================================
Closes `Kindle.exe` if running, removes all downloaded `<ASIN>_EBOK` folders from
`%LOCALAPPDATA%\\Packages\\AMZNKindle.AmazonKindleReadingApp_m1sc522ngdk36\\LocalState\\Classic\\Content`,
and clears + vacuums `book_asset.db`.
"""
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path


def empty_kindle_pc(close_app: bool = True) -> None:
    print("=" * 76)
    print(" EMPTYING KINDLE FOR WINDOWS DOWNLOADED BOOKS (Classic\\Content)")
    print("=" * 76)

    if close_app and os.name == "nt":
        subprocess.run(["taskkill", "/F", "/IM", "Kindle.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    local_app = os.environ.get("LOCALAPPDATA") or r"C:\Users\oscar\AppData\Local"
    content_dir = Path(local_app) / "Packages" / "AMZNKindle.AmazonKindleReadingApp_m1sc522ngdk36" / "LocalState" / "Classic" / "Content"
    if not content_dir.exists():
        print(f"Kindle Content directory not found: {content_dir}")
        return

    folders = [d for d in content_dir.iterdir() if d.is_dir()]
    removed = 0
    for d in folders:
        shutil.rmtree(d, ignore_errors=True)
        removed += 1

    db_path = content_dir / "book_asset.db"
    rem_books = 0
    rem_assets = 0
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        for q in ("DELETE FROM AssetDownloadInfo", "DELETE FROM Asset", "DELETE FROM BookDownloadInfo", "DELETE FROM Book"):
            try:
                cur.execute(q)
            except Exception:
                pass
        conn.commit()
        try:
            cur.execute("VACUUM")
            rem_books = cur.execute("SELECT COUNT(*) FROM Book").fetchone()[0]
            rem_assets = cur.execute("SELECT COUNT(*) FROM Asset").fetchone()[0]
        except Exception:
            pass
        conn.close()

    rem_dirs = [d for d in content_dir.iterdir() if d.is_dir()]
    print(f" Removed Book Folders : {removed}")
    print(f" Remaining Folders    : {len(rem_dirs)}")
    print(f" book_asset.db Books  : {rem_books} (Assets: {rem_assets})")
    print("=" * 76)


if __name__ == "__main__":
    empty_kindle_pc(close_app=True)
