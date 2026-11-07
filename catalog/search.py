import datetime
import re
from typing import Any, Dict, List, Optional, Tuple
from django.db.models import Case, F, IntegerField, Q, Value, When
from .models import CatalogItem, FileLocation


# Structured reference of all supported search keywords & operators for UI hints and autocomplete
SEARCH_KEYWORDS_SPEC: List[Dict[str, Any]] = [
    # Field Scopes
    {
        'keyword': 'title:',
        'aliases': ['t:'],
        'category': 'Field',
        'insert': 'title:',
        'is_prefix': True,
        'example': 'title:"Dune Messiah"',
        'label': 'Title Match',
        'desc': 'Limit search to book title only (use quotes for multi-word titles)',
    },
    {
        'keyword': 'author:',
        'aliases': ['a:'],
        'category': 'Field',
        'insert': 'author:',
        'is_prefix': True,
        'example': 'author:"Brandon Sanderson"',
        'label': 'Author Match',
        'desc': 'Limit search to author name only',
    },
    {
        'keyword': 'pub:',
        'aliases': ['publisher:'],
        'category': 'Field',
        'insert': 'pub:',
        'is_prefix': True,
        'example': 'pub:"O\'Reilly"',
        'label': 'Publisher Match',
        'desc': 'Limit search to publisher name (e.g. pub:Packt, pub:Apress, pub:Tor)',
    },
    {
        'keyword': 'isbn:',
        'aliases': [],
        'category': 'Field',
        'insert': 'isbn:',
        'is_prefix': True,
        'example': 'isbn:9780593135204',
        'label': 'ISBN Match',
        'desc': 'Exact or prefix ISBN-10 / ISBN-13 lookup',
    },
    {
        'keyword': 'asin:',
        'aliases': [],
        'category': 'Field',
        'insert': 'asin:',
        'is_prefix': True,
        'example': 'asin:B08F685349',
        'label': 'ASIN Match',
        'desc': 'Amazon Kindle / Audible ASIN lookup',
    },
    {
        'keyword': 'desc:',
        'aliases': ['synopsis:', 'summary:'],
        'category': 'Field',
        'insert': 'desc:',
        'is_prefix': True,
        'example': 'desc:cyberpunk',
        'label': 'Synopsis / Description',
        'desc': 'Search inside book synopsis / description text',
    },
    {
        'keyword': 'cat:',
        'aliases': ['category:', 'folder:', 'dir:'],
        'category': 'Field',
        'insert': 'cat:',
        'is_prefix': True,
        'example': 'cat:"Science Fiction"',
        'label': 'Category Directory',
        'desc': 'Limit to top-level category directory (Fiction, Science Fiction, Political, Recipes, etc.)',
    },
    {
        'keyword': 'tag:',
        'aliases': ['subject:'],
        'category': 'Field',
        'insert': 'tag:',
        'is_prefix': True,
        'example': 'tag:History',
        'label': 'Enriched Subject Tag',
        'desc': 'Limit to books with a matching enriched subject / genre tag',
    },
    {
        'keyword': 'file:',
        'aliases': ['filename:'],
        'category': 'Field',
        'insert': 'file:',
        'is_prefix': True,
        'example': 'file:retail',
        'label': 'Filename Match',
        'desc': 'Search physical file names on disk',
    },
    # Format, Type & Year
    {
        'keyword': 'format:epub',
        'aliases': ['fmt:epub', 'ext:epub', 'format:pdf', 'format:mobi', 'format:azw3'],
        'category': 'Format & Date',
        'insert': 'format:epub',
        'is_prefix': False,
        'example': 'format:epub',
        'label': 'Format (EPUB/PDF/MOBI)',
        'desc': 'Limit by file format (format:epub, format:pdf, format:mobi, format:azw3, -format:pdf)',
    },
    {
        'keyword': 'type:book',
        'aliases': ['type:magazine', 'type:mag', 'type:kindle', 'type:audible', 'type:audiobook'],
        'category': 'Format & Date',
        'insert': 'type:book',
        'is_prefix': False,
        'example': 'type:book',
        'label': 'Media Type',
        'desc': 'Limit by catalog item type (book, magazine, kindle, audible, audiobook)',
    },
    {
        'keyword': 'year:',
        'aliases': ['published:', 'after:', 'before:'],
        'category': 'Format & Date',
        'insert': 'year:',
        'is_prefix': True,
        'example': 'year:2024..2026',
        'label': 'Publication Year / Range',
        'desc': 'Filter by year or range (year:2025, year:2020..2026, year:>=2024, after:2023, before:2000)',
    },
    # Status & Virtual Shelves
    {
        'keyword': 'is:enriched',
        'aliases': ['+enriched'],
        'category': 'Shelf & Status',
        'insert': 'is:enriched',
        'is_prefix': False,
        'example': 'is:enriched python',
        'label': 'Enriched Books Only',
        'desc': 'Only include books already enriched by epubmeta',
    },
    {
        'keyword': 'is:unenriched',
        'aliases': ['-is:enriched', 'is:needs_enriching'],
        'category': 'Shelf & Status',
        'insert': 'is:unenriched',
        'is_prefix': False,
        'example': 'is:unenriched',
        'label': 'Needs Enriching Only',
        'desc': 'Only include books that have not been enriched yet',
    },
    {
        'keyword': 'is:review',
        'aliases': ['is:needs_review'],
        'category': 'Shelf & Status',
        'insert': 'is:review',
        'is_prefix': False,
        'example': 'is:review',
        'label': 'In Review Queue',
        'desc': 'Books flagged for metadata review in scanner_state.db',
    },
    {
        'keyword': 'is:interesting',
        'aliases': ['is:starred'],
        'category': 'Shelf & Status',
        'insert': 'is:interesting',
        'is_prefix': False,
        'example': 'is:interesting',
        'label': 'Interesting Shelf',
        'desc': 'Books marked ⭐ Interesting to read/download next',
    },
    {
        'keyword': 'is:badmeta',
        'aliases': ['is:bad', 'is:flagged'],
        'category': 'Shelf & Status',
        'insert': 'is:badmeta',
        'is_prefix': False,
        'example': 'is:badmeta',
        'label': 'Bad Metadata Shelf',
        'desc': 'Books flagged as having bad metadata from Moon+ Reader or Web UI',
    },
    {
        'keyword': 'is:reading',
        'aliases': ['is:bookmarked', 'is:unread', 'is:read', 'has:notes'],
        'category': 'Shelf & Status',
        'insert': 'is:reading',
        'is_prefix': False,
        'example': 'is:reading',
        'label': 'Reading / Bookmarked',
        'desc': 'Filter by Moon+ / Kindle reading state (is:reading, is:unread, is:read, is:bookmarked, has:notes)',
    },
    {
        'keyword': 'is:amazon',
        'aliases': ['is:kindle', 'is:kindleplus', 'is:audible'],
        'category': 'Shelf & Status',
        'insert': 'is:amazon',
        'is_prefix': False,
        'example': 'is:kindle',
        'label': 'Amazon / Kindle / Audible',
        'desc': 'Limit to books in your Amazon Kindle, Kindle+, or Audible library',
    },
    # Completeness, Exclusion & Sort
    {
        'keyword': 'missing:cover',
        'aliases': ['has:cover', 'missing:isbn', 'has:isbn', 'missing:desc', 'has:desc', 'missing:author'],
        'category': 'Completeness & Sort',
        'insert': 'missing:cover',
        'is_prefix': False,
        'example': 'missing:cover',
        'label': 'Has / Missing Metadata',
        'desc': 'Find books with or missing cover, isbn, desc/synopsis, or author',
    },
    {
        'keyword': '-term',
        'aliases': ['-author:', '-format:pdf'],
        'category': 'Completeness & Sort',
        'insert': '-',
        'is_prefix': True,
        'example': 'python -beginner',
        'label': 'Exclude (-)',
        'desc': 'Prefix any word or keyword with - to exclude matches (e.g. -format:pdf, -author:Unknown)',
    },
    {
        'keyword': 'sort:newest',
        'aliases': ['sort:oldest', 'sort:added', 'sort:title', 'sort:author'],
        'category': 'Completeness & Sort',
        'insert': 'sort:newest',
        'is_prefix': False,
        'example': 'author:Asimov sort:newest',
        'label': 'Sort Order',
        'desc': 'Order results directly from search (sort:newest, sort:oldest, sort:added, sort:title, sort:author)',
    },
    {
        'keyword': '+enrich',
        'aliases': ['!enrich', '+badmeta'],
        'category': 'Actions',
        'insert': '+enrich',
        'is_prefix': False,
        'example': 'Helidon +enrich',
        'label': 'On-Demand Action',
        'desc': 'Append +enrich to enrich top unenriched search results on the fly (or +badmeta in OPDS)',
    },
]


