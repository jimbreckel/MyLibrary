import base64
import datetime
import io
import os
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BOOK_EXTENSIONS = {
    '.epub', '.pdf', '.mobi', '.azw3', '.cbz', '.cbr',
    '.djvu', '.fb2', '.txt', '.rtf', '.chm', '.doc', '.docx',
}

_GARBAGE_META_VALUES = {
    '', 'unknown', 'unknown author', 'untitled', 'none', 'null', 'n/a', '..', '.',
    'acrobat distiller', 'adobe indesign', 'microsoft word', 'calibre', 'quarkxpress',
}

# Explicit subject/tag aliases -> canonical `/books/<Category>` folder name
_SUBJECT_FOLDER_ALIASES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'^(?:chess|chess\s+library)$', re.I), 'Chess Library'),
    (re.compile(r'^(?:china|chinese|chinese[\s\-]*china)$', re.I), 'Chinese-China'),
    (re.compile(r'^(?:japan|japanese|manga|anime)$', re.I), 'Japan'),
    (re.compile(r'^(?:recipe|recipes|cooking|cookbook|cookbooks|culinary|food|baking)$', re.I), 'Recipes'),
    (re.compile(r'^(?:political|politics|government|geopolitics|current\s+affairs|world\s+affairs)$', re.I), 'Political'),
    (re.compile(r'^(?:religion|religious|theology|bible|christianity|islam|judaism|buddhism|spirituality|religon\s+related\s+texts)$', re.I), 'Religon related texts'),
    (re.compile(r'^(?:rpg|role\s*playing\s*games?|d&d|dungeons\s*(?:&|and)\s*dragons|tabletop\s+rpg|pathfinder)$', re.I), 'Role Playing Games'),
    (re.compile(r'^(?:game\s+design|game\s+engine|game\s+development|game\s+programming|game\s+engine\s+and\s+game\s+design|gamedev)$', re.I), 'Game Engine and Game Design'),
    (re.compile(r'^(?:electronics?|electronic\s+schematics?|schematics?|circuits?|arduino|raspberry\s+pi|ham\s+radio|microcontrollers?)$', re.I), 'Electronic Schematics'),
    (re.compile(r'^(?:mainframe|mainframe\s+files|ibm\s+z|z/os|cobol|jcl|cics|db2)$', re.I), 'MainFrame Files'),
    (re.compile(r'^(?:memory|mnemonics|memory\s+improvement)$', re.I), 'Memory'),
    (re.compile(r'^(?:jokes?|humor|humour|comedy)$', re.I), 'Jokes'),
    (re.compile(r'^(?:sci[\s\-]*fi|science\s+fiction|sf|cyberpunk|space\s+opera)$', re.I), 'Science Fiction'),
    (re.compile(r'^(?:computer\s+hardware|pc\s+hardware|hardware)$', re.I), 'Computer Hardware'),
    (re.compile(r'^(?:file\s+formats?)$', re.I), 'File Formats'),
    (re.compile(r'^(?:computer|computers|programming|software|coding|it|tech|technology|computer\s+science|linux|python|java|cybersecurity|security|ai|machine\s+learning|data\s+science|computer\s+mags\s*&\s*faq)$', re.I), 'Computer Mags & FAQ'),
    (re.compile(r'^(?:fiction|novel|novels|fantasy|mystery|thriller|romance|horror|literature|general\s+fiction|historical\s+fiction)$', re.I), 'Fiction'),
    (re.compile(r'^(?:misc|miscellaneous|general|non[\s\-]*fiction|nonfiction)$', re.I), 'Misc'),
]

