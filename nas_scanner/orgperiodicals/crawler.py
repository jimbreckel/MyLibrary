import os
from pathlib import Path
from typing import Generator

def find_files(base_path: str, max_depth: int = 2, max_levels: int = None) -> Generator[Path, None, None]:
    """
    Search for PDF and EPUB files up to max_depth levels.
    
    max_depth = 1: search base_path only.
    max_depth = 2: search base_path and its immediate subdirectories.
    """
    if max_levels is not None:
        max_depth = max_levels
    base = Path(base_path).resolve()
    if not base.exists():
        return
        
    for root, dirs, files in os.walk(base):
        root_path = Path(root)
        try:
            depth = len(root_path.relative_to(base).parts) + 1
        except ValueError:
            continue
            
        if depth > max_depth:
            dirs.clear()
            continue
            
        if depth == max_depth:
            dirs.clear()  # Do not descend further than max_depth
            
        for file in files:
            file_path = root_path / file
            if file_path.suffix.lower() in ('.pdf', '.epub', '.cbr', '.cbz', '.webp', '.mobi', '.djvu', '.apk'):
                yield file_path
