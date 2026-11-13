import re
import uuid
from django.db import models
from django.db.models import CharField, Q, TextField
from django.db.models.lookups import IContains, IStartsWith, Lookup


@CharField.register_lookup
@TextField.register_lookup
class PostgresILikeContains(IContains):
    lookup_name = 'icontains'

    def as_postgresql(self, compiler, connection):
        lhs, lhs_params = Lookup.process_lhs(self, compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        return f'{lhs} ILIKE {rhs}', lhs_params + rhs_params


@CharField.register_lookup
@TextField.register_lookup
class PostgresILikeStartsWith(IStartsWith):
    lookup_name = 'istartswith'

    def as_postgresql(self, compiler, connection):
        lhs, lhs_params = Lookup.process_lhs(self, compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        return f'{lhs} ILIKE {rhs}', lhs_params + rhs_params


_AUTHOR_SUFFIXES = {
    'jr', 'sr', 'ii', 'iii', 'iv', 'v', 'phd', 'md', 'ed', 'eds',
    'inc', 'ltd', 'llc', 'plc', 'corp', 'co', 'press', 'staff', 'team',
}

_SURNAME_PARTICLES = {
    'van', 'von', 'de', 'del', 'della', 'da', 'di', 'le', 'la', 'du',
    'st', 'st.', 'san', 'santa', 'mac', 'mc', 'bin', 'ibn', 'al', 'el',
}

_ORG_KEYWORDS = {
    'united', 'nations', 'national', 'council', 'committee', 'institute',
    'association', 'society', 'university', 'department', 'ministry',
    'commission', 'foundation', 'corporation', 'board', 'office', 'center',
    'centre', 'group', 'press', 'publishing', 'publications', 'organization',
    'organisation', 'agency', 'bureau', 'library', 'academy', 'college',
}


def _is_author_suffix(tok: str) -> bool:
    clean = re.sub(r'[^a-z0-9]', '', (tok or '').lower())
    return clean in _AUTHOR_SUFFIXES


def _is_org_segment(s: str) -> bool:
    words = {re.sub(r'[^a-z0-9]', '', w.lower()) for w in (s or '').split()}
    return bool(words & _ORG_KEYWORDS)


def _is_likely_surname_token(s: str) -> bool:
    """Return True if `s` looks like a surname in 'Lastname, Firstname' format."""
    s = (s or '').strip()
    if not s or _is_org_segment(s):
        return False
    if ' ' not in s:
        return True
    first_word = s.split()[0].lower().rstrip('.')
    return first_word in _SURNAME_PARTICLES


def _is_likely_given_name(s: str) -> bool:
    """Return True if `s` looks like 'Firstname' or 'Firstname M.' in 'Lastname, Firstname'."""
    s = (s or '').strip()
    if not s or _is_author_suffix(s) or _is_org_segment(s):
        return False
    words = s.split()
    if len(words) == 1:
        return True
    if len(words) == 2 and (len(words[1].rstrip('.')) == 1 or len(words[0].rstrip('.')) == 1):
        return True
    return False


def parse_and_normalize_authors(raw: str) -> list[str]:
    """
    Split an author string into individual authors and normalize 'Lastname, Firstname'
    entries to 'Firstname Lastname' (e.g. 'Grandel, Cyril, Miller, Rebecca' ->
    ['Cyril Grandel', 'Rebecca Miller']).
    """
    raw = (raw or '').strip()
    if not raw:
        return ['Unknown Author']

    segments = []
    for semi in raw.split(';'):
        semi = semi.strip()
        if not semi:
            continue
        if _is_org_segment(semi):
            segments.append(semi)
            continue
        for sub in re.split(r'\s+(?:&|and)\s+', semi, flags=re.IGNORECASE):
            if sub.strip():
                segments.append(sub.strip())

    result = []
    for seg in segments:
        if _is_org_segment(seg):
            result.append(seg)
            continue
        cparts = [p.strip() for p in seg.split(',') if p.strip()]
        if not cparts:
            continue
        if len(cparts) == 1:
            result.append(cparts[0])
            continue
        if len(cparts) == 2:
            p0, p1 = cparts[0], cparts[1]
            if _is_author_suffix(p1):
                result.append(f'{p0}, {p1}')
            elif _is_likely_surname_token(p0) and not _is_org_segment(p1) and len(p1.split()) <= 3:
                result.append(f'{p1} {p0}')
            elif _is_likely_given_name(p1) and not _is_org_segment(p0):
                result.append(f'{p1} {p0}')
            else:
                result.append(p0)
                result.append(p1)
            continue

        i = 0
        n = len(cparts)
        while i < n:
            cur = cparts[i]
            nxt = cparts[i + 1] if i + 1 < n else None
            nxt2 = cparts[i + 2] if i + 2 < n else None

            if nxt and _is_author_suffix(nxt):
                result.append(f'{cur}, {nxt}')
                i += 2
                continue

            if nxt and _is_likely_surname_token(cur) and not _is_org_segment(nxt) and len(nxt.split()) <= 3:
                if nxt2 and _is_author_suffix(nxt2):
                    result.append(f'{nxt} {cur}, {nxt2}')
                    i += 3
                else:
                    result.append(f'{nxt} {cur}')
                    i += 2
                continue

            result.append(cur)
            i += 1

    dedup = []
    seen = set()
    for a in result:
        a_clean = re.sub(r'\s+', ' ', a).strip(' ,;')
        if not a_clean:
            continue
        low = a_clean.lower()
        if low in seen:
            continue
        merged_variant = False
        for idx, existing in enumerate(dedup):
            if _are_same_person_author_variants(existing, a_clean):
                dedup[idx] = _pick_preferred_author_variant(existing, a_clean)
                seen.add(dedup[idx].lower())
                merged_variant = True
                break
        if not merged_variant:
            seen.add(low)
            dedup.append(a_clean)
    return dedup or [raw]


def _are_same_person_author_variants(name_a: str, name_b: str) -> bool:
    """
    Return True if `name_a` and `name_b` are variants of the same person's name
    (e.g. 'Gardner D. Hiscox' vs 'Gardner Dexter Hiscox', 'William Calvin' vs 'William H. Calvin',
    or 'G. Hiscox' vs 'Gardner D. Hiscox').
    """
    if not name_a or not name_b:
        return False
    if _is_org_segment(name_a) or _is_org_segment(name_b):
        return name_a.strip().lower() == name_b.strip().lower()
    wa = [re.sub(r'[^a-z0-9]', '', w.lower()) for w in name_a.split(',')[0].split()]
    wb = [re.sub(r'[^a-z0-9]', '', w.lower()) for w in name_b.split(',')[0].split()]
    wa = [w for w in wa if w and w not in _AUTHOR_SUFFIXES]
    wb = [w for w in wb if w and w not in _AUTHOR_SUFFIXES]
    if len(wa) < 2 or len(wb) < 2:
        return False
    # Surnames must match
    if wa[-1] != wb[-1]:
        return False
    # First names must match or one must be an initial of the other
    fa, fb = wa[0], wb[0]
    if fa != fb and not (len(fa) == 1 and fb.startswith(fa)) and not (len(fb) == 1 and fa.startswith(fb)):
        return False
    # Check middle names/initials if both have them
    ma, mb = wa[1:-1], wb[1:-1]
    if not ma or not mb:
        return True
    if len(ma) == len(mb):
        return all(
            x == y or (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y))
            for x, y in zip(ma, mb)
        )
    return False


def _pick_preferred_author_variant(name_a: str, name_b: str) -> str:
    """
    Given two variants of the same author (e.g. 'Gardner D. Hiscox' and 'Gardner Dexter Hiscox',
    or 'G. Hiscox' and 'Gardner D. Hiscox'), pick the cleaner canonical form.
    """
    wa = name_a.split()
    wb = name_b.split()
    # Prefer full first name over first initial ('Gardner' over 'G.')
    fa_init = len(wa[0].rstrip('.')) == 1 if wa else True
    fb_init = len(wb[0].rstrip('.')) == 1 if wb else True
    if fa_init and not fb_init:
        return name_b
    if fb_init and not fa_init:
        return name_a
    # If both have full first names and one has a standard middle initial ('Gardner D. Hiscox')
    # while the other expands the middle name ('Gardner Dexter Hiscox'), prefer 'Firstname M. Lastname'
    # so it stays consistent across editions; if one has a middle initial and the other has none, prefer middle initial.
    if len(wa) == 3 and len(wa[1].rstrip('.')) == 1:
        return name_a
    if len(wb) == 3 and len(wb[1].rstrip('.')) == 1:
        return name_b
    return name_a if len(name_a) >= len(name_b) else name_b


def format_normalized_authors_str(authors_input) -> str:
    """Normalize a list of author strings or a raw author string into 'Firstname Lastname, ...'."""
    if isinstance(authors_input, (list, tuple)):
        raw = '; '.join(str(a).strip() for a in authors_input if str(a or '').strip())
    else:
        raw = str(authors_input or '').strip()
    if not raw:
        return 'Unknown Author'
    norm_list = parse_and_normalize_authors(raw)
    return ', '.join(norm_list) if norm_list else raw


def build_author_q_filter(author_query: str) -> Q:
    """
    Build a Django Q filter for an author name that matches both 'Firstname Lastname'
    and 'Lastname, Firstname' forms in `CatalogItem.authors`.
    """
    q_str = (author_query or '').strip()
    if not q_str:
        return Q()
    cond = Q(authors__icontains=q_str)
    norm_list = parse_and_normalize_authors(q_str)
    for norm_a in norm_list:
        if norm_a != q_str:
            cond |= Q(authors__icontains=norm_a)
        base_a = norm_a.split(',')[0].strip()
        words = base_a.split()
        if len(words) >= 2:
            last_w = words[-1]
            first_w = ' '.join(words[:-1])
            cond |= Q(authors__icontains=f'{last_w}, {first_w}')
            if len(words) > 2:
                cond |= Q(authors__icontains=f'{last_w}, {words[0]}')
    return cond


class CatalogItem(models.Model):
    ITEM_TYPES = [
        ('BOOK', 'Book'),
        ('MAGAZINE', 'Magazine'),
        ('KINDLE', 'Kindle Title'),
        ('AUDIBLE', 'Audible Title'),
        ('AUDIOBOOK', 'Audiobook'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=512, db_index=True)
    authors = models.CharField(max_length=512, default='Unknown Author', db_index=True)
    publisher = models.CharField(max_length=256, blank=True, null=True)
    pub_date = models.DateField(blank=True, null=True, db_index=True)
    
    # Standard identifiers
    isbn = models.CharField(max_length=32, blank=True, null=True, db_index=True)
    asin = models.CharField(max_length=32, blank=True, null=True, db_index=True)
    
    item_type = models.CharField(max_length=16, choices=ITEM_TYPES, default='BOOK', db_index=True)
    description = models.TextField(blank=True, null=True)
    cover_image_url = models.URLField(max_length=1024, blank=True, null=True)
    cover_thumbnail = models.TextField(blank=True, null=True)  # Base64 encoded thumbnail
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def file_date(self):
        """Return the most recent file modification/creation date across attached files, or created_at."""
        try:
            dates = [
                f.file_modified_at or f.file_created_at
                for f in self.files.all()
                if (f.file_modified_at or f.file_created_at)
            ]
            if dates:
                return max(dates)
        except Exception:
            pass
        return self.created_at

    @property
    def author_list(self):
        """Return individual author names normalized to 'Firstname Lastname' for clickable author links."""
        return parse_and_normalize_authors(self.authors)

    def files_json(self):
        import json
        try:
            from .opds import (
                _ENRICHED_PATHS_CACHE,
                get_file_enrich_diagnostic,
                get_file_tags,
                is_file_enriched,
            )
            excl_set = _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids_set')
            item_in_processed = (not isinstance(excl_set, set)) or (not excl_set) or (self.id in excl_set)
        except Exception:
            is_file_enriched = lambda p: False
            get_file_tags = lambda p: []
            get_file_enrich_diagnostic = lambda p: None
            item_in_processed = False
        out = []
        pre_diag = getattr(self, 'enrich_diag', '_unset')
        for f in list(self.files.all())[:25]:
            enr = bool(is_file_enriched(f.file_path)) if item_in_processed else False
            if enr or not item_in_processed:
                diag = None
            elif pre_diag != '_unset':
                diag = pre_diag
            else:
                diag = get_file_enrich_diagnostic(f.file_path)
            out.append({
                'id': str(f.id),
                'file_name': f.file_name,
                'file_path': f.file_path,
                'file_size': f.file_size,
                'file_format': f.file_format,
                'file_hash': f.file_hash,
                'file_date': (f.file_modified_at or f.file_created_at).strftime('%Y-%m-%d') if (f.file_modified_at or f.file_created_at) else '',
                'is_invalid': f.is_invalid,
                'has_drm': f.has_drm,
                'drm_lock_state': f.drm_lock_state,
                'library_name': f.library_name,
                'library_path': f.library_path,
                'is_enriched': enr,
                'enrich_status': (diag or {}).get('status', ''),
                'enrich_error': (diag or {}).get('error', ''),
                'tags': get_file_tags(f.file_path) if item_in_processed else [],
            })
        return json.dumps(out)

    def __str__(self):
        return f"{self.title} by {self.authors} ({self.get_item_type_display()})"

    class Meta:
        ordering = ['title']
        verbose_name = 'Catalog Item'
        verbose_name_plural = 'Catalog Items'


_DRM_LOCK_STATE_CACHE: dict[str, str] = {}
_DRM_LOCK_CACHE_LOADED = False


def _get_drm_cache_file():
    from pathlib import Path
    for d in (Path('/books_bad'), Path('/volume1/common/Media/Books_bad'), Path(r'U:\Media\Books_bad')):
        try:
            if d.exists() and d.is_dir():
                return d / '.drm_lock_state_cache.json'
        except Exception:
            continue
    return Path('/tmp/mylibrary_drm_lock_state_cache.json')


def _resolve_local_nas_path(file_path: str) -> str:
    import os
    fp = str(file_path or '').strip()
    if not fp:
        return ''
    if os.path.exists(fp):
        return fp
    if fp.startswith('/volume1/common/Media/Books/'):
        alt = '/books/' + fp[len('/volume1/common/Media/Books/'):]
        if os.path.exists(alt):
            return alt
    elif fp.startswith('/books/'):
        alt = '/volume1/common/Media/Books/' + fp[len('/books/'):]
        if os.path.exists(alt):
            return alt
    return ''


def inspect_file_drm_lock_state(file_path: str, file_format: str = '', allow_disk_io: bool = False) -> str:
    """
    Distinguish between:
    - 'locked': Unopenable due to user password or full content DRM (closed padlock).
    - 'unlocked': Has owner permission flags or font-only obfuscation, but opens/reads normally (unlocked padlock).
    When `allow_disk_io=False` (default during template rendering), only in-memory/disk cache and
    `processed_files` status are consulted so rendering never blocks on NAS disk reads.
    """
    global _DRM_LOCK_CACHE_LOADED
    import json
    import os
    import zipfile

    fp = str(file_path or '').strip()
    if not fp:
        return 'locked'

    if not _DRM_LOCK_CACHE_LOADED:
        _DRM_LOCK_CACHE_LOADED = True
        try:
            cf = _get_drm_cache_file()
            if cf.exists():
                data = json.loads(cf.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    _DRM_LOCK_STATE_CACHE.update(data)
        except Exception:
            pass

    if fp in _DRM_LOCK_STATE_CACHE:
        return _DRM_LOCK_STATE_CACHE[fp]

    try:
        from .opds import _ENRICHED_PATHS_CACHE, get_enriched_paths_set
        get_enriched_paths_set()
        paths_set = _ENRICHED_PATHS_CACHE.get('paths_set') or set()
        status_map = _ENRICHED_PATHS_CACHE.get('status_by_path') or {}
        alt_fp = (
            '/books/' + fp[len('/volume1/common/Media/Books/'):]
            if fp.startswith('/volume1/common/Media/Books/')
            else ('/volume1/common/Media/Books/' + fp[len('/books/'):] if fp.startswith('/books/') else fp)
        )
        if fp in paths_set or alt_fp in paths_set:
            _DRM_LOCK_STATE_CACHE[fp] = 'unlocked'
            return 'unlocked'
        st_obj = status_map.get(fp) or status_map.get(alt_fp)
        st = st_obj.get('status') if isinstance(st_obj, dict) else st_obj
        if st == 'HAS_DRM':
            _DRM_LOCK_STATE_CACHE[fp] = 'locked'
            return 'locked'
        if st in ('SUCCESS', 'NO_ISBN', 'NOT_FOUND', 'NEEDS_REVIEW'):
            _DRM_LOCK_STATE_CACHE[fp] = 'unlocked'
            return 'unlocked'
    except Exception:
        pass

    if not allow_disk_io:
        return 'locked'

    local_p = _resolve_local_nas_path(fp)
    if not local_p:
        return 'locked'

    fmt = (file_format or os.path.splitext(local_p)[1]).upper().lstrip('.')
    state = 'locked'
    try:
        if fmt == 'PDF':
            import pymupdf
            doc = pymupdf.open(local_p)
            try:
                state = 'locked' if doc.needs_pass else 'unlocked'
            finally:
                doc.close()
        elif fmt == 'EPUB':
            if zipfile.is_zipfile(local_p):
                with zipfile.ZipFile(local_p, 'r') as zf:
                    names = set(zf.namelist())
                    if 'META-INF/rights.xml' in names or 'META-INF/license.lcpl' in names:
                        state = 'locked'
                    elif 'META-INF/encryption.xml' in names:
                        enc_xml = zf.read('META-INF/encryption.xml').decode('utf-8', errors='ignore').lower()
                        if any(ext in enc_xml for ext in ('.xhtml', '.html', '.htm', '.opf', '.ncx')):
                            state = 'locked'
                        else:
                            state = 'unlocked'
                    else:
                        state = 'unlocked'
    except Exception:
        state = 'locked'

    _DRM_LOCK_STATE_CACHE[fp] = state
    if len(_DRM_LOCK_STATE_CACHE) % 25 == 0 or state == 'unlocked':
        try:
            cf = _get_drm_cache_file()
            cf.write_text(json.dumps(_DRM_LOCK_STATE_CACHE), encoding='utf-8')
        except Exception:
            pass
    return state


class FileLocation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    catalog_item = models.ForeignKey(
        CatalogItem,
        on_delete=models.CASCADE,
        related_name='files',
        db_index=True
    )
    
    file_path = models.TextField()  # Paths can be very long (especially on networks)
    file_name = models.CharField(max_length=512)
    file_size = models.BigIntegerField()  # Bytes
    file_format = models.CharField(max_length=16, db_index=True) # EPUB, PDF, MOBI, etc.
    
    file_created_at = models.DateTimeField(blank=True, null=True)
    file_modified_at = models.DateTimeField(blank=True, null=True)
    file_hash = models.CharField(max_length=64, db_index=True)  # SHA-256 hash
    is_invalid = models.BooleanField(default=False, db_index=True)
    has_drm = models.BooleanField(default=False, db_index=True)
    library_name = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    library_path = models.TextField(blank=True, null=True)
    scanned_at = models.DateTimeField(auto_now=True)

    @property
    def drm_lock_state(self) -> str:
        """Return '' if no DRM/encryption, 'locked' if unopenable, or 'unlocked' if owner-restricted/openable."""
        if not self.has_drm:
            return ''
        return inspect_file_drm_lock_state(self.file_path, self.file_format)

    def __str__(self):
        return f"{self.file_name} ({self.file_format})"

    class Meta:
        ordering = ['file_name']
        verbose_name = 'File Location'
        verbose_name_plural = 'File Locations'