_TOKEN_RE = re.compile(
    r'(-?[A-Za-z_+!]+:"[^"]*"|-?[A-Za-z_+!]+:\'[^\']*\'|-?"[^"]+"|-?\'[^\']+\'|\S+)'
)


def normalize_title(title):
    """Normalize a title by removing subtitles, parentheticals, and bracketed notes."""
    if not title:
        return ""
    title = re.sub(r'\(.*?\)', '', title)
    title = re.sub(r'\[.*?\]', '', title)

    for sep in (':', ' - '):
        if sep in title:
            parts = title.split(sep)
            if len(parts[0].strip()) >= 3:
                title = parts[0]

    title = re.sub(r'[^\w\s]', '', title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip().lower()


def normalize_author(author):
    """Normalize author string by converting to lowercase and removing spaces and punctuation."""
    if not author:
        return ""
    author = author.lower()
    author = re.sub(r'[^a-z0-9]', '', author)
    return author


def find_similar_item(title, authors, isbn=None, asin=None, threshold=0.6):
    """
    Finds an existing CatalogItem that matches the metadata using indexed identifiers,
    exact B-tree lookups, or targeted similarity matching.
    """
    if isbn:
        item = CatalogItem.objects.filter(isbn=isbn).first()
        if item:
            return item

    if asin:
        item = CatalogItem.objects.filter(asin=asin).first()
        if item:
            return item

    item = CatalogItem.objects.filter(title=title, authors=authors).first()
    if item:
        return item

    if not authors or authors == 'Unknown Author':
        return None

    norm_title = normalize_title(title)
    norm_author = normalize_author(authors)
    if norm_title and norm_author:
        same_author_items = list(CatalogItem.objects.filter(authors=authors)[:50])
        for candidate in same_author_items:
            if normalize_title(candidate.title) == norm_title:
                return candidate

        same_title_items = list(CatalogItem.objects.filter(title=title)[:20])
        for candidate in same_title_items:
            if normalize_author(candidate.authors) == norm_author:
                return candidate

    return None


def parse_search_query(query_string: str) -> Dict[str, Any]:
    """
    Parse a search query string into structured keyword filters, status flags,
    date bounds, sort overrides, action triggers, and free-text terms.
    """
    raw = (query_string or '').strip()
    parsed: Dict[str, Any] = {
        'raw_query': raw,
        'free_terms': [],           # List[str]
        'neg_free_terms': [],       # List[str]
        'field_filters': [],        # List[Tuple[str, str, bool]] -> (canonical_field, value, negated)
        'year_filters': [],         # List[Tuple[str, int, Optional[int], bool]] -> (op, y1, y2, negated)
        'status_flags': [],         # List[Tuple[str, bool]] -> (canonical_flag, negated)
        'sort_override': '',        # str
        'enrich_on_search': False,
        'flag_bad_on_search': False,
        'active_badges': [],        # List[Dict[str, str]] for UI display & removal
    }
    if not raw:
        return parsed

    tokens = _TOKEN_RE.findall(raw)
    field_alias_map = {
        'title': 'title',
        't': 'title',
        'author': 'author',
        'authors': 'author',
        'a': 'author',
        'pub': 'publisher',
        'publisher': 'publisher',
        'isbn': 'isbn',
        'asin': 'asin',
        'desc': 'description',
        'description': 'description',
        'synopsis': 'description',
        'summary': 'description',
        'series': 'series',
        'file': 'file',
        'filename': 'file',
        'path': 'path',
        'cat': 'category',
        'category': 'category',
        'folder': 'category',
        'dir': 'category',
        'tag': 'tag',
        'tags': 'tag',
        'subject': 'tag',
        'format': 'format',
        'fmt': 'format',
        'ext': 'format',
        'type': 'type',
    }

    for tok in tokens:
        t_low = tok.lower()
        # Action triggers (+enrich, !enrich, +badmeta, !badmeta)
        if t_low in ('+enrich', '!enrich'):
            parsed['enrich_on_search'] = True
            parsed['active_badges'].append({'raw': tok, 'label': '✨ +enrich (On-Demand)', 'kind': 'action'})
            continue
        if t_low in ('+bad', '!bad', '+badmeta', '!badmeta', '+badmetadata', '!badmetadata', '+fixmeta', '!fixmeta'):
            parsed['flag_bad_on_search'] = True
            parsed['active_badges'].append({'raw': tok, 'label': '🚩 +badmeta (Flag Result)', 'kind': 'action'})
            continue
        if t_low in ('+enriched',):
            parsed['status_flags'].append(('enriched', False))
            parsed['active_badges'].append({'raw': tok, 'label': 'Status: Enriched', 'kind': 'status'})
            continue
        if t_low in ('-enriched',):
            parsed['status_flags'].append(('enriched', True))
            parsed['active_badges'].append({'raw': tok, 'label': 'Status: Not Enriched', 'kind': 'status'})
            continue

        negated = False
        work = tok
        if work.startswith('-') and len(work) > 1:
            negated = True
            work = work[1:]

        if ':' in work:
            k_raw, v_raw = work.split(':', 1)
            k = k_raw.lower().strip()
            v = v_raw.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1].strip()
            if not v:
                continue

            # 1. Year / Date operators (`year:`, `published:`, `after:`, `since:`, `before:`)
            if k in ('year', 'published', 'pubyear', 'after', 'since', 'before'):
                v_clean = v.strip()
                if k in ('after', 'since'):
                    m_yr = re.match(r'^(\d{4})', v_clean)
                    if m_yr:
                        yr = int(m_yr.group(1))
                        parsed['year_filters'].append(('gte', yr, None, negated))
                        parsed['active_badges'].append({'raw': tok, 'label': f"{'Not ' if negated else ''}Published ≥ {yr}", 'kind': 'date'})
                        continue
                elif k == 'before':
                    m_yr = re.match(r'^(\d{4})', v_clean)
                    if m_yr:
                        yr = int(m_yr.group(1))
                        parsed['year_filters'].append(('lte', yr, None, negated))
                        parsed['active_badges'].append({'raw': tok, 'label': f"{'Not ' if negated else ''}Published ≤ {yr}", 'kind': 'date'})
                        continue
                else:
                    m_rng = re.match(r'^(\d{4})\s*(?:\.\.|-)\s*(\d{4})$', v_clean)
                    m_cmp = re.match(r'^(>=|<=|>|<)\s*(\d{4})$', v_clean)
                    m_exact = re.match(r'^(\d{4})$', v_clean)
                    if m_rng:
                        y1, y2 = int(m_rng.group(1)), int(m_rng.group(2))
                        if y1 > y2:
                            y1, y2 = y2, y1
                        parsed['year_filters'].append(('range', y1, y2, negated))
                        parsed['active_badges'].append({'raw': tok, 'label': f"{'Not ' if negated else ''}Year: {y1}–{y2}", 'kind': 'date'})
                        continue
                    elif m_cmp:
                        op_sym, yr = m_cmp.group(1), int(m_cmp.group(2))
                        op_code = {'<=': 'lte', '>=': 'gte', '<': 'lt', '>': 'gt'}[op_sym]
                        parsed['year_filters'].append((op_code, yr, None, negated))
                        parsed['active_badges'].append({'raw': tok, 'label': f"{'Not ' if negated else ''}Year {op_sym} {yr}", 'kind': 'date'})
                        continue
                    elif m_exact:
                        yr = int(m_exact.group(1))
                        parsed['year_filters'].append(('exact', yr, None, negated))
                        parsed['active_badges'].append({'raw': tok, 'label': f"{'Not ' if negated else ''}Year: {yr}", 'kind': 'date'})
                        continue

            # 2. Status / Shelf / Completeness (`is:`, `has:`, `missing:`)
            if k in ('is', 'has', 'missing'):
                v_low = v.lower().replace('-', '_')
                is_neg = negated or (k == 'missing')
                if k in ('has', 'missing') or v_low in ('cover', 'isbn', 'asin', 'desc', 'description', 'synopsis', 'author', 'notes', 'highlights', 'annotations', 'bookmark', 'bookmarks'):
                    if v_low in ('cover', 'thumbnail', 'art'):
                        parsed['status_flags'].append(('has_cover', is_neg))
                        parsed['active_badges'].append({'raw': tok, 'label': 'Missing Cover' if is_neg else 'Has Cover', 'kind': 'meta'})
                        continue
                    if v_low == 'isbn':
                        parsed['status_flags'].append(('has_isbn', is_neg))
                        parsed['active_badges'].append({'raw': tok, 'label': 'Missing ISBN' if is_neg else 'Has ISBN', 'kind': 'meta'})
                        continue
                    if v_low == 'asin':
                        parsed['status_flags'].append(('has_asin', is_neg))
                        parsed['active_badges'].append({'raw': tok, 'label': 'Missing ASIN' if is_neg else 'Has ASIN', 'kind': 'meta'})
                        continue
                    if v_low in ('desc', 'description', 'synopsis', 'summary'):
                        parsed['status_flags'].append(('has_desc', is_neg))
                        parsed['active_badges'].append({'raw': tok, 'label': 'Missing Synopsis' if is_neg else 'Has Synopsis', 'kind': 'meta'})
                        continue
                    if v_low in ('author', 'authors'):
                        parsed['status_flags'].append(('has_author', is_neg))
                        parsed['active_badges'].append({'raw': tok, 'label': 'Unknown Author' if is_neg else 'Has Known Author', 'kind': 'meta'})
                        continue
                    if v_low in ('notes', 'note', 'highlights', 'highlight', 'annotations', 'an'):
                        parsed['status_flags'].append(('annotations', is_neg))
                        parsed['active_badges'].append({'raw': tok, 'label': f"{'No ' if is_neg else ''}Highlights/Notes (.an)", 'kind': 'status'})
                        continue
                    if v_low in ('bookmark', 'bookmarks', 'bm'):
                        parsed['status_flags'].append(('bookmarked', is_neg))
                        parsed['active_badges'].append({'raw': tok, 'label': f"{'Not ' if is_neg else ''}Bookmarked", 'kind': 'status'})
                        continue

                status_map = {
                    'enriched': ('enriched', False, 'Enriched'),
                    'unenriched': ('enriched', True, 'Needs Enriching'),
                    'needs_enriching': ('enriched', True, 'Needs Enriching'),
                    'unprocessed': ('enriched', True, 'Needs Enriching'),
                    'review': ('needs_review', False, 'In Review Queue'),
                    'needs_review': ('needs_review', False, 'In Review Queue'),
                    'in_review': ('needs_review', False, 'In Review Queue'),
                    'interesting': ('interesting', False, '⭐ Interesting'),
                    'starred': ('interesting', False, '⭐ Interesting'),
                    'saved': ('interesting', False, '⭐ Interesting'),
                    'bad': ('bad_metadata', False, '🚩 Bad Metadata'),
                    'badmeta': ('bad_metadata', False, '🚩 Bad Metadata'),
                    'bad_metadata': ('bad_metadata', False, '🚩 Bad Metadata'),
                    'flagged': ('bad_metadata', False, '🚩 Bad Metadata'),
                    'bookmarked': ('bookmarked', False, 'Bookmarked'),
                    'bm': ('bookmarked', False, 'Bookmarked'),
                    'reading': ('reading', False, 'Reading (1–99%)'),
                    'inprogress': ('reading', False, 'Reading (1–99%)'),
                    'in_progress': ('reading', False, 'Reading (1–99%)'),
                    'read': ('read', False, '100% Read'),
                    'finished': ('read', False, '100% Read'),
                    'completed': ('read', False, '100% Read'),
                    'unread': ('unread', False, 'Unread (<100%)'),
                    'annotated': ('annotations', False, 'Highlights & Notes (.an)'),
                    'amazon': ('amazon', False, 'Amazon Library'),
                    'kindle': ('kindle', False, 'Amazon Kindle'),
                    'kindleplus': ('kindle_plus', False, 'Kindle+'),
                    'kindle_plus': ('kindle_plus', False, 'Kindle+'),
                    'audible': ('audible', False, 'Audible'),
                }
                if v_low in status_map:
                    canon_st, default_neg, st_label = status_map[v_low]
                    final_neg = default_neg ^ negated
                    parsed['status_flags'].append((canon_st, final_neg))
                    parsed['active_badges'].append({
                        'raw': tok,
                        'label': f"Not {st_label}" if (negated and not default_neg) else st_label,
                        'kind': 'status',
                    })
                    continue

            # 3. Sort override (`sort:`, `order:`)
            if k in ('sort', 'order'):
                s_low = v.lower().replace('-', '_')
                sort_map = {
                    'newest': ('pubdate_desc', 'Newest Published'),
                    'pubdate': ('pubdate_desc', 'Newest Published'),
                    'pubdate_desc': ('pubdate_desc', 'Newest Published'),
                    'oldest': ('pubdate_asc', 'Oldest Published'),
                    'pubdate_asc': ('pubdate_asc', 'Oldest Published'),
                    'added': ('added_desc', 'Recently Added'),
                    'recent': ('added_desc', 'Recently Added'),
                    'filedate': ('added_desc', 'Recently Added'),
                    'updated': ('updated_desc', 'Recently Updated'),
                    'title': ('title_asc', 'Title (A→Z)'),
                    'title_asc': ('title_asc', 'Title (A→Z)'),
                    'title_desc': ('title_desc', 'Title (Z→A)'),
                    'author': ('author_asc', 'Author (A→Z)'),
                    'author_asc': ('author_asc', 'Author (A→Z)'),
                    'author_desc': ('author_desc', 'Author (Z→A)'),
                }
                if s_low in sort_map:
                    parsed['sort_override'] = sort_map[s_low][0]
                    parsed['active_badges'].append({'raw': tok, 'label': f"Sort: {sort_map[s_low][1]}", 'kind': 'sort'})
                    continue

            # 4. Field filters (`title:`, `author:`, `pub:`, `isbn:`, `asin:`, `desc:`, `cat:`, `tag:`, `file:`, `format:`, `type:`)
            if k in field_alias_map:
                canon_field = field_alias_map[k]
                parsed['field_filters'].append((canon_field, v, negated))
                nice_field = {
                    'title': 'Title',
                    'author': 'Author',
                    'publisher': 'Publisher',
                    'isbn': 'ISBN',
                    'asin': 'ASIN',
                    'description': 'Synopsis',
                    'series': 'Series',
                    'file': 'File',
                    'path': 'Path',
                    'category': 'Category',
                    'tag': 'Tag',
                    'format': 'Format',
                    'type': 'Type',
                }.get(canon_field, canon_field.title())
                parsed['active_badges'].append({
                    'raw': tok,
                    'label': f"{'Not ' if negated else ''}{nice_field}: {v}",
                    'kind': 'field',
                })
                continue

        # Free-text term or quoted phrase (positive or negated)
        clean_term = work
        if (clean_term.startswith('"') and clean_term.endswith('"')) or (clean_term.startswith("'") and clean_term.endswith("'")):
            clean_term = clean_term[1:-1].strip()
        if not clean_term:
            continue
        if negated:
            parsed['neg_free_terms'].append(clean_term)
            parsed['active_badges'].append({'raw': tok, 'label': f"Exclude: {clean_term}", 'kind': 'exclude'})
        else:
            parsed['free_terms'].append(clean_term)

    return parsed