# Domain rules evaluated against combined `subjects + title + publisher + description`
_DOMAIN_RULES: List[Tuple[str, re.Pattern, int]] = [
    ('Chess Library', re.compile(r'\b(chess|grandmaster|checkmate|endgame|opening\s+repertoire|sicilian|fide|kasparov|carlsen|fischer)\b', re.I), 60),
    ('Chinese-China', re.compile(r'\b(china|chinese|beijing|shanghai|taiwan|hong\s+kong|mandarin|ccp|mao\s+zedong|confuci|dynasty|shenzhen)\b', re.I), 55),
    ('Japan', re.compile(r'\b(japan|japanese|tokyo|kyoto|samurai|shogun|anime|manga|zen\s+buddh|bushido|okinawa)\b', re.I), 55),
    ('Recipes', re.compile(r'\b(recipe|recipes|cookbook|cooking|baking|culinary|kitchen|chef|gastronomy|slow\s+cooker|instant\s+pot|air\s+fryer|bread\s+making|desserts?)\b', re.I), 58),
    ('Role Playing Games', re.compile(r'\b(role[\s\-]*playing|rpg|dungeons\s*(?:&|and)\s*dragons|d&d|pathfinder|warhammer|shadowrun|gurps|call\s+of\s+cthulhu|tabletop|gamemaster|dungeon\s+master|campaign\s+setting|player\'?s\s+handbook|monster\s+manual)\b', re.I), 60),
    ('Game Engine and Game Design', re.compile(r'\b(game\s+engine|game\s+design|game\s+development|game\s+programming|game\s+artists?|game\s+art|unity\s*3d|unreal\s+engine|godot|directx|opengl|vulkan|shader|3d\s+graphics|3d\s+modeling.*game|2d\s+artwork.*game|pygame|level\s+design)\b', re.I), 60),
    ('MainFrame Files', re.compile(r'\b(mainframe|ibm\s+z|z/os|cobol|jcl|cics|db2|vsam|tso|ispf|as/400|system/360|system/370|system/390|rexx)\b', re.I), 60),
    ('Electronic Schematics', re.compile(r'\b(schematic|circuit\s+design|electronics|transistor|oscilloscope|arduino|raspberry\s+pi|microcontroller|pcb|amplifi|voltage|resistor|capacitor|soldering|ham\s+radio|datasheet|semiconductor|fpga|vhdl|verilog)\b', re.I), 58),
    ('Computer Hardware', re.compile(r'\b(computer\s+hardware|pc\s+hardware|hardware\s+hacking|motherboard|bios|uefi|microprocessor|x86|arm64|upgrading\s+and\s+repairing)\b', re.I), 56),
    ('Memory', re.compile(r'\b(memory\s+improvement|mnemonics|memorization|brain\s+training|speed\s+reading)\b', re.I), 56),
    ('Jokes', re.compile(r'\b(joke\s+book|jokes|humour|comedy|funny|satire|parody|riddles|cartoons)\b', re.I), 56),
    ('Religon related texts', re.compile(r'\b(religio|theolog|bible|biblical|christian|church|jesus|islam|quran|koran|muslim|buddhis|hindu|judaism|jewish|torah|talmud|spiritual|faith|gospel|scripture)\b', re.I), 55),
    ('Political', re.compile(r'\b(politic|government|democracy|authoritarian|geopolitic|foreign\s+policy|diplomacy|statecraft|election|globalization|capitalism|socialism|marx|governance|public\s+policy|international\s+relations|imperialism|constitution)\b', re.I), 52),
    ('Science Fiction', re.compile(r'\b(science\s+fiction|sci[\s\-]*fi|cyberpunk|space\s+opera|dystopia|post[\s\-]*apocalyp|time\s+travel|starship|galactic|extraterrestrial|asimov|heinlein|arthur\s+c\.?\s*clarke|philip\s+k\.?\s*dick|frank\s+herbert|dune|star\s+wars|star\s+trek)\b', re.I), 52),
    ('Computer Mags & FAQ', re.compile(r'\b(programming|software\s+engineering|computer\s+science|python|java|javascript|typescript|rust|golang|c\+\+|c#|linux|unix|database|sql|cloud\s+computing|kubernetes|docker|artificial\s+intelligence|machine\s+learning|deep\s+learning|data\s+science|data\s+engineering|cybersecurity|networking|devops|algorithm|o\'reilly|packt|manning|no\s+starch|addison[\s\-]*wesley|apress|pragmatic\s+bookshelf|wrox|sybex)\b', re.I), 48),
    ('Fiction', re.compile(r'\b(fiction|a\s+novel|fantasy|mystery|thriller|romance|horror|detective|adventure|saga|trilogy|chronicles|harpercollins|tor\s+books|st\.?\s*martin\'?s|penguin\s+random\s+house)\b', re.I), 45),
]


