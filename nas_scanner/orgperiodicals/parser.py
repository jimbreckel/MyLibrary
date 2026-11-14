import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Tuple, Optional
import pymupdf

# Regex patterns for date extraction
MONTHS_PATTERN = (
    r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|'
    r'aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
)

DATE_PATTERNS = [
    # DD Month YYYY or DD_Month_YYYY or DD.Month.YYYY
    (re.compile(rf'(?<![a-zA-Z0-9])(0?[1-9]|[12]\d|3[01])[-_\s\.]+({MONTHS_PATTERN})[-_\s\.]+(19\d\d|20\d\d)(?![a-zA-Z0-9])', re.IGNORECASE), "dd-month-yyyy"),
    # Month DD YYYY or Month_DD_YYYY or Month.DD.YYYY
    (re.compile(rf'(?<![a-zA-Z0-9])({MONTHS_PATTERN})[-_\s\.,]+(0?[1-9]|[12]\d|3[01])[-_\s\.,]+(19\d\d|20\d\d)(?![a-zA-Z0-9])', re.IGNORECASE), "month-dd-yyyy"),
    # YYYY-MM-DD or YYYY_MM_DD or YYYY.MM.DD or YYYYMMDD
    (re.compile(r'(?<![a-zA-Z0-9])(19\d\d|20\d\d)[-_\.]?(0[1-9]|1[0-2])[-_\.]?(0[1-9]|[12]\d|3[01])(?![0-9])', re.IGNORECASE), "yyyy-mm-dd"),
    # DD-MM-YYYY or DD_MM_YYYY or DD.MM.YYYY
    (re.compile(r'(?<![a-zA-Z0-9])(0[1-9]|[12]\d|3[01])[-_\.](0[1-9]|1[0-2])[-_\.](19\d\d|20\d\d)(?![a-zA-Z0-9])', re.IGNORECASE), "dd-mm-yyyy"),
    # YYYY-MM or YYYY_MM or YYYY.MM
    (re.compile(r'(?<![a-zA-Z0-9])(19\d\d|20\d\d)[-_\.](0[1-9]|1[0-2])(?![a-zA-Z0-9])', re.IGNORECASE), "yyyy-mm"),
    # MM-YYYY or MM_YYYY or MM.YYYY
    (re.compile(r'(?<![a-zA-Z0-9])(0[1-9]|1[0-2])[-_\.](19\d\d|20\d\d)(?![a-zA-Z0-9])', re.IGNORECASE), "mm-yyyy"),
    # Month YYYY or Month_YYYY or Month.YYYY
    (re.compile(rf'(?<![a-zA-Z0-9])({MONTHS_PATTERN})[-_\s\.]+(19\d\d|20\d\d)(?![a-zA-Z0-9])', re.IGNORECASE), "month-yyyy"),
    # YYYY Month or YYYY_Month or YYYY.Month
    (re.compile(rf'(?<![a-zA-Z0-9])(19\d\d|20\d\d)[-_\s\.]+({MONTHS_PATTERN})(?![a-zA-Z0-9])', re.IGNORECASE), "yyyy-month"),
    # YYYY (Single Year)
    (re.compile(r'(?<![a-zA-Z0-9])(19\d\d|20\d\d)(?![a-zA-Z0-9])', re.IGNORECASE), "yyyy")
]

def clean_title(title: str) -> str:
    """Sanitize the extracted title by removing special characters, generic suffixes, and extra spaces."""
    if not title:
        return ""
    # Replace dots, underscores, hyphens, and multiple spaces with a single space
    title = re.sub(r'[-_\.\s]+', ' ', title)
    
    # Strip off known metadata tags and site names case-insensitively
    junk_patterns = [
        r'\btruepdf\b',
        r'\bpdf\b',
        r'\bdownmagaz\s*(?:net|com)?\b',
        r'\bfreemagazines\s*(?:top)?\b',
        r'\bdownmagaz\b',
        r'\bmagazines\b',
        r'\bmagazine\b',
    ]
    for jp in junk_patterns:
        title = re.sub(jp, '', title, flags=re.IGNORECASE)
        
    # Remove leading/trailing non-alphanumeric chars except brackets
    title = re.sub(r'^[^a-zA-Z0-9\(]+|[^a-zA-Z0-9\)]+$', '', title)
    # Replace multiple spaces
    title = re.sub(r'\s+', ' ', title)
    return title.strip()