def get_keyword_autocomplete_suggestions(query_string: str) -> List[Dict[str, str]]:
    """
    Return contextual search keyword hints for the autocomplete dropdown when the user
    is typing a keyword prefix (e.g. `author:`, `is:`, `format:`, `cat:`, `tag:`, `year:`, `sort:`)
    or starts a token with `+` or `-`.
    """
    raw = (query_string or '')
    if not raw.strip():
        return []

    # If the user just typed a space, don't flood with keyword hints unless they typed a trailing operator
    if raw.endswith(' '):
        return []

    tokens = _TOKEN_RE.findall(raw.strip())
    if not tokens:
        return []

    last_tok = tokens[-1]
    prefix_before_last = raw[: raw.rfind(last_tok)]
    low_tok = last_tok.lower()
    neg_prefix = '-' if low_tok.startswith('-') else ''
    clean_low = low_tok[1:] if neg_prefix else low_tok

    hints: List[Dict[str, str]] = []

    # Case 1: Dynamic Category completion (`cat:`, `category:`, `folder:`)
    if clean_low.startswith(('cat:', 'category:', 'folder:', 'dir:')):
        k_part, val_part = last_tok.split(':', 1)
        val_clean = val_part.strip('"\'').lower()
        try:
            from .opds import get_enriched_tags_and_folders_index
            f_counts = get_enriched_tags_and_folders_index().get('folder_counts') or []
        except Exception:
            f_counts = []
        if not f_counts:
            f_counts = [
                ('Computer Mags & FAQ', 0), ('Fiction', 0), ('Science Fiction', 0),
                ('Political', 0), ('Recipes', 0), ('Computer Hardware', 0),
                ('Role Playing Games', 0), ('Chess Library', 0), ('Religon related texts', 0),
                ('Chinese-China', 0), ('Japan', 0), ('Electronic Schematics', 0),
            ]
        for fld_name, cnt in f_counts:
            if not val_clean or val_clean in fld_name.lower():
                formatted_val = f'"{fld_name}"' if ' ' in fld_name else fld_name
                rep_tok = f"{neg_prefix}cat:{formatted_val}"
                hints.append({
                    'kind': 'keyword',
                    'keyword': rep_tok,
                    'full_query': f"{prefix_before_last}{rep_tok} ",
                    'label': f"Category: {fld_name}" + (f" ({cnt:,} enriched)" if cnt else ""),
                    'category': 'CATEGORY',
                    'submit_immediately': False,
                })
                if len(hints) >= 6:
                    break
        return hints

    # Case 2: Dynamic Tag completion (`tag:`, `subject:`)
    if clean_low.startswith(('tag:', 'tags:', 'subject:')):
        k_part, val_part = last_tok.split(':', 1)
        val_clean = val_part.strip('"\'').lower()
        try:
            from .opds import get_enriched_tags_and_folders_index
            t_counts = get_enriched_tags_and_folders_index().get('tag_counts') or []
        except Exception:
            t_counts = []
        for tag_name, cnt in t_counts:
            if not val_clean or val_clean in tag_name.lower():
                formatted_val = f'"{tag_name}"' if ' ' in tag_name else tag_name
                rep_tok = f"{neg_prefix}tag:{formatted_val}"
                hints.append({
                    'kind': 'keyword',
                    'keyword': rep_tok,
                    'full_query': f"{prefix_before_last}{rep_tok} ",
                    'label': f"Tag: {tag_name} ({cnt:,} books)",
                    'category': 'TAG',
                    'submit_immediately': False,
                })
                if len(hints) >= 6:
                    break
        return hints

    # Case 3: Specific value completions for `is:`, `has:`, `missing:`, `format:`, `type:`, `sort:`
    value_Menus = {
        'is:': [
            ('is:enriched', 'Only books enriched by epubmeta'),
            ('is:unenriched', 'Only books needing enrichment'),
            ('is:review', 'Books in the Needs Review queue'),
            ('is:reading', 'Books currently in progress (1–99% read)'),
            ('is:unread', 'Bookmarked books < 100% read'),
            ('is:read', 'Bookmarked books 100% finished'),
            ('is:bookmarked', 'All Moon+ or Kindle bookmarked books'),
            ('is:interesting', 'Books marked ⭐ Interesting'),
            ('is:badmeta', 'Books flagged as 🚩 Bad Metadata'),
            ('is:amazon', 'Books in your Amazon Kindle / Audible library'),
            ('is:kindle', 'Amazon Kindle purchased books'),
            ('is:audible', 'Amazon Audible audiobooks'),
        ],
        'has:': [
            ('has:cover', 'Books that have cover artwork'),
            ('has:isbn', 'Books that have an ISBN'),
            ('has:desc', 'Books that have a synopsis / description'),
            ('has:notes', 'Books with Moon+ Reader highlights/notes (.an)'),
            ('has:author', 'Books with a known author'),
        ],
        'missing:': [
            ('missing:cover', 'Books missing cover artwork'),
            ('missing:isbn', 'Books missing an ISBN'),
            ('missing:desc', 'Books missing a synopsis / description'),
            ('missing:author', 'Books with Unknown Author'),
        ],
        'format:': [
            ('format:epub', 'EPUB eBooks only'),
            ('format:pdf', 'PDF documents only'),
            ('format:mobi', 'MOBI eBooks only'),
            ('format:azw3', 'AZW3 Kindle eBooks only'),
            ('format:mp3', 'MP3 Audiobooks only'),
            ('format:m4b', 'M4B Audiobooks only'),
        ],
        'fmt:': [
            ('format:epub', 'EPUB eBooks only'),
            ('format:pdf', 'PDF documents only'),
            ('format:mobi', 'MOBI eBooks only'),
            ('format:azw3', 'AZW3 Kindle eBooks only'),
        ],
        'type:': [
            ('type:book', 'Physical Books'),
            ('type:magazine', 'Magazines & Periodicals'),
            ('type:kindle', 'Amazon Kindle Titles'),
            ('type:audible', 'Amazon Audible Titles'),
            ('type:audiobook', 'Local Audiobooks'),
        ],
        'sort:': [
            ('sort:newest', 'Sort by newest publication date first'),
            ('sort:oldest', 'Sort by oldest publication date first'),
            ('sort:added', 'Sort by most recently added to catalog'),
            ('sort:title', 'Sort alphabetically by title (A→Z)'),
            ('sort:author', 'Sort alphabetically by author (A→Z)'),
        ],
    }
    for prefix_key, options in value_Menus.items():
        if clean_low.startswith(prefix_key):
            for opt_kw, opt_desc in options:
                if clean_low in opt_kw.lower():
                    rep_tok = f"{neg_prefix}{opt_kw}"
                    hints.append({
                        'kind': 'keyword',
                        'keyword': rep_tok,
                        'full_query': f"{prefix_before_last}{rep_tok} ",
                        'label': opt_desc,
                        'category': prefix_key.rstrip(':').upper(),
                        'submit_immediately': False,
                    })
            return hints[:6]

    # Case 4: User is typing the start of a keyword operator (e.g. `auth`, `tit`, `pub`, `year`, `is`, `miss`, `+enr`)
    if ':' not in clean_low and len(clean_low) >= 2:
        for spec in SEARCH_KEYWORDS_SPEC:
            cand_keys = [spec['keyword']] + list(spec.get('aliases') or [])
            matched_k = next((ck for ck in cand_keys if ck.lower().startswith(clean_low) or ck.lower().lstrip('+!-').startswith(clean_low)), None)
            if matched_k:
                insert_val = spec['insert']
                if neg_prefix and not insert_val.startswith(('-', '+', '!')):
                    insert_val = f"-{insert_val}"
                trailing = '' if spec.get('is_prefix') else ' '
                hints.append({
                    'kind': 'keyword',
                    'keyword': f"{insert_val} (e.g. {spec['example']})",
                    'full_query': f"{prefix_before_last}{insert_val}{trailing}",
                    'label': f"{spec['label']} — {spec['desc']}",
                    'category': 'KEYWORD',
                    'submit_immediately': False,
                })
                if len(hints) >= 5:
                    break

    return hints