def _clean_str(val: Any) -> str:
    s = re.sub(r'\s+', ' ', str(val or '')).strip()
    if s.lower() in _GARBAGE_META_VALUES:
        return ''
    return s


def _parse_date_str(raw: str) -> Optional[datetime.date]:
    s = (raw or '').strip()
    if not s:
        return None
    m_iso = re.search(r'\b(1[89]\d{2}|20\d{2})[-/](0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])\b', s)
    if m_iso:
        try:
            return datetime.date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except Exception:
            pass
    m_yr = re.search(r'\b(1[89]\d{2}|20\d{2})\b', s)
    if m_yr:
        try:
            return datetime.date(int(m_yr.group(1)), 1, 1)
        except Exception:
            pass
    return None


def _make_jpeg_data_uri(img_bytes: bytes) -> Optional[str]:
    if not img_bytes:
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(img_bytes)) as im:
            im.thumbnail((360, 540))
            if im.mode not in ('RGB', 'L'):
                im = im.convert('RGB')
            buf = io.BytesIO()
            im.save(buf, format='JPEG', quality=82)
            b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
            return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return None


def extract_book_metadata_for_routing(file_path: Path) -> Dict[str, Any]:
    """
    Extract embedded metadata (title, authors, subjects/tags, publisher, pub_date, isbn,
    description, cover_thumbnail) from an EPUB or PDF file, falling back to filename parsing.
    """
    ext = file_path.suffix.lower()
    emb_title = ''
    emb_authors: List[str] = []
    emb_subjects: List[str] = []
    emb_publisher = ''
    emb_pub_date: Optional[datetime.date] = None
    emb_isbn = ''
    emb_asin = ''
    emb_desc = ''
    cover_data_uri: Optional[str] = None

    if ext == '.epub':
        try:
            with zipfile.ZipFile(file_path, 'r') as zf:
                opf_path = None
                try:
                    container_xml = zf.read('META-INF/container.xml')
                    c_root = ET.fromstring(container_xml)
                    for el in c_root.iter():
                        if el.tag.endswith('rootfile') and el.attrib.get('full-path'):
                            opf_path = el.attrib['full-path']
                            break
                except Exception:
                    pass
                if not opf_path:
                    for name in zf.namelist():
                        if name.lower().endswith('.opf'):
                            opf_path = name
                            break
                if opf_path and opf_path in zf.namelist():
                    opf_xml = zf.read(opf_path)
                    root = ET.fromstring(opf_xml)
                    for el in root.iter():
                        tag_low = el.tag.split('}')[-1].lower()
                        txt = (el.text or '').strip()
                        if tag_low == 'meta' and not emb_asin:
                            m_name = (el.attrib.get('name') or el.attrib.get('property') or '').lower()
                            if m_name in ('mobi-asin', 'asin', 'amazon') or 'asin' in m_name:
                                m_val = (el.attrib.get('content') or txt or '').strip().upper()
                                if re.match(r'^[A-Z0-9]{10}$', m_val):
                                    emb_asin = m_val
                        if not txt:
                            continue
                        if tag_low == 'title' and not emb_title:
                            emb_title = _clean_str(txt)
                        elif tag_low == 'creator':
                            c_a = _clean_str(txt)
                            if c_a and c_a not in emb_authors:
                                emb_authors.append(c_a)
                        elif tag_low == 'subject':
                            for part in re.split(r'[;,/]+', txt):
                                c_s = _clean_str(part)
                                if c_s and c_s not in emb_subjects:
                                    emb_subjects.append(c_s)
                        elif tag_low == 'publisher' and not emb_publisher:
                            emb_publisher = _clean_str(txt)
                        elif tag_low == 'date' and not emb_pub_date:
                            emb_pub_date = _parse_date_str(txt)
                        elif tag_low == 'description' and not emb_desc:
                            emb_desc = re.sub(r'<[^>]+>', ' ', txt).strip()
                        elif tag_low == 'identifier':
                            scheme = (el.attrib.get('{http://www.idpf.org/2007/opf}scheme') or el.attrib.get('scheme') or '').lower()
                            m_asin = re.search(r'(?:urn:asin:|asin:|amazon:)?\b(B[0-9A-Z]{9})\b', txt, re.IGNORECASE)
                            if m_asin and not emb_asin:
                                emb_asin = m_asin.group(1).upper()
                            elif scheme in ('mobi-asin', 'asin', 'amazon') and re.match(r'^[A-Z0-9]{10}$', txt, re.IGNORECASE) and not emb_asin:
                                emb_asin = txt.upper()
                            if not emb_isbn:
                                clean_id = re.sub(r'[^0-9Xx]', '', txt)
                                if len(clean_id) in (10, 13) and (clean_id.startswith(('978', '979')) or len(clean_id) == 10):
                                    emb_isbn = clean_id.upper()

                    # Extract cover image from EPUB if present
                    opf_dir = os.path.dirname(opf_path)
                    cover_href = None
                    cover_id = None
                    for el in root.iter():
                        t_low = el.tag.split('}')[-1].lower()
                        if t_low == 'meta' and el.attrib.get('name', '').lower() == 'cover':
                            cover_id = el.attrib.get('content')
                        elif t_low == 'item' and 'cover-image' in el.attrib.get('properties', '').lower():
                            cover_href = el.attrib.get('href')
                    if not cover_href and cover_id:
                        for el in root.iter():
                            if el.tag.split('}')[-1].lower() == 'item' and el.attrib.get('id') == cover_id:
                                cover_href = el.attrib.get('href')
                                break
                    if not cover_href:
                        for name in zf.namelist():
                            if re.search(r'cover.*\.(?:jpe?g|png|webp)$', name, re.I):
                                cover_href = name
                                opf_dir = ''
                                break
                    if cover_href:
                        cand_zip_path = f"{opf_dir}/{cover_href}".lstrip('/') if opf_dir else cover_href.lstrip('/')
                        if cand_zip_path in zf.namelist():
                            cover_data_uri = _make_jpeg_data_uri(zf.read(cand_zip_path))
        except Exception:
            pass

    elif ext == '.pdf':
        try:
            import fitz
            doc = fitz.open(file_path)
            meta = doc.metadata or {}
            t_cand = _clean_str(meta.get('title'))
            if t_cand and len(t_cand) >= 3 and not t_cand.lower().endswith(('.pdf', '.doc', '.indd', '.qxd')):
                emb_title = t_cand
            a_cand = _clean_str(meta.get('author'))
            if a_cand and len(a_cand) >= 2:
                emb_authors = [a.strip() for a in re.split(r'[;&]+', a_cand) if a.strip()]
            for k in ('subject', 'keywords'):
                raw_kw = meta.get(k) or ''
                for part in re.split(r'[;,/]+', raw_kw):
                    c_s = _clean_str(part)
                    if c_s and c_s not in emb_subjects:
                        emb_subjects.append(c_s)
            emb_pub_date = _parse_date_str(meta.get('creationDate') or '')
            if len(doc) > 0:
                page = doc[0]
                pix = page.get_pixmap(dpi=130)
                cover_data_uri = _make_jpeg_data_uri(pix.tobytes('jpeg'))
            doc.close()
        except Exception:
            pass

    # Fallback filename parsing
    fn_title = ''
    fn_authors: List[str] = []
    fn_isbn = ''
    fn_pub_date: Optional[datetime.date] = None
    try:
        from epubmeta.filename_parser import parse_filename
        pf = parse_filename(file_path.name)
        fn_title = _clean_str(pf.get('title'))
        if pf.get('authors'):
            fn_authors = [_clean_str(a) for a in pf['authors'] if _clean_str(a)]
        fn_isbn = _clean_str(pf.get('isbn'))
        if pf.get('published_date'):
            fn_pub_date = _parse_date_str(str(pf['published_date']))
    except Exception:
        pass

    if not fn_title:
        stem = file_path.stem
        stem = re.sub(r'[._]+', ' ', stem).strip()
        fn_title = stem or file_path.name

    # If the embedded title has a different volume/level number than the filename title
    # (e.g. publisher copy-pasted "HSK Level 1" inside the OPF of "HSK Level 2" / "HSK Level 3"),
    # prefer the filename title so volumes don't collide.
    if emb_title and fn_title:
        emb_nums = re.findall(r'\b(?:level|book|vol(?:ume)?|part|grade)\s*(\d+)\b', emb_title, re.I)
        fn_nums = re.findall(r'\b(?:level|book|vol(?:ume)?|part|grade)\s*(\d+)\b', fn_title, re.I)
        if emb_nums and fn_nums and emb_nums != fn_nums:
            emb_title = fn_title

    has_embedded_meta = bool(emb_title and emb_authors)
    final_title = emb_title or fn_title or file_path.stem
    final_authors = ', '.join(emb_authors) if emb_authors else (', '.join(fn_authors) if fn_authors else 'Unknown Author')
    final_isbn = emb_isbn or fn_isbn
    final_pub_date = emb_pub_date or fn_pub_date

    return {
        'title': final_title[:510],
        'authors': final_authors[:510],
        'subjects': emb_subjects,
        'publisher': emb_publisher[:250],
        'pub_date': final_pub_date,
        'isbn': final_isbn[:32],
        'asin': emb_asin[:32] if emb_asin else '',
        'description': emb_desc,
        'cover_thumbnail': cover_data_uri,
        'has_embedded_meta': has_embedded_meta,
    }


