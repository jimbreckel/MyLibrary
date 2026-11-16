import os
import re
import shutil
from pathlib import Path
from typing import Tuple, Optional, Dict, Any

from .parser import clean_title, parse_filename, extract_pdf_metadata, extract_epub_metadata
from .cover import extract_cover
from .gemini import analyze_cover
from .classifier import PeriodicalClassifier


def sanitize_folder_name(name: str) -> str:
    """Removes invalid characters for folder names across OSes and replaces separators with spaces."""
    if not name:
        return "Unknown"
    cleaned = clean_title(name)
    if not cleaned:
        cleaned = name
    sanitized = re.sub(r'[-_\.]+', ' ', cleaned)
    invalid_chars = r'[<>:"/\|?*]'
    sanitized = re.sub(invalid_chars, '', sanitized)
    sanitized = re.sub(r'\s+', ' ', sanitized)
    return sanitized.strip() or "Unknown"


def get_unique_target_path(target_path: Path) -> Path:
    """If target_path exists, append _1, _2, etc., to avoid overwrite."""
    if not target_path.exists():
        return target_path

    stem = target_path.stem
    suffix = target_path.suffix
    parent = target_path.parent

    counter = 1
    while True:
        new_path = parent / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


def organize_file(
    file_path: Path,
    dest_dir: Path,
    title: Optional[str],
    year: Optional[int],
    copy_mode: bool = False,
    dry_run: bool = False
) -> Tuple[Path, str]:
    """
    Organizes a single file based on the extracted title and year.
    Returns: Tuple[target_path, status_message]
    """
    folder_title = sanitize_folder_name(title) if title else "Unorganized"
    folder_year = str(year) if year else "Unknown"

    if folder_title == "Unorganized":
        target_subdir = dest_dir / "Unorganized"
    else:
        target_subdir = dest_dir / folder_title / folder_year

    target_path = target_subdir / file_path.name

    if not dry_run:
        target_subdir.mkdir(parents=True, exist_ok=True)
        if target_path.resolve() != file_path.resolve():
            target_path = get_unique_target_path(target_path)
    else:
        if target_path.exists() and target_path.resolve() != file_path.resolve():
            stem = target_path.stem
            suffix = target_path.suffix
            counter = 1
            while (target_subdir / f"{stem}_{counter}{suffix}").exists():
                counter += 1
            target_path = target_subdir / f"{stem}_{counter}{suffix}"

    op_name = "Copying" if copy_mode else "Moving"

    try:
        rel_target = target_path.relative_to(dest_dir)
        target_desc = f"{dest_dir.name}/{rel_target}"
    except ValueError:
        target_desc = str(target_path)

    action = f"{op_name} '{file_path.name}' -> '{target_desc}'"

    if not dry_run:
        try:
            if target_path.resolve() == file_path.resolve():
                return target_path, f"SUCCESS: Already at '{target_desc}'"
            if copy_mode:
                shutil.copy2(file_path, target_path)
            else:
                shutil.move(str(file_path), str(target_path))
            return target_path, f"SUCCESS: {action}"
        except Exception as e:
            return target_path, f"FAILED: {action} (Error: {e})"
    else:
        return target_path, f"DRY-RUN: {action}"


_SHARED_CLASSIFIER: Optional[PeriodicalClassifier] = None


def get_shared_classifier() -> PeriodicalClassifier:
    global _SHARED_CLASSIFIER
    if _SHARED_CLASSIFIER is None:
        _SHARED_CLASSIFIER = PeriodicalClassifier()
    return _SHARED_CLASSIFIER