def parse_filename(filename: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Attempt to extract the publication title and publication year from the filename.
    
    Returns:
        Tuple[title, year]
    """
    # Remove extension
    stem = Path(filename).stem
    
    for pattern, pat_type in DATE_PATTERNS:
        match = pattern.search(stem)
        if match:
            # Determine which group is the year based on pattern type
            if pat_type in ("yyyy-mm-dd", "yyyy-mm", "yyyy-month", "yyyy"):
                year_val = int(match.group(1))
            elif pat_type in ("dd-mm-yyyy", "dd-month-yyyy", "month-dd-yyyy"):
                year_val = int(match.group(3))
            elif pat_type in ("mm-yyyy", "month-yyyy"):
                year_val = int(match.group(2))
            else:
                continue
                
            if not (1900 <= year_val <= 2099):
                continue
                
            # Extract title from the parts around the match
            start, end = match.span()
            before = stem[:start].strip()
            after = stem[end:].strip()
            
            # Decide which part represents the title
            title_candidate = before if len(before) > len(after) else after
            if not title_candidate and (before or after):
                title_candidate = before or after
                
            cleaned = clean_title(title_candidate)
            if cleaned:
                return cleaned, year_val
                
    return None, None

def parse_pdf_date(date_str: Optional[str]) -> Optional[int]:
    """Parse 4-digit year from PDF date metadata (often in format D:YYYYMMDD...)"""
    if not date_str:
        return None
    if date_str.startswith('D:'):
        date_str = date_str[2:]
    match = re.search(r'\b(19\d\d|20\d\d)\b', date_str)
    if match:
        return int(match.group(1))
    if len(date_str) >= 4 and date_str[:4].isdigit():
        year = int(date_str[:4])
        if 1900 <= year <= 2099:
            return year
    return None

def extract_pdf_metadata(pdf_path: Path) -> Tuple[Optional[str], Optional[int]]:
    """Extract title and year from PDF metadata."""
    try:
        doc = pymupdf.open(pdf_path)
        meta = doc.metadata
        title = meta.get('title')
        creation_date = meta.get('creationDate')
        mod_date = meta.get('modDate')
        
        year = parse_pdf_date(creation_date) or parse_pdf_date(mod_date)
        
        cleaned_title = clean_title(title) if title else None
        
        # If the title is generic (like "untitled", "layout", "scan"), ignore it
        if cleaned_title and cleaned_title.lower() in ("untitled", "document", "layout", "scan", "microsoft word"):
            cleaned_title = None
            
        doc.close()
        return cleaned_title, year
    except Exception as e:
        # Fallback if fitz fails to read
        return None, None

def extract_epub_metadata(epub_path: Path) -> Tuple[Optional[str], Optional[int]]:
    """Extract title and year from EPUB metadata using standard zipfile/xml parsing."""
    try:
        with zipfile.ZipFile(epub_path, 'r') as z:
            # 1. Read container.xml to locate the OPF file
            container_content = z.read('META-INF/container.xml')
            root = ET.fromstring(container_content)
            
            opf_path = None
            for elem in root.iter():
                if elem.tag.endswith('rootfile'):
                    opf_path = elem.attrib.get('full-path')
                    break
            
            if not opf_path:
                return None, None
                
            # 2. Read OPF file
            opf_content = z.read(opf_path)
            opf_root = ET.fromstring(opf_content)
            
            title = None
            year = None
            
            for elem in opf_root.iter():
                tag_local = elem.tag.split('}')[-1]
                if tag_local == 'title':
                    title = elem.text
                elif tag_local == 'date':
                    date_str = elem.text
                    if date_str:
                        match = re.search(r'\b(19\d\d|20\d\d)\b', date_str)
                        if match:
                            year = int(match.group(1))
                            
            cleaned_title = clean_title(title) if title else None
            return cleaned_title, year
    except Exception:
        return None, None