def classify_book_target_folder(
    file_path: Path,
    books_root: Path,
    meta: Dict[str, Any],
    tobooks_root: Optional[Path] = None,
    explicit_folder: Optional[str] = None,
) -> str:
    """
    Determine the target `/books/<Category>` directory name for `file_path`:
      1. `explicit_folder` if specified by caller.
      2. Subfolder inside `/toBooks/<Subfolder>/...` if dropped into a named category folder.
      3. Direct match or alias match on embedded `meta['subjects']` against `/books/` directories.
      4. Domain keyword scoring on `subjects + title + publisher + description + authors`.
      5. Default fallback to `'Misc'`.
    """
    existing_dirs_map: Dict[str, str] = {}
    if books_root and books_root.exists():
        try:
            for entry in os.scandir(books_root):
                if entry.is_dir() and not entry.name.startswith('.'):
                    if entry.name not in ('Computer Book Code', 'File Formats'):
                        existing_dirs_map[entry.name.lower()] = entry.name
        except Exception:
            pass

    if explicit_folder:
        clean_exp = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', explicit_folder.strip()).strip(' .')
        if clean_exp and clean_exp.lower() not in ('periodicals', 'archive-x', 'tomags', 'tobooks', 'xadult', 'xtoadult'):
            return existing_dirs_map.get(clean_exp.lower(), clean_exp)

    # Check if placed inside a named subfolder of `/toBooks`
    if tobooks_root and tobooks_root.exists():
        try:
            rel = file_path.resolve().relative_to(tobooks_root.resolve())
            if len(rel.parts) >= 2:
                sub = rel.parts[0].strip()
                # Ignore 3-letter uppercase splitfiles shards if someone copied a shard dir
                if sub and not (len(sub) == 3 and sub.isupper()):
                    if sub.lower() in existing_dirs_map:
                        return existing_dirs_map[sub.lower()]
                    for rx, target in _SUBJECT_FOLDER_ALIASES:
                        if rx.match(sub):
                            return existing_dirs_map.get(target.lower(), target)
                    return sub
        except Exception:
            pass

    # Check embedded subjects/tags first (since user often sets metadata before placing in toBooks)
    subjects: List[str] = list(meta.get('subjects') or [])
    for subj in subjects:
        s_clean = subj.strip()
        if not s_clean:
            continue
        if s_clean.lower() in existing_dirs_map:
            return existing_dirs_map[s_clean.lower()]
        for rx, target in _SUBJECT_FOLDER_ALIASES:
            if rx.match(s_clean):
                return existing_dirs_map.get(target.lower(), target)

    # Evaluate domain rules against metadata blob (weighting subjects + title highest)
    subj_title_blob = f"{' '.join(subjects)} {meta.get('title') or ''} {file_path.stem.replace('.', ' ')}"
    full_blob = (
        f"{subj_title_blob} {meta.get('publisher') or ''} "
        f"{(meta.get('description') or '')[:800]} {meta.get('authors') or ''}"
    )

    best_cat = None
    best_score = 0
    for cat_name, rx, base_pts in _DOMAIN_RULES:
        score = 0
        if rx.search(subj_title_blob):
            score = base_pts + 20
        elif rx.search(full_blob):
            score = base_pts
        if score > best_score:
            best_score = score
            best_cat = cat_name

    if best_cat:
        return existing_dirs_map.get(best_cat.lower(), best_cat)

    return existing_dirs_map.get('misc', 'Misc')