def identify_and_organize_periodical(
    file_path: Path,
    periodicals_dir: Optional[Path] = None,
    adult_dir: Optional[Path] = None,
    gemini_key: Optional[str] = None,
    classifier: Optional[PeriodicalClassifier] = None,
    force_cover: bool = False,
    move_file: bool = False,
    force_adult: bool = False,
    force_standard: bool = False,
) -> Dict[str, Any]:
    """
    Unified pipeline for identifying a periodical's series title, publication year,
    bucket (from magazines.json), and extracting its Page-1 cover bytes.
    Optionally moves the file into `<periodicals_dir>/<Series>/<Year>/<filename>`
    (or `<adult_dir>/<Series>/<Year>/<filename>` if Adult).
    """
    clf = classifier or get_shared_classifier()
    gemini_key = gemini_key or os.environ.get("GEMINI_API_KEY")

    # Check if already inside /periodicals/<Series>/<Year>/ or /xAdult/<Series>/<Year>/
    folder_series = None
    folder_year = None
    parts = file_path.parts
    for root_marker in ("periodicals", "Periodicals", "xAdult"):
        if root_marker in parts:
            idx = parts.index(root_marker)
            rem = parts[idx + 1:]
            if len(rem) >= 2 and rem[0] not in ("Unorganized", "Unknown"):
                cand_fs = rem[0].strip()
                # Ignore contaminated or overly generic folder_series names so they get re-evaluated cleanly
                is_bad_fs = (
                    cand_fs.lower() in {
                        'computer', 'computers', 'pc', 'mac', 'linux', 'windows', 'games', 'game',
                        'science', 'history', 'home', 'music', 'art', 'photo', 'cars', 'sport', 'info',
                    }
                    or cand_fs.isdigit()
                    or bool(
                        re.search(
                            r'(?:\b(?:no|nr|issue|iss|vol|volume|heft|ausgabe)\b|#\d|\b(?:19\d\d|20\d\d)\b|\s+(?:0[1-9]|1[0-2])$)',
                            cand_fs,
                            re.IGNORECASE,
                        )
                        and cand_fs.upper() != "INFO 64"
                    )
                )
                if not is_bad_fs:
                    folder_series = cand_fs
            if len(rem) >= 3 and re.match(r"^(19\d{2}|20\d{2})$", rem[1]):
                folder_year = int(rem[1])
            break

    title: Optional[str] = None
    year: Optional[int] = None
    bucket_name: str = "Monthly and Special Interest Magazines"
    method: str = ""
    cover_bytes: Optional[bytes] = None

    parsed_title, parsed_year = parse_filename(file_path.name)

    # 1. Match against magazines.json
    matched = clf.match_filename(file_path.name)
    if not matched and folder_series:
        matched = clf.match_filename(folder_series)

    if matched and not force_cover:
        bucket_name, matched_pattern, pub_year_avail = matched
        # Prefer parsed_title if matched_pattern is a shorter prefix/substring of parsed_title (e.g. 'PC Games' vs 'PC Games Hardware')
        if (
            parsed_title
            and len(parsed_title) > len(matched_pattern)
            and matched_pattern.lower() in parsed_title.lower()
        ):
            title = parsed_title
        else:
            title = folder_series or matched_pattern
        method = "magazines.json pattern match"
        if pub_year_avail:
            extracted_year = parsed_year or folder_year
            if not extracted_year:
                ext = file_path.suffix.lower()
                if ext == ".pdf":
                    _, extracted_year = extract_pdf_metadata(file_path)
                elif ext == ".epub":
                    _, extracted_year = extract_epub_metadata(file_path)
            if not extracted_year and gemini_key:
                cover_bytes = extract_cover(file_path)
                if cover_bytes:
                    _, c_year = analyze_cover(cover_bytes, gemini_key)
                    extracted_year = c_year
            year = extracted_year
    else:
        detected_title, detected_year = None, None
        if not force_cover:
            detected_title, detected_year = parsed_title, parsed_year
            if folder_series and (not detected_title or len(detected_title) < 3):
                detected_title = folder_series
            if folder_year and not detected_year:
                detected_year = folder_year
            if detected_title and detected_year:
                method = "Filename / folder parsing"

        if not (detected_title and detected_year) and not force_cover:
            ext = file_path.suffix.lower()
            if ext == ".pdf":
                m_title, m_year = extract_pdf_metadata(file_path)
                detected_title = detected_title or m_title
                detected_year = detected_year or m_year
            elif ext == ".epub":
                m_title, m_year = extract_epub_metadata(file_path)
                detected_title = detected_title or m_title
                detected_year = detected_year or m_year
            if detected_title and detected_year and not method:
                method = "Internal metadata parsing"

        if (not (detected_title and detected_year) or force_cover) and gemini_key:
            cover_bytes = extract_cover(file_path)
            if cover_bytes:
                c_title, c_year = analyze_cover(cover_bytes, gemini_key)
                detected_title = c_title or detected_title
                detected_year = c_year or detected_year
                method = "Gemini cover analysis"

        if not detected_title:
            if folder_series:
                detected_title = folder_series
                method = method or "Folder hierarchy"
            else:
                parent = file_path.parent.name
                if parent and not (len(parent) == 3 and parent.isupper()) and parent.lower() not in ("tomags", "xtoadult", "periodicals", "xadult", "unorganized"):
                    detected_title = clean_title(parent) or parent
                else:
                    detected_title = clean_title(file_path.stem) or file_path.stem
                method = method or "Filename fallback"

        detected_year = detected_year or folder_year
        bucket_name = clf.classify_new_title(detected_title, file_path, detected_year is not None)
        title = detected_title
        year = detected_year

    if force_standard:
        if bucket_name == "Adult Periodicals":
            bucket_name = "Monthly and Special Interest Magazines"
    elif force_adult or "xadult" in str(file_path).lower() or "xtoadult" in str(file_path).lower():
        bucket_name = "Adult Periodicals"

    # Always ensure Page-1 cover is extracted for magazines
    if cover_bytes is None:
        cover_bytes = extract_cover(file_path)

    clean_series = sanitize_folder_name(title) if title else (folder_series or "Unorganized")
    target_path = file_path
    status_msg = "IDENTIFIED"

    if move_file:
        is_adult = False if force_standard else ((bucket_name == "Adult Periodicals") or force_adult)
        dest_root = adult_dir if (is_adult and adult_dir) else periodicals_dir
        if dest_root:
            target_path, status_msg = organize_file(
                file_path=file_path,
                dest_dir=dest_root,
                title=clean_series,
                year=year,
                copy_mode=False,
                dry_run=False,
            )

    return {
        "series_title": clean_series,
        "year": year,
        "bucket": bucket_name,
        "method": method,
        "cover_bytes": cover_bytes,
        "target_path": target_path,
        "status_msg": status_msg,
    }