def perform_search(query_string, filters=None):
    """
    Executes a high-performance search with keyword operator filtering on CatalogItem (1.15M+ rows)
    without expensive unindexed cross-table tsvector/trigram joins.
    """
    queryset = CatalogItem.objects.prefetch_related('files')

    # Apply explicit form/API type & format filters
    item_types = list((filters or {}).get('item_type') or [])
    file_formats = list((filters or {}).get('file_format') or [])
    neg_file_formats: List[str] = []

    parsed = parse_search_query(query_string or '')

    # 1. Process field-specific filters extracted from search keywords
    for canon_field, val, negated in parsed['field_filters']:
        if canon_field == 'type':
            t_map = {
                'book': 'BOOK',
                'books': 'BOOK',
                'mag': 'MAGAZINE',
                'mags': 'MAGAZINE',
                'magazine': 'MAGAZINE',
                'magazines': 'MAGAZINE',
                'kindle': 'KINDLE',
                'audible': 'AUDIBLE',
                'audiobook': 'AUDIOBOOK',
                'audiobooks': 'AUDIOBOOK',
            }
            mapped_t = t_map.get(val.lower(), val.upper())
            if negated:
                queryset = queryset.exclude(item_type__iexact=mapped_t)
            else:
                item_types.append(mapped_t)
        elif canon_field == 'format':
            fmt_val = val.upper().lstrip('.')
            if negated:
                neg_file_formats.append(fmt_val)
            else:
                file_formats.append(fmt_val)
        elif canon_field == 'title':
            q_cond = Q(title__icontains=val)
            queryset = queryset.exclude(q_cond) if negated else queryset.filter(q_cond)
        elif canon_field == 'author':
            q_cond = Q(authors__icontains=val)
            queryset = queryset.exclude(q_cond) if negated else queryset.filter(q_cond)
        elif canon_field == 'publisher':
            q_cond = Q(publisher__icontains=val)
            queryset = queryset.exclude(q_cond) if negated else queryset.filter(q_cond)
        elif canon_field == 'isbn':
            clean_i = re.sub(r'[^0-9Xx]', '', val)
            q_cond = Q(isbn__icontains=clean_i or val)
            queryset = queryset.exclude(q_cond) if negated else queryset.filter(q_cond)
        elif canon_field == 'asin':
            q_cond = Q(asin__icontains=val.strip())
            queryset = queryset.exclude(q_cond) if negated else queryset.filter(q_cond)
        elif canon_field in ('description', 'series'):
            q_cond = Q(description__icontains=val) | Q(title__icontains=val)
            queryset = queryset.exclude(q_cond) if negated else queryset.filter(q_cond)
        elif canon_field in ('file', 'path'):
            loc_ids = list(
                FileLocation.objects.filter(file_name__icontains=val)
                .order_by()
                .values_list('catalog_item_id', flat=True)[:600]
            )
            queryset = queryset.exclude(id__in=loc_ids) if negated else queryset.filter(id__in=loc_ids)
        elif canon_field == 'tag':
            try:
                from .opds import get_enriched_tags_and_folders_index
                tags_idx = get_enriched_tags_and_folders_index()
                tag_to_titles = tags_idx.get('tag_to_titles') or {}
                matched_titles: set = set()
                val_low = val.lower()
                for t_name, t_list in tag_to_titles.items():
                    if t_name.lower() == val_low or val_low in t_name.lower():
                        matched_titles.update(t_list)
                if matched_titles:
                    t_Cond = Q(title__in=list(matched_titles)[:3500])
                    queryset = queryset.exclude(t_Cond) if negated else queryset.filter(t_Cond)
                else:
                    tag_to_paths = tags_idx.get('tag_to_paths') or {}
                    matched_paths: List[str] = []
                    for t_name, p_list in tag_to_paths.items():
                        if t_name.lower() == val_low or val_low in t_name.lower():
                            matched_paths.extend(p_list)
                    if matched_paths:
                        p_cond = Q(files__file_path__in=matched_paths[:2000])
                        queryset = (queryset.exclude(p_cond) if negated else queryset.filter(p_cond)).distinct()
                    elif not negated:
                        queryset = queryset.none()
            except Exception:
                pass
        elif canon_field == 'category':
            try:
                from .opds import get_enriched_tags_and_folders_index
                tags_idx = get_enriched_tags_and_folders_index()
                fld_to_titles = tags_idx.get('folder_to_titles') or {}
                matched_fld_titles: set = set()
                val_low = val.lower()
                exact_fld_name = val
                for f_name, t_list in fld_to_titles.items():
                    if f_name.lower() == val_low or val_low in f_name.lower():
                        matched_fld_titles.update(t_list)
                        if f_name.lower() == val_low:
                            exact_fld_name = f_name
                if matched_fld_titles:
                    c_cond = Q(title__in=list(matched_fld_titles)[:4500])
                    queryset = queryset.exclude(c_cond) if negated else queryset.filter(c_cond)
                else:
                    p_cond = (
                        Q(files__file_path__istartswith=f'/books/{exact_fld_name}/')
                        | Q(files__file_path__istartswith=f'/volume1/common/Media/Books/{exact_fld_name}/')
                    )
                    queryset = (queryset.exclude(p_cond) if negated else queryset.filter(p_cond)).distinct()
            except Exception:
                pass

    if item_types:
        queryset = queryset.filter(item_type__in=item_types)
    if file_formats:
        queryset = queryset.filter(files__file_format__in=file_formats).distinct()
    if neg_file_formats:
        queryset = queryset.exclude(files__file_format__in=neg_file_formats).distinct()

    # 2. Apply publication year / date range filters (`year:`, `after:`, `before:`)
    for op_code, y1, y2, negated in parsed['year_filters']:
        if op_code == 'exact':
            d_start = datetime.date(y1, 1, 1)
            d_end = datetime.date(y1, 12, 31)
            y_cond = Q(pub_date__gte=d_start, pub_date__lte=d_end)
        elif op_code == 'range' and y2 is not None:
            d_start = datetime.date(y1, 1, 1)
            d_end = datetime.date(y2, 12, 31)
            y_cond = Q(pub_date__gte=d_start, pub_date__lte=d_end)
        elif op_code == 'gte':
            y_cond = Q(pub_date__gte=datetime.date(y1, 1, 1))
        elif op_code == 'gt':
            y_cond = Q(pub_date__gt=datetime.date(y1, 12, 31))
        elif op_code == 'lte':
            y_cond = Q(pub_date__lte=datetime.date(y1, 12, 31))
        elif op_code == 'lt':
            y_cond = Q(pub_date__lt=datetime.date(y1, 1, 1))
        else:
            continue
        queryset = queryset.exclude(y_cond) if negated else queryset.filter(y_cond)

    # 3. Apply status & completeness flags (`is:...`, `has:...`, `missing:...`)
    for flag, negated in parsed['status_flags']:
        if flag == 'has_cover':
            cov_cond = (Q(cover_thumbnail__isnull=False) & ~Q(cover_thumbnail='')) | (Q(cover_image_url__isnull=False) & ~Q(cover_image_url=''))
            queryset = queryset.exclude(cov_cond) if negated else queryset.filter(cov_cond)
        elif flag == 'has_isbn':
            isbn_cond = Q(isbn__isnull=False) & ~Q(isbn='')
            queryset = queryset.exclude(isbn_cond) if negated else queryset.filter(isbn_cond)
        elif flag == 'has_asin':
            asin_cond = Q(asin__isnull=False) & ~Q(asin='')
            queryset = queryset.exclude(asin_cond) if negated else queryset.filter(asin_cond)
        elif flag == 'has_desc':
            desc_cond = Q(description__isnull=False) & ~Q(description='')
            queryset = queryset.exclude(desc_cond) if negated else queryset.filter(desc_cond)
        elif flag == 'has_author':
            unk_cond = Q(authors__isnull=True) | Q(authors='') | Q(authors__iexact='Unknown Author') | Q(authors__iexact='Unknown')
            queryset = queryset.filter(unk_cond) if negated else queryset.exclude(unk_cond)
        elif flag in ('enriched', 'needs_review'):
            try:
                from .opds import get_enriched_item_ids_index
                enr_idx = get_enriched_item_ids_index()
                if flag == 'enriched':
                    target_ids = enr_idx.get('enriched_ids') or []
                    if negated:
                        excl_ids = enr_idx.get('excluded_from_needs_enriching_ids') or target_ids
                        queryset = queryset.exclude(id__in=excl_ids)
                    else:
                        queryset = queryset.filter(id__in=target_ids)
                else:
                    target_ids = enr_idx.get('needs_review_ids') or []
                    queryset = queryset.exclude(id__in=target_ids) if negated else queryset.filter(id__in=target_ids)
            except Exception:
                pass
        elif flag == 'interesting':
            try:
                from .opds import get_interesting_item_ids
                int_ids = get_interesting_item_ids()
                queryset = queryset.exclude(id__in=int_ids) if negated else queryset.filter(id__in=int_ids)
            except Exception:
                pass
        elif flag == 'bad_metadata':
            try:
                from .opds import get_bad_metadata_item_ids
                bad_ids = get_bad_metadata_item_ids()
                queryset = queryset.exclude(id__in=bad_ids) if negated else queryset.filter(id__in=bad_ids)
            except Exception:
                pass
        elif flag in ('bookmarked', 'reading', 'read', 'unread', 'annotations'):
            try:
                from .opds import get_dynamic_amazon_shelf, get_dynamic_bookmarked_shelf
                bm_data = get_dynamic_bookmarked_shelf()
                if flag == 'annotations':
                    bm_ids = [it.id for it, meta in bm_data.get('items_with_meta', []) if meta.get('has_an')]
                elif flag == 'bookmarked':
                    amz_bm = get_dynamic_amazon_shelf().get('kindle_bookmarked_ids', [])
                    bm_ids = list(dict.fromkeys(list(bm_data.get('item_ids', [])) + list(amz_bm)))
                else:
                    moon_meta = bm_data.get('moon_meta_by_id', {})
                    amz_data = get_dynamic_amazon_shelf()
                    amz_by_id = amz_data.get('by_item_id', {})
                    cand_ids = list(dict.fromkeys(
                        [str(x) for x in bm_data.get('item_ids', [])]
                        + [str(x) for x in amz_data.get('kindle_bookmarked_ids', [])]
                    ))
                    bm_ids = []
                    for cid in cand_ids:
                        m_pct = float((moon_meta.get(cid) or {}).get('pct_val', 0.0) or 0.0)
                        a_pct = float((amz_by_id.get(cid) or {}).get('percentage_read', 0) or 0.0)
                        pct = max(m_pct, a_pct)
                        if flag == 'read' and pct >= 99.0:
                            bm_ids.append(cid)
                        elif flag == 'unread' and pct < 99.0:
                            bm_ids.append(cid)
                        elif flag == 'reading' and 0.0 < pct < 99.0:
                            bm_ids.append(cid)
                queryset = queryset.exclude(id__in=bm_ids) if negated else queryset.filter(id__in=bm_ids)
            except Exception:
                pass
        elif flag in ('amazon', 'kindle', 'kindle_plus', 'audible'):
            try:
                from .opds import get_dynamic_amazon_shelf
                amz_data = get_dynamic_amazon_shelf()
                key_map = {
                    'amazon': 'all_ids',
                    'kindle': 'kindle_purchased_ids',
                    'kindle_plus': 'kindle_plus_ids',
                    'audible': 'audible_ids',
                }
                amz_ids = amz_data.get(key_map[flag], [])
                queryset = queryset.exclude(id__in=amz_ids) if negated else queryset.filter(id__in=amz_ids)
            except Exception:
                pass

    # 4. Apply negated free-text exclusions (`-term`)
    for neg_term in parsed['neg_free_terms']:
        queryset = queryset.exclude(
            Q(title__icontains=neg_term) | Q(authors__icontains=neg_term)
        )

    # 5. Apply positive free-text terms
    free_q = ' '.join(parsed['free_terms']).strip()
    if free_q:
        clean_q = free_q.replace('-', '').replace(' ', '')
        if (clean_q.isdigit() and len(clean_q) in (10, 13)) or (
            len(clean_q) == 10 and clean_q.upper().startswith('B0')
        ):
            exact_qs = queryset.filter(Q(isbn__iexact=clean_q) | Q(asin__iexact=clean_q))
            if exact_qs.exists():
                return exact_qs

        if free_q.lower().endswith(('.epub', '.pdf', '.mobi', '.azw3', '.mp3', '.m4b')):
            loc_item_ids = list(
                FileLocation.objects.filter(file_name__icontains=free_q)
                .order_by()
                .values_list('catalog_item_id', flat=True)[:250]
            )
            return queryset.filter(id__in=loc_item_ids).order_by('title')

        for term in parsed['free_terms']:
            queryset = queryset.filter(
                Q(title__icontains=term)
                | Q(authors__icontains=term)
                | Q(isbn__icontains=term)
                | Q(asin__icontains=term)
                | Q(publisher__icontains=term)
            )

    # 6. Apply sort override from `sort:` keyword or relevance ranking
    sort_by = parsed['sort_override']
    if sort_by == 'pubdate_desc':
        return queryset.order_by(F('pub_date').desc(nulls_last=True), F('created_at').desc(nulls_last=True), 'title')
    if sort_by == 'pubdate_asc':
        return queryset.order_by(F('pub_date').asc(nulls_last=True), F('created_at').asc(nulls_last=True), 'title')
    if sort_by == 'added_desc':
        return queryset.order_by(F('created_at').desc(nulls_last=True), 'title')
    if sort_by == 'updated_desc':
        return queryset.order_by('-updated_at', 'title')
    if sort_by == 'title_asc':
        return queryset.order_by('title')
    if sort_by == 'title_desc':
        return queryset.order_by('-title')
    if sort_by == 'author_asc':
        return queryset.order_by('authors', 'title')
    if sort_by == 'author_desc':
        return queryset.order_by('-authors', 'title')

    if not free_q:
        return queryset

    relevance_rank = Case(
        When(title__iexact=free_q, then=Value(0)),
        When(title__istartswith=f"{free_q} ", then=Value(1)),
        When(title__istartswith=f"{free_q}:", then=Value(1)),
        When(title__istartswith=f"{free_q} -", then=Value(1)),
        When(title__istartswith=free_q, then=Value(2)),
        When(authors__iexact=free_q, then=Value(3)),
        When(authors__istartswith=free_q, then=Value(4)),
        When(title__icontains=f" {free_q} ", then=Value(5)),
        When(title__icontains=f" {free_q}", then=Value(6)),
        When(title__icontains=free_q, then=Value(7)),
        default=Value(8),
        output_field=IntegerField(),
    )

    return queryset.annotate(_search_rank=relevance_rank).order_by('_search_rank', 'title')