def compute_splitfiles_subdir(filename_stem: str, leading: int = 3, dir_suffix: str = '') -> str:
    """
    Exact Python implementation of `splitfiles.ps1` / `splitfiles.py`:
      - Takes the first `leading` (3) characters of `filename_stem`, `.strip().upper()`
      - Appends `'~'` if in Windows reserved names `('CON', 'NUL', 'PRN', 'AUX')`
      - Replaces `'.'` with `'~'` and appends `dir_suffix`
    """
    stem = str(filename_stem or '')
    base_dir = stem[:min(leading, len(stem))].strip().upper()
    if not base_dir:
        base_dir = 'MIS'
    if base_dir in ('CON', 'NUL', 'PRN', 'AUX'):
        base_dir += '~'
    base_dir = base_dir.replace('.', '~') + dir_suffix
    return base_dir


def move_file_with_splitfiles(
    source_path: Path,
    dest_parent_dir: Path,
    leading: int = 3,
    dir_suffix: str = '',
    use_shard: bool = True,
) -> Tuple[Path, bool]:
    """
    Move `source_path` into `dest_parent_dir/<3-char-splitfiles-dir>/<filename>` (or directly into
    `dest_parent_dir/<filename>` if `use_shard=False`) using the exact `splitfiles.ps1` / `splitfiles.py`
    collision & deduplication logic:
      - Computes the 3-character subdirectory via `compute_splitfiles_subdir`.
      - Loops `dupCount = 1, 2, 3...`:
        - If `currentDestPath` does not exist, moves `source_path` to `currentDestPath`.
        - If `currentDestPath` exists, compares byte length + full content (`filecmp.cmp(..., shallow=False)`):
          - If identical, deletes duplicate `source_path` and returns `(currentDestPath, True)`.
          - If different, increments `dupCount` (`dupCount += 1`) and tests `f"{baseName} ({dupCount}){extension}"`.
    """
    import filecmp

    if use_shard:
        sub_name = compute_splitfiles_subdir(source_path.stem, leading=leading, dir_suffix=dir_suffix)
        target_dir = dest_parent_dir / sub_name
    else:
        target_dir = dest_parent_dir

    target_dir.mkdir(parents=True, exist_ok=True)
    base_name = source_path.stem
    extension = source_path.suffix
    dest_path = target_dir / source_path.name

    if source_path.resolve() == dest_path.resolve():
        return dest_path, False

    dup_count = 1
    current_dest_path = dest_path
    while True:
        if not current_dest_path.exists():
            shutil.move(str(source_path), str(current_dest_path))
            try:
                os.chmod(str(current_dest_path), 0o664)
            except Exception:
                pass
            return current_dest_path, False
        else:
            if source_path.resolve() == current_dest_path.resolve():
                return current_dest_path, False
            is_same = False
            try:
                if source_path.stat().st_size == current_dest_path.stat().st_size:
                    is_same = filecmp.cmp(str(source_path), str(current_dest_path), shallow=False)
            except Exception:
                is_same = False
            if is_same:
                try:
                    source_path.unlink()
                except Exception:
                    pass
                return current_dest_path, True
            else:
                dup_count += 1
                current_dest_path = target_dir / f"{base_name} ({dup_count}){extension}"


def organize_book_file(
    file_path: Path,
    books_root: Path,
    tobooks_root: Optional[Path] = None,
    explicit_folder: Optional[str] = None,
    move_file: bool = True,
    hint_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Extract metadata from `file_path`, classify its target `/books/<Category>` folder,
    and move it into `/books/<Category>/<splitfiles-3char-dir>/<filename>` using the exact
    `splitfiles` directory naming and deduplication logic.
    """
    if not file_path.exists():
        return {'ok': False, 'error': f'File does not exist: {file_path}'}

    orig_name = file_path.name
    old_parent = file_path.parent
    meta = extract_book_metadata_for_routing(file_path)
    if hint_meta:
        merged_subjs = list(meta.get('subjects') or [])
        for hs in (hint_meta.get('subjects') or []):
            if hs and hs not in merged_subjs:
                merged_subjs.append(hs)
        meta['subjects'] = merged_subjs
        for k in ('title', 'authors', 'publisher', 'description', 'isbn', 'pub_date'):
            if hint_meta.get(k) and (not meta.get(k) or (k == 'authors' and meta.get(k) == 'Unknown Author')):
                meta[k] = hint_meta[k]
    target_folder = classify_book_target_folder(
        file_path=file_path,
        books_root=books_root,
        meta=meta,
        tobooks_root=tobooks_root,
        explicit_folder=explicit_folder,
    )
    cat_dir = books_root / target_folder

    # Check if target category directory uses splitfiles 3-character subdirectories (default True)
    use_shard = True
    if cat_dir.exists():
        try:
            subdirs = [e.name for _, e in zip(range(40), os.scandir(cat_dir)) if e.is_dir() and not e.name.startswith('.')]
            if not subdirs:
                files_in_root = [e.name for _, e in zip(range(10), os.scandir(cat_dir)) if e.is_file() and not e.name.startswith('.')]
                if files_in_root:
                    use_shard = False
        except Exception:
            pass

    if move_file:
        dest_path, deduped = move_file_with_splitfiles(
            source_path=file_path,
            dest_parent_dir=cat_dir,
            leading=3,
            use_shard=use_shard,
        )
        # Stamp mtime to now when ingesting from /toBooks so Recently Added shows the newly ingested book at the top
        if tobooks_root is not None and not deduped:
            try:
                os.utime(dest_path, None)
            except Exception:
                pass
        # Clean up empty parent directory if inside toBooks or Periodicals
        try:
            if old_parent.exists() and old_parent != books_root and (not tobooks_root or old_parent != tobooks_root):
                if not any(old_parent.iterdir()):
                    old_parent.rmdir()
        except Exception:
            pass
    else:
        sub_name = compute_splitfiles_subdir(file_path.stem, leading=3) if use_shard else ''
        dest_path = (cat_dir / sub_name / file_path.name) if sub_name else (cat_dir / file_path.name)
        deduped = False

    rel = dest_path.resolve().relative_to(books_root.resolve())
    db_path = f"/books/{str(rel).replace(chr(92), '/')}"

    return {
        'ok': True,
        'target_folder': target_folder,
        'target_path': dest_path,
        'db_path': db_path,
        'deduped_existing': deduped,
        'meta': meta,
        'status_msg': f"Organized '{orig_name}' -> {db_path}" + (" (identical copy already existed; deduplicated)" if deduped else ""),
    }


def process_tobooks_directory(
    tobooks_dir: Path,
    books_dir: Path,
) -> List[Dict[str, Any]]:
    """
    Find all book files in `tobooks_dir` (`/toBooks`), organize each into `books_dir` (`/books/<Category>/<Shard>/`),
    and clean up any empty subdirectories inside `tobooks_dir`.
    """
    results: List[Dict[str, Any]] = []
    if not tobooks_dir or not tobooks_dir.exists() or not books_dir or not books_dir.exists():
        return results

    found_files: List[Path] = []
    for root, dirs, files in os.walk(tobooks_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d.lower() != '@eadir']
        for fn in files:
            if fn.startswith('.'):
                continue
            p = Path(root) / fn
            if p.suffix.lower() in BOOK_EXTENSIONS:
                found_files.append(p)

    for fp in sorted(found_files):
        try:
            res = organize_book_file(
                file_path=fp,
                books_root=books_dir,
                tobooks_root=tobooks_dir,
                move_file=True,
            )
            results.append(res)
        except Exception as e:
            results.append({'ok': False, 'file': str(fp), 'error': str(e)})

    # Clean up empty subdirectories inside tobooks_dir
    for root, dirs, _ in os.walk(tobooks_dir, topdown=False):
        for d in dirs:
            if d.startswith('.'):
                continue
            dp = Path(root) / d
            try:
                if not any(dp.iterdir()):
                    dp.rmdir()
            except Exception:
                pass

    return results
