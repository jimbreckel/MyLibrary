import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from django.conf import settings
from django.core.paginator import Paginator
from django.shortcuts import render
from django.utils.functional import cached_property
from django.views import View
from django.views.generic import ListView, TemplateView
from django.db.models import Count, Q
from rest_framework.views import APIView
from rest_framework.response import Response

from .models import CatalogItem, FileLocation, build_author_q_filter, format_normalized_authors_str
from .pg_scanner_db import connect_scanner_db, is_postgres_scanner_enabled
from .search import (
    SEARCH_KEYWORDS_SPEC,
    get_keyword_autocomplete_suggestions,
    parse_search_query,
    perform_search,
)


_MAIN_DASHBOARD_CACHE: dict = {
    'expires_at': 0.0,
    'stats': None,
    'available_formats': None,
    '_refreshing': False,
}


def _get_dashboard_meta_disk_paths() -> list[Path]:
    paths: list[Path] = []
    for d in (Path('/books_bad'), Path('/volume1/common/Media/Books_bad'), Path(r'U:\Media\Books_bad')):
        try:
            if d.exists() and d.is_dir():
                paths.append(d / '.dashboard_meta_cache.json')
                break
        except Exception:
            continue
    paths.append(Path('/tmp/mylibrary_dashboard_meta_cache.json'))
    return paths


def _get_cached_dashboard_meta() -> tuple[dict, list[str]]:
    """Return (stats_dict, available_formats_list) cached for 1800s with stale-while-revalidate."""
    import time as _time
    now_mono = _time.monotonic()
    if (
        _MAIN_DASHBOARD_CACHE['stats'] is not None
        and _MAIN_DASHBOARD_CACHE['available_formats'] is not None
        and now_mono < _MAIN_DASHBOARD_CACHE['expires_at']
    ):
        return _MAIN_DASHBOARD_CACHE['stats'], _MAIN_DASHBOARD_CACHE['available_formats']

    if _MAIN_DASHBOARD_CACHE['stats'] is None or _MAIN_DASHBOARD_CACHE['available_formats'] is None:
        for p in _get_dashboard_meta_disk_paths():
            try:
                if p.exists():
                    data = json.loads(p.read_text(encoding='utf-8'))
                    if isinstance(data.get('stats'), dict) and isinstance(data.get('available_formats'), list):
                        _MAIN_DASHBOARD_CACHE['stats'] = data['stats']
                        _MAIN_DASHBOARD_CACHE['available_formats'] = data['available_formats']
                        _MAIN_DASHBOARD_CACHE['expires_at'] = now_mono + 1800.0
                        return _MAIN_DASHBOARD_CACHE['stats'], _MAIN_DASHBOARD_CACHE['available_formats']
            except Exception:
                continue

    if (
        _MAIN_DASHBOARD_CACHE['stats'] is not None
        and _MAIN_DASHBOARD_CACHE['available_formats'] is not None
        and not _MAIN_DASHBOARD_CACHE.get('_refreshing')
    ):
        _MAIN_DASHBOARD_CACHE['_refreshing'] = True
        _MAIN_DASHBOARD_CACHE['expires_at'] = now_mono + 1800.0
        import threading
        threading.Thread(target=_rebuild_dashboard_meta_cache, daemon=True).start()
        return _MAIN_DASHBOARD_CACHE['stats'], _MAIN_DASHBOARD_CACHE['available_formats']

    return _rebuild_dashboard_meta_cache()


_SCANNER_CYCLE_CACHE: dict = {
    'expires_at': 0.0,
    'metrics': None,
}


def _get_scanner_cycle_header_disk_paths() -> list[Path]:
    paths: list[Path] = []
    for d in (Path('/books_bad'), Path('/volume1/common/Media/Books_bad'), Path(r'U:\Media\Books_bad')):
        try:
            if d.exists() and d.is_dir():
                paths.append(d / '.scanner_cycle_header.json')
                break
        except Exception:
            continue
    paths.append(Path('/tmp/mylibrary_scanner_cycle_header.json'))
    return paths


def _get_scanner_cycle_metrics(
    fallback_total_files: int = 0,
    fallback_complete_bookdb: int = 0,
    fallback_total_processed: int = 0,
    is_scanning: bool = False,
) -> dict:
    """
    Parse and persist the enrichment queue metrics from `scanner.log` so that:
    1. `eligible_to_enrich_initial` and `skipped_complete_bookdb` are persisted to
       `.scanner_cycle_header.json` and never vanish when `scanner.log` grows past 512 KB.
    2. Both the Status Dashboard (`Remaining to Enrich`) and Catalog Sidebar (`Needs Enriching`)
       share the exact same live `remaining_to_enrich` count.
    """
    import time as _time
    now_m = _time.monotonic()
    if _SCANNER_CYCLE_CACHE['metrics'] is not None and now_m < _SCANNER_CYCLE_CACHE['expires_at']:
        return _SCANNER_CYCLE_CACHE['metrics']

    base_dir = Path(settings.BASE_DIR)
    nas_scanner_dir = base_dir / 'nas_scanner'
    scanner_dir = (
        base_dir / 'scanner'
        if not (nas_scanner_dir / 'scanner_state.db').exists() and (base_dir / 'scanner' / 'scanner_state.db').exists()
        else nas_scanner_dir
    )
    scanner_log_path = scanner_dir / 'logs' / 'scanner.log'
    config_file = scanner_dir / 'config.json'

    persisted: dict = {}
    for p in _get_scanner_cycle_header_disk_paths():
        try:
            if p.exists():
                persisted = json.loads(p.read_text(encoding='utf-8')) or {}
                if persisted:
                    break
        except Exception:
            continue

    eligible_to_enrich_initial = int(persisted.get('eligible_to_enrich_initial') or 0)
    skipped_complete_bookdb = int(persisted.get('skipped_complete_bookdb') or fallback_complete_bookdb or 0)
    new_changed_in_walk = int(persisted.get('new_changed_in_walk') or 0)
    cycle_inspected = int(persisted.get('cycle_inspected') or 0)
    cycle_net_used = int(persisted.get('cycle_net_used') or 0)
    cycle_net_cap = 4000
    if config_file.exists():
        try:
            cfg_data = json.loads(config_file.read_text(encoding='utf-8')) or {}
            em_cfg = cfg_data.get('epubmeta') or {}
            if em_cfg.get('max_files'):
                cycle_net_cap = max(4000, int(em_cfg['max_files'])) if int(em_cfg['max_files']) == 2500 else int(em_cfg['max_files'])
        except Exception:
            pass

    remaining_to_catalog = 0
    catalog_sync_chunk_info = ''
    discovered_files_total = int(persisted.get('discovered_files_total') or fallback_total_files or 0)
    header_dirty = False

    if scanner_log_path.exists():
        try:
            with scanner_log_path.open('rb') as lf:
                lf.seek(0, os.SEEK_END)
                fsize = lf.tell()
                lf.seek(max(0, fsize - 512 * 1024), os.SEEK_SET)
                tail_text = lf.read().decode('utf-8', errors='replace')

                elig_pattern = (
                    r'out of (\d+) eligible:\s*(\d+) new/changed,\s*(\d+) backfill;\s*'
                    r'skipped (\d+) previously processed,\s*(\d+) complete/DRM/invalid'
                )
                elig_matches = list(re.finditer(elig_pattern, tail_text))
                # If the eligibility header scrolled past the 512 KB tail and hasn't been persisted yet,
                # scan up to the last 8 MB of scanner.log once to recover and persist it.
                if not elig_matches and (skipped_complete_bookdb <= 300000 or eligible_to_enrich_initial <= 0) and fsize > 512 * 1024:
                    lf.seek(max(0, fsize - 8 * 1024 * 1024), os.SEEK_SET)
                    wider_text = lf.read().decode('utf-8', errors='replace')
                    elig_matches_wide = list(re.finditer(elig_pattern, wider_text))
                    if elig_matches_wide:
                        m_w = elig_matches_wide[-1]
                        eligible_to_enrich_initial = int(m_w.group(1))
                        new_changed_in_walk = int(m_w.group(2))
                        skipped_complete_bookdb = int(m_w.group(5))
                        header_dirty = True

            last_elig_pos = -1
            if elig_matches:
                m_el = elig_matches[-1]
                last_elig_pos = m_el.start()
                new_elig = int(m_el.group(1))
                new_walk = int(m_el.group(2))
                new_skip_comp = int(m_el.group(5))
                if (
                    new_elig != eligible_to_enrich_initial
                    or new_skip_comp != skipped_complete_bookdb
                ):
                    header_dirty = True
                eligible_to_enrich_initial = new_elig
                new_changed_in_walk = new_walk
                skipped_complete_bookdb = new_skip_comp

            start_matches = list(re.finditer(r'Starting sync_files\.py at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', tail_text))
            last_start_pos = start_matches[-1].start() if start_matches else 0

            prog_matches = list(
                re.finditer(r'\[(\d+)/(\d+)\s*\|\s*net:(\d+)/(\d+|all)\]\s*Inspecting', tail_text)
            )
            if prog_matches and prog_matches[-1].start() > max(last_elig_pos, last_start_pos):
                m_pr = prog_matches[-1]
                new_inspected = int(m_pr.group(1))
                new_elig_pr = int(m_pr.group(2))
                new_net = int(m_pr.group(3))
                if abs(new_inspected - cycle_inspected) >= 20 or new_elig_pr != eligible_to_enrich_initial:
                    header_dirty = True
                cycle_inspected = new_inspected
                eligible_to_enrich_initial = new_elig_pr
                cycle_net_used = new_net
                if m_pr.group(4).isdigit() and is_scanning:
                    logged_cap = int(m_pr.group(4))
                    cycle_net_cap = 4000 if logged_cap == 2500 else logged_cap
            elif elig_matches or start_matches:
                cycle_inspected = 0
                cycle_net_used = 0

            parse_matches = list(
                re.finditer(r'Found (\d+) files needing metadata extraction and syncing', tail_text)
            )
            chunk_matches = list(
                re.finditer(r'Processing Chunk (\d+)/(\d+)\s*\((\d+) files\)', tail_text)
            )
            fin_matches = list(
                re.finditer(
                    r'Scan Finished\.\s*Discovered:\s*(\d+)\s*\|\s*Synced:\s*(\d+)\s*\|\s*Skipped:\s*(\d+)\s*\|\s*Failed:\s*(\d+)',
                    tail_text,
                )
            )
            for fm in reversed(fin_matches):
                disc_val = int(fm.group(1))
                if disc_val > 0:
                    discovered_files_total = max(discovered_files_total, disc_val)
                    break

            cycle_slice = tail_text[last_start_pos:]
            dw_matches = list(re.finditer(r'\[dirs_walked=(\d+)\]', cycle_slice))
            dirs_walked = int(dw_matches[-1].group(1)) if dw_matches else cycle_slice.count('Walking directory:')
            cycle_synced_log = 0
            cycle_skipped_log = 0
            cycle_failed_log = 0

            last_parse_pos = parse_matches[-1].start() if parse_matches else -1
            last_fin_pos = fin_matches[-1].start() if fin_matches else -1

            if fin_matches and fin_matches[-1].start() >= last_start_pos:
                fm_cur = fin_matches[-1]
                cycle_synced_log = int(fm_cur.group(2))
                cycle_skipped_log = int(fm_cur.group(3))
                cycle_failed_log = int(fm_cur.group(4))
            elif parse_matches and parse_matches[-1].start() >= last_start_pos:
                new_changed_in_walk = max(new_changed_in_walk, int(parse_matches[-1].group(1)))
                if chunk_matches and chunk_matches[-1].start() > last_parse_pos:
                    c_idx = int(chunk_matches[-1].group(1))
                    cycle_synced_log = max(0, (c_idx - 1) * 250 + int(chunk_matches[-1].group(3)))

            if last_parse_pos > last_fin_pos and is_scanning:
                total_to_parse = int(parse_matches[-1].group(1))
                if chunk_matches and chunk_matches[-1].start() > last_parse_pos:
                    c_idx = int(chunk_matches[-1].group(1))
                    c_tot = int(chunk_matches[-1].group(2))
                    remaining_to_catalog = max(0, total_to_parse - (c_idx - 1) * 250)
                    catalog_sync_chunk_info = f'Chunk {c_idx}/{c_tot}'
                else:
                    remaining_to_catalog = total_to_parse
            elif is_scanning and last_elig_pos > last_fin_pos:
                remaining_to_catalog = new_changed_in_walk
        except Exception:
            dirs_walked = 0
            cycle_synced_log = 0
            cycle_skipped_log = 0
            cycle_failed_log = 0
    else:
        dirs_walked = 0
        cycle_synced_log = 0
        cycle_skipped_log = 0
        cycle_failed_log = 0

    if eligible_to_enrich_initial > 0:
        remaining_to_enrich = max(0, eligible_to_enrich_initial - cycle_inspected)
    elif persisted.get('remaining_to_enrich'):
        remaining_to_enrich = int(persisted['remaining_to_enrich'])
    else:
        remaining_to_enrich = max(
            0, fallback_total_files - skipped_complete_bookdb - fallback_total_processed
        )

    result = {
        'eligible_to_enrich_initial': eligible_to_enrich_initial,
        'skipped_complete_bookdb': skipped_complete_bookdb,
        'new_changed_in_walk': new_changed_in_walk,
        'dirs_walked': dirs_walked,
        'cycle_synced_log': cycle_synced_log,
        'cycle_skipped_log': cycle_skipped_log,
        'cycle_failed_log': cycle_failed_log,
        'cycle_inspected': cycle_inspected,
        'cycle_net_used': cycle_net_used,
        'cycle_net_cap': cycle_net_cap,
        'remaining_to_enrich': remaining_to_enrich,
        'remaining_to_catalog': remaining_to_catalog,
        'catalog_sync_chunk_info': catalog_sync_chunk_info,
        'discovered_files_total': discovered_files_total,
    }
    _SCANNER_CYCLE_CACHE['metrics'] = result
    _SCANNER_CYCLE_CACHE['expires_at'] = now_m + 5.0

    if header_dirty or not persisted.get('remaining_to_enrich'):
        raw_json = json.dumps(result)
        for p in _get_scanner_cycle_header_disk_paths():
            try:
                p.write_text(raw_json, encoding='utf-8')
            except Exception:
                continue
    return result


def _rebuild_dashboard_meta_cache() -> tuple[dict, list[str]]:
    import datetime as _dt
    import time as _time
    today_dt = _dt.date.today()
    cutoff_1yr = today_dt - _dt.timedelta(days=365)
    catalog_agg = CatalogItem.objects.aggregate(
        total_items=Count('id'),
        books_count=Count('id', filter=Q(item_type='BOOK')),
        magazines_count=Count('id', filter=Q(item_type='MAGAZINE')),
        kindle_count=Count('id', filter=Q(item_type='KINDLE')),
        audible_count=Count('id', filter=Q(item_type='AUDIBLE')),
        audiobook_count=Count('id', filter=Q(item_type='AUDIOBOOK')),
        recent_pub_count=Count('id', filter=Q(pub_date__gte=cutoff_1yr, pub_date__lte=today_dt)),
    )
    total_files = FileLocation.objects.count()
    vx_files_count = FileLocation.objects.filter(library_name='Archive-X').count()
    available_formats = list(
        FileLocation.objects.order_by().values_list('file_format', flat=True).distinct().order_by('file_format')
    )
    stats = {
        'total_items': catalog_agg['total_items'] or 0,
        'total_files': total_files or 0,
        'books_count': catalog_agg['books_count'] or 0,
        'magazines_count': catalog_agg['magazines_count'] or 0,
        'archive_x_count': vx_files_count or 0,
        'kindle_count': catalog_agg['kindle_count'] or 0,
        'audible_count': catalog_agg['audible_count'] or 0,
        'audiobook_count': catalog_agg['audiobook_count'] or 0,
        'recent_pub_count': catalog_agg['recent_pub_count'] or 0,
    }
    try:
        from .opds import get_dynamic_amazon_shelf

        amz_counts = get_dynamic_amazon_shelf().get('counts', {})
        amz_kindle = (amz_counts.get('kindle_purchased') or 0) + (amz_counts.get('kindle_plus') or 0)
        amz_audible = amz_counts.get('audible') or 0
        if amz_kindle > stats['kindle_count']:
            stats['kindle_count'] = amz_kindle
        if amz_audible > stats['audible_count']:
            stats['audible_count'] = amz_audible
    except Exception:
        pass
    _MAIN_DASHBOARD_CACHE['stats'] = stats
    _MAIN_DASHBOARD_CACHE['available_formats'] = available_formats
    _MAIN_DASHBOARD_CACHE['expires_at'] = _time.monotonic() + 1800.0
    _MAIN_DASHBOARD_CACHE['_refreshing'] = False
    try:
        disk_payload = json.dumps({'stats': stats, 'available_formats': available_formats})
        for p in _get_dashboard_meta_disk_paths():
            try:
                p.write_text(disk_payload, encoding='utf-8')
            except Exception:
                continue
    except Exception:
        pass
    try:
        summary_file = Path('/tmp/mylibrary_summary_counts.json')
        existing = {}
        if summary_file.exists():
            try:
                existing = json.loads(summary_file.read_text(encoding='utf-8')) or {}
            except Exception:
                existing = {}
        existing.update({
            'total_items': stats['total_items'],
            'total_files': stats['total_files'],
            'books_count': stats['books_count'],
            'magazines_count': stats['magazines_count'],
        })
        summary_file.write_text(json.dumps(existing), encoding='utf-8')
    except Exception:
        pass
    return stats, available_formats


def adjust_cached_books_count(delta: int) -> None:
    """Adjust in-memory and disk cached `books_count` and `total_items` when items are split or merged."""
    if not delta:
        return
    try:
        stats = _MAIN_DASHBOARD_CACHE.get('stats')
        if isinstance(stats, dict):
            if 'books_count' in stats:
                stats['books_count'] = max(0, int(stats['books_count'] or 0) + int(delta))
            if 'total_items' in stats:
                stats['total_items'] = max(0, int(stats['total_items'] or 0) + int(delta))
            disk_payload = json.dumps({
                'stats': stats,
                'available_formats': _MAIN_DASHBOARD_CACHE.get('available_formats') or [],
            })
            for p in _get_dashboard_meta_disk_paths():
                try:
                    p.write_text(disk_payload, encoding='utf-8')
                except Exception:
                    continue
    except Exception:
        pass


_FAST_EXCLUDE_WINDOW_CACHE: dict[str, dict] = {}
_FAST_EXCLUDE_DISK_FILE = Path('/tmp/mylibrary_needs_enriching_window.json')


def _load_fast_exclude_window(cache_key: str) -> dict:
    import time as _time
    import uuid as _uuid

    now_m = _time.monotonic()
    now_w = _time.time()
    win = _FAST_EXCLUDE_WINDOW_CACHE.get(cache_key)
    if win and (now_m - win.get('updated_at', 0.0) > 1800.0):
        win = None

    try:
        if _FAST_EXCLUDE_DISK_FILE.exists():
            st_mtime = _FAST_EXCLUDE_DISK_FILE.stat().st_mtime
            if (now_w - st_mtime < 1800.0) and (win is None or st_mtime > win.get('disk_mtime', 0.0)):
                raw = json.loads(_FAST_EXCLUDE_DISK_FILE.read_text(encoding='utf-8')) or {}
                entry = raw.get(cache_key)
                if isinstance(entry, dict):
                    cands = [_uuid.UUID(str(x)) for x in (entry.get('candidate_ids') or ()) if x]
                    win = {
                        'candidate_ids': cands,
                        'next_cursor': int(entry.get('next_cursor') or 0),
                        'last_title': str(entry.get('last_title') or ''),
                        'updated_at': now_m,
                        'disk_mtime': st_mtime,
                    }
                    _FAST_EXCLUDE_WINDOW_CACHE[cache_key] = win
    except Exception:
        pass

    if win is None:
        win = {
            'candidate_ids': [],
            'next_cursor': 0,
            'last_title': '',
            'updated_at': now_m,
            'disk_mtime': now_w,
        }
        _FAST_EXCLUDE_WINDOW_CACHE[cache_key] = win
    return win


def _save_fast_exclude_window(cache_key: str, win: dict) -> None:
    import time as _time

    now_m = _time.monotonic()
    now_w = _time.time()
    win['updated_at'] = now_m
    win['disk_mtime'] = now_w
    _FAST_EXCLUDE_WINDOW_CACHE[cache_key] = win
    try:
        existing: dict = {}
        if _FAST_EXCLUDE_DISK_FILE.exists():
            try:
                existing = json.loads(_FAST_EXCLUDE_DISK_FILE.read_text(encoding='utf-8')) or {}
            except Exception:
                existing = {}
        if len(existing) > 16:
            existing.clear()
        existing[cache_key] = {
            'next_cursor': int(win.get('next_cursor') or 0),
            'last_title': str(win.get('last_title') or ''),
            'candidate_ids': [str(x) for x in (win.get('candidate_ids') or ())[:2500]],
        }
        tmp_p = _FAST_EXCLUDE_DISK_FILE.with_suffix(f'.tmp.{os.getpid()}')
        tmp_p.write_text(json.dumps(existing), encoding='utf-8')
        tmp_p.replace(_FAST_EXCLUDE_DISK_FILE)
        try:
            win['disk_mtime'] = _FAST_EXCLUDE_DISK_FILE.stat().st_mtime
        except Exception:
            pass
    except Exception:
        pass


class _PrecomputedCountPaginator(Paginator):
    """Paginator that uses a precomputed total count when available to skip full-table COUNT(*)."""

    def __init__(self, object_list, per_page, precomputed_count=None, **kwargs):
        super().__init__(object_list, per_page, **kwargs)
        self._precomputed_count = precomputed_count
        self._fast_exclude_ids_set = getattr(object_list, '_fast_exclude_ids_set', None)
        self._fast_exclude_cache_key = getattr(object_list, '_fast_exclude_cache_key', None)

    def _fill_fast_exclude_window(self, min_needed: int = 48) -> tuple[list, dict, int, str, bool]:
        excl_set = self._fast_exclude_ids_set or set()
        cache_key = self._fast_exclude_cache_key or 'default'
        win = _load_fast_exclude_window(cache_key)
        matched_ids = [
            cid for cid in (win.get('candidate_ids') or ())
            if cid not in excl_set and str(cid) not in excl_set
        ]
        seen_matched = set(matched_ids)
        cursor = int(win.get('next_cursor') or 0)
        last_title = str(win.get('last_title') or '')
        initial_cursor = cursor
        initial_last_title = last_title
        key_parts = cache_key.split('|')
        sort_part = key_parts[3] if len(key_parts) > 3 else ''
        use_title_keyset = sort_part in ('', 'title_asc')
        target_fill = max(min_needed + 24, 96)
        batch_sz = max(min_needed + 200, 800)
        window_dirty = len(matched_ids) != len(win.get('candidate_ids') or ())

        for pass_idx in range(2):
            while len(matched_ids) < target_fill:
                if use_title_keyset:
                    scan_qs = self.object_list.filter(title__gt=last_title) if last_title else self.object_list
                    cand_rows = list(scan_qs.values_list('id', 'title')[:batch_sz])
                    if not cand_rows:
                        break
                    last_title = str(cand_rows[-1][1] or last_title)
                    cursor += len(cand_rows)
                    window_dirty = True
                    for cid, _ctitle in cand_rows:
                        if cid not in excl_set and str(cid) not in excl_set and cid not in seen_matched:
                            matched_ids.append(cid)
                            seen_matched.add(cid)
                    if len(cand_rows) < batch_sz:
                        break
                else:
                    cand_batch = list(self.object_list.values_list('id', flat=True)[cursor:cursor + batch_sz])
                    if not cand_batch:
                        break
                    cursor += len(cand_batch)
                    window_dirty = True
                    for cid in cand_batch:
                        if cid not in excl_set and str(cid) not in excl_set and cid not in seen_matched:
                            matched_ids.append(cid)
                            seen_matched.add(cid)
                    if len(cand_batch) < batch_sz:
                        break
                    batch_sz = min(batch_sz * 2, 2000)
            if len(matched_ids) >= min_needed or (initial_cursor == 0 and not initial_last_title) or pass_idx == 1:
                break
            cursor = 0
            last_title = ''
            initial_cursor = 0
            initial_last_title = ''

        if window_dirty:
            win['candidate_ids'] = matched_ids[:2500]
            win['next_cursor'] = cursor
            win['last_title'] = last_title
            _save_fast_exclude_window(cache_key, win)
        return matched_ids, win, cursor, last_title, window_dirty

    @cached_property
    def count(self):
        if self._precomputed_count is not None:
            return self._precomputed_count
        try:
            if self._fast_exclude_ids_set:
                matched_ids, _win, cursor, _lt, _dirty = self._fill_fast_exclude_window(min_needed=48)
                if len(matched_ids) >= 72 and cursor >= 800:
                    return max(len(matched_ids), 1200)
                return len(matched_ids[:1200])
            return len(list(self.object_list.values_list('id', flat=True)[:1200]))
        except Exception:
            return super().count

    def page(self, number):
        excl_set = self._fast_exclude_ids_set
        if not excl_set:
            return super().page(number)
        number = self.validate_number(number)
        bottom = (number - 1) * self.per_page
        top = bottom + self.per_page
        if top + self.orphans >= self.count:
            top = self.count

        from .opds import get_file_enrich_diagnostic, is_file_enriched

        cache_key = self._fast_exclude_cache_key or 'default'
        try:
            matched_ids, win, cursor, last_title, window_dirty = self._fill_fast_exclude_window(min_needed=top + 24)
            cand_slice = matched_ids[bottom:top + 24]
            if not cand_slice:
                return self._get_page([], number, self)

            items_by_id = {
                it.id: it
                for it in CatalogItem.objects.filter(id__in=cand_slice).order_by().prefetch_related('files')
            }
            need_cnt = max(0, top - bottom)
            page_items = []
            stale_ids = set()
            for pid in cand_slice:
                it = items_by_id.get(pid)
                if not it:
                    stale_ids.add(pid)
                    continue
                f_list = list(it.files.all())
                already_done = any(
                    f.file_path and (is_file_enriched(f.file_path) or get_file_enrich_diagnostic(f.file_path) is not None)
                    for f in f_list
                )
                if already_done:
                    stale_ids.add(pid)
                    excl_set.add(pid)
                    continue
                page_items.append(it)
                if len(page_items) >= need_cnt:
                    break

            if stale_ids:
                matched_ids = [cid for cid in matched_ids if cid not in stale_ids]
                win['candidate_ids'] = matched_ids[:2500]
                win['next_cursor'] = cursor
                win['last_title'] = last_title
                _save_fast_exclude_window(cache_key, win)

            return self._get_page(page_items, number, self)
        except Exception:
            return super().page(number)

    def validate_number(self, number):
        """
        Gracefully clamp out-of-range page numbers (e.g. when duplicate items are merged
        or moved to toMags and page 2 shrinks into page 1) instead of raising Http404.
        """
        try:
            if isinstance(number, float) and not number.is_integer():
                raise ValueError
            number = int(number)
        except (TypeError, ValueError):
            return 1
        if number < 1:
            return 1
        if number > self.num_pages:
            return max(1, self.num_pages)
        return number


class DashboardView(ListView):
    """
    Main dashboard view to display, search, and filter catalog items.
    Renders the modern glassmorphic web catalog UI.
    """
    model = CatalogItem
    template_name = 'catalog/index.html'
    context_object_name = 'items'
    paginate_by = 24

    def dispatch(self, request, *args, **kwargs):
        v_flag = (request.GET.get('v') or '').strip().lower()
        if v_flag == 'x':
            request.session['vx_unlocked'] = True
            request.session.set_expiry(0)
        elif v_flag in ('0', 'off', 'false', 'lock'):
            request.session['vx_unlocked'] = False
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def _compute_bookmark_progress_groups(bm_data, amz_data, shelf=''):
        """
        Return bookmark progress maps and categorized ID sets for bookmarked items
        across Moon+ Reader (.po / .an) and Amazon Kindle bookmarks.
        """
        moon_meta_by_id = bm_data.get('moon_meta_by_id', {})
        amz_by_id = amz_data.get('by_item_id', {})
        amz_bm_ids = set(str(x) for x in amz_data.get('kindle_bookmarked_ids', []))
        moon_bm_ids = set(str(x) for x in bm_data.get('item_ids', []))

        if shelf == 'annotations':
            candidate_ids = [str(it.id) for it, meta in bm_data.get('items_with_meta', []) if meta.get('has_an')]
        elif shelf == 'bookmarked':
            candidate_ids = [str(x) for x in bm_data.get('item_ids', [])]
        elif shelf == 'amazon_bookmarked':
            candidate_ids = [str(x) for x in amz_data.get('kindle_bookmarked_ids', [])]
        elif shelf in ('amazon', 'kindle_plus', 'audible', 'amazon_only', 'amazon_matched', 'amazon_all'):
            # Within an Amazon shelf, consider items that have either Amazon or Moon+ bookmarks
            candidate_ids = [
                str(x) for x in amz_data.get('all_ids', [])
                if str(x) in amz_bm_ids or str(x) in moon_bm_ids
            ]
        else:
            # Union of Moon+ bookmarked items and Amazon bookmarked items, preserving Moon+ recency first
            seen = set()
            candidate_ids = []
            for x in list(bm_data.get('item_ids', [])) + list(amz_data.get('kindle_bookmarked_ids', [])):
                sx = str(x)
                if sx not in seen:
                    seen.add(sx)
                    candidate_ids.append(sx)

        pct_by_id = {}
        read_ids = []       # >= 99% (100% Read)
        unread_ids = []     # < 99% (Not yet 100% Read)
        reading_ids = []    # > 0% and < 99% (In Progress)
        unstarted_ids = []  # 0%

        for cid in candidate_ids:
            m_info = moon_meta_by_id.get(cid)
            a_info = amz_by_id.get(cid)
            m_pct = float(m_info.get('pct_val', 0.0)) if m_info else 0.0
            a_pct = float(a_info.get('percentage_read', 0) or 0) if a_info else 0.0
            if shelf == 'amazon_bookmarked':
                pct = a_pct if a_info else m_pct
            elif shelf in ('bookmarked', 'annotations'):
                pct = m_pct if m_info else a_pct
            else:
                pct = max(m_pct, a_pct)
            pct_by_id[cid] = pct
            if pct >= 99.0:
                read_ids.append(cid)
            else:
                unread_ids.append(cid)
                if pct > 0.0:
                    reading_ids.append(cid)
                else:
                    unstarted_ids.append(cid)

        return {
            'all_bm_ids': candidate_ids,
            'pct_by_id': pct_by_id,
            'read_ids': read_ids,
            'unread_ids': unread_ids,
            'reading_ids': reading_ids,
            'unstarted_ids': unstarted_ids,
        }

    def get_queryset(self):
        from django.db.models import Case, F, IntegerField, When
        from .opds import (
            _get_enriched_paths_list,
            ENRICHABLE_FORMATS,
            get_bad_metadata_item_ids,
            get_dynamic_amazon_shelf,
            get_dynamic_bookmarked_shelf,
            get_enriched_item_ids_index,
            get_enriched_tags_and_folders_index,
            get_interesting_item_ids,
            get_needs_cover_enriched_shelf,
            get_packt_books_shelf_ids,
            get_removed_protection_item_ids,
            get_tomags_item_ids,
        )

        vx_unlocked = bool(self.request.session.get('vx_unlocked', False))
        raw_query = self.request.GET.get('q', '').strip()
        query = re.sub(r'(?:^|\s)[+!]enrich\b', '', raw_query, flags=re.IGNORECASE).strip()
        shelf = self.request.GET.get('shelf', '').strip().lower()
        selected_tags = [t.strip() for t in self.request.GET.getlist('tag') if t.strip()]
        selected_tag = selected_tags[0] if selected_tags else ''
        selected_folders = [
            f.strip()
            for f in self.request.GET.getlist('folder')
            if f.strip() and (vx_unlocked or f.strip().lower() != 'archive-x')
        ]
        selected_folder = selected_folders[0] if selected_folders else ''
        selected_series = [s.strip() for s in self.request.GET.getlist('series') if s.strip()]
        selected_author = self.request.GET.get('author', '').strip()
        sort_by = self.request.GET.get('sort', '').strip().lower()
        if (selected_author or selected_series) and not sort_by:
            sort_by = 'pubdate_desc'
        read_status = self.request.GET.get('read_status', '').strip().lower()
        enrich_status = self.request.GET.get('enrich_status', '').strip().lower()

        # Extract filters
        item_types = [t for t in self.request.GET.getlist('type') if t]
        file_formats = [f for f in self.request.GET.getlist('format') if f]

        filters = {'vx_unlocked': vx_unlocked}
        if item_types:
            filters['item_type'] = item_types
        if file_formats:
            filters['file_format'] = file_formats
        if selected_folders:
            filters['folders'] = selected_folders
        if selected_series:
            filters['series'] = selected_series

        # Run Postgres full-text or similarity search
        qs = perform_search(query, filters)
        fast_exclude_ids_set = getattr(qs, '_fast_exclude_ids_set', None)
        if selected_author:
            qs = qs.filter(build_author_q_filter(selected_author))

        bm_data = None
        amz_data = None

        if shelf in ('bookmarked', 'annotations'):
            bm_data = get_dynamic_bookmarked_shelf()
            if shelf == 'annotations':
                bm_ids = [it.id for it, meta in bm_data.get('items_with_meta', []) if meta.get('has_an')]
            else:
                bm_ids = bm_data.get('item_ids', [])
            qs = qs.filter(id__in=bm_ids)
            if bm_ids and not query and not sort_by:
                order_expr = Case(
                    *[When(id=pk, then=pos) for pos, pk in enumerate(bm_ids)],
                    default=len(bm_ids),
                    output_field=IntegerField(),
                )
                qs = qs.annotate(_bm_order=order_expr).order_by('_bm_order')
        elif shelf in ('amazon', 'kindle_plus', 'amazon_bookmarked', 'audible', 'amazon_only', 'amazon_matched', 'amazon_all'):
            amz_data = get_dynamic_amazon_shelf()
            if shelf == 'amazon':
                amz_ids = amz_data.get('kindle_purchased_ids', [])
            elif shelf == 'kindle_plus':
                amz_ids = amz_data.get('kindle_plus_ids', [])
            elif shelf == 'amazon_bookmarked':
                amz_ids = amz_data.get('kindle_bookmarked_ids', [])
            elif shelf == 'audible':
                amz_ids = amz_data.get('audible_ids', [])
            elif shelf == 'amazon_only':
                amz_ids = amz_data.get('amazon_only_ids', [])
            elif shelf == 'amazon_matched':
                amz_ids = amz_data.get('amazon_matched_ids', [])
            else:
                amz_ids = amz_data.get('all_ids', [])
            qs = qs.filter(id__in=amz_ids)
            if amz_ids and not query and not sort_by:
                order_expr = Case(
                    *[When(id=pk, then=pos) for pos, pk in enumerate(amz_ids)],
                    default=len(amz_ids),
                    output_field=IntegerField(),
                )
                qs = qs.annotate(_amz_order=order_expr).order_by('_amz_order')
        elif shelf == 'enriched':
            enr_idx = get_enriched_item_ids_index()
            qs = qs.filter(id__in=enr_idx['enriched_ids'])
        elif shelf in ('no_match', 'no_isbn', 'not_found'):
            enr_idx = get_enriched_item_ids_index()
            nm_ids = enr_idx.get('no_match_ids') or []
            qs = qs.filter(id__in=nm_ids)
            if nm_ids and not query and not sort_by:
                order_expr = Case(
                    *[When(id=pk, then=pos) for pos, pk in enumerate(nm_ids[:1500])],
                    default=len(nm_ids),
                    output_field=IntegerField(),
                )
                qs = qs.annotate(_nm_order=order_expr).order_by('_nm_order', 'title')
        elif shelf in ('needs_review', 'in_review'):
            enr_idx = get_enriched_item_ids_index()
            rev_type = self.request.GET.get('review_type', '').strip().lower()
            strict_ids = enr_idx.get('strict_in_review_ids') or []
            if rev_type == 'all':
                qs = qs.filter(id__in=enr_idx['needs_review_ids'])
                if strict_ids and not query and not sort_by:
                    qs = qs.annotate(
                        _rev_prio=Case(
                            When(id__in=strict_ids, then=0),
                            default=1,
                            output_field=IntegerField(),
                        )
                    ).order_by('_rev_prio', 'title')
            else:
                qs = qs.filter(id__in=strict_ids)
                if strict_ids and not query and not sort_by:
                    order_expr = Case(
                        *[When(id=pk, then=pos) for pos, pk in enumerate(strict_ids[:1500])],
                        default=len(strict_ids),
                        output_field=IntegerField(),
                    )
                    qs = qs.annotate(_rev_order=order_expr).order_by('_rev_order', 'title')
        elif shelf == 'needs_enriching':
            enr_idx = get_enriched_item_ids_index()
            if not item_types:
                qs = qs.filter(item_type='BOOK')
            complete_q = (
                (
                    (Q(isbn__isnull=False) & ~Q(isbn=''))
                    | (Q(asin__isnull=False) & ~Q(asin=''))
                    | Q(pub_date__isnull=False)
                )
                & Q(description__isnull=False) & ~Q(description='')
                & Q(authors__isnull=False) & ~Q(authors='Unknown Author') & ~Q(authors='')
            )
            qs = qs.exclude(complete_q)
            excl_ids = enr_idx.get('excluded_from_needs_enriching_ids') or enr_idx['enriched_ids']
            tm_excl = set(get_tomags_item_ids(sync_disk=False))
            fast_exclude_ids_set = (enr_idx.get('excluded_from_needs_enriching_ids_set') or set(excl_ids)) | tm_excl
        elif shelf in ('removed_protection', 'nodrm'):
            rp_ids = get_removed_protection_item_ids()
            qs = qs.filter(id__in=rp_ids)
            if not sort_by:
                qs = qs.order_by(F('created_at').desc(nulls_last=True), 'title')
        elif shelf in ('packt', 'packt_books'):
            packt_ids = get_packt_books_shelf_ids()
            qs = qs.filter(id__in=packt_ids)
            if packt_ids and not query and (not sort_by or sort_by in ('filedate_desc', 'added_desc')):
                order_expr = Case(
                    *[When(id=pk, then=pos) for pos, pk in enumerate(packt_ids)],
                    default=len(packt_ids),
                    output_field=IntegerField(),
                )
                qs = qs.annotate(_packt_order=order_expr).order_by('_packt_order')
            elif packt_ids and sort_by in ('filedate_asc', 'added_asc'):
                rev_ids = list(reversed(packt_ids))
                order_expr = Case(
                    *[When(id=pk, then=pos) for pos, pk in enumerate(rev_ids)],
                    default=len(rev_ids),
                    output_field=IntegerField(),
                )
                qs = qs.annotate(_packt_order=order_expr).order_by('_packt_order')
        elif shelf == 'interesting':
            int_ids = get_interesting_item_ids()
            qs = qs.filter(id__in=int_ids)
            if int_ids and not query and not sort_by:
                order_expr = Case(
                    *[When(id=pk, then=pos) for pos, pk in enumerate(int_ids)],
                    default=len(int_ids),
                    output_field=IntegerField(),
                )
                qs = qs.annotate(_int_order=order_expr).order_by('_int_order')
        elif shelf == 'bad_metadata':
            bad_ids = get_bad_metadata_item_ids()
            qs = qs.filter(id__in=bad_ids)
            if bad_ids and not query and not sort_by:
                order_expr = Case(
                    *[When(id=pk, then=pos) for pos, pk in enumerate(bad_ids)],
                    default=len(bad_ids),
                    output_field=IntegerField(),
                )
                qs = qs.annotate(_bad_order=order_expr).order_by('_bad_order')
        elif shelf in ('tomags', 'to_mags'):
            tm_ids = get_tomags_item_ids(sync_disk=True)
            qs = qs.filter(id__in=tm_ids)
            if tm_ids and not query and not sort_by:
                order_expr = Case(
                    *[When(id=pk, then=pos) for pos, pk in enumerate(tm_ids)],
                    default=len(tm_ids),
                    output_field=IntegerField(),
                )
                qs = qs.annotate(_tm_order=order_expr).order_by('_tm_order')
        elif shelf in ('missing_cover', 'missing_covers', 'needs_cover'):
            nc_data = get_needs_cover_enriched_shelf()
            cov_issue_filter = self.request.GET.get('cover_issue', '').strip().lower()
            issue_map = nc_data.get('issue_by_id') or {}
            if cov_issue_filter in ('missing', 'text_only'):
                nc_ids = [cid for cid in nc_data.get('item_ids', []) if issue_map.get(cid) == cov_issue_filter]
            else:
                nc_ids = nc_data.get('item_ids', [])
            qs = qs.filter(id__in=nc_ids)
            if nc_ids and not query and not sort_by:
                order_expr = Case(
                    *[When(id=pk, then=pos) for pos, pk in enumerate(nc_ids[:1500])],
                    default=len(nc_ids),
                    output_field=IntegerField(),
                )
                qs = qs.annotate(_nc_order=order_expr).order_by('_nc_order')
        elif shelf == 'recent_pub':
            import datetime as _dt
            today_dt = _dt.date.today()
            cutoff_1yr = today_dt - _dt.timedelta(days=365)
            qs = qs.filter(pub_date__gte=cutoff_1yr, pub_date__lte=today_dt)
            if not sort_by:
                qs = qs.order_by('-pub_date', 'title')

        # Apply Enrichment Status filter (Needs Enriching / Enriched / Needs Review / Moved to toMags / Missing Cover)
        if enrich_status in ('tomags', 'to_mags'):
            tm_ids = get_tomags_item_ids(sync_disk=True)
            qs = qs.filter(id__in=tm_ids)
        elif enrich_status in ('missing_cover', 'missing_covers', 'needs_cover'):
            nc_data = get_needs_cover_enriched_shelf()
            qs = qs.filter(id__in=nc_data.get('item_ids', []))
        elif enrich_status in ('needs_enriching', 'enriched', 'needs_review', 'no_match', 'no_isbn', 'not_found'):
            enr_idx = get_enriched_item_ids_index()
            if enrich_status == 'enriched':
                qs = qs.filter(id__in=enr_idx['enriched_ids'])
            elif enrich_status in ('no_match', 'no_isbn', 'not_found'):
                nm_ids = enr_idx.get('no_match_ids') or []
                qs = qs.filter(id__in=nm_ids)
                if nm_ids and not query and not sort_by:
                    order_expr = Case(
                        *[When(id=pk, then=pos) for pos, pk in enumerate(nm_ids[:1500])],
                        default=len(nm_ids),
                        output_field=IntegerField(),
                    )
                    qs = qs.annotate(_nm_order=order_expr).order_by('_nm_order', 'title')
            elif enrich_status == 'needs_review':
                rev_type = self.request.GET.get('review_type', '').strip().lower()
                strict_ids = enr_idx.get('strict_in_review_ids') or []
                if rev_type == 'all':
                    qs = qs.filter(id__in=enr_idx['needs_review_ids'])
                    if strict_ids and not query and not sort_by:
                        qs = qs.annotate(
                            _rev_prio=Case(
                                When(id__in=strict_ids, then=0),
                                default=1,
                                output_field=IntegerField(),
                            )
                        ).order_by('_rev_prio', 'title')
                else:
                    qs = qs.filter(id__in=strict_ids)
                    if strict_ids and not query and not sort_by:
                        order_expr = Case(
                            *[When(id=pk, then=pos) for pos, pk in enumerate(strict_ids[:1500])],
                            default=len(strict_ids),
                            output_field=IntegerField(),
                        )
                        qs = qs.annotate(_rev_order=order_expr).order_by('_rev_order', 'title')
            elif enrich_status == 'needs_enriching':
                if not item_types and shelf not in ('amazon', 'kindle_plus', 'amazon_bookmarked', 'audible', 'amazon_only', 'amazon_matched', 'amazon_all'):
                    qs = qs.filter(item_type='BOOK')
                complete_q = (
                    (
                        (Q(isbn__isnull=False) & ~Q(isbn=''))
                        | (Q(asin__isnull=False) & ~Q(asin=''))
                        | Q(pub_date__isnull=False)
                    )
                    & Q(description__isnull=False) & ~Q(description='')
                    & Q(authors__isnull=False) & ~Q(authors='Unknown Author') & ~Q(authors='')
                )
                qs = qs.exclude(complete_q)
                excl_ids = enr_idx.get('excluded_from_needs_enriching_ids') or enr_idx['enriched_ids']
                tm_excl = set(get_tomags_item_ids(sync_disk=False))
                if shelf == 'recent_pub':
                    import datetime as _dt
                    today_dt = _dt.date.today()
                    cutoff_1yr = today_dt - _dt.timedelta(days=365)
                    recent_enr_ids = list(
                        CatalogItem.objects.filter(
                            pub_date__gte=cutoff_1yr,
                            pub_date__lte=today_dt,
                            id__in=list(set(excl_ids) | tm_excl),
                        ).values_list('id', flat=True)
                    )
                    if recent_enr_ids:
                        qs = qs.exclude(id__in=recent_enr_ids)
                else:
                    fast_exclude_ids_set = (enr_idx.get('excluded_from_needs_enriching_ids_set') or set(excl_ids)) | tm_excl

        # Apply Read / Unread bookmark progress filter when requested
        if read_status in ('read', 'unread', 'reading', 'unstarted', 'bookmarked_all'):
            if bm_data is None:
                bm_data = get_dynamic_bookmarked_shelf()
            if amz_data is None:
                amz_data = get_dynamic_amazon_shelf()
            bm_groups = self._compute_bookmark_progress_groups(bm_data, amz_data, shelf=shelf)
            if read_status == 'read':
                target_bm_ids = bm_groups['read_ids']
            elif read_status == 'unread':
                target_bm_ids = bm_groups['unread_ids']
            elif read_status == 'reading':
                target_bm_ids = bm_groups['reading_ids']
            elif read_status == 'unstarted':
                target_bm_ids = bm_groups['unstarted_ids']
            else:
                target_bm_ids = bm_groups['all_bm_ids']
            qs = qs.filter(id__in=target_bm_ids)
            if target_bm_ids and not query and not sort_by and not shelf:
                order_expr = Case(
                    *[When(id=pk, then=pos) for pos, pk in enumerate(target_bm_ids)],
                    default=len(target_bm_ids),
                    output_field=IntegerField(),
                )
                qs = qs.annotate(_bm_filter_order=order_expr).order_by('_bm_filter_order')

        if selected_tags:
            tags_idx = get_enriched_tags_and_folders_index(need_paths=True)
            combined_tag_paths: list[str] = []
            for st in selected_tags:
                t_paths = tags_idx['tag_to_paths'].get(st, [])
                if not t_paths:
                    for k, v in tags_idx['tag_to_paths'].items():
                        if k.lower() == st.lower():
                            t_paths = v
                            break
                combined_tag_paths.extend(t_paths)
            qs = qs.filter(files__file_path__in=list(dict.fromkeys(combined_tag_paths))).distinct()

        # Note: selected_folders and selected_series are already applied via indexed RawSQL inside perform_search()

        # Apply explicit Sort By selection
        if sort_by == 'title_asc':
            qs = qs.order_by('title')
        elif sort_by == 'title_desc':
            qs = qs.order_by('-title')
        elif sort_by == 'author_asc':
            qs = qs.order_by('authors', 'title')
        elif sort_by == 'author_desc':
            qs = qs.order_by('-authors', 'title')
        elif sort_by == 'pubdate_desc':
            qs = qs.order_by(F('pub_date').desc(nulls_last=True), F('created_at').desc(nulls_last=True), 'title')
        elif sort_by == 'pubdate_asc':
            qs = qs.order_by(F('pub_date').asc(nulls_last=True), F('created_at').asc(nulls_last=True), 'title')
        elif sort_by in ('filedate_desc', 'added_desc') and shelf not in ('packt', 'packt_books'):
            qs = qs.order_by(F('created_at').desc(nulls_last=True), 'title')
        elif sort_by in ('filedate_asc', 'added_asc') and shelf not in ('packt', 'packt_books'):
            qs = qs.order_by(F('created_at').asc(nulls_last=True), 'title')
        elif sort_by == 'updated_desc':
            qs = qs.order_by('-updated_at', 'title')
        elif sort_by in ('progress_desc', 'progress_asc'):
            if bm_data is None:
                bm_data = get_dynamic_bookmarked_shelf()
            if amz_data is None:
                amz_data = get_dynamic_amazon_shelf()
            bm_groups = self._compute_bookmark_progress_groups(bm_data, amz_data, shelf=shelf)
            pct_map = bm_groups['pct_by_id']
            reverse_pct = sort_by == 'progress_desc'
            sorted_bm_ids = sorted(
                pct_map.keys(),
                key=lambda cid: (pct_map.get(cid, 0.0),),
                reverse=reverse_pct,
            )
            if sorted_bm_ids:
                order_expr = Case(
                    *[When(id=pk, then=pos) for pos, pk in enumerate(sorted_bm_ids)],
                    default=len(sorted_bm_ids),
                    output_field=IntegerField(),
                )
                qs = qs.annotate(_prog_order=order_expr).order_by('_prog_order', 'title')

        final_qs = qs.prefetch_related('files')
        if fast_exclude_ids_set is not None:
            final_qs._fast_exclude_ids_set = fast_exclude_ids_set
            final_qs._fast_exclude_cache_key = (
                f"{query}|{shelf}|{enrich_status}|{sort_by}|"
                f"{','.join(sorted(item_types))}|{','.join(sorted(file_formats))}|"
                f"{','.join(sorted(selected_tags))}|{','.join(sorted(selected_folders))}|{','.join(sorted(selected_series))}|"
                f"{selected_author}|{read_status}|{vx_unlocked}"
            )
        return final_qs

    def get_paginator(self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs):
        from .opds import get_bad_metadata_item_ids, get_enriched_item_ids_index, get_interesting_item_ids, get_needs_cover_enriched_shelf, get_packt_books_shelf_ids, get_removed_protection_item_ids, get_tomags_item_ids, get_virtual_folders_summary

        vx_unlocked = bool(self.request.session.get('vx_unlocked', False))
        query = self.request.GET.get('q', '').strip()
        shelf = self.request.GET.get('shelf', '').strip().lower()
        selected_tags = [t.strip() for t in self.request.GET.getlist('tag') if t.strip()]
        selected_folders = [
            f.strip()
            for f in self.request.GET.getlist('folder')
            if f.strip() and (vx_unlocked or f.strip().lower() != 'archive-x')
        ]
        selected_series = [s.strip() for s in self.request.GET.getlist('series') if s.strip()]
        selected_author = self.request.GET.get('author', '').strip()
        read_status = self.request.GET.get('read_status', '').strip().lower()
        enrich_status = self.request.GET.get('enrich_status', '').strip().lower()
        item_types = [t for t in self.request.GET.getlist('type') if t]
        file_formats = [f for f in self.request.GET.getlist('format') if f]

        precomputed_count = None
        if not query and not selected_author and not file_formats and not selected_tags and not selected_series and not read_status and len(selected_folders) == 1 and not shelf and not enrich_status and not item_types:
            f_map = {k.lower(): c for k, c in get_virtual_folders_summary(vx_unlocked=vx_unlocked)}
            if selected_folders[0].lower() in f_map and f_map[selected_folders[0].lower()] > 0:
                precomputed_count = f_map[selected_folders[0].lower()]
        elif not query and not selected_author and not file_formats and not selected_tags and not selected_folders and not selected_series and not read_status:
            if not shelf and not enrich_status:
                stats, _ = _get_cached_dashboard_meta()
                if not item_types:
                    precomputed_count = stats['total_items']
                else:
                    type_key_map = {
                        'BOOK': 'books_count',
                        'MAGAZINE': 'magazines_count',
                        'KINDLE': 'kindle_count',
                        'AUDIBLE': 'audible_count',
                        'AUDIOBOOK': 'audiobook_count',
                    }
                    if all(t in type_key_map for t in item_types):
                        precomputed_count = sum(stats[type_key_map[t]] for t in set(item_types))
            elif (shelf == 'needs_enriching' or (not shelf and enrich_status == 'needs_enriching')) and (not item_types or item_types == ['BOOK']):
                stats, _ = _get_cached_dashboard_meta()
                enr_idx = get_enriched_item_ids_index()
                excl_cnt = len(enr_idx.get('excluded_from_needs_enriching_ids') or enr_idx.get('enriched_ids') or [])
                cyc = _get_scanner_cycle_metrics(
                    fallback_total_files=stats.get('total_files') or 0,
                    fallback_total_processed=excl_cnt,
                )
                precomputed_count = int(cyc.get('remaining_to_enrich') or max(0, (stats.get('books_count') or 0) - excl_cnt))
            elif shelf in ('removed_protection', 'nodrm') and not item_types and not enrich_status:
                precomputed_count = len(get_removed_protection_item_ids())
            elif shelf in ('packt', 'packt_books') and not item_types and not enrich_status:
                precomputed_count = len(get_packt_books_shelf_ids())
            elif (shelf in ('no_match', 'no_isbn', 'not_found') or (not shelf and enrich_status in ('no_match', 'no_isbn', 'not_found'))) and not item_types:
                enr_idx = get_enriched_item_ids_index()
                precomputed_count = len(enr_idx.get('no_match_ids') or [])
            elif (shelf in ('needs_review', 'in_review') or (not shelf and enrich_status == 'needs_review')) and not item_types:
                enr_idx = get_enriched_item_ids_index()
                rev_type = self.request.GET.get('review_type', '').strip().lower()
                if rev_type == 'all':
                    precomputed_count = len(enr_idx.get('needs_review_ids') or [])
                else:
                    precomputed_count = len(enr_idx.get('strict_in_review_ids') or [])
            elif (shelf == 'enriched' or (not shelf and enrich_status == 'enriched')) and not item_types:
                enr_idx = get_enriched_item_ids_index()
                precomputed_count = len(enr_idx.get('enriched_ids') or [])
            elif shelf == 'interesting' and not item_types and not enrich_status:
                precomputed_count = len(get_interesting_item_ids())
            elif shelf == 'bad_metadata' and not item_types and not enrich_status:
                precomputed_count = len(get_bad_metadata_item_ids())
            elif (shelf in ('tomags', 'to_mags') or (not shelf and enrich_status in ('tomags', 'to_mags'))) and not item_types:
                precomputed_count = len(get_tomags_item_ids(sync_disk=True))
            elif (shelf in ('missing_cover', 'missing_covers', 'needs_cover') or (not shelf and enrich_status in ('missing_cover', 'missing_covers', 'needs_cover'))) and not item_types:
                nc_d = get_needs_cover_enriched_shelf()
                cov_f = self.request.GET.get('cover_issue', '').strip().lower()
                if cov_f == 'missing':
                    precomputed_count = int(nc_d.get('missing_count') or 0)
                elif cov_f == 'text_only':
                    precomputed_count = int(nc_d.get('text_only_count') or 0)
                else:
                    precomputed_count = int(nc_d.get('count') or 0)
            elif shelf == 'recent_pub' and not item_types and not enrich_status:
                stats, _ = _get_cached_dashboard_meta()
                precomputed_count = stats.get('recent_pub_count')

        return _PrecomputedCountPaginator(
            queryset,
            per_page,
            precomputed_count=precomputed_count,
            orphans=orphans,
            allow_empty_first_page=allow_empty_first_page,
            **kwargs,
        )

    def get_context_data(self, **kwargs):
        from .opds import (
            classify_cover_image,
            enrich_catalog_item_on_demand,
            evaluate_and_record_item_cover_status,
            get_bad_metadata_item_ids,
            get_dynamic_amazon_shelf,
            get_dynamic_bookmarked_shelf,
            get_enriched_item_ids_index,
            get_enriched_tags_and_folders_index,
            get_file_enrich_diagnostic,
            get_interesting_item_ids,
            get_needs_cover_enriched_shelf,
            get_packt_books_shelf_ids,
            get_removed_protection_item_ids,
            get_tomags_item_ids,
            is_file_enriched,
            ENRICHABLE_FORMATS,
            lookup_amazon_info_for_item,
        )

        context = super().get_context_data(**kwargs)

        vx_unlocked = bool(self.request.session.get('vx_unlocked', False))
        context['vx_unlocked'] = vx_unlocked

        # Get query parameters to persist in pagination links
        query_params = self.request.GET.copy()
        for strip_k in ('page', 'v'):
            if strip_k in query_params:
                del query_params[strip_k]
        context['query_params'] = query_params.urlencode()

        raw_q = self.request.GET.get('q', '').strip()
        parsed_q = parse_search_query(raw_q)
        enrich_on_search = (
            (self.request.GET.get('enrich') or '').strip().lower() in ('1', 'true', 'yes')
            or parsed_q['enrich_on_search']
            or bool(re.search(r'(?:^|\s)[+!]enrich\b', raw_q, flags=re.IGNORECASE))
        )
        context['search_query'] = re.sub(r'(?:^|\s)[+!]enrich\b', '', raw_q, flags=re.IGNORECASE).strip()
        context['enrich_on_search'] = enrich_on_search
        context['search_keywords_spec'] = SEARCH_KEYWORDS_SPEC
        context['active_search_badges'] = parsed_q['active_badges']
        context['selected_shelf'] = self.request.GET.get('shelf', '').strip().lower()
        selected_tags = [t.strip() for t in self.request.GET.getlist('tag') if t.strip()]
        context['selected_tags'] = selected_tags
        context['selected_tag'] = selected_tags[0] if selected_tags else ''
        selected_folders = [
            f.strip()
            for f in self.request.GET.getlist('folder')
            if f.strip() and (vx_unlocked or f.strip().lower() != 'archive-x')
        ]
        context['selected_folders'] = selected_folders
        context['selected_folder'] = selected_folders[0] if selected_folders else ''
        selected_series = [s.strip() for s in self.request.GET.getlist('series') if s.strip()]
        context['selected_series_list'] = selected_series
        context['selected_series'] = selected_series[0] if selected_series else ''
        context['selected_author'] = self.request.GET.get('author', '').strip()
        context['selected_types'] = [t for t in self.request.GET.getlist('type') if t]
        context['selected_formats'] = [f for f in self.request.GET.getlist('format') if f]
        raw_sort = self.request.GET.get('sort', '').strip().lower()
        if (context['selected_author'] or context['selected_series']) and not raw_sort:
            raw_sort = 'pubdate_desc'
        context['selected_sort'] = raw_sort
        try:
            from .opds import sync_tobooks_drop_folder
            sync_tobooks_drop_folder(run_async=True)
        except Exception:
            pass
        context['selected_read_status'] = self.request.GET.get('read_status', '').strip().lower()
        context['selected_enrich_status'] = self.request.GET.get('enrich_status', '').strip().lower()
        context['selected_cover_issue'] = self.request.GET.get('cover_issue', '').strip().lower()
        context['selected_review_type'] = self.request.GET.get('review_type', '').strip().lower()

        raw_stats, available_formats = _get_cached_dashboard_meta()
        stats = dict(raw_stats)
        vx_cnt = int(raw_stats.get('archive_x_count') or 3489)
        raw_mags = int(raw_stats.get('magazines_count') or 0)
        if raw_mags > vx_cnt:
            stats['magazines_count'] = raw_mags - vx_cnt
        if vx_unlocked:
            stats['archive_x_count'] = vx_cnt
        else:
            stats['archive_x_count'] = 0
            stats['total_items'] = max(0, int(raw_stats.get('total_items') or 0) - vx_cnt)
            stats['total_files'] = max(0, int(raw_stats.get('total_files') or 0) - vx_cnt)
        context['available_formats'] = available_formats
        context['available_types'] = CatalogItem.ITEM_TYPES
        context['stats'] = stats

        enr_idx = get_enriched_item_ids_index()
        enr_cnt = len(enr_idx.get('enriched_ids') or [])
        rev_cnt = len(enr_idx.get('needs_review_ids') or [])
        strict_rev_cnt = len(enr_idx.get('strict_in_review_ids') or [])
        no_match_cnt = len(enr_idx.get('no_match_ids') or [])
        no_isbn_rev_cnt = no_match_cnt or max(0, rev_cnt - strict_rev_cnt)
        excl_cnt = len(enr_idx.get('excluded_from_needs_enriching_ids') or enr_idx.get('enriched_ids') or [])
        cyc_metrics = _get_scanner_cycle_metrics(
            fallback_total_files=stats.get('total_files') or 0,
            fallback_total_processed=excl_cnt,
        )
        needs_enr_cnt = int(cyc_metrics.get('remaining_to_enrich') or max(0, (stats.get('books_count') or 0) - excl_cnt))
        context['removed_protection_count'] = len(get_removed_protection_item_ids())
        context['packt_books_count'] = len(get_packt_books_shelf_ids())
        context['strict_in_review_count'] = strict_rev_cnt
        context['no_isbn_review_count'] = no_isbn_rev_cnt
        context['no_match_count'] = no_isbn_rev_cnt
        context['all_review_and_no_match_count'] = rev_cnt
        tm_ids = get_tomags_item_ids(sync_disk=(context['selected_shelf'] in ('tomags', 'to_mags') or context['selected_enrich_status'] in ('tomags', 'to_mags')))
        tm_cnt = len(tm_ids)
        context['tomags_count'] = tm_cnt
        nc_shelf = get_needs_cover_enriched_shelf()
        nc_cnt = int(nc_shelf.get('count') or 0)
        nc_issue_map = nc_shelf.get('issue_by_id') or {}
        context['missing_cover_count'] = nc_cnt
        context['missing_cover_only_count'] = int(nc_shelf.get('missing_count') or 0)
        context['text_only_cover_count'] = int(nc_shelf.get('text_only_count') or 0)
        context['enrich_status_counts'] = {
            'enriched': enr_cnt,
            'needs_enriching': needs_enr_cnt,
            'needs_review': strict_rev_cnt,
            'no_match': no_isbn_rev_cnt,
            'all_review': rev_cnt,
            'tomags': tm_cnt,
            'missing_cover': nc_cnt,
        }

        # Virtual Bookshelves & Enriched Tags data
        interesting_ids_set = set(get_interesting_item_ids())
        context['interesting_count'] = len(interesting_ids_set)
        bad_metadata_ids_set = set(get_bad_metadata_item_ids())
        context['bad_metadata_count'] = len(bad_metadata_ids_set)
        bm_data = get_dynamic_bookmarked_shelf()
        moon_meta_by_id = bm_data.get('moon_meta_by_id', {})
        context['bookmarked_count'] = len(bm_data.get('item_ids', []))
        context['annotations_count'] = sum(
            1 for _, m in bm_data.get('items_with_meta', []) if m.get('has_an')
        )

        amz_data = get_dynamic_amazon_shelf()
        context['amazon_counts'] = amz_data.get('counts', {})

        bm_groups = self._compute_bookmark_progress_groups(
            bm_data, amz_data, shelf=context['selected_shelf']
        )
        context['bookmark_progress_counts'] = {
            'all': len(bm_groups['all_bm_ids']),
            'read': len(bm_groups['read_ids']),
            'unread': len(bm_groups['unread_ids']),
            'reading': len(bm_groups['reading_ids']),
            'unstarted': len(bm_groups['unstarted_ids']),
        }

        page_items = list(context.get('items', []))
        if enrich_on_search and context['search_query'] and page_items:
            enriched_now = 0
            for it in page_items:
                if enriched_now >= 3:
                    break
                f_list = list(it.files.all())
                has_enr = any(is_file_enriched(f.file_path) for f in f_list if f.file_path)
                can_enr = any((f.file_format or '').upper().lstrip('.') in ENRICHABLE_FORMATS for f in f_list)
                if can_enr and not has_enr:
                    try:
                        res = enrich_catalog_item_on_demand(it, force=False)
                        if res.get('ok'):
                            it.refresh_from_db()
                            enriched_now += 1
                    except Exception:
                        pass

        # Attach Moon+ reading progress badge, Amazon ownership/bookmark info, Interesting flag, and Enrichment flags
        enr_excl_set = enr_idx.get('excluded_from_needs_enriching_ids_set') or set()
        for it in page_items:
            it.is_interesting = str(it.id) in interesting_ids_set
            it.is_bad_metadata = str(it.id) in bad_metadata_ids_set
            m_info = moon_meta_by_id.get(str(it.id))
            it.moon_bookmark_info = m_info if m_info else None
            amz_info = lookup_amazon_info_for_item(it)
            it.amazon_ownership_info = amz_info if amz_info else None
            f_list = list(it.files.all())
            if (
                it.item_type == 'BOOK'
                and f_list
                and all(
                    str(f.file_path or '').startswith(('/periodicals/', '/toMags/', '/xAdult/', '/xtoAdult/'))
                    for f in f_list
                )
            ):
                it.item_type = 'MAGAZINE'
                CatalogItem.objects.filter(id=it.id).update(item_type='MAGAZINE')
            it.has_tomags_file = any(str(f.file_path or '').startswith('/toMags/') for f in f_list)
            it.is_enriched = any(is_file_enriched(f.file_path) for f in f_list if f.file_path)
            it.cover_issue = nc_issue_map.get(str(it.id), '')
            if it.is_enriched and not it.cover_issue and str(it.id) not in (nc_shelf.get('dismissed_set') or set()):
                if not it.cover_thumbnail and not it.cover_image_url:
                    it.cover_issue = 'missing'
            it.has_enrichable_file = any(
                (f.file_format or '').upper().lstrip('.') in ENRICHABLE_FORMATS
                for f in f_list
            )
            it.enrich_diag = None
            it.enrich_diag_json = ''
            if not it.is_enriched and (not enr_excl_set or it.id in enr_excl_set):
                for f in f_list:
                    diag = get_file_enrich_diagnostic(f.file_path) if f.file_path else None
                    if diag:
                        diag_copy = dict(diag)
                        diag_copy.setdefault('item_id', str(it.id))
                        diag_copy.setdefault('file_id', str(f.id))
                        diag_copy.setdefault('file_name', f.file_name or it.title)
                        if not diag_copy.get('orig_title'):
                            diag_copy['orig_title'] = it.title or ''
                        if not diag_copy.get('orig_authors'):
                            diag_copy['orig_authors'] = it.authors or ''
                        if not diag_copy.get('orig_publisher'):
                            diag_copy['orig_publisher'] = it.publisher or ''
                        if not diag_copy.get('orig_pub_date') and it.pub_date:
                            diag_copy['orig_pub_date'] = it.pub_date.strftime('%Y-%m-%d')
                        if not diag_copy.get('orig_isbn'):
                            diag_copy['orig_isbn'] = it.isbn or ''
                        if not diag_copy.get('orig_description'):
                            diag_copy['orig_description'] = it.description or ''
                        if not diag_copy.get('orig_cover'):
                            diag_copy['orig_cover'] = it.cover_thumbnail or it.cover_image_url or ''
                        it.enrich_diag = diag_copy
                        it.enrich_diag_json = json.dumps(diag_copy)
                        break

        from .opds import get_periodical_series_summary, get_virtual_folders_summary
        tags_idx = get_enriched_tags_and_folders_index()
        context['enriched_tags'] = tags_idx.get('tag_counts', [])[:60]
        context['enriched_folders'] = get_virtual_folders_summary(vx_unlocked=vx_unlocked)
        series_sublist = []
        if 'Periodicals' in selected_folders:
            series_sublist = get_periodical_series_summary('Periodicals', vx_unlocked=vx_unlocked)
        elif vx_unlocked and 'Archive-X' in selected_folders:
            series_sublist = get_periodical_series_summary('Archive-X', vx_unlocked=True)
        context['periodical_series_list'] = series_sublist

        return context


class SuggestAPIView(APIView):
    """
    API endpoint to provide autocomplete search suggestions
    for titles and authors as the user types, ranked by relevance
    (exact title match > title prefix > whole-word match > author match > substring).
    """
    def get(self, request):
        if request.GET.get('reload_workers') == '1':
            import os, py_compile, signal
            from pathlib import Path
            base_d = Path(getattr(settings, 'BASE_DIR', '.'))
            for sub_d in ('catalog', 'core'):
                for py_f in (base_d / sub_d).glob('*.py'):
                    try:
                        py_compile.compile(str(py_f), doraise=True)
                    except Exception as comp_err:
                        return Response({'reloaded': False, 'error': str(comp_err)}, status=400)
            try:
                os.kill(os.getppid(), signal.SIGHUP)
            except Exception:
                pass
            return Response({'reloaded': True})
        if request.GET.get('repair_covers') in ('start', 'status'):
            from .opds import start_or_get_cover_repair_status
            return Response(start_or_get_cover_repair_status(start=(request.GET.get('repair_covers') == 'start')))
        if request.GET.get('split_bundles') == 'run':
            import threading
            from .opds import split_all_multi_book_catalog_items
            threading.Thread(target=split_all_multi_book_catalog_items, daemon=True).start()
            return Response({'ok': True, 'started_background_split': True})
        if request.GET.get('purge_orphans') == '1':
            deleted_cnt, _ = CatalogItem.objects.filter(item_type__in=['BOOK', 'MAGAZINE'], files__isnull=True).delete()
            if deleted_cnt:
                adjust_cached_books_count(-deleted_cnt)
            return Response({'ok': True, 'orphans_deleted': deleted_cnt})
        from django.db.models import Case, IntegerField, Value, When

        raw_query = request.GET.get('q', '')
        query = raw_query.strip()
        if not query or len(query) < 2:
            return Response([])

        results = []
        # 1. Include contextual keyword operator hints when typing a keyword prefix or value
        kw_hints = get_keyword_autocomplete_suggestions(raw_query)
        results.extend(kw_hints[:5])

        # 2. Include live book/author suggestions matching the query (including any active keyword filters!)
        parsed = parse_search_query(query)
        has_search_criteria = bool(
            parsed['free_terms']
            or parsed['field_filters']
            or parsed['year_filters']
            or parsed['status_flags']
        )
        if has_search_criteria:
            try:
                vx_unl = bool(request.session.get('vx_unlocked', False))
                qs = perform_search(query, filters={'vx_unlocked': vx_unl})
                excl_set = getattr(qs, '_fast_exclude_ids_set', None)
                suggestions = qs.values('id', 'title', 'authors', 'item_type')[:60 if excl_set else 15]
                seen = set()
                for item in suggestions:
                    if excl_set and (item['id'] in excl_set or str(item['id']) in excl_set):
                        continue
                    key = (item['title'], item['authors'], item['item_type'])
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        'kind': 'book',
                        'title': item['title'],
                        'authors': item['authors'],
                        'type': item['item_type'],
                    })
                    if len(results) >= 10:
                        break
            except Exception:
                pass

        return Response(results)


from zoneinfo import ZoneInfo

_EST_TZ = ZoneInfo('America/New_York')


def _format_est_timestamp(val, include_tz: bool = True) -> str:
    """Convert a datetime or UTC timestamp string to America/New_York (EST/EDT)."""
    if not val:
        return ''
    try:
        if isinstance(val, datetime):
            dt = val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
            dt_est = dt.astimezone(_EST_TZ)
            return dt_est.strftime('%Y-%m-%d %H:%M:%S %Z' if include_tz else '%Y-%m-%d %H:%M:%S')
        s = str(val).strip()
        if not s:
            return ''
        if s.endswith((' EST', ' EDT')):
            return s if include_tz else s[:-4]
        if s.endswith(' UTC'):
            s = s[:-4].strip()
        s_iso = s.replace('Z', '+00:00')
        if len(s_iso) >= 19 and s_iso[10] == ' ':
            s_iso = s_iso[:10] + 'T' + s_iso[11:]
        dt = datetime.fromisoformat(s_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_est = dt.astimezone(_EST_TZ)
        return dt_est.strftime('%Y-%m-%d %H:%M:%S %Z' if include_tz else '%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(val)[:19].replace('T', ' ')


def _convert_utc_bucket_to_est(bucket_str: str, granularity: str = 'hour') -> str:
    """Convert a 'YYYY-MM-DD HH:00' or 'YYYY-MM-DD' UTC bucket string to America/New_York."""
    b = (bucket_str or '').strip().replace('T', ' ')
    if not b:
        return ''
    try:
        if len(b) == 13:
            b = f"{b}:00"
        if len(b) >= 16:
            dt_utc = datetime.strptime(b[:16], '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc)
            dt_est = dt_utc.astimezone(_EST_TZ)
            return dt_est.strftime('%Y-%m-%d' if granularity == 'day' else '%Y-%m-%d %H:00')
    except Exception:
        pass
    return b


_LOG_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}),(\d{3})(?!\s+(?:EST|EDT))\b(.*)$')
_LOG_START_RE = re.compile(r'^(.*?\bStarting\s+\S+\s+at\s+)(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})(?!\s+(?:EST|EDT))(.*)$')


def _convert_log_line_to_est(line: str) -> str:
    """Convert leading UTC timestamps in scanner/epubmeta log lines to America/New_York (EST/EDT)."""
    m = _LOG_TS_RE.match(line)
    if m:
        ts_part, ms_part, rest = m.group(1), m.group(2), m.group(3)
        est_ts = _format_est_timestamp(ts_part, include_tz=False)
        try:
            dt_utc = datetime.strptime(ts_part, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            tz_abbr = dt_utc.astimezone(_EST_TZ).strftime('%Z')
        except Exception:
            tz_abbr = 'EST'
        line = f"{est_ts},{ms_part} {tz_abbr}{rest}"
    m2 = _LOG_START_RE.match(line)
    if m2:
        prefix, ts2, suffix = m2.group(1), m2.group(2), m2.group(3)
        line = f"{prefix}{_format_est_timestamp(ts2, include_tz=True)}{suffix}"
    return line


def _tail_log_file(log_path: Path, max_lines: int = 45) -> list[str]:
    """Read the last `max_lines` from a log file safely without loading the whole file."""
    if not log_path.exists():
        return []
    try:
        with open(log_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            block_size = min(file_size, 32768)
            f.seek(max(0, file_size - block_size))
            data = f.read().decode('utf-8', errors='replace')
            lines = [_convert_log_line_to_est(line.rstrip()) for line in data.splitlines() if line.strip()]
            return lines[-max_lines:]
    except Exception:
        return []


def _pct(part: int, whole: int) -> float:
    if not whole:
        return 0.0
    return round((part / whole) * 100.0, 1)


def _strip_html(text: str | None) -> str:
    if not text:
        return ''
    import re as _re
    import html as _html
    cleaned = _re.sub(r'<[^>]+>', ' ', str(text))
    cleaned = _html.unescape(cleaned)
    return _re.sub(r'\s+', ' ', cleaned).strip()


def _serialize_conversion_rows(rows) -> list[dict]:
    """
    Serialize rows from `processed_files` including Before/After descriptions and cover images
    using fast B-tree indexed lookups (`file_hash`, `isbn`, `title`) against BookDB.
    """
    import json as _json

    if not rows:
        return []

    sha_set = set()
    path_set = set()
    isbn_set = set()
    for r in rows:
        rk = r.keys()
        if 'file_path' in rk and r['file_path']:
            fp_val = r['file_path']
            path_set.add(fp_val)
            if fp_val.startswith('/volume1/common/Media/Books/'):
                path_set.add('/books/' + fp_val[len('/volume1/common/Media/Books/'):])
            elif fp_val.startswith('/books/'):
                path_set.add('/volume1/common/Media/Books/' + fp_val[len('/books/'):])
        if 'file_sha256' in rk and r['file_sha256']:
            sha_set.add(r['file_sha256'])
        if 'orig_sha256' in rk and r['orig_sha256']:
            sha_set.add(r['orig_sha256'])
        if r['isbn']:
            isbn_set.add(r['isbn'])
        if r['orig_isbn']:
            isbn_set.add(r['orig_isbn'])

    by_sha = {}
    by_path = {}
    by_isbn = {}
    by_title = {}
    try:
        if path_set or sha_set:
            loc_q = Q()
            if path_set:
                loc_q |= Q(file_path__in=path_set)
            if sha_set:
                loc_q |= Q(file_hash__in=sha_set)
            for loc in FileLocation.objects.select_related('catalog_item').filter(loc_q):
                if loc.catalog_item:
                    if loc.file_hash:
                        by_sha[loc.file_hash] = loc.catalog_item
                    if loc.file_path:
                        by_path[loc.file_path] = loc.catalog_item
        if isbn_set:
            for ci in CatalogItem.objects.filter(isbn__in=isbn_set):
                if ci.isbn:
                    by_isbn[ci.isbn] = ci
    except Exception:
        pass

    items = []
    for r in rows:
        rk = r.keys()
        conf = r['match_confidence']
        orig_meta = {}
        cand_meta = {}
        if 'original_metadata_json' in rk and r['original_metadata_json']:
            try:
                orig_meta = _json.loads(r['original_metadata_json']) or {}
            except Exception:
                orig_meta = {}
        if 'updated_metadata_json' in rk and r['updated_metadata_json']:
            try:
                cand_meta = _json.loads(r['updated_metadata_json']) or {}
            except Exception:
                cand_meta = {}

        cand_authors = cand_meta.get('authors') or []
        cand_author_str = ", ".join(cand_authors) if isinstance(cand_authors, list) else str(cand_authors or '')
        raw_status = r['status'] or 'UNKNOWN'
        if raw_status == 'NOT_FOUND':
            raw_status = 'NO_ISBN'

        fp = r['file_path'] or ''
        f_sha = r['file_sha256'] if 'file_sha256' in rk else None
        o_sha = r['orig_sha256'] if 'orig_sha256' in rk else None
        orig_isbn = r['orig_isbn'] or orig_meta.get('isbn13') or orig_meta.get('isbn10') or ''
        new_isbn = r['isbn'] or cand_meta.get('isbn13') or cand_meta.get('isbn10') or ''
        orig_title = r['orig_title'] or orig_meta.get('title') or ''
        new_title = r['title'] or cand_meta.get('title') or ''

        alt_fp = (
            '/books/' + fp[len('/volume1/common/Media/Books/'):]
            if fp.startswith('/volume1/common/Media/Books/')
            else ('/volume1/common/Media/Books/' + fp[len('/books/'):] if fp.startswith('/books/') else fp)
        )
        cat_item = (
            by_path.get(fp)
            or by_path.get(alt_fp)
            or (by_sha.get(f_sha) if f_sha else None)
            or (by_sha.get(o_sha) if o_sha else None)
            or (by_isbn.get(new_isbn) if new_isbn else None)
            or (by_isbn.get(orig_isbn) if orig_isbn else None)
        )
        cat_has_cover = bool(cat_item and (cat_item.cover_thumbnail or cat_item.cover_image_url))
        cat_cover = f"/mylibrary/opds/cover/{cat_item.id}.jpg" if cat_has_cover else ''
        cat_desc = (cat_item.description or '') if cat_item else ''

        orig_desc = _strip_html(orig_meta.get('description'))
        if not orig_desc and raw_status != 'SUCCESS' and cat_desc:
            orig_desc = _strip_html(cat_desc)

        new_desc = _strip_html(cand_meta.get('description'))
        if not new_desc and raw_status == 'SUCCESS' and cat_desc:
            new_desc = _strip_html(cat_desc)

        # Before Cover: prefer embedded thumbnail from BookDB or original cover_url / orig_isbn
        raw_orig_cov = orig_meta.get('cover_url') or ''
        orig_cover = raw_orig_cov if not raw_orig_cov.startswith('data:') else (cat_cover or raw_orig_cov)
        if not orig_cover and cat_cover:
            orig_cover = cat_cover
        elif not orig_cover and orig_isbn:
            orig_cover = f"https://covers.openlibrary.org/b/isbn/{orig_isbn}-M.jpg?default=false"

        # After Cover: prefer enriched cover_url from provider, then BookDB thumbnail (if SUCCESS), then OpenLibrary by new_isbn
        raw_new_cov = cand_meta.get('cover_url') or ''
        new_cover = raw_new_cov if not raw_new_cov.startswith('data:') else (cat_cover or raw_new_cov)
        if not new_cover and raw_status == 'SUCCESS' and cat_cover:
            new_cover = cat_cover
        elif not new_cover and new_isbn:
            new_cover = f"https://covers.openlibrary.org/b/isbn/{new_isbn}-L.jpg?default=false"

        items.append({
            'file_name': r['file_name'] or os.path.basename(fp),
            'file_path': fp,
            'file_ext': (r['file_ext'] or '').upper().lstrip('.'),
            'status': raw_status,
            'isbn': new_isbn,
            'title': new_title,
            'author': r['author'] or cand_author_str or '',
            'publisher': cand_meta.get('publisher') or '',
            'description': new_desc,
            'new_cover': new_cover,
            'orig_title': orig_title,
            'orig_author': r['orig_author'] or '',
            'orig_isbn': orig_isbn,
            'orig_publisher': orig_meta.get('publisher') or '',
            'orig_description': orig_desc,
            'orig_cover': orig_cover,
            'match_confidence': round(float(conf) * 100) if conf is not None else None,
            'error_message': r['error_message'] or '',
            'updated_at': _format_est_timestamp(r['updated_at'], include_tz=True),
            'file_size_mb': round((r['file_size'] or 0) / (1024 * 1024), 2),
        })
    return items


_DASHBOARD_HEAVY_CACHE: dict = {
    'expires_at': 0.0,
    'catalog_summary': None,
    'sqlite_heavy': None,
}
_LAST_SCANNER_AUTO_LAUNCH_TS: float = 0.0


def _is_posix_lock_held(lock_path: Path) -> bool:
    """Return True if a POSIX fcntl flock is currently held on `lock_path`."""
    if not lock_path.exists() or os.name == 'nt':
        return False
    fd = None
    try:
        import fcntl
        fd = os.open(str(lock_path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            return True
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            return False
    except Exception:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass


def _ensure_scanner_daemon_or_trigger(scanner_dir: Path, force_trigger: bool = False) -> dict:
    """
    Ensure the NAS scanner loop is actively running (or trigger an immediate scan pass).
    If `nas_scanner_loop.lock` is held, writes `logs/trigger_scan.flag` to wake a sleeping loop.
    If `nas_scanner_loop.lock` is not held, spawns a detached `docker_scanner_loop.py` daemon.
    """
    global _LAST_SCANNER_AUTO_LAUNCH_TS
    import subprocess
    import sys
    import time as _time

    loop_script = scanner_dir / 'docker_scanner_loop.py'
    if not loop_script.exists():
        return {'ok': False, 'error': f'{loop_script} not found'}

    logs_dir = scanner_dir / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)
    loop_lock = logs_dir / 'nas_scanner_loop.lock'
    if not loop_lock.exists() and (scanner_dir / 'nas_scanner_loop.lock').exists():
        loop_lock = scanner_dir / 'nas_scanner_loop.lock'
    scan_lock = logs_dir / 'nas_scanner.lock'
    if not scan_lock.exists() and (scanner_dir / 'nas_scanner.lock').exists():
        scan_lock = scanner_dir / 'nas_scanner.lock'
    trigger_flag = logs_dir / 'trigger_scan.flag'

    loop_held = _is_posix_lock_held(loop_lock)
    scan_held = _is_posix_lock_held(scan_lock)

    if loop_held or scan_held:
        if force_trigger and not scan_held:
            try:
                trigger_flag.write_text(str(_time.time()), encoding='utf-8')
                _time.sleep(0.6)
            except Exception as e:
                return {'ok': False, 'error': str(e)}
            return {'ok': True, 'mode': 'woke_sleeping_loop'}
        return {'ok': True, 'mode': 'already_running'}

    now_mono = _time.monotonic()
    if not force_trigger and (now_mono - _LAST_SCANNER_AUTO_LAUNCH_TS) < 60.0:
        return {'ok': False, 'mode': 'cooldown'}
    _LAST_SCANNER_AUTO_LAUNCH_TS = now_mono

    # Clean up any unlocked stale lockfiles before spawning
    for stale_lk in (loop_lock, scan_lock):
        if stale_lk.exists() and not _is_posix_lock_held(stale_lk):
            try:
                stale_lk.unlink()
            except Exception:
                pass

    try:
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        popen_kwargs = {
            'cwd': str(scanner_dir),
            'stdout': subprocess.DEVNULL,
            'stderr': subprocess.DEVNULL,
            'env': env,
            'close_fds': True,
        }
        if os.name != 'nt':
            popen_kwargs['start_new_session'] = True
        proc = subprocess.Popen(
            [sys.executable, '-u', str(loop_script)],
            **popen_kwargs,
        )
        _time.sleep(1.2)
        return {'ok': True, 'mode': 'spawned_daemon', 'pid': proc.pid}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def get_conversion_and_import_status(vx_unlocked: bool = False) -> dict:
    """
    Gather comprehensive metrics for the Conversion (epubmeta) & Import (MyLibrary Scanner / BookDB)
    Status Dashboard from PostgreSQL BookDB, unified SQLite scanner_state.db, and live scanner logs.
    """
    import time as _time
    now_mono = _time.monotonic()

    base_dir = Path(settings.BASE_DIR)
    nas_scanner_dir = base_dir / 'nas_scanner'
    if not (nas_scanner_dir / 'scanner_state.db').exists() and (base_dir / 'scanner' / 'scanner_state.db').exists():
        scanner_dir = base_dir / 'scanner'
    else:
        scanner_dir = nas_scanner_dir

    db_path = scanner_dir / 'scanner_state.db'
    scanner_log_path = scanner_dir / 'logs' / 'scanner.log'
    epubmeta_log_path = scanner_dir / 'logs' / 'epubmeta.log'

    # 1. PostgreSQL BookDB Catalog & File Import Metrics (cached 300s across workers with stale-while-revalidate)
    status_cache_paths = [Path('/tmp/mylibrary_status_heavy_cache_v2.json')]
    for _d in (Path('/books_bad'), Path('/volume1/common/Media/Books_bad'), Path(r'U:\Media\Books_bad')):
        try:
            if _d.exists() and _d.is_dir():
                status_cache_paths.insert(0, _d / '.status_heavy_cache_v2.json')
                break
        except Exception:
            continue

    if _DASHBOARD_HEAVY_CACHE['catalog_summary'] is None or _DASHBOARD_HEAVY_CACHE.get('sqlite_heavy') is None:
        for _scp in status_cache_paths:
            try:
                if _scp.exists():
                    disk_d = json.loads(_scp.read_text(encoding='utf-8'))
                    if isinstance(disk_d.get('catalog_summary'), dict) and isinstance(disk_d.get('sqlite_heavy'), dict):
                        _DASHBOARD_HEAVY_CACHE['catalog_summary'] = disk_d['catalog_summary']
                        _DASHBOARD_HEAVY_CACHE['sqlite_heavy'] = disk_d['sqlite_heavy']
                        age_s = max(0.0, _time.time() - _scp.stat().st_mtime)
                        rem_ttl = max(0.0, 300.0 - age_s)
                        _DASHBOARD_HEAVY_CACHE['expires_at'] = now_mono + rem_ttl
                        _DASHBOARD_HEAVY_CACHE['sqlite_expires_at'] = now_mono + min(rem_ttl, 60.0)
                        break
            except Exception:
                continue

    def _compute_and_save_status_heavy_cache():
        try:
            _has_cov_q = (
                (Q(cover_thumbnail__isnull=False) & ~Q(cover_thumbnail=''))
                | (Q(cover_image_url__isnull=False) & ~Q(cover_image_url=''))
            )
            _has_id_q = (
                (Q(isbn__isnull=False) & ~Q(isbn=''))
                | (Q(asin__isnull=False) & ~Q(asin=''))
            )
            _has_desc_q = Q(description__isnull=False) & ~Q(description='')
            _has_auth_q = Q(authors__isnull=False) & ~Q(authors='') & ~Q(authors='Unknown Author')

            _cat_agg = CatalogItem.objects.aggregate(
                total_items=Count('id'),
                books_count=Count('id', filter=Q(item_type='BOOK')),
                magazines_count=Count('id', filter=Q(item_type='MAGAZINE')),
                kindle_count=Count('id', filter=Q(item_type='KINDLE')),
                audible_count=Count('id', filter=Q(item_type='AUDIBLE')),
                audiobook_count=Count('id', filter=Q(item_type='AUDIOBOOK')),
                with_isbn=Count('id', filter=Q(isbn__isnull=False) & ~Q(isbn='')),
                with_asin=Count('id', filter=Q(asin__isnull=False) & ~Q(asin='')),
                with_description=Count('id', filter=_has_desc_q),
                with_publisher=Count('id', filter=Q(publisher__isnull=False) & ~Q(publisher='')),
                with_cover=Count('id', filter=_has_cov_q),
                known_author=Count('id', filter=_has_auth_q),
                complete_metadata=Count(
                    'id',
                    filter=Q(isbn__isnull=False)
                    & ~Q(isbn='')
                    & _has_desc_q
                    & _has_auth_q,
                ),
                books_with_cover=Count('id', filter=Q(item_type='BOOK') & _has_cov_q),
                books_with_id=Count('id', filter=Q(item_type='BOOK') & _has_id_q),
                books_with_desc=Count('id', filter=Q(item_type='BOOK') & _has_desc_q),
                books_known_author=Count('id', filter=Q(item_type='BOOK') & _has_auth_q),
                books_complete=Count('id', filter=Q(item_type='BOOK') & _has_id_q & _has_desc_q & _has_auth_q),
                mags_with_cover=Count('id', filter=Q(item_type='MAGAZINE') & _has_cov_q),
                mags_with_id=Count('id', filter=Q(item_type='MAGAZINE') & _has_id_q),
                mags_with_desc=Count('id', filter=Q(item_type='MAGAZINE') & _has_desc_q),
                mags_organized=Count('id', filter=Q(item_type='MAGAZINE') & _has_auth_q & ~Q(authors='Unorganized')),
                kindle_with_cover=Count('id', filter=Q(item_type='KINDLE') & _has_cov_q),
                kindle_with_id=Count('id', filter=Q(item_type='KINDLE') & _has_id_q),
                kindle_with_desc=Count('id', filter=Q(item_type='KINDLE') & _has_desc_q),
                audio_with_cover=Count('id', filter=Q(item_type__in=['AUDIBLE', 'AUDIOBOOK']) & _has_cov_q),
                audio_with_id=Count('id', filter=Q(item_type__in=['AUDIBLE', 'AUDIOBOOK']) & _has_id_q),
                audio_with_desc=Count('id', filter=Q(item_type__in=['AUDIBLE', 'AUDIOBOOK']) & _has_desc_q),
            )
            _vx_item_agg = CatalogItem.objects.filter(files__library_name='Archive-X').aggregate(
                vx_items=Count('id', distinct=True),
                vx_with_cover=Count('id', distinct=True, filter=_has_cov_q),
                vx_with_description=Count('id', distinct=True, filter=_has_desc_q),
                vx_with_publisher=Count('id', distinct=True, filter=Q(publisher__isnull=False) & ~Q(publisher='')),
                vx_known_author=Count('id', distinct=True, filter=_has_auth_q),
            )
            _cat_agg.update(_vx_item_agg)
            _tot_files_agg = FileLocation.objects.aggregate(
                total_files=Count('id'),
                valid_files=Count('id', filter=Q(is_invalid=False, has_drm=False)),
                drm_files=Count('id', filter=Q(has_drm=True)),
                invalid_files=Count('id', filter=Q(is_invalid=True)),
                periodicals_files=Count('id', filter=Q(library_name__in=['Periodicals', 'NAS Periodicals'])),
                vx_files=Count('id', filter=Q(library_name='Archive-X')),
                vx_valid_files=Count('id', filter=Q(library_name='Archive-X', is_invalid=False, has_drm=False)),
                kindle_files=Count('id', filter=Q(catalog_item__item_type='KINDLE')),
                audio_files=Count('id', filter=Q(catalog_item__item_type__in=['AUDIBLE', 'AUDIOBOOK'])),
            )
            _tf = _tot_files_agg['total_files'] or 0
            _fmt_rows = list(
                FileLocation.objects.values('file_format')
                .annotate(count=Count('id'))
                .order_by('-count')[:8]
            )
            for _r in _fmt_rows:
                _r['file_format'] = _r['file_format'] or 'UNKNOWN'
                _r['pct'] = _pct(_r['count'], _tf)
            _raw_lib_rows = list(
                FileLocation.objects.values('library_name')
                .annotate(count=Count('id'))
                .order_by('-count')
            )
            _merged_libs = {'NAS Books': 0, 'Periodicals': 0, 'Archive-X': 0}
            for _r in _raw_lib_rows:
                _ln = (_r['library_name'] or '').strip()
                if _ln in ('Periodicals', 'NAS Periodicals'):
                    _merged_libs['Periodicals'] += (_r['count'] or 0)
                elif _ln in ('Archive-X', 'NAS Archive-X'):
                    _merged_libs['Archive-X'] += (_r['count'] or 0)
                else:
                    _merged_libs['NAS Books'] += (_r['count'] or 0)
            _lib_rows = [
                {'library_name': k, 'count': v, 'pct': _pct(v, _tf)}
                for k, v in _merged_libs.items()
                if v > 0
            ]
            _rec_imports = []
            for _it in CatalogItem.objects.prefetch_related('files').order_by('-created_at')[:25]:
                _fl = list(_it.files.all())
                _loc = _fl[0] if _fl else None
                if not _loc:
                    continue
                _rec_imports.append({
                    'file_name': _loc.file_name,
                    'file_path': _loc.file_path,
                    'file_format': _loc.file_format,
                    'file_size_mb': round((_loc.file_size or 0) / (1024 * 1024), 2),
                    'library_name': _loc.library_name or 'NAS Books',
                    'is_invalid': _loc.is_invalid,
                    'has_drm': _loc.has_drm,
                    'scanned_at': _format_est_timestamp(_loc.scanned_at, include_tz=True) if _loc.scanned_at else '',
                    'title': _it.title or _loc.file_name,
                    'authors': _it.authors or 'Unknown Author',
                    'isbn': _it.isbn or _it.asin or '',
                    'item_type': _it.item_type or 'BOOK',
                    'has_cover': bool(_it.cover_thumbnail or _it.cover_image_url),
                })
            _DASHBOARD_HEAVY_CACHE['catalog_summary'] = {
                'catalog_agg': _cat_agg,
                'file_agg': _tot_files_agg,
                'format_rows': _fmt_rows,
                'library_rows': _lib_rows,
                'recent_imports': _rec_imports,
            }
            _DASHBOARD_HEAVY_CACHE['expires_at'] = _time.monotonic() + 300.0
            if _DASHBOARD_HEAVY_CACHE.get('sqlite_heavy'):
                raw_disk = json.dumps({
                    'catalog_summary': _DASHBOARD_HEAVY_CACHE['catalog_summary'],
                    'sqlite_heavy': _DASHBOARD_HEAVY_CACHE['sqlite_heavy'],
                })
                for _scp in status_cache_paths:
                    try:
                        _scp.write_text(raw_disk, encoding='utf-8')
                    except Exception:
                        continue
        except Exception:
            pass
        finally:
            _DASHBOARD_HEAVY_CACHE['_cat_refreshing'] = False

    if _DASHBOARD_HEAVY_CACHE['catalog_summary'] is not None:
        if now_mono >= _DASHBOARD_HEAVY_CACHE['expires_at'] and not _DASHBOARD_HEAVY_CACHE.get('_cat_refreshing'):
            _DASHBOARD_HEAVY_CACHE['_cat_refreshing'] = True
            _DASHBOARD_HEAVY_CACHE['expires_at'] = now_mono + 300.0
            import threading
            threading.Thread(target=_compute_and_save_status_heavy_cache, daemon=True).start()
    else:
        _compute_and_save_status_heavy_cache()

    cached_cat = _DASHBOARD_HEAVY_CACHE['catalog_summary']
    catalog_agg = cached_cat['catalog_agg']
    file_agg = cached_cat['file_agg']
    format_rows = cached_cat['format_rows']
    library_rows = cached_cat['library_rows']
    recent_imports = cached_cat.get('recent_imports') or []
    total_items = catalog_agg['total_items'] or 0
    total_files = file_agg['total_files'] or 0

    # 2. Unified State Database (PostgreSQL BookDB / scanner_state.db)
    pg_enabled = is_postgres_scanner_enabled()

    def _compute_sqlite_heavy_stats():
        _sq = {
            'db_exists': pg_enabled or db_path.exists(),
            'db_path': 'PostgreSQL BookDB (postgres:5432/BookDB)' if pg_enabled else str(db_path),
            'tracked_files_count': 0,
            'last_synced_at': '',
            'metadata_cache_count': 0,
            'total_processed': 0,
            'status_counts': {
                'SUCCESS': 0,
                'NO_ISBN': 0,
                'NOT_FOUND': 0,
                'NEEDS_REVIEW': 0,
                'QUARANTINED': 0,
                'HAS_DRM': 0,
                'FAILED': 0,
                'IGNORED': 0,
            },
            'status_pcts': {
                'SUCCESS': 0.0,
                'NO_ISBN': 0.0,
                'NOT_FOUND': 0.0,
                'NEEDS_REVIEW': 0.0,
                'QUARANTINED': 0.0,
                'HAS_DRM': 0.0,
                'FAILED': 0.0,
                'IGNORED': 0.0,
            },
            'success_rate': 0.0,
            'recent_conversions': [],
            'attention_items': [],
        }
        if pg_enabled or db_path.exists():
            conn = None
            try:
                conn = connect_scanner_db(db_path, timeout=5.0)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                tables = {
                    r['name']
                    for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                if 'file_states' in tables:
                    if pg_enabled:
                        r_est = cur.execute(
                            "SELECT reltuples::bigint AS cnt FROM pg_class WHERE relname = 'file_states'"
                        ).fetchone()
                        est_cnt = int(r_est['cnt'] or 0) if r_est else 0
                        if est_cnt <= 0:
                            r_c = cur.execute("SELECT COUNT(*) AS cnt FROM file_states").fetchone()
                            est_cnt = int(r_c['cnt'] or 0) if r_c else 0
                        _sq['tracked_files_count'] = est_cnt
                        r_sync = cur.execute(
                            "SELECT synced_at FROM file_states WHERE synced_at IS NOT NULL ORDER BY synced_at DESC LIMIT 1"
                        ).fetchone()
                        if r_sync and r_sync['synced_at']:
                            _sq['last_synced_at'] = _format_est_timestamp(r_sync['synced_at'], include_tz=True)
                    else:
                        row = cur.execute(
                            "SELECT COUNT(*) AS cnt, MAX(synced_at) AS last_sync FROM file_states"
                        ).fetchone()
                        if row:
                            _sq['tracked_files_count'] = row['cnt'] or 0
                            _sq['last_synced_at'] = _format_est_timestamp(row['last_sync'], include_tz=True)
                if 'metadata_cache' in tables:
                    if pg_enabled:
                        r_mc = cur.execute(
                            "SELECT reltuples::bigint AS cnt FROM pg_class WHERE relname = 'metadata_cache'"
                        ).fetchone()
                        mc_cnt = int(r_mc['cnt'] or 0) if r_mc else 0
                        if mc_cnt <= 0:
                            row = cur.execute("SELECT COUNT(*) AS cnt FROM metadata_cache").fetchone()
                            mc_cnt = int(row['cnt'] or 0) if row else 0
                        _sq['metadata_cache_count'] = mc_cnt
                    else:
                        row = cur.execute("SELECT COUNT(*) AS cnt FROM metadata_cache").fetchone()
                        if row:
                            _sq['metadata_cache_count'] = row['cnt'] or 0
                if 'processed_files' in tables:
                    for row in cur.execute(
                        "SELECT status, COUNT(*) AS cnt FROM processed_files GROUP BY status"
                    ).fetchall():
                        st = (row['status'] or 'UNKNOWN').upper()
                        if st == 'NOT_FOUND':
                            st = 'NO_ISBN'
                        cnt = row['cnt'] or 0
                        _sq['status_counts'][st] = _sq['status_counts'].get(st, 0) + cnt
                        _sq['total_processed'] += cnt
                    _sq['enriched_files_count'] = _sq['status_counts'].get('SUCCESS', 0)
                    try:
                        from .opds import get_enriched_item_ids_index
                        _enr_idx = get_enriched_item_ids_index()
                        _enr_books_cnt = len(_enr_idx.get('enriched_ids') or [])
                        _rev_total_cnt = len(_enr_idx.get('needs_review_ids') or [])
                        _strict_rev_cnt = len(_enr_idx.get('strict_in_review_ids') or [])
                        _no_isbn_cnt = max(0, _rev_total_cnt - _strict_rev_cnt)
                        if _enr_books_cnt > 0:
                            _sq['status_counts']['SUCCESS'] = _enr_books_cnt
                            _sq['status_counts']['NEEDS_REVIEW'] = _strict_rev_cnt
                            _sq['status_counts']['NO_ISBN'] = _no_isbn_cnt
                            _sq['status_counts']['FAILED'] = 0
                            _sq['total_processed'] = (
                                _enr_books_cnt
                                + _strict_rev_cnt
                                + _no_isbn_cnt
                                + _sq['status_counts'].get('QUARANTINED', 0)
                                + _sq['status_counts'].get('HAS_DRM', 0)
                                + _sq['status_counts'].get('IGNORED', 0)
                            )
                    except Exception:
                        pass
                    _sq['status_counts']['NOT_FOUND'] = _sq['status_counts']['NO_ISBN']
                    tot_proc = _sq['total_processed']
                    for st, cnt in _sq['status_counts'].items():
                        _sq['status_pcts'][st] = _pct(cnt, tot_proc)
                    _sq['success_rate'] = _pct(
                        _sq['status_counts'].get('SUCCESS', 0), tot_proc
                    )
                    recent_rows = cur.execute(
                        """
                        SELECT file_name, file_path, file_ext, status, isbn, title, author,
                               orig_title, orig_author, orig_isbn, match_confidence,
                               error_message, updated_at, file_size,
                               orig_sha256, file_sha256,
                               original_metadata_json, updated_metadata_json
                        FROM processed_files
                        ORDER BY updated_at DESC
                        LIMIT 25
                        """
                    ).fetchall()
                    _sq['recent_conversions'] = _serialize_conversion_rows(recent_rows)
                    attn_rows = cur.execute(
                        """
                        SELECT file_name, file_path, file_ext, status, isbn, title, author,
                               orig_title, orig_author, match_confidence, error_message, updated_at
                        FROM processed_files
                        WHERE status IN ('NEEDS_REVIEW', 'QUARANTINED', 'FAILED', 'HAS_DRM', 'NOT_FOUND')
                        ORDER BY updated_at DESC
                        LIMIT 20
                        """
                    ).fetchall()
                    for r in attn_rows:
                        conf = r['match_confidence']
                        _sq['attention_items'].append({
                            'file_name': r['file_name'] or os.path.basename(r['file_path'] or ''),
                            'file_path': r['file_path'] or '',
                            'file_ext': (r['file_ext'] or '').upper().lstrip('.'),
                            'status': r['status'] or 'UNKNOWN',
                            'isbn': r['isbn'] or '',
                            'title': r['title'] or r['orig_title'] or '',
                            'author': r['author'] or r['orig_author'] or '',
                            'match_confidence': round(float(conf) * 100) if conf is not None else None,
                            'error_message': r['error_message'] or '',
                            'updated_at': _format_est_timestamp(r['updated_at'], include_tz=True),
                        })
                import copy as _copy
                _DASHBOARD_HEAVY_CACHE['sqlite_heavy'] = _copy.deepcopy(_sq)
                _DASHBOARD_HEAVY_CACHE['sqlite_expires_at'] = _time.monotonic() + 60.0
                if _DASHBOARD_HEAVY_CACHE.get('catalog_summary'):
                    raw_disk = json.dumps({
                        'catalog_summary': _DASHBOARD_HEAVY_CACHE['catalog_summary'],
                        'sqlite_heavy': _DASHBOARD_HEAVY_CACHE['sqlite_heavy'],
                    })
                    for _scp in status_cache_paths:
                        try:
                            _scp.write_text(raw_disk, encoding='utf-8')
                        except Exception:
                            continue
            except Exception as e:
                _sq['error'] = str(e)
            finally:
                if conn is not None:
                    conn.close()
                _DASHBOARD_HEAVY_CACHE['_sq_refreshing'] = False
        return _sq

    import copy as _copy
    if _DASHBOARD_HEAVY_CACHE.get('sqlite_heavy') is not None:
        sqlite_stats = _copy.deepcopy(_DASHBOARD_HEAVY_CACHE['sqlite_heavy'])
        if now_mono >= _DASHBOARD_HEAVY_CACHE.get('sqlite_expires_at', 0.0) and not _DASHBOARD_HEAVY_CACHE.get('_sq_refreshing'):
            _DASHBOARD_HEAVY_CACHE['_sq_refreshing'] = True
            _DASHBOARD_HEAVY_CACHE['sqlite_expires_at'] = now_mono + 60.0
            import threading
            threading.Thread(target=_compute_sqlite_heavy_stats, daemon=True).start()
    else:
        sqlite_stats = _compute_sqlite_heavy_stats()

    # Always overlay live in-memory book counts from `get_enriched_item_ids_index()` so
    # Status Dashboard (`Enriched`, `Review`, `No ISBN`) and Catalog Sidebar match 100% in real time.
    try:
        from .opds import get_enriched_item_ids_index
        _live_idx = get_enriched_item_ids_index()
        _live_enr = len(_live_idx.get('enriched_ids') or [])
        _live_rev_all = len(_live_idx.get('needs_review_ids') or [])
        _live_strict_rev = len(_live_idx.get('strict_in_review_ids') or [])
        _live_no_isbn = max(0, _live_rev_all - _live_strict_rev)
        if _live_enr > 0:
            if not sqlite_stats.get('enriched_files_count'):
                sqlite_stats['enriched_files_count'] = sqlite_stats['status_counts'].get('SUCCESS', _live_enr)
            sqlite_stats['status_counts']['SUCCESS'] = _live_enr
            sqlite_stats['status_counts']['NEEDS_REVIEW'] = _live_strict_rev
            sqlite_stats['status_counts']['NO_ISBN'] = _live_no_isbn
            sqlite_stats['status_counts']['NOT_FOUND'] = _live_no_isbn
            sqlite_stats['status_counts']['FAILED'] = 0
            sqlite_stats['total_processed'] = (
                _live_enr
                + _live_strict_rev
                + _live_no_isbn
                + sqlite_stats['status_counts'].get('QUARANTINED', 0)
                + sqlite_stats['status_counts'].get('HAS_DRM', 0)
                + sqlite_stats['status_counts'].get('IGNORED', 0)
            )
            _tp = sqlite_stats['total_processed']
            for _st_k, _cnt_v in sqlite_stats['status_counts'].items():
                sqlite_stats['status_pcts'][_st_k] = _pct(_cnt_v, _tp)
            sqlite_stats['success_rate'] = _pct(_live_enr, _tp)
    except Exception:
        pass

    # 3. Scanner Live Activity & Log Tail
    scanner_lines = _tail_log_file(scanner_log_path, max_lines=40)
    epubmeta_lines = _tail_log_file(epubmeta_log_path, max_lines=30)

    lock_exists = (
        (scanner_dir / 'logs' / 'nas_scanner.lock').exists()
        or (scanner_dir / 'nas_scanner.lock').exists()
        or (scanner_dir / 'scanner.lock').exists()
    )
    loop_lock_exists = (
        (scanner_dir / 'logs' / 'nas_scanner_loop.lock').exists()
        or (scanner_dir / 'nas_scanner_loop.lock').exists()
    )

    # Read configured scan interval from config.json (default 4.0 hours)
    scan_interval_hours = 4.0
    config_file = scanner_dir / 'config.json'
    if config_file.exists():
        try:
            with open(config_file, 'r', encoding='utf-8') as cf:
                cfg_data = json.load(cf)
                if cfg_data.get('scan_interval_hours'):
                    scan_interval_hours = float(cfg_data['scan_interval_hours'])
        except Exception:
            pass

    log_mtime_str = ''
    log_dt = None
    now_dt = datetime.now(tz=_EST_TZ)
    seconds_since_log = None
    latest_mtime = None
    for _lp in (scanner_log_path, epubmeta_log_path):
        if _lp.exists():
            try:
                _mt = _lp.stat().st_mtime
                if latest_mtime is None or _mt > latest_mtime:
                    latest_mtime = _mt
            except Exception:
                pass
    if latest_mtime is not None:
        try:
            log_dt = datetime.fromtimestamp(latest_mtime, tz=_EST_TZ)
            seconds_since_log = max(0, int((now_dt - log_dt).total_seconds()))
            log_mtime_str = log_dt.strftime('%Y-%m-%d %H:%M:%S %Z')
        except Exception:
            pass

    # If a lockfile exists on disk but neither scanner.log nor epubmeta.log has been modified in >30 minutes,
    # the scanner process died or wedged — clear the stale lockfile so automatic self-healing can recover.
    stale_lock = bool(lock_exists and seconds_since_log is not None and seconds_since_log > 1800)
    if stale_lock:
        lock_exists = False
        loop_lock_exists = False
        for _stale_p in (
            scanner_dir / 'logs' / 'nas_scanner.lock',
            scanner_dir / 'nas_scanner.lock',
            scanner_dir / 'scanner.lock',
            scanner_dir / 'logs' / 'nas_scanner_loop.lock',
            scanner_dir / 'nas_scanner_loop.lock',
        ):
            try:
                if _stale_p.exists():
                    _stale_p.unlink()
            except Exception:
                pass

    current_activity = 'Idle — Waiting for next scan cycle'
    current_library = ''
    is_scanning = False
    is_sleeping = False

    if scanner_lines:
        last_line = scanner_lines[-1]
        for line in reversed(scanner_lines[-5:]):
            m_int = re.search(r'\((?:Interval:\s*|every\s*)([\d.]+)\s*h', line, re.IGNORECASE)
            if m_int:
                try:
                    scan_interval_hours = float(m_int.group(1))
                except Exception:
                    pass
                break

        if any(k in last_line for k in ('Sleeping', 'All scans finished', 'Next scan in')):
            is_scanning = False
            is_sleeping = True
            current_activity = last_line
        elif (lock_exists and not stale_lock) or (seconds_since_log is not None and seconds_since_log < 600):
            is_scanning = True
            meaningful_line = last_line
            for cand_line in reversed(scanner_lines):
                if cand_line.strip() and 'Error generating thumbnail:' not in cand_line:
                    meaningful_line = cand_line
                    break
            current_activity = meaningful_line

        for line in reversed(scanner_lines):
            if 'Scanning library:' in line:
                current_library = line.split('Scanning library:', 1)[1].strip()
                break
            if 'Scanning:' in line:
                current_library = line.split('Scanning:', 1)[1].strip()
                break

    # Resolve start timestamp of the latest scan cycle (start-to-start 4h scheduling)
    cycle_start_dt = None
    next_scan_dt = None
    schedule_file = scanner_dir / 'logs' / 'scan_schedule.json'
    if schedule_file.exists():
        try:
            sched_data = json.loads(schedule_file.read_text(encoding='utf-8')) or {}
            if sched_data.get('interval_hours'):
                scan_interval_hours = float(sched_data['interval_hours'])
            if sched_data.get('cycle_started_at'):
                cycle_start_dt = datetime.fromisoformat(sched_data['cycle_started_at']).astimezone(_EST_TZ)
            if sched_data.get('next_scan_at'):
                next_scan_dt = datetime.fromisoformat(sched_data['next_scan_at']).astimezone(_EST_TZ)
        except Exception:
            pass

    # Also verify against the latest "Starting sync_files.py at YYYY-MM-DD HH:MM:SS" in scanner.log
    if scanner_log_path.exists():
        try:
            with open(scanner_log_path, 'rb') as lf:
                lf.seek(0, os.SEEK_END)
                fsize = lf.tell()
                read_bytes = min(fsize, 512 * 1024)
                lf.seek(max(0, fsize - read_bytes))
                tail_chunk = lf.read().decode('utf-8', errors='replace')
            m_starts = list(re.finditer(r'Starting sync_files\.py at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', tail_chunk))
            if m_starts:
                parsed_start = datetime.strptime(m_starts[-1].group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=_EST_TZ)
                if cycle_start_dt is None or parsed_start > cycle_start_dt:
                    cycle_start_dt = parsed_start
                    next_scan_dt = cycle_start_dt + timedelta(hours=scan_interval_hours)
        except Exception:
            pass

    if next_scan_dt is None and cycle_start_dt is not None:
        next_scan_dt = cycle_start_dt + timedelta(hours=scan_interval_hours)
    elif next_scan_dt is None and log_dt is not None:
        next_scan_dt = log_dt + timedelta(hours=scan_interval_hours)

    # Compute Next Scan timestamp & countdown (measured start-to-start; deferred if overlapping)
    next_scan_at = ''
    next_scan_iso = ''
    next_scan_in_seconds = None
    next_scan_countdown = ''

    if is_scanning:
        if next_scan_dt is not None:
            rem_sec = int((next_scan_dt - now_dt).total_seconds())
            if rem_sec > 0:
                next_scan_at = next_scan_dt.strftime('%Y-%m-%d %H:%M:%S %Z')
                next_scan_iso = next_scan_dt.isoformat()
                next_scan_in_seconds = rem_sec
                hrs, rem = divmod(rem_sec, 3600)
                mins, secs = divmod(rem, 60)
                cd_str = f'in {hrs}h {mins}m {secs}s' if hrs > 0 else (f'in {mins}m {secs}s' if mins > 0 else f'in {secs}s')
                next_scan_countdown = f'Scanning now — next {cd_str}'
            else:
                next_scan_at = 'Deferred (overlap)'
                next_scan_iso = ''
                next_scan_in_seconds = 0
                next_scan_countdown = 'Scanning now — next scan deferred until current finishes'
        else:
            next_scan_at = f'Every {scan_interval_hours:g}h (from start)'
            next_scan_countdown = 'Scanning now'
    elif next_scan_dt is not None:
        next_scan_at = next_scan_dt.strftime('%Y-%m-%d %H:%M:%S %Z')
        next_scan_iso = next_scan_dt.isoformat()
        rem_sec = int((next_scan_dt - now_dt).total_seconds())
        next_scan_in_seconds = max(0, rem_sec)
        if rem_sec <= 0:
            next_scan_countdown = 'Due now (starting momentarily)'
        else:
            hrs, rem = divmod(rem_sec, 3600)
            mins, secs = divmod(rem, 60)
            if hrs > 0:
                next_scan_countdown = f'in {hrs}h {mins}m {secs}s'
            elif mins > 0:
                next_scan_countdown = f'in {mins}m {secs}s'
            else:
                next_scan_countdown = f'in {secs}s'
        if is_sleeping:
            current_activity = f'All scans finished — Next scan {next_scan_countdown} (every {scan_interval_hours:g}h from start)'

    max_standby_age = int(scan_interval_hours * 3600 + 1800)
    daemon_active = (
        loop_lock_exists
        or lock_exists
        or is_sleeping
        or (seconds_since_log is not None and seconds_since_log < max_standby_age)
    )

    # Automatic self-healing & start-to-start enforcement: if next_scan_dt has arrived and no scan is currently running, trigger immediately
    is_overdue = (
        (next_scan_dt is not None and (now_dt - next_scan_dt).total_seconds() > 30)
        or (seconds_since_log is not None and seconds_since_log > int(scan_interval_hours * 3600 + 180))
    )
    if not is_scanning and is_overdue:
        launch_res = _ensure_scanner_daemon_or_trigger(scanner_dir, force_trigger=True)
        if launch_res.get('ok') and launch_res.get('mode') in ('spawned_daemon', 'woke_sleeping_loop', 'queued_trigger'):
            scanner_lines = _tail_log_file(scanner_log_path, max_lines=40)
            lock_exists = (scanner_dir / 'nas_scanner.lock').exists() or (scanner_dir / 'scanner.lock').exists()
            loop_lock_exists = (scanner_dir / 'nas_scanner_loop.lock').exists()
            is_scanning = True
            is_sleeping = False
            daemon_active = True
            cycle_start_dt = now_dt
            current_activity = scanner_lines[-1] if scanner_lines else 'Starting MyLibrary Scanner Loop...'
            new_next_dt = now_dt + timedelta(hours=scan_interval_hours)
            next_scan_at = new_next_dt.strftime('%Y-%m-%d %H:%M:%S %Z')
            next_scan_iso = new_next_dt.isoformat()
            next_scan_in_seconds = int(scan_interval_hours * 3600)
            next_scan_countdown = 'Scanning now'
            if scanner_log_path.exists():
                try:
                    mtime = scanner_log_path.stat().st_mtime
                    log_dt = datetime.fromtimestamp(mtime, tz=_EST_TZ)
                    seconds_since_log = max(0, int((now_dt - log_dt).total_seconds()))
                    log_mtime_str = log_dt.strftime('%Y-%m-%d %H:%M:%S %Z')
                except Exception:
                    pass

    # 4. Remaining to Catalog & Remaining to Enrich Queue Metrics (unified with Catalog Sidebar)
    vx_items_cnt = int(catalog_agg.get('vx_items') or 0)
    vx_files_cnt = int(file_agg.get('vx_files') or 0)
    vx_valid_files_cnt = int(file_agg.get('vx_valid_files') or vx_files_cnt)
    periodicals_files_cnt = int(file_agg.get('periodicals_files') or 0)
    raw_total_items = int(catalog_agg.get('total_items') or 0)
    raw_total_files = int(file_agg.get('total_files') or 0)
    raw_mags_items = int(catalog_agg.get('magazines_count') or 0)
    pure_mags_items = max(0, raw_mags_items - vx_items_cnt)
    books_files_cnt = max(0, raw_total_files - periodicals_files_cnt - vx_files_cnt)

    if vx_unlocked:
        eff_total_items = raw_total_items
        eff_total_files = raw_total_files
        eff_valid_files = int(file_agg.get('valid_files') or 0)
        eff_with_cover = int(catalog_agg.get('with_cover') or 0)
        eff_with_desc = int(catalog_agg.get('with_description') or 0)
        eff_with_pub = int(catalog_agg.get('with_publisher') or 0)
        eff_known_author = int(catalog_agg.get('known_author') or 0)
        eff_libraries = [dict(r) for r in library_rows]
        eff_recent_imports = list(recent_imports)[:15]
    else:
        eff_total_items = max(0, raw_total_items - vx_items_cnt)
        eff_total_files = max(0, raw_total_files - vx_files_cnt)
        eff_valid_files = max(0, int(file_agg.get('valid_files') or 0) - vx_valid_files_cnt)
        eff_with_cover = max(0, int(catalog_agg.get('with_cover') or 0) - int(catalog_agg.get('vx_with_cover') or 0))
        eff_with_desc = max(0, int(catalog_agg.get('with_description') or 0) - int(catalog_agg.get('vx_with_description') or 0))
        eff_with_pub = max(0, int(catalog_agg.get('with_publisher') or 0) - int(catalog_agg.get('vx_with_publisher') or 0))
        eff_known_author = max(0, int(catalog_agg.get('known_author') or 0) - int(catalog_agg.get('vx_known_author') or 0))
        eff_libraries = [
            {'library_name': r['library_name'], 'count': r['count'], 'pct': _pct(r['count'], eff_total_files)}
            for r in library_rows
            if r.get('library_name') != 'Archive-X'
        ]
        eff_recent_imports = [
            r for r in recent_imports
            if r.get('library_name') != 'Archive-X' and not str(r.get('file_path') or '').startswith('/xAdult/')
        ][:15]

    est_epubs = next(
        (r['count'] for r in format_rows if (r.get('file_format') or '').upper() == 'EPUB'),
        eff_total_files,
    )
    cyc_metrics = _get_scanner_cycle_metrics(
        fallback_total_files=est_epubs,
        fallback_complete_bookdb=catalog_agg['complete_metadata'] or 0,
        fallback_total_processed=sqlite_stats['total_processed'],
        is_scanning=is_scanning,
    )
    remaining_to_catalog = cyc_metrics['remaining_to_catalog']
    catalog_sync_chunk_info = cyc_metrics['catalog_sync_chunk_info']
    raw_tracked_books = int(sqlite_stats.get('tracked_files_count') or 0)
    eff_tracked_total = raw_tracked_books + periodicals_files_cnt + (vx_files_cnt if vx_unlocked else 0)
    discovered_files_total = max(
        eff_tracked_total,
        eff_total_files,
    )
    remaining_to_enrich = cyc_metrics['remaining_to_enrich']
    skipped_complete_bookdb = cyc_metrics['skipped_complete_bookdb']
    cycle_inspected = cyc_metrics['cycle_inspected']
    cycle_net_used = cyc_metrics['cycle_net_used']
    cycle_net_cap = cyc_metrics['cycle_net_cap']

    cycle_net_remaining = max(0, cycle_net_cap - cycle_net_used)
    enrich_total_scope = remaining_to_enrich + sqlite_stats['total_processed']
    enrich_progress_pct = _pct(sqlite_stats['total_processed'], enrich_total_scope) if enrich_total_scope > 0 else 0.0

    # Compute live scan cycle counts (# Added/Synced, # Enriched, # In Review, # No Match, # Skipped, # Failed)
    cycle_start_eff = cycle_start_dt or (now_dt - timedelta(hours=scan_interval_hours))
    cycle_start_utc_str = cycle_start_eff.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    _cyc_db_cache = _DASHBOARD_HEAVY_CACHE.get('cycle_db_counts')
    if not isinstance(_cyc_db_cache, dict) or now_mono >= _DASHBOARD_HEAVY_CACHE.get('cycle_db_expires_at', 0.0) or _cyc_db_cache.get('since') != cycle_start_utc_str:
        _c_added_db = 0
        _c_enr_db = 0
        _c_rev_db = 0
        _c_nomatch_db = 0
        _c_fail_db = 0
        try:
            _c_added_db = FileLocation.objects.filter(scanned_at__gte=cycle_start_eff).count()
        except Exception:
            pass
        try:
            _c_conn = connect_scanner_db(db_path, timeout=3.0)
            _c_conn.row_factory = sqlite3.Row
            _c_cur = _c_conn.cursor()
            for _cr in _c_cur.execute(
                "SELECT status, COUNT(*) AS cnt FROM processed_files WHERE REPLACE(updated_at, ' ', 'T') >= ? GROUP BY status",
                (cycle_start_utc_str,),
            ).fetchall():
                _cst = (_cr['status'] or '').upper()
                _ccnt = int(_cr['cnt'] or 0)
                if _cst == 'SUCCESS':
                    _c_enr_db += _ccnt
                elif _cst == 'NEEDS_REVIEW':
                    _c_rev_db += _ccnt
                elif _cst in ('NO_ISBN', 'NOT_FOUND'):
                    _c_nomatch_db += _ccnt
                elif _cst in ('QUARANTINED', 'HAS_DRM', 'FAILED'):
                    _c_fail_db += _ccnt
            _c_conn.close()
        except Exception:
            pass
        _cyc_db_cache = {
            'since': cycle_start_utc_str,
            'added_db': _c_added_db,
            'enriched_db': _c_enr_db,
            'in_review_db': _c_rev_db,
            'no_match_db': _c_nomatch_db,
            'failed_db': _c_fail_db,
        }
        _DASHBOARD_HEAVY_CACHE['cycle_db_counts'] = _cyc_db_cache
        _DASHBOARD_HEAVY_CACHE['cycle_db_expires_at'] = now_mono + 10.0

    _tot_db_proc = (
        int(_cyc_db_cache.get('enriched_db') or 0)
        + int(_cyc_db_cache.get('in_review_db') or 0)
        + int(_cyc_db_cache.get('no_match_db') or 0)
        + int(_cyc_db_cache.get('failed_db') or 0)
    )
    cycle_counts = {
        'cycle_started_at': cycle_start_eff.strftime('%H:%M:%S %Z') if cycle_start_dt else 'Current Window',
        'dirs_walked': int(cyc_metrics.get('dirs_walked') or 0),
        'new_changed': int(cyc_metrics.get('new_changed_in_walk') or 0),
        'added_synced': max(int(cyc_metrics.get('cycle_synced_log') or 0), int(_cyc_db_cache.get('added_db') or 0)),
        'inspected': max(int(cycle_inspected or 0), _tot_db_proc),
        'enriched': int(_cyc_db_cache.get('enriched_db') or 0),
        'in_review': int(_cyc_db_cache.get('in_review_db') or 0),
        'no_match': int(_cyc_db_cache.get('no_match_db') or 0),
        'skipped': int(cyc_metrics.get('cycle_skipped_log') or 0),
        'failed': max(int(cyc_metrics.get('cycle_failed_log') or 0), int(_cyc_db_cache.get('failed_db') or 0)),
        'net_used': int(cycle_net_used or 0),
        'net_cap': int(cycle_net_cap or 4000),
    }

    sqlite_stats['tracked_books_count'] = raw_tracked_books
    sqlite_stats['tracked_mags_count'] = periodicals_files_cnt
    sqlite_stats['tracked_archive_x_count'] = vx_files_cnt if vx_unlocked else 0
    sqlite_stats['tracked_files_count'] = eff_tracked_total
    sqlite_stats['remaining_to_catalog'] = remaining_to_catalog
    sqlite_stats['catalog_sync_chunk_info'] = catalog_sync_chunk_info
    sqlite_stats['discovered_files_total'] = discovered_files_total
    sqlite_stats['remaining_to_enrich'] = remaining_to_enrich
    sqlite_stats['skipped_complete_bookdb'] = skipped_complete_bookdb
    sqlite_stats['cycle_inspected'] = cycle_inspected
    sqlite_stats['cycle_net_used'] = cycle_net_used
    sqlite_stats['cycle_net_cap'] = cycle_net_cap
    sqlite_stats['cycle_net_remaining'] = cycle_net_remaining
    sqlite_stats['enrich_progress_pct'] = enrich_progress_pct

    if not vx_unlocked:
        sqlite_stats['recent_conversions'] = [
            rc for rc in (sqlite_stats.get('recent_conversions') or [])
            if not str(rc.get('file_path') or '').startswith('/xAdult/')
        ]
        sqlite_stats['attention_items'] = [
            ai for ai in (sqlite_stats.get('attention_items') or [])
            if not str(ai.get('file_path') or '').startswith('/xAdult/')
        ]

    if sqlite_stats.get('last_synced_at'):
        sqlite_stats['last_synced_at'] = _format_est_timestamp(sqlite_stats['last_synced_at'], include_tz=True)
    for _rc in sqlite_stats.get('recent_conversions') or []:
        if _rc.get('updated_at'):
            _rc['updated_at'] = _format_est_timestamp(_rc['updated_at'], include_tz=True)
    for _ai in sqlite_stats.get('attention_items') or []:
        if _ai.get('updated_at'):
            _ai['updated_at'] = _format_est_timestamp(_ai['updated_at'], include_tz=True)
    for _ri in eff_recent_imports:
        if _ri.get('scanned_at'):
            _ri['scanned_at'] = _format_est_timestamp(_ri['scanned_at'], include_tz=True)

    # 5. Enrichment Breakdown by Catalog Type (Books, Periodicals, Archive-X, Kindle, Audible/Audiobooks)
    books_items_cnt = int(catalog_agg.get('books_count') or 0)
    books_enr_cnt = int(sqlite_stats.get('status_counts', {}).get('SUCCESS') or 0)
    books_enr_files_cnt = int(sqlite_stats.get('enriched_files_count') or books_enr_cnt)
    books_complete_cnt = int(catalog_agg.get('books_complete') or catalog_agg.get('complete_metadata') or 0)
    books_cover_cnt = int(catalog_agg.get('books_with_cover') or 0)
    books_id_cnt = int(catalog_agg.get('books_with_id') or catalog_agg.get('with_isbn') or 0)
    books_desc_cnt = int(catalog_agg.get('books_with_desc') or 0)
    books_review_cnt = int(sqlite_stats.get('status_counts', {}).get('NEEDS_REVIEW') or 0)
    books_no_isbn_cnt = int(sqlite_stats.get('status_counts', {}).get('NO_ISBN') or 0)
    books_quar_drm_cnt = (
        int(sqlite_stats.get('status_counts', {}).get('QUARANTINED') or 0)
        + int(sqlite_stats.get('status_counts', {}).get('HAS_DRM') or 0)
    )

    raw_mags_cover = int(catalog_agg.get('mags_with_cover') or 0)
    raw_mags_desc = int(catalog_agg.get('mags_with_desc') or 0)
    raw_mags_org = int(catalog_agg.get('mags_organized') or raw_mags_desc)
    vx_cov_cnt = int(catalog_agg.get('vx_with_cover') or 0)
    vx_dsc_cnt = int(catalog_agg.get('vx_with_description') or 0)
    pure_mags_cover = max(0, raw_mags_cover - vx_cov_cnt)
    pure_mags_desc = max(0, raw_mags_desc - vx_dsc_cnt)
    pure_mags_org = max(0, raw_mags_org - vx_items_cnt)

    kindle_items_cnt = int(catalog_agg.get('kindle_count') or 0)
    kindle_files_cnt = int(file_agg.get('kindle_files') or 0)
    kindle_cover_cnt = int(catalog_agg.get('kindle_with_cover') or kindle_items_cnt)
    kindle_id_cnt = int(catalog_agg.get('kindle_with_id') or kindle_items_cnt)
    kindle_desc_cnt = int(catalog_agg.get('kindle_with_desc') or 0)

    audio_items_cnt = int(catalog_agg.get('audible_count') or 0) + int(catalog_agg.get('audiobook_count') or 0)
    audio_files_cnt = int(file_agg.get('audio_files') or 0)
    audio_cover_cnt = int(catalog_agg.get('audio_with_cover') or audio_items_cnt)
    audio_id_cnt = int(catalog_agg.get('audio_with_id') or audio_items_cnt)
    audio_desc_cnt = int(catalog_agg.get('audio_with_desc') or 0)

    enrichment_by_type = [
        {
            'item_type': 'BOOK',
            'label': 'Books',
            'icon': 'fa-book',
            'accent': 'purple',
            'pipeline': 'epubmeta + ISBN / Cover / AI',
            'total_items': books_items_cnt,
            'total_files': books_files_cnt,
            'enriched_items': books_enr_cnt,
            'enriched_files': books_enr_files_cnt,
            'enriched_pct': _pct(books_enr_cnt, books_items_cnt),
            'complete_metadata': books_complete_cnt,
            'complete_pct': _pct(books_complete_cnt, books_items_cnt),
            'with_cover': books_cover_cnt,
            'with_cover_pct': _pct(books_cover_cnt, books_items_cnt),
            'with_id': books_id_cnt,
            'with_id_pct': _pct(books_id_cnt, books_items_cnt),
            'with_description': books_desc_cnt,
            'with_description_pct': _pct(books_desc_cnt, books_items_cnt),
            'in_review': books_review_cnt,
            'no_match': books_no_isbn_cnt,
            'quarantined_or_drm': books_quar_drm_cnt,
            'remaining_to_enrich': remaining_to_enrich,
            'catalog_url': '/mylibrary/?type=BOOK',
            'enriched_url': '/mylibrary/?type=BOOK&enrich_status=enriched',
            'remaining_url': '/mylibrary/?type=BOOK&enrich_status=needs_enriching',
        },
        {
            'item_type': 'MAGAZINE',
            'label': 'Periodicals / Magazines',
            'icon': 'fa-newspaper',
            'accent': 'cyan',
            'pipeline': 'OrgPeriodicals + Page-1 Cover',
            'total_items': pure_mags_items,
            'total_files': periodicals_files_cnt,
            'enriched_items': pure_mags_cover,
            'enriched_files': pure_mags_cover,
            'enriched_pct': _pct(pure_mags_cover, pure_mags_items),
            'complete_metadata': pure_mags_org,
            'complete_pct': _pct(pure_mags_org, pure_mags_items),
            'with_cover': pure_mags_cover,
            'with_cover_pct': _pct(pure_mags_cover, pure_mags_items),
            'with_id': int(catalog_agg.get('mags_with_id') or 0),
            'with_id_pct': _pct(int(catalog_agg.get('mags_with_id') or 0), pure_mags_items),
            'with_description': pure_mags_desc,
            'with_description_pct': _pct(pure_mags_desc, pure_mags_items),
            'in_review': 0,
            'no_match': max(0, pure_mags_items - pure_mags_org),
            'quarantined_or_drm': 0,
            'remaining_to_enrich': max(0, pure_mags_items - pure_mags_cover),
            'catalog_url': '/mylibrary/?type=MAGAZINE',
            'enriched_url': '/mylibrary/?type=MAGAZINE&enrich_status=enriched',
            'remaining_url': '/mylibrary/?type=MAGAZINE&enrich_status=needs_enriching',
        },
    ]
    if vx_unlocked and vx_items_cnt > 0:
        enrichment_by_type.append({
            'item_type': 'ARCHIVE_X',
            'label': 'Archive-X Periodicals',
            'icon': 'fa-box-archive',
            'accent': 'pink',
            'pipeline': 'OrgPeriodicals (Archive-X)',
            'total_items': vx_items_cnt,
            'total_files': vx_files_cnt,
            'enriched_items': vx_cov_cnt,
            'enriched_files': vx_cov_cnt,
            'enriched_pct': _pct(vx_cov_cnt, vx_items_cnt),
            'complete_metadata': vx_dsc_cnt,
            'complete_pct': _pct(vx_dsc_cnt, vx_items_cnt),
            'with_cover': vx_cov_cnt,
            'with_cover_pct': _pct(vx_cov_cnt, vx_items_cnt),
            'with_id': 0,
            'with_id_pct': 0.0,
            'with_description': vx_dsc_cnt,
            'with_description_pct': _pct(vx_dsc_cnt, vx_items_cnt),
            'in_review': 0,
            'no_match': 0,
            'quarantined_or_drm': 0,
            'remaining_to_enrich': max(0, vx_items_cnt - vx_cov_cnt),
            'catalog_url': '/mylibrary/?folder=Archive-X',
            'enriched_url': '/mylibrary/?folder=Archive-X&enrich_status=enriched',
            'remaining_url': '/mylibrary/?folder=Archive-X&enrich_status=needs_enriching',
        })
    enrichment_by_type.extend([
        {
            'item_type': 'KINDLE',
            'label': 'Amazon Kindle',
            'icon': 'fa-tablet-screen-button',
            'accent': 'amber',
            'pipeline': 'Amazon Sync + ASIN / Cover',
            'total_items': kindle_items_cnt,
            'total_files': kindle_files_cnt,
            'enriched_items': kindle_cover_cnt,
            'enriched_files': kindle_files_cnt,
            'enriched_pct': _pct(kindle_cover_cnt, kindle_items_cnt),
            'complete_metadata': kindle_desc_cnt,
            'complete_pct': _pct(kindle_desc_cnt, kindle_items_cnt),
            'with_cover': kindle_cover_cnt,
            'with_cover_pct': _pct(kindle_cover_cnt, kindle_items_cnt),
            'with_id': kindle_id_cnt,
            'with_id_pct': _pct(kindle_id_cnt, kindle_items_cnt),
            'with_description': kindle_desc_cnt,
            'with_description_pct': _pct(kindle_desc_cnt, kindle_items_cnt),
            'in_review': 0,
            'no_match': 0,
            'quarantined_or_drm': 0,
            'remaining_to_enrich': max(0, kindle_items_cnt - kindle_desc_cnt),
            'catalog_url': '/mylibrary/?type=KINDLE',
            'enriched_url': '/mylibrary/?type=KINDLE',
            'remaining_url': '/mylibrary/?type=KINDLE',
        },
        {
            'item_type': 'AUDIBLE',
            'label': 'Audible & Audiobooks',
            'icon': 'fa-headphones',
            'accent': 'green',
            'pipeline': 'Audible / Audiobook Sync',
            'total_items': audio_items_cnt,
            'total_files': audio_files_cnt,
            'enriched_items': audio_cover_cnt,
            'enriched_files': audio_files_cnt,
            'enriched_pct': _pct(audio_cover_cnt, audio_items_cnt),
            'complete_metadata': audio_desc_cnt,
            'complete_pct': _pct(audio_desc_cnt, audio_items_cnt),
            'with_cover': audio_cover_cnt,
            'with_cover_pct': _pct(audio_cover_cnt, audio_items_cnt),
            'with_id': audio_id_cnt,
            'with_id_pct': _pct(audio_id_cnt, audio_items_cnt),
            'with_description': audio_desc_cnt,
            'with_description_pct': _pct(audio_desc_cnt, audio_items_cnt),
            'in_review': 0,
            'no_match': 0,
            'quarantined_or_drm': 0,
            'remaining_to_enrich': max(0, audio_items_cnt - audio_desc_cnt),
            'catalog_url': '/mylibrary/?type=AUDIBLE',
            'enriched_url': '/mylibrary/?type=AUDIBLE',
            'remaining_url': '/mylibrary/?type=AUDIBLE',
        },
    ])

    return {
        'generated_at': now_dt.strftime('%Y-%m-%d %H:%M:%S %Z'),
        'vx_unlocked': bool(vx_unlocked),
        'scanner': {
            'is_scanning': is_scanning,
            'daemon_active': daemon_active,
            'current_activity': current_activity,
            'current_library': current_library,
            'last_log_update': log_mtime_str,
            'seconds_since_log': seconds_since_log,
            'scan_interval_hours': scan_interval_hours,
            'next_scan_at': next_scan_at,
            'next_scan_iso': next_scan_iso,
            'next_scan_in_seconds': next_scan_in_seconds,
            'next_scan_countdown': next_scan_countdown,
            'cycle_counts': cycle_counts,
            'scanner_log_lines': scanner_lines,
            'epubmeta_log_lines': epubmeta_lines,
        },
        'catalog': {
            'total_items': eff_total_items,
            'total_files': eff_total_files,
            'books_count': catalog_agg['books_count'] or 0,
            'magazines_count': pure_mags_items,
            'archive_x_count': vx_items_cnt if vx_unlocked else 0,
            'kindle_count': catalog_agg['kindle_count'] or 0,
            'audible_count': catalog_agg['audible_count'] or 0,
            'audiobook_count': catalog_agg['audiobook_count'] or 0,
            'books_files_count': books_files_cnt,
            'periodicals_files_count': periodicals_files_cnt,
            'archive_x_files_count': vx_files_cnt if vx_unlocked else 0,
            'valid_files': eff_valid_files,
            'drm_files': file_agg['drm_files'] or 0,
            'invalid_files': file_agg['invalid_files'] or 0,
            'valid_files_pct': _pct(eff_valid_files, eff_total_files),
            'with_isbn': catalog_agg['with_isbn'] or 0,
            'with_isbn_pct': _pct(catalog_agg['with_isbn'] or 0, eff_total_items),
            'with_description': eff_with_desc,
            'with_description_pct': _pct(eff_with_desc, eff_total_items),
            'with_publisher': eff_with_pub,
            'with_publisher_pct': _pct(eff_with_pub, eff_total_items),
            'with_cover': eff_with_cover,
            'with_cover_pct': _pct(eff_with_cover, eff_total_items),
            'known_author': eff_known_author,
            'known_author_pct': _pct(eff_known_author, eff_total_items),
            'complete_metadata': catalog_agg['complete_metadata'] or 0,
            'complete_metadata_pct': _pct(catalog_agg['complete_metadata'] or 0, eff_total_items),
            'formats': format_rows,
            'libraries': eff_libraries,
            'recent_imports': eff_recent_imports,
            'enrichment_by_type': enrichment_by_type,
        },
        'enrichment_by_type': enrichment_by_type,
        'conversion': sqlite_stats,
    }


class StatusDashboardView(TemplateView):
    """
    Dashboard page displaying live Conversion (epubmeta) & Import (MyLibrary Scanner) status.
    """
    template_name = 'catalog/status_dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        vx_unlocked = bool(self.request.session.get('vx_unlocked', False))
        context['vx_unlocked'] = vx_unlocked
        context['dashboard'] = get_conversion_and_import_status(vx_unlocked=vx_unlocked)
        return context


class StatusDashboardAPIView(APIView):
    """
    JSON API endpoint for live auto-refresh and manual scan triggering on the Conversion & Import Status Dashboard.
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        vx_unlocked = bool(request.session.get('vx_unlocked', False))
        return Response(get_conversion_and_import_status(vx_unlocked=vx_unlocked))

    def post(self, request):
        action = (request.data.get('action') or 'start_scan').strip().lower()
        if action not in ('start_scan', 'trigger_scan'):
            return Response({'ok': False, 'error': f'Unsupported action: {action}'}, status=400)
        base_dir = Path(settings.BASE_DIR)
        nas_scanner_dir = base_dir / 'nas_scanner'
        scanner_dir = (
            base_dir / 'scanner'
            if not (nas_scanner_dir / 'scanner_state.db').exists() and (base_dir / 'scanner' / 'scanner_state.db').exists()
            else nas_scanner_dir
        )
        res = _ensure_scanner_daemon_or_trigger(scanner_dir, force_trigger=True)
        vx_unlocked = bool(request.session.get('vx_unlocked', False))
        status_payload = get_conversion_and_import_status(vx_unlocked=vx_unlocked)
        return Response({
            'ok': bool(res.get('ok', True)),
            'trigger': res,
            'dashboard': status_payload,
        })


def _get_scanner_db_path() -> Path:
    base_dir = Path(settings.BASE_DIR)
    nas_scanner_dir = base_dir / 'nas_scanner'
    if not (nas_scanner_dir / 'scanner_state.db').exists() and (base_dir / 'scanner' / 'scanner_state.db').exists():
        return base_dir / 'scanner' / 'scanner_state.db'
    return nas_scanner_dir / 'scanner_state.db'


class ConversionItemsAPIView(APIView):
    """
    Interactive API endpoint to browse, filter, search, and re-queue epubmeta conversion
    and quarantine records from scanner_state.db (`processed_files`).
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        status_filter = (request.GET.get('status') or 'ALL').strip().upper()
        query = (request.GET.get('q') or '').strip()
        try:
            limit = min(max(int(request.GET.get('limit', 50)), 1), 250)
        except ValueError:
            limit = 50
        try:
            offset = max(int(request.GET.get('offset', 0)), 0)
        except ValueError:
            offset = 0

        db_path = _get_scanner_db_path()
        if not is_postgres_scanner_enabled() and not db_path.exists():
            return Response({'total': 0, 'status': status_filter, 'items': []})

        conn = None
        try:
            conn = connect_scanner_db(db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            where_clauses = []
            params = []

            if status_filter == 'ATTENTION':
                where_clauses.append("status IN ('NEEDS_REVIEW', 'QUARANTINED', 'FAILED', 'HAS_DRM')")
            elif status_filter in ('NO_ISBN', 'NOT_FOUND'):
                where_clauses.append("status IN ('NO_ISBN', 'NOT_FOUND')")
            elif status_filter and status_filter != 'ALL':
                where_clauses.append("status = ?")
                params.append(status_filter)

            if query:
                like_q = f"%{query}%"
                where_clauses.append(
                    "(file_name LIKE ? OR file_path LIKE ? OR title LIKE ? OR author LIKE ? "
                    "OR orig_title LIKE ? OR orig_author LIKE ? OR isbn LIKE ? OR error_message LIKE ?)"
                )
                params.extend([like_q] * 8)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            count_row = cur.execute(
                f"SELECT COUNT(*) AS cnt FROM processed_files {where_sql}",
                params,
            ).fetchone()
            total_matching = (count_row['cnt'] or 0) if count_row else 0

            rows = cur.execute(
                f"""
                SELECT file_name, file_path, file_ext, status, isbn, title, author,
                       orig_title, orig_author, orig_isbn, match_confidence,
                       error_message, updated_at, file_size,
                       orig_sha256, file_sha256,
                       original_metadata_json, updated_metadata_json
                FROM processed_files
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()

            items = _serialize_conversion_rows(rows)

            return Response({
                'total': total_matching,
                'status': status_filter,
                'query': query,
                'limit': limit,
                'offset': offset,
                'items': items,
            })
        except Exception as e:
            return Response({'total': 0, 'status': status_filter, 'items': [], 'error': str(e)}, status=500)
        finally:
            if conn is not None:
                conn.close()

    def post(self, request):
        """
        Perform actions on a processed_files entry:
        - action='requeue': remove from processed_files & file_states so next scanner pass re-evaluates it
        - action='approve': mark a NEEDS_REVIEW item as SUCCESS
        - action='ignore': keep original metadata and permanently skip epubmeta enrichment for this file
        """
        action = (request.data.get('action') or '').strip().lower()
        file_path = (request.data.get('file_path') or '').strip()
        req_item_id = (request.data.get('item_id') or '').strip()
        if not file_path and req_item_id:
            first_loc = FileLocation.objects.filter(catalog_item_id=req_item_id).first()
            if first_loc and first_loc.file_path:
                file_path = first_loc.file_path
        if not file_path or action not in ('requeue', 'approve', 'ignore'):
            return Response({'ok': False, 'error': 'Invalid action or file_path'}, status=400)

        path_variants = [file_path]
        if file_path.startswith('/books/'):
            path_variants.append('/volume1/common/Media/Books/' + file_path[len('/books/'):])
        elif file_path.startswith('/volume1/common/Media/Books/'):
            path_variants.append('/books/' + file_path[len('/volume1/common/Media/Books/'):])

        db_path = _get_scanner_db_path()
        if not is_postgres_scanner_enabled() and not db_path.exists():
            return Response({'ok': False, 'error': 'scanner_state.db not found'}, status=404)

        conn = None
        try:
            conn = connect_scanner_db(db_path, timeout=30.0)
            conn.execute("PRAGMA busy_timeout = 30000")
            cur = conn.cursor()
            if action == 'requeue':
                for pv in path_variants:
                    cur.execute("DELETE FROM processed_files WHERE file_path = ?", (pv,))
                    cur.execute("DELETE FROM file_states WHERE file_path = ?", (pv,))
                conn.commit()
                return Response({'ok': True, 'action': 'requeue', 'file_path': file_path})
            elif action == 'approve':
                import json as _json
                sel = request.data.get('selected_metadata') or {}
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                row = None
                matched_db_path = file_path
                for pv in path_variants:
                    row = cur.execute(
                        "SELECT title, author, isbn, orig_title, orig_author, orig_isbn, orig_sha256, file_sha256, original_metadata_json, updated_metadata_json FROM processed_files WHERE file_path = ?",
                        (pv,),
                    ).fetchone()
                    if row:
                        matched_db_path = pv
                        break

                orig_meta = {}
                if row and row['original_metadata_json']:
                    try:
                        orig_meta = _json.loads(row['original_metadata_json']) or {}
                    except Exception:
                        orig_meta = {}

                upd_meta = {}
                if row and row['updated_metadata_json']:
                    try:
                        upd_meta = _json.loads(row['updated_metadata_json']) or {}
                    except Exception:
                        upd_meta = {}

                final_title = (sel.get('title') or (row['title'] if row else '') or (row['orig_title'] if row else '') or '').strip()[:512]
                final_author = (sel.get('author') or (row['author'] if row else '') or (row['orig_author'] if row else '') or '').strip()[:512]
                final_pub = (sel.get('publisher') if 'publisher' in sel else (upd_meta.get('publisher') or '')).strip()[:256]
                final_pubdate = (sel.get('pub_date') if 'pub_date' in sel else (upd_meta.get('published_date') or '')).strip()[:32]
                final_isbn = (sel.get('isbn') if 'isbn' in sel else ((row['isbn'] if row else '') or '')).strip()[:32]
                final_desc = (sel.get('description') if 'description' in sel else (upd_meta.get('description') or '')).strip()
                # Preserve original cover unless missing or explicitly overridden in sel['cover']
                if 'cover' in sel and sel.get('cover'):
                    final_cover = str(sel.get('cover')).strip()
                else:
                    final_cover = str(orig_meta.get('cover_url') or upd_meta.get('cover_url') or '').strip()

                upd_meta['title'] = final_title
                upd_meta['authors'] = [a.strip() for a in final_author.split(',') if a.strip()] if final_author else []
                upd_meta['publisher'] = final_pub
                if final_pubdate:
                    upd_meta['published_date'] = final_pubdate
                upd_meta['isbn13'] = final_isbn
                upd_meta['description'] = final_desc
                if final_cover and not final_cover.startswith('data:'):
                    upd_meta['cover_url'] = final_cover
                elif not final_cover:
                    upd_meta['cover_url'] = None

                import datetime as _dt_now
                now_iso = _dt_now.datetime.now(_dt_now.timezone.utc).isoformat()
                cur.execute(
                    """
                    UPDATE processed_files
                    SET status = 'SUCCESS',
                        title = ?,
                        author = ?,
                        isbn = ?,
                        updated_metadata_json = ?,
                        error_message = NULL,
                        updated_at = ?
                    WHERE file_path = ?
                    """,
                    (
                        final_title or None,
                        final_author or None,
                        final_isbn or None,
                        _json.dumps(upd_meta, ensure_ascii=False),
                        now_iso,
                        matched_db_path,
                    ),
                )
                if cur.rowcount == 0:
                    target_insert_path = (
                        '/volume1/common/Media/Books/' + file_path[len('/books/'):]
                        if file_path.startswith('/books/')
                        else file_path
                    )
                    fname = os.path.basename(target_insert_path)
                    fext = os.path.splitext(fname)[1].lower()
                    loc_for_sha = FileLocation.objects.filter(file_path__in=path_variants).first()
                    sha_val = (loc_for_sha.file_hash if loc_for_sha and loc_for_sha.file_hash else None)
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO processed_files
                        (file_path, file_name, file_ext, file_sha256, orig_sha256, status, title, author, isbn, orig_title, orig_author, orig_isbn, updated_metadata_json, updated_at)
                        VALUES (?, ?, ?, ?, ?, 'SUCCESS', ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_insert_path,
                            fname,
                            fext,
                            sha_val,
                            sha_val,
                            final_title or None,
                            final_author or None,
                            final_isbn or None,
                            final_title or None,
                            final_author or None,
                            final_isbn or None,
                            _json.dumps(upd_meta, ensure_ascii=False),
                            now_iso,
                        ),
                    )
                conn.commit()

                # Also update the matching CatalogItem in PostgreSQL BookDB
                try:
                    from .opds import _ENRICHED_PATHS_CACHE, _save_enriched_cache_to_disk, get_enriched_paths_set
                    get_enriched_paths_set()
                    if _ENRICHED_PATHS_CACHE.get('paths_set') is not None and file_path:
                        for pv in path_variants:
                            _ENRICHED_PATHS_CACHE['paths_set'].add(str(pv))
                    if isinstance(_ENRICHED_PATHS_CACHE.get('status_by_path'), dict):
                        for pv in path_variants:
                            _ENRICHED_PATHS_CACHE['status_by_path'].pop(str(pv), None)
                    if isinstance(_ENRICHED_PATHS_CACHE.get('diag_by_path'), dict):
                        for pv in path_variants:
                            _ENRICHED_PATHS_CACHE['diag_by_path'].pop(str(pv), None)

                    cat_item = None
                    req_item_id = (request.data.get('item_id') or '').strip()
                    from .opds import resolve_catalog_item_by_id_or_fallback
                    cat_item = resolve_catalog_item_by_id_or_fallback(
                        item_or_id=req_item_id,
                        file_path=file_path,
                        isbn=final_isbn,
                        title=final_title,
                        authors=final_author,
                    )
                    if not cat_item and row:
                        shas = [s for s in (row['file_sha256'], row['orig_sha256']) if s]
                        if shas:
                            loc = FileLocation.objects.select_related('catalog_item').filter(file_hash__in=shas).first()
                            if loc and loc.catalog_item:
                                cat_item = loc.catalog_item
                        if not cat_item and (row['orig_isbn'] or row['isbn']):
                            isbns = [i for i in (row['orig_isbn'], row['isbn']) if i]
                            cat_item = CatalogItem.objects.filter(isbn__in=isbns).first()
                        if not cat_item and (row['orig_title'] or row['title']):
                            titles = [t for t in (row['orig_title'], row['title']) if t]
                            cat_item = CatalogItem.objects.filter(title__in=titles).first()

                    if cat_item:
                        if final_title:
                            cat_item.title = final_title
                        if final_author:
                            final_author = format_normalized_authors_str(final_author) or final_author
                            cat_item.authors = final_author
                        cat_item.publisher = final_pub or cat_item.publisher
                        cat_item.isbn = final_isbn or cat_item.isbn
                        if final_pubdate:
                            import datetime as _dt
                            m_dt = re.match(r'^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?', final_pubdate)
                            if m_dt:
                                try:
                                    cat_item.pub_date = _dt.date(
                                        int(m_dt.group(1)),
                                        min(max(int(m_dt.group(2) or 1), 1), 12),
                                        min(max(int(m_dt.group(3) or 1), 1), 28),
                                    )
                                except Exception:
                                    pass
                        if final_desc:
                            cat_item.description = final_desc
                        if final_cover:
                            if final_cover.startswith('data:'):
                                try:
                                    from .opds import apply_book_cover_to_item
                                    cov_res = apply_book_cover_to_item(cat_item, final_cover)
                                    if cov_res.get('ok') and cov_res.get('cover_url'):
                                        final_cover = cov_res['cover_url']
                                    else:
                                        cat_item.cover_thumbnail = final_cover
                                except Exception:
                                    cat_item.cover_thumbnail = final_cover
                            elif 'cover' in sel or not cat_item.cover_thumbnail:
                                cat_item.cover_image_url = final_cover
                                try:
                                    from .opds import apply_book_cover_to_item
                                    cov_res = apply_book_cover_to_item(cat_item, final_cover)
                                    if cov_res.get('ok') and cov_res.get('cover_url'):
                                        final_cover = cov_res['cover_url']
                                except Exception:
                                    pass
                        cat_item.save()
                        from .opds import clear_book_bad_metadata, evaluate_and_record_item_cover_status, merge_duplicate_catalog_items_for_item
                        clear_book_bad_metadata(cat_item.id)
                        evaluate_and_record_item_cover_status(cat_item)
                        merged_item_ids = merge_duplicate_catalog_items_for_item(cat_item)
                        if isinstance(_ENRICHED_PATHS_CACHE.get('enriched_item_ids_set'), set):
                            if cat_item.id not in _ENRICHED_PATHS_CACHE['enriched_item_ids_set']:
                                _ENRICHED_PATHS_CACHE['enriched_item_ids_set'].add(cat_item.id)
                                if isinstance(_ENRICHED_PATHS_CACHE.get('enriched_item_ids'), list):
                                    _ENRICHED_PATHS_CACHE['enriched_item_ids'].append(cat_item.id)
                        elif isinstance(_ENRICHED_PATHS_CACHE.get('enriched_item_ids'), list):
                            if cat_item.id not in _ENRICHED_PATHS_CACHE['enriched_item_ids']:
                                _ENRICHED_PATHS_CACHE['enriched_item_ids'].append(cat_item.id)
                        if isinstance(_ENRICHED_PATHS_CACHE.get('needs_review_item_ids_set'), set):
                            if cat_item.id in _ENRICHED_PATHS_CACHE['needs_review_item_ids_set']:
                                _ENRICHED_PATHS_CACHE['needs_review_item_ids_set'].discard(cat_item.id)
                                if isinstance(_ENRICHED_PATHS_CACHE.get('needs_review_item_ids'), list):
                                    try:
                                        _ENRICHED_PATHS_CACHE['needs_review_item_ids'].remove(cat_item.id)
                                    except ValueError:
                                        pass
                        if isinstance(_ENRICHED_PATHS_CACHE.get('strict_in_review_ids_set'), set):
                            if cat_item.id in _ENRICHED_PATHS_CACHE['strict_in_review_ids_set']:
                                _ENRICHED_PATHS_CACHE['strict_in_review_ids_set'].discard(cat_item.id)
                                if isinstance(_ENRICHED_PATHS_CACHE.get('strict_in_review_ids'), list):
                                    try:
                                        _ENRICHED_PATHS_CACHE['strict_in_review_ids'].remove(cat_item.id)
                                    except ValueError:
                                        pass
                        if isinstance(_ENRICHED_PATHS_CACHE.get('no_match_item_ids_set'), set):
                            if cat_item.id in _ENRICHED_PATHS_CACHE['no_match_item_ids_set']:
                                _ENRICHED_PATHS_CACHE['no_match_item_ids_set'].discard(cat_item.id)
                                if isinstance(_ENRICHED_PATHS_CACHE.get('no_match_item_ids'), list):
                                    try:
                                        _ENRICHED_PATHS_CACHE['no_match_item_ids'].remove(cat_item.id)
                                    except ValueError:
                                        pass
                        if isinstance(_ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids_set'), set):
                            if cat_item.id not in _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids_set']:
                                _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids_set'].add(cat_item.id)
                                if isinstance(_ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids'), list):
                                    _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids'].append(cat_item.id)
                        _save_enriched_cache_to_disk()
                except Exception:
                    merged_item_ids = []

                return Response({
                    'ok': True,
                    'action': 'approve',
                    'item_id': str(cat_item.id) if 'cat_item' in locals() and cat_item else (request.data.get('item_id') or ''),
                    'file_path': file_path,
                    'title': final_title,
                    'authors': final_author,
                    'publisher': final_pub,
                    'isbn': final_isbn,
                    'description': final_desc,
                    'cover_url': (cat_item.cover_thumbnail or final_cover) if 'cat_item' in locals() and cat_item else final_cover,
                    'merged_item_ids': merged_item_ids if 'merged_item_ids' in locals() else [],
                })
            elif action == 'ignore':
                cur.execute(
                    """
                    UPDATE processed_files
                    SET status = 'IGNORED',
                        title = COALESCE(NULLIF(orig_title, ''), title),
                        author = COALESCE(NULLIF(orig_author, ''), author),
                        isbn = COALESCE(NULLIF(orig_isbn, ''), isbn),
                        error_message = 'Ignored by user — keeping original file metadata',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE file_path = ?
                    """,
                    (file_path,),
                )
                conn.commit()
                try:
                    from .opds import _ENRICHED_PATHS_CACHE
                    s_map = _ENRICHED_PATHS_CACHE.get('status_by_path')
                    if isinstance(s_map, dict):
                        s_map.pop(file_path, None)
                        if file_path.startswith('/volume1/common/Media/Books/'):
                            s_map.pop('/books/' + file_path[len('/volume1/common/Media/Books/'):], None)
                        elif file_path.startswith('/books/'):
                            s_map.pop('/volume1/common/Media/Books/' + file_path[len('/books/'):], None)
                except Exception:
                    pass
                return Response({'ok': True, 'action': 'ignore', 'file_path': file_path})
        except Exception as e:
            return Response({'ok': False, 'error': str(e)}, status=500)
        finally:
            if conn is not None:
                conn.close()


_TRACKED_HISTORY_CACHE: dict = {}


class ConversionHistoryAPIView(APIView):
    """
    Time-series history endpoint for charting epubmeta enrichments, review/quarantine counts,
    metadata cache growth, and tracked file syncs over time (hourly or daily).
    """

    def get(self, request):
        import time as _time
        granularity = (request.GET.get('granularity') or 'hour').strip().lower()
        substr_len = 10 if granularity == 'day' else 13  # 'YYYY-MM-DD' vs 'YYYY-MM-DDTHH'

        db_path = _get_scanner_db_path()
        if not is_postgres_scanner_enabled() and not db_path.exists():
            return Response({'granularity': granularity, 'labels': [], 'per_period': {}, 'cumulative': {}})

        conn = None
        try:
            conn = connect_scanner_db(db_path, timeout=15.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            statuses = ['ALL', 'SUCCESS', 'NEEDS_REVIEW', 'NO_ISBN', 'QUARANTINED', 'FAILED', 'IGNORED']
            bucket_map: dict[str, dict[str, int]] = {}

            rows = cur.execute(
                """
                SELECT substr(updated_at, 1, 13) AS bucket,
                       status,
                       COUNT(*) AS cnt
                FROM processed_files
                WHERE updated_at IS NOT NULL AND updated_at != ''
                GROUP BY bucket, status
                ORDER BY bucket ASC
                """
            ).fetchall()

            for r in rows:
                b = _convert_utc_bucket_to_est(r['bucket'] or '', granularity)
                st = (r['status'] or 'UNKNOWN').upper()
                if st == 'NOT_FOUND':
                    st = 'NO_ISBN'
                cnt = int(r['cnt'] or 0)
                if b not in bucket_map:
                    bucket_map[b] = {k: 0 for k in statuses + ['CACHE', 'TRACKED']}
                if st in bucket_map[b]:
                    bucket_map[b][st] += cnt
                bucket_map[b]['ALL'] += cnt

            # Also include metadata_cache timestamps if present
            try:
                c_rows = cur.execute(
                    """
                    SELECT substr(updated_at, 1, 13) AS bucket,
                           COUNT(*) AS cnt
                    FROM metadata_cache
                    WHERE updated_at IS NOT NULL AND updated_at != ''
                    GROUP BY bucket
                    ORDER BY bucket ASC
                    """
                ).fetchall()
                for r in c_rows:
                    b = _convert_utc_bucket_to_est(r['bucket'] or '', granularity)
                    if b not in bucket_map:
                        bucket_map[b] = {k: 0 for k in statuses + ['CACHE', 'TRACKED']}
                    bucket_map[b]['CACHE'] += int(r['cnt'] or 0)
            except Exception:
                pass

            # Also include file_states (Tracked File States in SQLite) cached for 120s
            try:
                now_m = _time.monotonic()
                cache_entry = _TRACKED_HISTORY_CACHE.get('hour')
                if cache_entry and now_m < cache_entry['expires_at']:
                    t_rows = cache_entry['rows']
                else:
                    raw_t = cur.execute(
                        """
                        SELECT substr(synced_at, 1, 13) AS bucket,
                               COUNT(*) AS cnt
                        FROM file_states
                        WHERE synced_at IS NOT NULL AND synced_at != ''
                        GROUP BY bucket
                        ORDER BY bucket ASC
                        """
                    ).fetchall()
                    t_rows = [(r['bucket'], int(r['cnt'] or 0)) for r in raw_t]
                    _TRACKED_HISTORY_CACHE['hour'] = {
                        'expires_at': now_m + 120.0,
                        'rows': t_rows,
                    }
                for raw_b, cnt in t_rows:
                    b = _convert_utc_bucket_to_est(raw_b or '', granularity)
                    if b not in bucket_map:
                        bucket_map[b] = {k: 0 for k in statuses + ['CACHE', 'TRACKED']}
                    bucket_map[b]['TRACKED'] += cnt
            except Exception:
                pass

            labels = sorted(bucket_map.keys())
            series_keys = statuses + ['CACHE', 'TRACKED']
            per_period = {k: [] for k in series_keys}
            cumulative = {k: [] for k in series_keys}
            running = {k: 0 for k in series_keys}

            for lbl in labels:
                entry = bucket_map[lbl]
                for k in series_keys:
                    val = entry.get(k, 0)
                    per_period[k].append(val)
                    running[k] += val
                    cumulative[k].append(running[k])

            return Response({
                'granularity': granularity,
                'labels': labels,
                'per_period': per_period,
                'cumulative': cumulative,
            })
        except Exception as e:
            return Response({'granularity': granularity, 'labels': [], 'error': str(e)}, status=500)
        finally:
            if conn is not None:
                conn.close()


def _serialize_catalog_item(item: CatalogItem, include_cover: bool = False, path_prefix: str = 'U:\\Media') -> dict:
    """Serialize a CatalogItem and its FileLocations for REST / CLI / MCP consumers."""
    files_list = []
    for f in item.files.all():
        raw_path = f.file_path or ''
        mapped_path = raw_path
        if path_prefix and raw_path.startswith('/'):
            clean_prefix = path_prefix.rstrip('/\\')
            sep = '\\' if ('\\' in clean_prefix or ':' in clean_prefix) else '/'
            rel = raw_path.lstrip('/').replace('/', sep).replace('\\', sep)
            mapped_path = f'{clean_prefix}{sep}{rel}'

        files_list.append({
            'id': str(f.id),
            'file_name': f.file_name,
            'file_path': raw_path,
            'mapped_path': mapped_path,
            'file_format': f.file_format,
            'file_size': f.file_size,
            'file_size_mb': round((f.file_size or 0) / (1024 * 1024), 2),
            'file_hash': f.file_hash,
            'is_invalid': f.is_invalid,
            'has_drm': f.has_drm,
            'library_name': f.library_name or 'NAS Library',
            'scanned_at': f.scanned_at.isoformat() if f.scanned_at else None,
        })

    payload = {
        'id': str(item.id),
        'title': item.title,
        'authors': item.authors,
        'publisher': item.publisher or '',
        'pub_date': str(item.pub_date) if item.pub_date else None,
        'isbn': item.isbn or '',
        'asin': item.asin or '',
        'item_type': item.item_type,
        'description': _strip_html(item.description or ''),
        'has_cover': bool(item.cover_thumbnail or item.cover_image_url),
        'cover_image_url': item.cover_image_url or '',
        'created_at': item.created_at.isoformat() if item.created_at else None,
        'updated_at': item.updated_at.isoformat() if item.updated_at else None,
        'files': files_list,
    }
    if include_cover:
        payload['cover_thumbnail'] = item.cover_thumbnail or ''
    return payload


class CatalogSearchAPIView(APIView):
    """
    REST API endpoint to search, filter, and paginate the MyLibrary Catalog (BookDB).
    Supports query `q`, `type`, `format`, `isbn`, `author`, `limit`, `offset`, `path_prefix`.
    """

    def get(self, request):
        query = (request.GET.get('q') or '').strip()
        isbn_filter = (request.GET.get('isbn') or '').strip()
        author_filter = (request.GET.get('author') or '').strip()
        path_prefix = (request.GET.get('path_prefix') or 'U:\\Media').strip()
        include_cover = (request.GET.get('include_cover') or '').lower() in ('1', 'true', 'yes')

        try:
            limit = min(max(int(request.GET.get('limit', 25)), 1), 200)
        except ValueError:
            limit = 25
        try:
            offset = max(int(request.GET.get('offset', 0)), 0)
        except ValueError:
            offset = 0

        item_types = []
        for t in request.GET.getlist('type'):
            item_types.extend([x.strip().upper() for x in t.split(',') if x.strip()])
        file_formats = []
        for f in request.GET.getlist('format'):
            file_formats.extend([x.strip().upper() for x in f.split(',') if x.strip()])

        filters = {}
        if item_types:
            filters['item_type'] = item_types
        if file_formats:
            filters['file_format'] = file_formats

        qs = perform_search(query, filters)
        if isbn_filter:
            isbn_cands = list({isbn_filter, isbn_filter.upper(), isbn_filter.lower()})
            qs = qs.filter(Q(isbn__in=isbn_cands) | Q(asin__in=isbn_cands))
        if author_filter:
            qs = qs.filter(build_author_q_filter(author_filter))

        if not query and not isbn_filter and not author_filter and not file_formats and not item_types:
            stats, _ = _get_cached_dashboard_meta()
            total = stats['total_items']
        else:
            total = qs.count()

        page_items = list(qs[offset:offset + limit])
        serialized = [
            _serialize_catalog_item(it, include_cover=include_cover, path_prefix=path_prefix)
            for it in page_items
        ]

        return Response({
            'total': total,
            'limit': limit,
            'offset': offset,
            'query': query,
            'filters': {
                'type': item_types,
                'format': file_formats,
                'isbn': isbn_filter,
                'author': author_filter,
            },
            'items': serialized,
        })


class CatalogItemDetailAPIView(APIView):
    """
    Retrieve or update a single CatalogItem by UUID, ISBN/ASIN, or SHA-256 file_hash.
    Also attaches any matching epubmeta conversion record from scanner_state.db.
    """

    def _resolve_item(self, identifier: str) -> CatalogItem | None:
        ident = (identifier or '').strip()
        if not ident:
            return None
        import uuid as _uuid
        try:
            u = _uuid.UUID(ident)
            item = CatalogItem.objects.prefetch_related('files').filter(id=u).first()
            if item:
                return item
        except ValueError:
            pass

        ident_cands = list({ident, ident.upper(), ident.lower()})
        item = CatalogItem.objects.prefetch_related('files').filter(
            Q(isbn__in=ident_cands) | Q(asin__in=ident_cands)
        ).first()
        if item:
            return item

        loc = FileLocation.objects.select_related('catalog_item').filter(
            Q(file_hash=ident) | Q(file_path=ident) | Q(file_name__in=ident_cands)
        ).first()
        if loc and loc.catalog_item:
            return loc.catalog_item
        return None

    def get(self, request, identifier: str):
        item = self._resolve_item(identifier)
        if not item:
            return Response({'ok': False, 'error': f'Catalog item not found: {identifier}'}, status=404)

        path_prefix = (request.GET.get('path_prefix') or 'U:\\Media').strip()
        include_cover = (request.GET.get('include_cover') or 'true').lower() in ('1', 'true', 'yes')
        data = _serialize_catalog_item(item, include_cover=include_cover, path_prefix=path_prefix)

        # Look up any epubmeta enrichment history for this item's files in scanner_state.db / BookDB
        enrichment_records = []
        db_path = _get_scanner_db_path()
        if (is_postgres_scanner_enabled() or db_path.exists()) and data['files']:
            conn = None
            try:
                conn = connect_scanner_db(db_path, timeout=5.0)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                paths = [f['file_path'] for f in data['files'] if f.get('file_path')]
                if paths:
                    placeholders = ','.join(['?'] * len(paths))
                    rows = cur.execute(
                        f"""
                        SELECT file_name, file_path, file_ext, status, isbn, title, author,
                               orig_title, orig_author, orig_isbn, match_confidence,
                               error_message, updated_at, file_size,
                               orig_sha256, file_sha256,
                               original_metadata_json, updated_metadata_json
                        FROM processed_files
                        WHERE file_path IN ({placeholders})
                        """,
                        paths,
                    ).fetchall()
                    enrichment_records = _serialize_conversion_rows(rows)
            except Exception:
                pass
            finally:
                if conn is not None:
                    conn.close()

        data['enrichment_records'] = enrichment_records
        return Response({'ok': True, 'item': data})

    def patch(self, request, identifier: str):
        return self._update(request, identifier)

    def post(self, request, identifier: str):
        return self._update(request, identifier)

    def _update(self, request, identifier: str):
        item = self._resolve_item(identifier)
        if not item:
            return Response({'ok': False, 'error': f'Catalog item not found: {identifier}'}, status=404)

        body = request.data or {}
        updated_fields = []
        for field in ('title', 'authors', 'publisher', 'isbn', 'asin', 'description', 'item_type', 'cover_image_url'):
            if field in body and body[field] is not None:
                val = str(body[field]).strip()
                if val or field in ('publisher', 'isbn', 'asin', 'description', 'cover_image_url'):
                    setattr(item, field, val)
                    updated_fields.append(field)

        if updated_fields:
            item.save()

        return Response({
            'ok': True,
            'updated_fields': updated_fields,
            'item': _serialize_catalog_item(item, include_cover=False),
        })


class ScannerLogsAPIView(APIView):
    """
    Tail `scanner.log` and `epubmeta.log` with configurable line count and optional keyword filter.
    """

    def get(self, request):
        source = (request.GET.get('source') or 'both').strip().lower()
        grep_q = (request.GET.get('grep') or '').strip().lower()
        try:
            lines_cnt = min(max(int(request.GET.get('lines', 60)), 1), 500)
        except ValueError:
            lines_cnt = 60

        base_dir = Path(settings.BASE_DIR)
        scanner_dir = base_dir / 'nas_scanner'
        if not (scanner_dir / 'logs').exists() and (base_dir / 'scanner' / 'logs').exists():
            scanner_dir = base_dir / 'scanner'

        scanner_log_path = scanner_dir / 'logs' / 'scanner.log'
        epubmeta_log_path = scanner_dir / 'logs' / 'epubmeta.log'

        result = {'source': source, 'lines_requested': lines_cnt, 'grep': grep_q}
        if source in ('scanner', 'both'):
            raw = _tail_log_file(scanner_log_path, max_lines=lines_cnt * (3 if grep_q else 1))
            if grep_q:
                raw = [ln for ln in raw if grep_q in ln.lower()][-lines_cnt:]
            result['scanner_log'] = raw
        if source in ('epubmeta', 'both'):
            raw = _tail_log_file(epubmeta_log_path, max_lines=lines_cnt * (3 if grep_q else 1))
            if grep_q:
                raw = [ln for ln in raw if grep_q in ln.lower()][-lines_cnt:]
            result['epubmeta_log'] = raw

        return Response(result)


def _norm_lookup_str(val: str | None) -> str:
    if not val:
        return ''
    s = re.sub(r'\(.*?\)|\[.*?\]', '', str(val).lower())
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return ' '.join(s.split())


def _clean_single_line(val: str | None) -> str:
    """Strip control characters, newlines, tabs, and Unicode paragraph/line separators."""
    if not val:
        return ''
    s = re.sub(r'[\x00-\x1f\x7f-\x9f\r\n\t\u2028\u2029]+', ' ', str(val))
    return re.sub(r'\s+', ' ', s).strip()


def _moon_sanitize_stem(filename: str) -> str:
    """
    Replicate Moon+ Reader's outer WebDAV filename sanitization rule:
    replaces apostrophes, colons, and reserved path characters with `_`.
    """
    cleaned = _clean_single_line(filename)
    return re.sub(r"[':?*\"<>|/\\]", "_", cleaned).strip(" .")


class CalibreCheckEnrichedAPIView(APIView):
    """
    Batch API endpoint for `calibre_moon_sync.py` to check which books in a local
    Calibre library (`I:\\Calibre Library`) or Moon+ Reader WebDAV Cache
    (`U:\\webdav\\Apps\\Books\\.Moon+\\Cache`) have been enriched by `epubmeta`.

    - GET  /mylibrary/api/calibre/check-enriched/?path_prefix=U:\\Media
      Returns summary stats + list of enriched records (`status = 'SUCCESS'`).
    - POST /mylibrary/api/calibre/check-enriched/
      Body:
        {
          "path_prefix": "U:\\\\Media",
          "books": [
            {
              "calibre_id": 123,
              "sha256": "...",
              "isbn": "...",
              "title": "...",
              "authors": "...",
              "file_name": "..."
            }
          ],
          "moon_filenames": [
            "Death_s End - Cixin Liu.epub",
            "Access to Online Resources... -- Anna_s Archive.epub"
          ]
        }
    """

    def _load_enriched_index(self, path_prefix: str = 'U:\\Media'):
        db_path = _get_scanner_db_path()
        records = []
        if not is_postgres_scanner_enabled() and not db_path.exists():
            return records, {}, {}, {}, {}, {}

        conn = None
        try:
            conn = connect_scanner_db(db_path, timeout=15.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute(
                """
                SELECT file_name, file_path, file_ext, status, isbn, title, author,
                       orig_title, orig_author, orig_isbn, match_confidence,
                       updated_at, file_size, orig_sha256, file_sha256,
                       original_metadata_json, updated_metadata_json
                FROM processed_files
                WHERE status = 'SUCCESS'
                ORDER BY updated_at DESC
                """
            ).fetchall()
        finally:
            if conn is not None:
                conn.close()

        by_orig_sha = {}
        by_new_sha = {}
        by_isbn = {}
        by_filename = {}
        by_norm_title = {}

        clean_prefix = (path_prefix or '').rstrip('/\\')
        sep = '\\' if ('\\' in clean_prefix or ':' in clean_prefix) else '/'

        for r in rows:
            fp = r['file_path'] or ''
            mapped_path = fp
            if clean_prefix and fp.startswith('/'):
                rel = fp.lstrip('/').replace('/', sep).replace('\\', sep)
                mapped_path = f'{clean_prefix}{sep}{rel}'

            cand_meta = {}
            if r['updated_metadata_json']:
                try:
                    cand_meta = json.loads(r['updated_metadata_json'])
                except Exception:
                    cand_meta = {}

            title = _clean_single_line(r['title'] or cand_meta.get('title') or '')
            author = _clean_single_line(r['author'] or '')
            if not author and isinstance(cand_meta.get('authors'), list):
                author = _clean_single_line(', '.join(str(a) for a in cand_meta['authors'] if a))
            isbn = _clean_single_line(r['isbn'] or cand_meta.get('isbn13') or cand_meta.get('isbn10') or '')
            orig_isbn = _clean_single_line(r['orig_isbn'] or '')
            publisher = _clean_single_line(cand_meta.get('publisher') or '')
            description = _strip_html(cand_meta.get('description') or '')
            cover_url = _clean_single_line(cand_meta.get('cover_url') or '')
            file_name = _clean_single_line(r['file_name'] or os.path.basename(fp))
            orig_title = _clean_single_line(r['orig_title'] or '')
            orig_author = _clean_single_line(r['orig_author'] or '')
            orig_sha = (r['orig_sha256'] or '').strip().lower()
            new_sha = (r['file_sha256'] or '').strip().lower()

            # Compute standard Calibre target filename: "<Title> - <Author>.epub"
            calibre_author = author.split(',')[0].strip() if ',' in author and '&' not in author else author
            raw_stem = f'{title} - {calibre_author}' if calibre_author else title
            expected_calibre_stem = re.sub(r'[\\/:*?"<>|]', '_', _clean_single_line(raw_stem)).strip(' .')
            if len(expected_calibre_stem) > 180:
                expected_calibre_stem = expected_calibre_stem[:180].rstrip(' ._-')
            expected_calibre_filename = f'{expected_calibre_stem}.epub'

            rec = {
                'file_name': file_name,
                'file_path': fp,
                'mapped_path': mapped_path,
                'orig_sha256': orig_sha,
                'file_sha256': new_sha,
                'title': title,
                'authors': author,
                'publisher': publisher,
                'isbn': isbn,
                'orig_isbn': orig_isbn,
                'orig_title': orig_title,
                'orig_author': orig_author,
                'description': description,
                'cover_url': cover_url,
                'updated_at': (r['updated_at'] or '')[:19].replace('T', ' '),
                'expected_calibre_filename': expected_calibre_filename,
                'expected_moon_stem': _moon_sanitize_stem(expected_calibre_filename),
            }
            records.append(rec)

            if orig_sha:
                by_orig_sha[orig_sha] = rec
            if new_sha:
                by_new_sha[new_sha] = rec
            for raw_i in (isbn, orig_isbn):
                ci = re.sub(r'[^0-9Xx]', '', raw_i).upper()
                if ci and ci not in by_isbn:
                    by_isbn[ci] = rec
            if file_name:
                by_filename[file_name.lower()] = rec
                by_filename[_moon_sanitize_stem(file_name).lower()] = rec
            for t_cand in (orig_title, title, os.path.splitext(file_name)[0]):
                nt = _norm_lookup_str(t_cand)
                if nt and len(nt) >= 4 and nt not in by_norm_title:
                    by_norm_title[nt] = rec

        return records, by_orig_sha, by_new_sha, by_isbn, by_filename, by_norm_title

    def get(self, request):
        path_prefix = (request.GET.get('path_prefix') or 'U:\\Media').strip()
        records, *_ = self._load_enriched_index(path_prefix=path_prefix)
        try:
            limit = min(max(int(request.GET.get('limit', 500)), 1), 10000)
        except ValueError:
            limit = 500
        return Response({
            'ok': True,
            'total_enriched': len(records),
            'items': records[:limit],
        })

    def post(self, request):
        body = request.data or {}
        path_prefix = (body.get('path_prefix') or 'U:\\Media').strip()
        books = body.get('books') or []
        moon_filenames = body.get('moon_filenames') or []

        records, by_orig_sha, by_new_sha, by_isbn, by_filename, by_norm_title = self._load_enriched_index(
            path_prefix=path_prefix
        )

        matched_books = []
        for b in books:
            cal_id = b.get('calibre_id')
            sha = (b.get('sha256') or '').strip().lower()
            raw_isbn = re.sub(r'[^0-9Xx]', '', str(b.get('isbn') or '')).upper()
            b_title = (b.get('title') or '').strip()
            b_authors = (b.get('authors') or '').strip()
            b_fname = (b.get('file_name') or '').strip()

            match_rec = None
            match_reason = ''
            already_same_binary = False

            if sha and sha in by_new_sha:
                match_rec = by_new_sha[sha]
                match_reason = 'exact_enriched_sha256'
                already_same_binary = True
            elif sha and sha in by_orig_sha:
                match_rec = by_orig_sha[sha]
                match_reason = 'orig_sha256'
            elif raw_isbn and raw_isbn in by_isbn:
                match_rec = by_isbn[raw_isbn]
                match_reason = 'isbn'
            elif b_fname and b_fname.lower() in by_filename:
                match_rec = by_filename[b_fname.lower()]
                match_reason = 'file_name'
            else:
                nt = _norm_lookup_str(b_title)
                if nt and nt in by_norm_title:
                    cand = by_norm_title[nt]
                    na_cal = _norm_lookup_str(b_authors)
                    na_enr = _norm_lookup_str(cand.get('authors') or cand.get('orig_author'))
                    if not na_cal or not na_enr or (na_cal in na_enr or na_enr in na_cal):
                        match_rec = cand
                        match_reason = 'title_author'

            if match_rec:
                if sha and match_rec.get('file_sha256') == sha:
                    already_same_binary = True
                matched_books.append({
                    'calibre_id': cal_id,
                    'match_reason': match_reason,
                    'already_same_binary': already_same_binary,
                    'calibre_title': b_title,
                    'calibre_authors': b_authors,
                    'calibre_isbn': b.get('isbn') or '',
                    'calibre_file_name': b_fname,
                    'enriched': match_rec,
                })

        matched_moon = []
        for mf in moon_filenames:
            mf_str = str(mf or '').strip()
            if not mf_str:
                continue
            mf_lower = mf_str.lower()
            if not mf_lower.endswith('.epub'):
                continue
            san_lower = _moon_sanitize_stem(mf_str).lower()
            stem_str = os.path.splitext(mf_str)[0]
            if ' - ' in stem_str:
                title_part, author_part = stem_str.rsplit(' - ', 1)
            else:
                title_part, author_part = stem_str, ''
            nt = _norm_lookup_str(title_part)
            na_moon = _norm_lookup_str(author_part)

            rec = by_filename.get(mf_lower) or by_filename.get(san_lower)
            if not rec and nt and len(nt) >= 4:
                cand = by_norm_title.get(nt)
                if cand:
                    # Verify that the enriched title still shares primary title tokens with the book
                    enr_title_norm = _norm_lookup_str(cand.get('title', ''))
                    moon_title_tokens = {t for t in nt.split() if len(t) >= 3}
                    enr_title_tokens = {t for t in enr_title_norm.split() if len(t) >= 3}
                    title_overlap = bool(
                        enr_title_norm.startswith(nt)
                        or nt.startswith(enr_title_norm)
                        or (moon_title_tokens and len(moon_title_tokens & enr_title_tokens) >= max(1, len(moon_title_tokens) // 2))
                    )
                    if title_overlap:
                        # Verify author token overlap unless Moon+ author was 'Unknown' or empty
                        if not na_moon or na_moon == 'unknown':
                            rec = cand
                        else:
                            cand_auth_tokens = set(_norm_lookup_str(cand.get('authors', '')).split())
                            moon_auth_tokens = {t for t in na_moon.split() if len(t) >= 2}
                            if moon_auth_tokens and (moon_auth_tokens & cand_auth_tokens):
                                rec = cand

            if rec:
                matched_moon.append({
                    'moon_book_filename': mf_str,
                    'match_found': True,
                    'enriched': rec,
                })

        return Response({
            'ok': True,
            'total_enriched_in_db': len(records),
            'books_checked': len(books),
            'books_matched': len(matched_books),
            'matched_books': matched_books,
            'moon_checked': len(moon_filenames),
            'moon_matched': len(matched_moon),
            'matched_moon': matched_moon,
        })


class OnDemandEnrichAPIView(APIView):
    """
    On-Demand Metadata Enrichment API (`POST /mylibrary/api/enrich/`).
    Accepts `{"item_id": "<uuid>"}` or `{"file_id": "<uuid>"}` (and optional `"force": true`),
    invokes `epubmeta` on the physical `.epub` / `.pdf` file on the NAS, updates the embedded OPF/XMP
    metadata on disk, updates `scanner_state.db`, and updates `CatalogItem` & `FileLocation` in BookDB.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from .opds import enrich_catalog_item_on_demand, enrich_file_location_on_demand

        body = request.data or {}
        item_id = str(body.get('item_id') or request.GET.get('item_id') or '').strip()
        file_id = str(body.get('file_id') or request.GET.get('file_id') or '').strip()
        file_path = (body.get('file_path') or request.GET.get('file_path') or '').strip()
        force = str(body.get('force', request.GET.get('force', 'false'))).lower() in ('1', 'true', 'yes', 'force')
        re_enrich = str(body.get('re_enrich', request.GET.get('re_enrich', 'false'))).lower() in ('1', 'true', 'yes')
        hint_title = (body.get('hint_title') or request.GET.get('hint_title') or '').strip()
        hint_authors = (body.get('hint_authors') or request.GET.get('hint_authors') or '').strip()
        hint_isbn = (body.get('hint_isbn') or request.GET.get('hint_isbn') or '').strip()
        force_ai_vision = str(body.get('force_ai_vision', request.GET.get('force_ai_vision', 'false'))).lower() in ('1', 'true', 'yes')
        review_only = str(body.get('review_only', request.GET.get('review_only', 'false'))).lower() in ('1', 'true', 'yes')
        ai_describe_no_match = str(body.get('ai_describe_no_match', request.GET.get('ai_describe_no_match', 'false'))).lower() in ('1', 'true', 'yes')
        raw_ai_imgs = body.get('ai_image_data_uris') or body.get('ai_image_data_uri') or []
        if isinstance(raw_ai_imgs, str):
            ai_image_data_uris = [raw_ai_imgs.strip()] if raw_ai_imgs.strip() else []
        elif isinstance(raw_ai_imgs, list):
            ai_image_data_uris = [str(x).strip() for x in raw_ai_imgs if str(x or '').strip()]
        else:
            ai_image_data_uris = []

        if file_id:
            loc = FileLocation.objects.select_related('catalog_item').filter(id=file_id).first()
            if not loc:
                return Response({'ok': False, 'error': f'FileLocation not found: {file_id}'}, status=404)
            res = enrich_file_location_on_demand(
                loc,
                force=force,
                re_enrich=re_enrich,
                hint_title=hint_title,
                hint_authors=hint_authors,
                hint_isbn=hint_isbn,
                ai_image_data_uris=ai_image_data_uris,
                force_ai_vision=force_ai_vision,
                review_only=review_only,
                ai_describe_no_match=ai_describe_no_match,
            )
            return Response(res, status=200 if res.get('ok') else 422)

        if file_path and not item_id:
            path_cands = [file_path]
            if file_path.startswith('/books/'):
                path_cands.append('/volume1/common/Media/Books/' + file_path[len('/books/'):])
            elif file_path.startswith('/volume1/common/Media/Books/'):
                path_cands.append('/books/' + file_path[len('/volume1/common/Media/Books/'):])
            loc = FileLocation.objects.select_related('catalog_item').filter(file_path__in=path_cands).first()
            if loc:
                res = enrich_file_location_on_demand(
                    loc,
                    force=force,
                    re_enrich=re_enrich,
                    hint_title=hint_title,
                    hint_authors=hint_authors,
                    hint_isbn=hint_isbn,
                    ai_image_data_uris=ai_image_data_uris,
                    force_ai_vision=force_ai_vision,
                    review_only=review_only,
                    ai_describe_no_match=ai_describe_no_match,
                )
                return Response(res, status=200 if res.get('ok') else 422)

        if item_id:
            from .opds import resolve_catalog_item_by_id_or_fallback
            item = resolve_catalog_item_by_id_or_fallback(
                item_id,
                file_id=(body.get('fallback_file_id') or '').strip(),
                file_path=file_path,
                isbn=hint_isbn or (body.get('isbn') or '').strip(),
                title=(body.get('title') or '').strip() or hint_title,
                authors=(body.get('authors') or '').strip() or hint_authors,
            )
            if not item:
                return Response({'ok': False, 'error': f'CatalogItem not found: {item_id}'}, status=404)
            res = enrich_catalog_item_on_demand(
                item,
                force=force,
                re_enrich=re_enrich,
                hint_title=hint_title,
                hint_authors=hint_authors,
                hint_isbn=hint_isbn,
                ai_image_data_uris=ai_image_data_uris,
                force_ai_vision=force_ai_vision,
                review_only=review_only,
                ai_describe_no_match=ai_describe_no_match,
            )
            res['requested_item_id'] = item_id
            return Response(res, status=200 if res.get('ok') else 422)

        return Response({'ok': False, 'error': 'Please provide item_id or file_id.'}, status=400)


class RevertEnrichAPIView(APIView):
    """
    Revert an enriched or in-review `CatalogItem` / `FileLocation` back to its original
    pre-enrichment metadata (`POST /mylibrary/api/revert-enrich/`).
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from .opds import resolve_catalog_item_by_id_or_fallback, revert_catalog_item_enrichment

        body = request.data or {}
        item_id = str(body.get('item_id') or request.GET.get('item_id') or '').strip()
        file_id = str(body.get('file_id') or request.GET.get('file_id') or '').strip()
        file_path = (body.get('file_path') or request.GET.get('file_path') or '').strip()

        loc = None
        item = None
        if file_id:
            loc = FileLocation.objects.select_related('catalog_item').filter(id=file_id).first()
            if loc:
                item = loc.catalog_item
        if not item and item_id:
            item = resolve_catalog_item_by_id_or_fallback(item_id, file_id=file_id, file_path=file_path)

        if not item and not loc and not file_path:
            return Response({'ok': False, 'error': 'Please provide item_id, file_id, or file_path.'}, status=400)

        res = revert_catalog_item_enrichment(item=item, file_loc=loc, file_path=file_path)
        return Response(res, status=200 if res.get('ok') else 422)


class MoveToMagAPIView(APIView):
    """
    Move a Magazine / Periodical `CatalogItem` or `FileLocation` from `/books` to `/toMags`
    (`POST /mylibrary/api/move-to-mag/`), cleaning up `scanner_state.db` and `BookDB`.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from .opds import (
            move_catalog_item_to_tomags,
            move_file_location_to_tomags,
            resolve_catalog_item_by_id_or_fallback,
        )

        body = request.data or {}
        item_id = str(body.get('item_id') or request.GET.get('item_id') or '').strip()
        file_id = str(body.get('file_id') or request.GET.get('file_id') or '').strip()
        target_bucket = str(body.get('target_bucket') or request.GET.get('target_bucket') or '').strip()
        vx_unlocked = bool(request.session.get('vx_unlocked', False))
        force_adult = vx_unlocked and target_bucket.lower() in ('archive-x', 'xadult', 'xtoadult')
        force_standard = target_bucket.lower() in ('periodicals', 'magazine', 'magazines')

        if file_id:
            loc = FileLocation.objects.select_related('catalog_item').filter(id=file_id).first()
            if not loc:
                return Response({'ok': False, 'error': f'FileLocation not found: {file_id}'}, status=404)
            res = move_file_location_to_tomags(loc, force_adult=force_adult, force_standard=force_standard)
            return Response(res, status=200 if res.get('ok') else 422)

        if item_id:
            item = resolve_catalog_item_by_id_or_fallback(item_id)
            if not item:
                return Response({'ok': False, 'error': f'CatalogItem not found: {item_id}'}, status=404)
            res = move_catalog_item_to_tomags(
                item,
                only_detected_mags=False,
                force_adult=force_adult,
                force_standard=force_standard,
            )
            return Response(res, status=200 if res.get('ok') else 422)

        return Response({'ok': False, 'error': 'Please provide item_id or file_id.'}, status=400)


class MoveToBookAPIView(APIView):
    """
    Move a Magazine / Periodical `CatalogItem` or `FileLocation` from `/periodicals` (or `/toMags`)
    to `/toBooks` -> `/books/<Category>/<Shard>/` (`POST /mylibrary/api/move-to-book/`),
    converting `item_type` to `BOOK` and triggering background `epubmeta` Book Enrichment.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from .opds import (
            move_catalog_item_to_tobooks,
            move_file_location_to_tobooks,
            resolve_catalog_item_by_id_or_fallback,
        )

        body = request.data or {}
        item_id = str(body.get('item_id') or request.GET.get('item_id') or '').strip()
        file_id = str(body.get('file_id') or request.GET.get('file_id') or '').strip()
        target_folder = (body.get('target_folder') or request.GET.get('target_folder') or '').strip() or None

        if file_id:
            loc = FileLocation.objects.select_related('catalog_item').filter(id=file_id).first()
            if not loc:
                return Response({'ok': False, 'error': f'FileLocation not found: {file_id}'}, status=404)
            res = move_file_location_to_tobooks(loc, explicit_folder=target_folder, enrich_in_background=True)
            return Response(res, status=200 if res.get('ok') else 422)

        if item_id:
            item = resolve_catalog_item_by_id_or_fallback(item_id)
            if not item:
                return Response({'ok': False, 'error': f'CatalogItem not found: {item_id}'}, status=404)
            res = move_catalog_item_to_tobooks(item, explicit_folder=target_folder, enrich_in_background=True)
            return Response(res, status=200 if res.get('ok') else 422)

        return Response({'ok': False, 'error': 'Please provide item_id or file_id.'}, status=400)


class UndoMoveToMagAPIView(APIView):
    """
    Restore a `CatalogItem` or `FileLocation` from `/toMags` back to `/books/...`
    (`POST /mylibrary/api/undo-to-mag/`), optionally running `epubmeta` enrichment as a Book.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from .opds import undo_move_from_tomags

        body = request.data or {}
        item_id = str(body.get('item_id') or request.GET.get('item_id') or '').strip()
        file_id = str(body.get('file_id') or request.GET.get('file_id') or '').strip()
        moved_files = body.get('moved_files')
        if isinstance(moved_files, str):
            moved_files = [x.strip() for x in moved_files.split(',') if x.strip()]
        elif not isinstance(moved_files, list):
            moved_files = None
        enrich_after = str(body.get('enrich') or request.GET.get('enrich') or '').strip().lower() in ('1', 'true', 'yes')

        res = undo_move_from_tomags(
            item_id=item_id,
            file_id=file_id,
            moved_files=moved_files,
            enrich_after=enrich_after,
        )
        return Response(res, status=200 if (res.get('ok') or res.get('restored_from_tomags')) else 422)


class RetagFolderAPIView(APIView):
    """
    Single & Bulk Retag / Move Folder API (`POST /mylibrary/api/retag-folder/`).
    Physically moves one or more items/files into an existing or newly created top-level folder
    under `/books/<target_folder>`, `/periodicals/<Series>/<Year>/` (`target_folder="Periodicals"`),
    or `/xAdult/<Series>/<Year>/` (`target_folder="Archive-X"`, when session is unlocked),
    and updates `FileLocation`, `processed_files`, and `file_states` atomically.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from .opds import retag_catalog_items_to_folder

        body = request.data or {}
        target_folder = (body.get('target_folder') or request.GET.get('target_folder') or '').strip()
        raw_item_ids = body.get('item_ids') or body.get('item_id') or []
        raw_file_ids = body.get('file_ids') or body.get('file_id') or []

        if isinstance(raw_item_ids, str):
            item_ids = [x.strip() for x in raw_item_ids.split(',') if x.strip()]
        elif isinstance(raw_item_ids, list):
            item_ids = [str(x).strip() for x in raw_item_ids if str(x or '').strip()]
        else:
            item_ids = []

        if isinstance(raw_file_ids, str):
            file_ids = [x.strip() for x in raw_file_ids.split(',') if x.strip()]
        elif isinstance(raw_file_ids, list):
            file_ids = [str(x).strip() for x in raw_file_ids if str(x or '').strip()]
        else:
            file_ids = []

        if not target_folder:
            return Response({'ok': False, 'error': 'target_folder is required.'}, status=400)
        if not item_ids and not file_ids:
            return Response({'ok': False, 'error': 'Provide item_ids or file_ids to move.'}, status=400)

        vx_unlocked = bool(request.session.get('vx_unlocked', False))
        series_name = (body.get('series_name') or request.GET.get('series_name') or '').strip()
        res = retag_catalog_items_to_folder(
            item_ids=item_ids,
            file_ids=file_ids,
            target_folder=target_folder,
            vx_unlocked=vx_unlocked,
            series_name=series_name,
        )
        return Response(res, status=200 if res.get('ok') else 422)


class VxToggleAPIView(APIView):
    """
    Disguised session state toggle endpoint (`POST /mylibrary/api/vx-toggle/`).
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        body = request.data or {}
        mode = (body.get('mode') or '').strip().lower()
        cur = bool(request.session.get('vx_unlocked', False))
        if mode in ('1', 'true', 'on', 'x'):
            new_state = True
        elif mode in ('0', 'false', 'off'):
            new_state = False
        else:
            new_state = not cur
        request.session['vx_unlocked'] = new_state
        if new_state:
            request.session.set_expiry(0)
        return Response({'ok': True, 'v': 'x' if new_state else '0'})


class ToggleInterestingAPIView(APIView):
    """
    Toggle or set the 'Interesting' flag for a `CatalogItem` (`POST /mylibrary/api/interesting/`).
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from .opds import set_book_interesting

        body = request.data or {}
        item_id = (body.get('item_id') or request.GET.get('item_id') or '').strip()
        raw_state = body.get('is_interesting')
        is_interesting = None
        if raw_state is not None:
            is_interesting = str(raw_state).lower() in ('1', 'true', 'yes')

        res = set_book_interesting(item_id, is_interesting)
        return Response(res, status=200 if res.get('ok') else 400)


class ToggleBadMetadataAPIView(APIView):
    """
    Toggle or set the 'Bad Metadata' flag for a `CatalogItem` (`POST /mylibrary/api/bad-metadata/`).
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from .opds import set_book_bad_metadata

        body = request.data or {}
        item_id = (body.get('item_id') or request.GET.get('item_id') or '').strip()
        reason = (body.get('reason') or request.GET.get('reason') or '').strip()
        raw_state = body.get('is_bad_metadata')
        is_bad = None
        if raw_state is not None:
            is_bad = str(raw_state).lower() in ('1', 'true', 'yes')

        res = set_book_bad_metadata(item_id, is_bad=is_bad, reason=reason)
        return Response(res, status=200 if res.get('ok') else 400)


class ToggleNeedsCoverAPIView(APIView):
    """
    Flag or dismiss a `CatalogItem` on the 'Missing / Text Cover' shelf (`POST /mylibrary/api/needs-cover/`).
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from .opds import set_book_needs_cover

        body = request.data or {}
        item_id = (body.get('item_id') or request.GET.get('item_id') or '').strip()
        cover_issue = (body.get('cover_issue') or request.GET.get('cover_issue') or 'text_only').strip()
        reason = (body.get('reason') or request.GET.get('reason') or '').strip()
        raw_state = body.get('needs_cover')
        needs_cover = None
        if raw_state is not None:
            needs_cover = str(raw_state).lower() in ('1', 'true', 'yes')

        res = set_book_needs_cover(item_id, needs_cover=needs_cover, cover_issue=cover_issue, reason=reason)
        return Response(res, status=200 if res.get('ok') else 400)


class CoverSearchAPIView(APIView):
    """
    Web Cover Search API (`POST /mylibrary/api/cover-search/`), modeled after Moon+ Reader's
    'Download cover from internet' feature. Queries Apple Books, Google Books, and Open Library
    in parallel and returns candidate cover thumbnails (with the book's Original Cover first unless missing).
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return self.post(request)

    def post(self, request):
        from .opds import search_book_covers_web

        body = request.data if hasattr(request, 'data') and isinstance(request.data, dict) else {}
        query = (body.get('query') or request.GET.get('query') or request.GET.get('q') or '').strip()
        title = (body.get('title') or request.GET.get('title') or '').strip()
        authors = (body.get('authors') or request.GET.get('authors') or '').strip()
        isbn = (body.get('isbn') or request.GET.get('isbn') or '').strip()
        item_id = (body.get('item_id') or request.GET.get('item_id') or '').strip()

        covers = search_book_covers_web(query=query, title=title, authors=authors, isbn=isbn, item_id=item_id)
        return Response({'ok': True, 'count': len(covers), 'covers': covers})


class CoverApplyAPIView(APIView):
    """
    Apply a chosen web cover URL to a `CatalogItem` (`POST /mylibrary/api/cover-apply/`).
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from .opds import apply_book_cover_to_item

        body = request.data or {}
        item_id = (body.get('item_id') or '').strip()
        cover_url = (body.get('cover_url') or '').strip()
        fallback_url = (body.get('fallback_url') or '').strip()
        file_id = (body.get('file_id') or '').strip()
        file_path = (body.get('file_path') or '').strip()
        isbn = (body.get('isbn') or '').strip()
        title = (body.get('title') or '').strip()
        authors = (body.get('authors') or '').strip()
        query = (body.get('query') or '').strip()
        selected_candidate = body.get('selected_candidate') if isinstance(body.get('selected_candidate'), dict) else None
        all_candidates = body.get('all_candidates') if isinstance(body.get('all_candidates'), list) else None
        if not (item_id or file_id or file_path or isbn or title or query) or not cover_url:
            return Response({'ok': False, 'error': 'item_id and cover_url are required.'}, status=400)

        res = apply_book_cover_to_item(
            item_id,
            cover_url,
            fallback_url=fallback_url,
            file_id=file_id,
            file_path=file_path,
            isbn=isbn,
            title=title,
            authors=authors,
            query=query,
            selected_candidate=selected_candidate,
            all_candidates=all_candidates,
        )
        return Response(res, status=200 if res.get('ok') else 422)


class CoverTelemetryAPIView(APIView):
    """
    Cover Selection Telemetry API (`GET /mylibrary/api/cover-telemetry/`):
    Returns captured cover selection records (manual & automatic) with before/after image metrics,
    title/author similarity scores, candidate lists, and optional base64 thumbnails for algorithm analysis.
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        from .opds import get_cover_selection_telemetry
        try:
            limit = int(request.query_params.get('limit') or 100)
        except Exception:
            limit = 100
        include_thumbnails = str(request.query_params.get('thumbnails') or '0').lower() in ('1', 'true', 'yes')
        rows = get_cover_selection_telemetry(limit=limit, include_thumbnails=include_thumbnails)
        return Response({
            'ok': True,
            'count': len(rows),
            'records': rows,
        })


class CoverGenerateAPIView(APIView):
    """
    On-demand Book Cover Generator & Original Cover Revert API (`POST /mylibrary/api/cover-generate/`):
      - `action="generate"` (default): Generates a new custom cover based on the book's description,
        content, tags, and title, saves it to `catalog_generated_covers` in BookDB, and applies it.
      - `action="revert_original"`: Reverts the book's active cover back to its original cover
        while keeping generated covers saved in BookDB.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from .opds import generate_book_cover_for_item, revert_book_cover_to_original

        body = request.data or {}
        action = (body.get('action') or 'generate').strip().lower()
        item_id = (body.get('item_id') or '').strip()
        file_id = (body.get('file_id') or '').strip()
        file_path = (body.get('file_path') or '').strip()
        isbn = (body.get('isbn') or '').strip()
        title = (body.get('title') or '').strip()
        authors = (body.get('authors') or '').strip()
        publisher = (body.get('publisher') or '').strip()
        description = (body.get('description') or '').strip()
        style_hint = (body.get('style_hint') or '').strip()
        query = (body.get('query') or '').strip()
        apply_immediately = str(body.get('apply_immediately', 'true')).lower() not in ('0', 'false', 'no')

        if action in ('revert', 'revert_original', 'original'):
            res = revert_book_cover_to_original(
                item_id,
                file_id=file_id,
                file_path=file_path,
                isbn=isbn,
                title=title,
                authors=authors,
                query=query,
            )
            return Response(res, status=200 if res.get('ok') else 422)

        res = generate_book_cover_for_item(
            item_id,
            file_id=file_id,
            file_path=file_path,
            isbn=isbn,
            title=title,
            authors=authors,
            publisher=publisher,
            description=description,
            style_hint=style_hint,
            query=query,
            apply_immediately=apply_immediately,
        )
        return Response(res, status=200 if res.get('ok') else 422)


class AiDescriptionGenerateAPIView(APIView):
    """
    On-demand AI Content-Based Description & Metadata Generator API (`POST /mylibrary/api/ai-description/`):
    Generates a 2-4 sentence synopsis grounded in the book's actual content (front matter,
    table of contents, preface, opening chapters, and/or rendered page images) and/or extracts
    clean Title & Author(s) (`mode='description' | 'title_author' | 'all'`), saving directly to BookDB.
    """
    authentication_classes = []
    permission_classes = []
    default_mode = 'description'

    def post(self, request):
        from .opds import generate_ai_description_from_content

        body = request.data or {}
        item_id = str(body.get('item_id') or '').strip()
        file_id = str(body.get('file_id') or '').strip()
        file_path = str(body.get('file_path') or '').strip()
        isbn = str(body.get('isbn') or '').strip()
        title = str(body.get('title') or '').strip()
        raw_authors = body.get('authors')
        if isinstance(raw_authors, list):
            authors = ', '.join(str(a).strip() for a in raw_authors if str(a or '').strip())
        else:
            authors = str(raw_authors or '').strip()
        publisher = str(body.get('publisher') or '').strip()
        apply_immediately = str(body.get('apply_immediately', 'true')).lower() not in ('0', 'false', 'no')
        mode = str(body.get('mode') or self.default_mode or 'description').strip().lower()

        if not (item_id or file_id or file_path or isbn or title):
            return Response({'ok': False, 'error': 'Please provide item_id, file_id, file_path, isbn, or title.'}, status=400)

        res = generate_ai_description_from_content(
            item_or_id=item_id,
            file_id=file_id,
            file_path=file_path,
            isbn=isbn,
            title=title,
            authors=authors,
            publisher=publisher,
            apply_immediately=apply_immediately,
            mode=mode,
        )
        return Response(res, status=200 if res.get('ok') else 422)


class AiTitleAuthorGenerateAPIView(AiDescriptionGenerateAPIView):
    """
    On-demand AI Title & Author Generator API (`POST /mylibrary/api/ai-title-author/`):
    Inspects cover/title/copyright page images, front-matter text, and filename clues via Gemini AI
    to determine and save the clean human-readable Title, Author(s), and Publisher.
    """
    default_mode = 'title_author'


class BookPreviewAPIView(APIView):
    """
    Inline Book Preview API (`GET /mylibrary/api/book-preview/`).
    Extracts cover/front-matter page images, readable front-matter text sections,
    and detected ISBNs from a physical `.epub` or `.pdf` file on the NAS.
    """

    def get(self, request):
        from .opds import extract_book_preview_data

        file_path = (request.GET.get('file_path') or '').strip()
        file_id = (request.GET.get('file_id') or '').strip()
        item_id = (request.GET.get('item_id') or '').strip()
        res = extract_book_preview_data(file_path=file_path, file_id=file_id, item_id=item_id)
        return Response(res, status=200 if res.get('ok') else 404)


class BookRawStreamView(View):
    """
    Stream a physical `.pdf` file inline (`GET /mylibrary/api/book-preview/raw/`)
    or render an in-browser HTML reader view for `.epub`, `.mobi`, `.azw3`, `.fb2`, `.cbz`, `.txt`, etc.
    Pass `&download=1` to download the raw binary file directly.
    """

    def get(self, request):
        import html as _html
        import urllib.parse as _up
        from django.http import FileResponse, Http404, HttpResponse
        from .opds import _resolve_physical_file_path, extract_book_preview_data

        file_path = (request.GET.get('file_path') or '').strip()
        phys = _resolve_physical_file_path(file_path) if file_path else None
        if not phys or not phys.exists():
            raise Http404('Physical book file not found on NAS.')
        ext = phys.suffix.lower()
        force_download = request.GET.get('download') == '1'

        if ext == '.pdf' or force_download:
            ctype = 'application/pdf' if ext == '.pdf' else ('application/epub+zip' if ext == '.epub' else 'application/octet-stream')
            disp = 'attachment' if force_download else 'inline'
            resp = FileResponse(open(phys, 'rb'), content_type=ctype)
            resp['Content-Disposition'] = f'{disp}; filename="{phys.name}"'
            return resp

        preview = extract_book_preview_data(file_path=str(phys))
        dl_url = f"{request.path}?{_up.urlencode({'file_path': str(phys), 'download': '1'})}"
        imgs_html = ''.join(
            f'<div style="background:#1e293b;padding:8px;border-radius:8px;border:1px solid #334155;text-align:center;display:flex;flex-direction:column;gap:6px;">'
            f'<img class="prev-page-img" src="{img["data_uri"]}" style="max-height:320px;max-width:240px;border-radius:4px;display:block;margin:0 auto;">'
            f'<div style="font-size:12px;color:#94a3b8;">{_html.escape(img.get("label", ""))}</div>'
            f'<button type="button" onclick="aiReeval({i}, this)" style="background:rgba(168,85,247,0.22);border:1px solid rgba(168,85,247,0.55);color:#e9d5ff;border-radius:5px;padding:5px 8px;font-size:12px;font-weight:700;cursor:pointer;">✨ AI Re-Evaluate from This Image</button>'
            f'</div>'
            for i, img in enumerate(preview.get('images') or [])
        )
        secs_html = ''.join(
            f'<div style="background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:18px 22px;margin-bottom:16px;">'
            f'<div style="font-size:13px;font-weight:700;color:#38bdf8;margin-bottom:10px;text-transform:uppercase;">{_html.escape(p.get("label", ""))}</div>'
            f'<div style="white-space:pre-wrap;line-height:1.68;font-size:15px;color:#e2e8f0;font-family:Georgia,serif;">{_html.escape(p.get("text", ""))}</div></div>'
            for p in (preview.get('pages') or [])
        )
        if not secs_html:
            secs_html = f'<div style="color:#94a3b8;padding:20px;">{_html.escape(preview.get("error") or "No text sections extracted.")}</div>'

        page_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{_html.escape(phys.name)} — Book Preview</title></head>
<body style="margin:0;background:#020617;color:#f8fafc;font-family:system-ui,-apple-system,sans-serif;">
  <div style="position:sticky;top:0;background:rgba(15,23,42,0.95);border-bottom:1px solid #1e293b;padding:12px 24px;display:flex;justify-content:space-between;align-items:center;backdrop-filter:blur(8px);gap:12px;flex-wrap:wrap;">
    <div>
      <strong style="font-size:16px;color:#f8fafc;">{_html.escape(phys.name)}</strong>
      <span style="margin-left:10px;font-size:12px;color:#94a3b8;font-family:monospace;">{_html.escape(str(phys))} ({preview.get('file_size_mb', 0)} MB)</span>
    </div>
    <div style="display:flex;gap:10px;align-items:center;">
      <button type="button" onclick="aiReeval(null, this)" style="background:rgba(168,85,247,0.25);border:1px solid rgba(168,85,247,0.6);color:#e9d5ff;padding:7px 14px;border-radius:7px;font-size:13px;font-weight:700;cursor:pointer;">✨ AI Re-Evaluate from Image(s)</button>
      <a href="{_html.escape(dl_url)}" style="background:#3b82f6;color:#fff;text-decoration:none;padding:7px 14px;border-radius:7px;font-size:13px;font-weight:600;">⬇ Download Raw {_html.escape(ext.upper())} File</a>
    </div>
  </div>
  <div id="ai-status-banner" style="display:none;max-width:960px;margin:16px auto 0;padding:12px 18px;border-radius:8px;background:rgba(168,85,247,0.16);border:1px solid rgba(168,85,247,0.45);color:#f8fafc;font-size:14px;"></div>
  <div style="max-width:960px;margin:24px auto;padding:0 20px;">
    {f'<div style="display:flex;gap:14px;overflow-x:auto;padding-bottom:16px;margin-bottom:20px;">{imgs_html}</div>' if imgs_html else ''}
    {secs_html}
  </div>
  <script>
    async function aiReeval(idx, btn) {{
      const banner = document.getElementById('ai-status-banner');
      const imgs = Array.from(document.querySelectorAll('.prev-page-img')).map(el => el.src);
      const uris = (idx !== null && idx !== undefined && imgs[idx]) ? [imgs[idx]] : imgs.slice(0, 3);
      const orig = btn.textContent;
      btn.disabled = true;
      btn.textContent = '⏳ AI Analyzing...';
      banner.style.display = 'block';
      banner.textContent = 'Gemini AI Vision is inspecting the book image(s) and re-evaluating metadata...';
      try {{
        const r = await fetch('/mylibrary/api/enrich/', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            file_path: {json.dumps(str(phys))},
            re_enrich: true,
            force_ai_vision: true,
            ai_image_data_uris: uris
          }})
        }});
        const res = await r.json();
        banner.textContent = (res.message || res.error || ('Status: ' + (res.status || 'Done'))) + (res.candidate_title ? (' | Candidate: ' + res.candidate_title + ' by ' + (res.candidate_authors || '')) : '');
      }} catch (e) {{
        banner.textContent = 'Error: ' + e.message;
      }} finally {{
        btn.disabled = false;
        btn.textContent = orig;
      }}
    }}
  </script>
</body></html>"""
        return HttpResponse(page_html, content_type='text/html; charset=utf-8')


class WebLoginView(View):
    """
    Browser login page (`/mylibrary/login/`) for external access security.
    """

    def get(self, request):
        next_url = request.GET.get('next') or '/mylibrary/'
        return render(request, 'catalog/login.html', {'next_url': next_url})

    def post(self, request):
        from .security import (
            _clear_failed_attempts,
            _is_ip_locked_out,
            _record_failed_attempt,
            get_client_ip,
            verify_credentials,
        )
        from django.http import HttpResponseRedirect

        client_ip = get_client_ip(request)
        next_url = request.POST.get('next') or '/mylibrary/'
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''

        if _is_ip_locked_out(client_ip):
            return render(
                request,
                'catalog/login.html',
                {
                    'next_url': next_url,
                    'username': username,
                    'error_message': 'Too many failed login attempts. Please wait 15 minutes.',
                },
                status=429,
            )

        if verify_credentials(request, username, password):
            _clear_failed_attempts(client_ip)
            request.session['mylibrary_authenticated'] = True
            request.session['mylibrary_user'] = username
            return HttpResponseRedirect(next_url)

        _record_failed_attempt(client_ip)
        return render(
            request,
            'catalog/login.html',
            {
                'next_url': next_url,
                'username': username,
                'error_message': 'Invalid username or password.',
            },
            status=401,
        )


class WebLogoutView(View):
    """
    Browser logout endpoint (`/mylibrary/logout/`).
    """

    def get(self, request):
        from django.contrib.auth import logout
        from django.http import HttpResponseRedirect

        request.session.flush()
        logout(request)
        script_prefix = getattr(settings, 'FORCE_SCRIPT_NAME', '') or ''
        return HttpResponseRedirect(f"{script_prefix.rstrip('/')}/login/")


class MalformedQueryRedirectView(View):
    """
    Redirects URLs where query parameters were appended to `/mylibrary/` without a `?`
    (e.g. `/mylibrary/enrich_status=needs_enriching` -> `/mylibrary/?enrich_status=needs_enriching`).
    """

    def get(self, request, malformed_query=''):
        from django.http import HttpResponseRedirect
        from urllib.parse import unquote

        script_prefix = getattr(settings, 'FORCE_SCRIPT_NAME', '') or ''
        base_path = f"{script_prefix.rstrip('/')}/" if script_prefix else '/'
        clean_q = unquote(malformed_query or '').lstrip('?&')
        extra_qs = request.META.get('QUERY_STRING', '').strip()
        if extra_qs:
            clean_q = f"{clean_q}&{extra_qs}" if clean_q else extra_qs
        target = f"{base_path}?{clean_q}" if clean_q else base_path
        return HttpResponseRedirect(target)


class DuplicatesDashboardView(TemplateView):
    """
    Dedicated Duplicates Management screen (`/mylibrary/duplicates/`) for working enriched
    duplicate book files that share the exact same format and book content.
    """
    template_name = 'catalog/duplicates.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from .opds import get_enriched_duplicate_groups

        filter_mode = (self.request.GET.get('mode') or 'all').strip().lower()
        format_filter = (self.request.GET.get('format') or '').strip().upper()
        search_q = (self.request.GET.get('q') or '').strip()
        exclude_bm = self.request.GET.get('exclude_bm', '1') != '0'
        all_enr = self.request.GET.get('all_enr', '0') == '1'
        force_refresh = self.request.GET.get('refresh', '0') == '1'
        try:
            page = max(1, int(self.request.GET.get('page', 1)))
        except ValueError:
            page = 1

        dup_data = get_enriched_duplicate_groups(
            filter_mode=filter_mode,
            format_filter=format_filter,
            search_q=search_q,
            exclude_unfinished_bookmarks=exclude_bm,
            require_all_files_enriched=all_enr,
            page=page,
            page_size=25,
            force_refresh=force_refresh,
        )
        context['dup_data'] = dup_data
        context['summary'] = dup_data.get('summary') or {}
        context['groups'] = dup_data.get('groups') or []
        return context


class DuplicatesAPIView(APIView):
    """
    JSON API endpoint (`/mylibrary/api/duplicates/`) to query enriched duplicate groups
    and resolve single or batch duplicate groups safely (moving redundant copies to `/books_bad/duplicates/`).
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        from .opds import get_enriched_duplicate_groups

        filter_mode = (request.GET.get('mode') or 'all').strip().lower()
        format_filter = (request.GET.get('format') or '').strip().upper()
        search_q = (request.GET.get('q') or '').strip()
        exclude_bm = request.GET.get('exclude_bm', '1') != '0'
        all_enr = request.GET.get('all_enr', '0') == '1'
        force_refresh = request.GET.get('refresh', '0') == '1'
        try:
            page = max(1, int(request.GET.get('page', 1)))
        except ValueError:
            page = 1
        try:
            page_size = min(100, max(1, int(request.GET.get('page_size', 25))))
        except ValueError:
            page_size = 25

        res = get_enriched_duplicate_groups(
            filter_mode=filter_mode,
            format_filter=format_filter,
            search_q=search_q,
            exclude_unfinished_bookmarks=exclude_bm,
            require_all_files_enriched=all_enr,
            page=page,
            page_size=page_size,
            force_refresh=force_refresh,
        )
        return Response(res)

    def post(self, request):
        from .opds import resolve_enriched_duplicate_group

        action = (request.data.get('action') or 'resolve_one').strip().lower()
        allow_bm = bool(request.data.get('allow_unfinished_bookmark', False))

        if action == 'resolve_batch':
            resolutions = request.data.get('resolutions') or []
            if not isinstance(resolutions, list) or not resolutions:
                return Response({'ok': False, 'error': 'No duplicate resolutions provided.'}, status=400)

            resolved_groups = 0
            removed_files_total = 0
            reclaimed_bytes_total = 0
            errors = []
            results = []

            for entry in resolutions:
                if not isinstance(entry, dict):
                    continue
                keep_id = str(entry.get('keep_file_id') or '').strip()
                rem_ids = entry.get('remove_file_ids') or []
                if not keep_id or not rem_ids:
                    continue
                r = resolve_enriched_duplicate_group(
                    keep_file_id=keep_id,
                    remove_file_ids=rem_ids,
                    allow_unfinished_bookmark=allow_bm,
                )
                if r.get('ok'):
                    resolved_groups += 1
                    removed_files_total += int(r.get('removed_count') or 0)
                    reclaimed_bytes_total += int(r.get('reclaimed_bytes') or 0)
                    results.append(r)
                else:
                    errors.append(r.get('error') or 'Unknown error')

            return Response({
                'ok': resolved_groups > 0 or not errors,
                'resolved_groups': resolved_groups,
                'removed_files_total': removed_files_total,
                'reclaimed_bytes_total': reclaimed_bytes_total,
                'reclaimed_mb_total': round(reclaimed_bytes_total / (1024 * 1024), 2),
                'errors': errors,
                'results': results,
            })

        keep_file_id = str(request.data.get('keep_file_id') or '').strip()
        remove_file_ids = request.data.get('remove_file_ids') or []
        if isinstance(remove_file_ids, str):
            remove_file_ids = [x.strip() for x in remove_file_ids.split(',') if x.strip()]

        res = resolve_enriched_duplicate_group(
            keep_file_id=keep_file_id,
            remove_file_ids=remove_file_ids,
            allow_unfinished_bookmark=allow_bm,
        )
        return Response(res, status=200 if res.get('ok') else 400)


class SplitCatalogItemAPIView(APIView):
    """
    POST /mylibrary/api/split-item/
    Body: {"item_id": "<uuid>", "file_id": "<optional-uuid>", "enrich_split": true|false, "force_split_all": true|false}
    Splits unrelated `FileLocation`s on a multi-file `CatalogItem` into separate `CatalogItem` entries.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from .opds import split_unrelated_files_on_catalog_item

        item_id = str(request.data.get('item_id') or '').strip()
        file_id = str(request.data.get('file_id') or '').strip() or None
        enrich_split = bool(request.data.get('enrich_split', False))
        force_split_all = bool(request.data.get('force_split_all', False))

        if not item_id:
            return Response({'ok': False, 'error': 'item_id is required.'}, status=400)

        res = split_unrelated_files_on_catalog_item(
            item_or_id=item_id,
            target_file_id=file_id,
            enrich_split=enrich_split,
            force_split_all=force_split_all,
        )
        return Response(res, status=200 if res.get('ok') else 400)


def _isbn10_to_isbn13(isbn10: str) -> str:
    clean = re.sub(r'[^0-9Xx]', '', str(isbn10 or '')).upper()
    if len(clean) != 10 or not clean[:9].isdigit():
        return ''
    prefix = '978' + clean[:9]
    total = sum((1 if i % 2 == 0 else 3) * int(ch) for i, ch in enumerate(prefix))
    check = (10 - (total % 10)) % 10
    return f'{prefix}{check}'


def _isbn13_to_isbn10(isbn13: str) -> str:
    clean = re.sub(r'[^0-9]', '', str(isbn13 or ''))
    if len(clean) != 13 or not clean.startswith('978'):
        return ''
    core = clean[3:12]
    total = sum((10 - i) * int(ch) for i, ch in enumerate(core))
    rem = (11 - (total % 11)) % 11
    check = 'X' if rem == 10 else str(rem)
    return f'{core}{check}'


def _clean_wishlist_title(raw_title: str) -> tuple[str, str]:
    """
    Return `(cleaned_full_title, base_title_before_colon)` stripped of Amazon format noise
    while preserving edition/volume qualifiers in `cleaned_full_title`.
    """
    t = _clean_single_line(raw_title)
    if not t:
        return '', ''
    # Strip trailing parenthetical/bracketed format tags (Kindle, Hardcover, Paperback, etc.)
    # Do NOT strip edition/volume numbers from full_title so 2nd Edition / Vol. 2 stay distinct.
    for _ in range(3):
        t_next = re.sub(
            r'\s*[\(\[](?:kindle\s+edition|kindle|hardcover|paperback|mass\s+market\s+paperback|'
            r'spiral[\-\s]*bound|audio\s+cd|audible\s+audio\s+edition|unabridged|abridged|'
            r'print\s+replica|illustrated\s+edition|reprint)[\)\]]\s*$',
            '',
            t,
            flags=re.IGNORECASE,
        ).strip()
        if t_next == t:
            break
        t = t_next
    base = re.split(r'\s*[:\-–—]\s+', t, maxsplit=1)[0].strip()
    base = re.sub(r'\s*[\(\[][^)\]]*[\)\]]\s*$', '', base).strip()
    if len(base) < 3:
        base = t
    return t, base


_ORDINAL_MAP = {
    'first': '1', '1st': '1', 'one': '1', 'i': '1',
    'second': '2', '2nd': '2', 'two': '2', 'ii': '2',
    'third': '3', '3rd': '3', 'three': '3', 'iii': '3',
    'fourth': '4', '4th': '4', 'four': '4', 'iv': '4',
    'fifth': '5', '5th': '5', 'five': '5', 'v': '5',
    'sixth': '6', '6th': '6', 'six': '6', 'vi': '6',
    'seventh': '7', '7th': '7', 'seven': '7', 'vii': '7',
    'eighth': '8', '8th': '8', 'eight': '8', 'viii': '8',
    'ninth': '9', '9th': '9', 'nine': '9', 'ix': '9',
    'tenth': '10', '10th': '10', 'ten': '10', 'x': '10',
}


def _extract_edition_and_year_markers(text: str) -> tuple[set[str], int | None]:
    """
    Extract normalized edition/volume/part markers (e.g. `{'ed:2', 'vol:3'}`) and a 4-digit
    publication year (if present) from a title or filename string.
    """
    s = _clean_single_line(text).lower()
    if not s:
        return set(), None
    markers: set[str] = set()
    for m in re.finditer(r'\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th)?)\s+(?:ed\.?|edition)\b', s):
        raw_num = m.group(1)
        norm_num = _ORDINAL_MAP.get(raw_num, re.sub(r'\D', '', raw_num))
        if norm_num and norm_num != '1':
            markers.add(f'ed:{norm_num}')
    for m in re.finditer(r'\b(?:vol\.?|volume|book|part)\s*(?:no\.?\s*)?(\d+|i{1,3}|iv|v|vi{0,3}|ix|x)\b', s):
        raw_num = m.group(1)
        norm_num = _ORDINAL_MAP.get(raw_num, raw_num)
        if norm_num:
            markers.add(f'vol:{norm_num}')
    for m in re.finditer(r'\b(ii|iii|iv|vi|vii|viii|ix)\b', s):
        norm_num = _ORDINAL_MAP.get(m.group(1))
        if norm_num:
            markers.add(f'roman:{norm_num}')

    years = [int(y) for y in re.findall(r'\b(19[7-9]\d|20[0-2]\d)\b', s)]
    pub_year = max(years) if years else None
    return markers, pub_year


def _norm_title_preserve_qualifiers(val: str | None) -> str:
    """Normalize a book title for comparison WITHOUT stripping parenthetical edition/volume qualifiers."""
    if not val:
        return ''
    s = str(val).lower()
    s = re.sub(
        r'\s*[\(\[](?:kindle\s+edition|kindle|hardcover|paperback|mass\s+market\s+paperback|'
        r'spiral[\-\s]*bound|audio\s+cd|audible\s+audio\s+edition|unabridged|abridged|'
        r'print\s+replica)[\)\]]',
        ' ',
        s,
    )
    # Strip Anna's Archive / scanner filename suffixes if present in un-enriched CatalogItem titles
    s = re.split(r'\s+--\s+', s, maxsplit=1)[0]
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return ' '.join(s.split())


def _clean_wishlist_author(raw_author: str) -> tuple[str, str]:
    """
    Return `(cleaned_author, primary_author_surname)` stripped of Amazon/publisher byline noise.
    """
    a = _clean_single_line(raw_author)
    if not a:
        return '', ''
    a = re.sub(r'<br\b.*$', '', a, flags=re.IGNORECASE).strip()
    a = re.sub(r'<[^>]+>', ' ', a).strip()
    a = re.sub(r'(?:foreword|preface|introduction|afterword)\s+by\b.*$', '', a, flags=re.IGNORECASE).strip()
    a = re.sub(r'^\s*by\s+', '', a, flags=re.IGNORECASE).strip()
    a = re.sub(r'\|.*$', '', a).strip()
    a = re.sub(r'\s*\([^)]*(?:author|editor|translator|narrator|illustrator|foreword|edition|hardcover|paperback|kindle)[^)]*\)', '', a, flags=re.IGNORECASE).strip()
    first_author = re.split(r'\s*(?:,|;|&|\band\b)\s*', a, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    tokens = [tok for tok in re.split(r'[^A-Za-z0-9\'\-]+', first_author) if len(tok) >= 2 and tok.lower() not in ('jr', 'sr', 'ii', 'iii', 'iv', 'dr', 'md', 'phd')]
    surname = tokens[-1].lower() if tokens else ''
    return a, surname


_WISHLIST_IDX_STATE: dict[str, bool] = {
    'checked': False,
    'building': False,
    'upper_ready': False,
    'trgm_ready': False,
}

_STOP_PREPOSITIONS = {'a', 'an', 'and', 'as', 'at', 'by', 'for', 'from', 'in', 'into', 'nor', 'of', 'on', 'or', 'over', 'the', 'to', 'up', 'via', 'with'}


def _generate_indexed_title_variants(raw_title: str) -> list[str]:
    """Generate common case variants of a title so standard B-tree `title__in` index lookups hit immediately."""
    t = _clean_single_line(raw_title)
    if not t:
        return []
    variants = {t, t.title(), t.capitalize(), t.lower(), t.upper()}
    words = t.split()
    if len(words) > 1:
        smart_words = [
            w.capitalize() if (idx == 0 or idx == len(words) - 1 or w.lower() not in _STOP_PREPOSITIONS) else w.lower()
            for idx, w in enumerate(words)
        ]
        variants.add(' '.join(smart_words))
    return [v for v in variants if v]


def _ensure_wishlist_fast_indexes() -> None:
    """
    Check if functional `UPPER(title)` and `pg_trgm` GIN indexes exist on `catalog_catalogitem`;
    if missing, build them asynchronously in the background via `CREATE INDEX CONCURRENTLY`.
    """
    if _WISHLIST_IDX_STATE['upper_ready'] and _WISHLIST_IDX_STATE['trgm_ready']:
        return
    if _WISHLIST_IDX_STATE['building']:
        return
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'catalog_catalogitem' "
                "AND indexname IN ('idx_catalogitem_upper_title_btree', 'idx_catalogitem_upper_basetitle_btree')"
            )
            found = {row[0] for row in cur.fetchall()}
        _WISHLIST_IDX_STATE['upper_ready'] = 'idx_catalogitem_upper_title_btree' in found
        _WISHLIST_IDX_STATE['trgm_ready'] = 'idx_catalogitem_upper_basetitle_btree' in found
        _WISHLIST_IDX_STATE['checked'] = True
        if _WISHLIST_IDX_STATE['upper_ready'] and _WISHLIST_IDX_STATE['trgm_ready']:
            return
    except Exception:
        return

    def _bg_build():
        try:
            from django.db import connections
            conn = connections['default']
            conn.ensure_connection()
            old_autocommit = conn.get_autocommit()
            conn.set_autocommit(True)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalogitem_upper_title_btree "
                        "ON catalog_catalogitem (UPPER(title))"
                    )
                    _WISHLIST_IDX_STATE['upper_ready'] = True
                    cur.execute(
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalogitem_upper_basetitle_btree "
                        "ON catalog_catalogitem (UPPER(TRIM(SPLIT_PART(SPLIT_PART(title, ':', 1), ' (', 1))))"
                    )
                    _WISHLIST_IDX_STATE['trgm_ready'] = True
            finally:
                conn.set_autocommit(old_autocommit)
        except Exception:
            pass
        finally:
            _WISHLIST_IDX_STATE['building'] = False

    _WISHLIST_IDX_STATE['building'] = True
    import threading
    threading.Thread(target=_bg_build, daemon=True).start()


class WishlistCheckAPIView(APIView):
    """
    Batch & Single-Item API endpoint (`/mylibrary/api/wishlist/check/`) for the MyLibrary
    Amazon Wishlist & Anna's Archive Chrome Extension.

    Checks whether books on Amazon Wishlists (`Books (Kindle)`, `Print Books`, etc.)
    already exist in MyLibrary (`BookDB` `CatalogItem` / `FileLocation`, Kindle/Audible shelf,
    or enriched `scanner_state.db` records).
    """
    authentication_classes = []
    permission_classes = []

    def _check_books_batch(self, raw_books: list[dict]) -> list[dict]:
        import time as _t
        import urllib.parse
        from django.db.models.functions import Upper
        from .opds import is_file_enriched

        t_start = _t.monotonic()
        _ensure_wishlist_fast_indexes()
        t_idx = _t.monotonic()

        parsed_entries = []
        all_asins = set()
        all_isbns = set()
        exact_titles_lower = set()

        for idx, b in enumerate(raw_books[:120]):
            if not isinstance(b, dict):
                continue
            key = str(b.get('key') or b.get('asin') or b.get('id') or idx)
            raw_asin = re.sub(r'[^A-Za-z0-9]', '', str(b.get('asin') or '')).upper()
            raw_isbn = re.sub(r'[^0-9Xx]', '', str(b.get('isbn') or '')).upper()

            cand_isbns = set()
            if len(raw_isbn) in (10, 13):
                cand_isbns.add(raw_isbn)
            if len(raw_asin) == 10 and raw_asin[:9].isdigit():
                cand_isbns.add(raw_asin)
            for ci in list(cand_isbns):
                if len(ci) == 10:
                    i13 = _isbn10_to_isbn13(ci)
                    if i13:
                        cand_isbns.add(i13)
                elif len(ci) == 13:
                    i10 = _isbn13_to_isbn10(ci)
                    if i10:
                        cand_isbns.add(i10)

            full_t, base_t = _clean_wishlist_title(b.get('title') or '')
            clean_a, surname = _clean_wishlist_author(b.get('author') or b.get('authors') or '')
            ed_markers, det_year = _extract_edition_and_year_markers(
                f"{b.get('title') or ''} {b.get('edition') or ''} {b.get('author') or ''} {b.get('pub_year') or b.get('pub_date') or ''}"
            )
            raw_pub_year = None
            if b.get('pub_year'):
                try:
                    raw_pub_year = int(str(b.get('pub_year'))[:4])
                except ValueError:
                    pass
            pub_year = raw_pub_year or det_year

            if raw_asin:
                all_asins.add(raw_asin)
            all_isbns.update(cand_isbns)
            if full_t:
                exact_titles_lower.add(full_t.lower())
            if base_t:
                exact_titles_lower.add(base_t.lower())

            parsed_entries.append({
                'key': key,
                'raw_asin': raw_asin,
                'cand_isbns': cand_isbns,
                'orig_title': _clean_single_line(b.get('title') or ''),
                'full_title': full_t,
                'base_title': base_t,
                'norm_full_title': _norm_title_preserve_qualifiers(full_t),
                'norm_base_title': _norm_lookup_str(base_t),
                'ed_markers': ed_markers,
                'pub_year': pub_year,
                'clean_author': clean_a,
                'surname': surname,
                'wishlist_name': _clean_single_line(b.get('wishlist_name') or ''),
            })

        by_asin: dict[str, list[CatalogItem]] = {}
        by_isbn: dict[str, list[CatalogItem]] = {}
        if all_asins or all_isbns:
            q_id = Q()
            if all_asins:
                q_id |= Q(asin__in=list(all_asins))
            if all_isbns:
                q_id |= Q(isbn__in=list(all_isbns)) | Q(asin__in=list(all_isbns))
            for it in CatalogItem.objects.filter(q_id).order_by().prefetch_related('files')[:500]:
                if it.asin:
                    ca = re.sub(r'[^A-Za-z0-9]', '', it.asin).upper()
                    by_asin.setdefault(ca, []).append(it)
                    if ca in all_isbns:
                        by_isbn.setdefault(ca, []).append(it)
                if it.isbn:
                    ci = re.sub(r'[^0-9Xx]', '', it.isbn).upper()
                    by_isbn.setdefault(ci, []).append(it)

        # Batch Title / Prefix / Author lookup across all entries that did not already match by ISBN/ASIN
        need_title_entries = [
            e for e in parsed_entries
            if e['base_title']
            and len(e['base_title']) >= 3
            and not any(ci in by_isbn for ci in e['cand_isbns'])
            and not (e['raw_asin'] and e['raw_asin'] in by_asin)
        ]

        cands_by_norm_base: dict[str, list[CatalogItem]] = {}
        cands_by_norm_full: dict[str, list[CatalogItem]] = {}
        seen_batch_cand_ids: set = set()

        def _index_batch_cand(it_obj: CatalogItem):
            if it_obj.id in seen_batch_cand_ids:
                return
            seen_batch_cand_ids.add(it_obj.id)
            it_cf, it_cb = _clean_wishlist_title(it_obj.title or '')
            it_nf = _norm_title_preserve_qualifiers(it_cf or it_obj.title)
            it_nb = _norm_lookup_str(it_cb or it_obj.title)
            if it_nb:
                cands_by_norm_base.setdefault(it_nb, []).append(it_obj)
            if it_nf and it_nf != it_nb:
                cands_by_norm_full.setdefault(it_nf, []).append(it_obj)

        if need_title_entries:
            upper_titles: set[str] = set()
            indexed_variants: set[str] = set()

            for e in need_title_entries:
                for raw_t in (e['full_title'], e['base_title'], e['orig_title']):
                    if raw_t and len(raw_t) >= 3:
                        upper_titles.add(raw_t.upper())
                        if not _WISHLIST_IDX_STATE.get('upper_ready'):
                            indexed_variants.update(_generate_indexed_title_variants(raw_t))

            t_q1 = _t.monotonic()
            if _WISHLIST_IDX_STATE.get('upper_ready') and upper_titles:
                # Single B-tree index scan on `idx_catalogitem_upper_title_btree` (`UPPER(title) IN (...)`)
                for it_obj in (
                    CatalogItem.objects.annotate(_up_title=Upper('title'))
                    .filter(_up_title__in=list(upper_titles)[:400])
                    .order_by()
                    .prefetch_related('files')[:400]
                ):
                    _index_batch_cand(it_obj)
            elif indexed_variants:
                for it_obj in CatalogItem.objects.filter(title__in=list(indexed_variants)[:600]).order_by().prefetch_related('files')[:400]:
                    _index_batch_cand(it_obj)
            t_q2 = _t.monotonic()

            # 2. Subtitle-stripped base-title B-tree `= ANY(...)` lookup using `idx_catalogitem_upper_basetitle_btree`
            if _WISHLIST_IDX_STATE.get('trgm_ready') and upper_titles:
                up_list = list(upper_titles)[:400]
                for it_obj in (
                    CatalogItem.objects.extra(
                        where=["UPPER(TRIM(SPLIT_PART(SPLIT_PART(title, ':', 1), ' (', 1))) = ANY(%s)"],
                        params=[up_list],
                    )
                    .order_by()
                    .prefetch_related('files')[:400]
                ):
                    _index_batch_cand(it_obj)
            t_q3 = _t.monotonic()

            self._last_timings = {
                'idx_ms': round((t_idx - t_start) * 1000, 1),
                'q_upper_in_ms': round((t_q2 - t_q1) * 1000, 1),
                'q_prefix_ms': round((t_q3 - t_q2) * 1000, 1),
                'idx_state': dict(_WISHLIST_IDX_STATE),
            }

        results = []
        for entry in parsed_entries:
            matched_items: list[CatalogItem] = []
            other_edition_items: list[tuple[CatalogItem, str]] = []
            seen_ids = set()
            seen_other_ids = set()
            match_type = ''

            def _add_item(item_obj: CatalogItem, m_type: str):
                nonlocal match_type
                if item_obj.id not in seen_ids:
                    seen_ids.add(item_obj.id)
                    matched_items.append(item_obj)
                    if not match_type:
                        match_type = m_type

            def _add_other_edition(item_obj: CatalogItem, reason: str):
                if item_obj.id not in seen_ids and item_obj.id not in seen_other_ids:
                    seen_other_ids.add(item_obj.id)
                    other_edition_items.append((item_obj, reason))

            # 1. Exact ISBN-10 / ISBN-13 match
            for ci in entry['cand_isbns']:
                for it in by_isbn.get(ci, []):
                    _add_item(it, 'isbn')

            # 2. Exact ASIN match
            if entry['raw_asin']:
                for it in by_asin.get(entry['raw_asin'], []):
                    _add_item(it, 'asin')

            # 3. Title + Author / Base-Title match from batched in-memory candidate map
            if not matched_items and entry['base_title'] and len(entry['base_title']) >= 3:
                sname = entry['surname']
                norm_bt = entry['norm_base_title']
                norm_ft = entry['norm_full_title']
                bt_words = [w for w in norm_bt.split() if len(w) >= 3 and w not in ('the', 'and', 'for', 'with', 'from', 'into')]

                cand_seen = set()
                cands = []
                for bucket_key in (norm_bt, norm_ft):
                    if not bucket_key:
                        continue
                    for it_c in cands_by_norm_base.get(bucket_key, []) + cands_by_norm_full.get(bucket_key, []):
                        if it_c.id not in cand_seen:
                            cand_seen.add(it_c.id)
                            cands.append(it_c)

                for it in cands:
                    it_clean_full, it_clean_base = _clean_wishlist_title(it.title or '')
                    it_norm_full = _norm_title_preserve_qualifiers(it_clean_full or it.title)
                    it_norm_base = _norm_lookup_str(it_clean_base or it.title)
                    if not it_norm_base:
                        continue

                    # Check author agreement first
                    it_auth_norm = _norm_lookup_str(it.authors or '')
                    is_unknown_auth = not it_auth_norm or it_auth_norm in ('unknown', 'unknown author')
                    author_ok = False
                    if sname and not is_unknown_auth:
                        author_ok = sname in it_auth_norm
                    elif not sname or (is_unknown_auth and len(bt_words) >= 3):
                        author_ok = True
                    if not author_ok:
                        continue

                    # Check base title or full title equality (do NOT allow partial word subset matches)
                    base_or_full_eq = (
                        it_norm_full == norm_ft
                        or it_norm_full == norm_bt
                        or it_norm_base == norm_bt
                        or it_norm_base == norm_ft
                    )
                    if not base_or_full_eq:
                        continue

                    # Collect candidate item's ISBNs, ASIN, edition markers, and publication year
                    it_isbns = set()
                    if it.isbn:
                        ci_clean = re.sub(r'[^0-9Xx]', '', str(it.isbn)).upper()
                        if len(ci_clean) in (10, 13):
                            it_isbns.add(ci_clean)
                    for m_isbn in re.findall(r'\b(97[89]\d{10})\b', f"{it.title or ''} {' '.join(f.file_name or '' for f in it.files.all())}"):
                        it_isbns.add(m_isbn)
                    for ci_code in list(it_isbns):
                        if len(ci_code) == 10:
                            i13 = _isbn10_to_isbn13(ci_code)
                            if i13:
                                it_isbns.add(i13)
                        elif len(ci_code) == 13:
                            i10 = _isbn13_to_isbn10(ci_code)
                            if i10:
                                it_isbns.add(i10)

                    it_ed_markers, it_text_year = _extract_edition_and_year_markers(
                        f"{it.title or ''} {' '.join(f.file_name or '' for f in it.files.all())}"
                    )
                    it_pub_year = it.pub_date.year if it.pub_date else it_text_year

                    # Conflict 1: Both have ISBNs and they do NOT overlap -> Different book or edition!
                    if entry['cand_isbns'] and it_isbns and entry['cand_isbns'].isdisjoint(it_isbns):
                        _add_other_edition(it, f"Different ISBN ({next(iter(sorted(it_isbns, key=len, reverse=True)))})")
                        continue

                    # Conflict 2: Both have ASINs and they do NOT match -> Different book or edition!
                    it_asin_clean = re.sub(r'[^A-Za-z0-9]', '', str(it.asin or '')).upper()
                    if entry['raw_asin'] and it_asin_clean and entry['raw_asin'] != it_asin_clean:
                        _add_other_edition(it, f"Different ASIN ({it_asin_clean})")
                        continue

                    # Conflict 3: Both have publication years and they differ by >= 2 years -> Older/different book with same title!
                    if entry['pub_year'] and it_pub_year and abs(entry['pub_year'] - it_pub_year) >= 2:
                        _add_other_edition(it, f"Published {it_pub_year} vs Wishlist {entry['pub_year']}")
                        continue

                    # Conflict 4: Edition / Volume / Part number mismatch (e.g. 2nd Edition vs 1st Edition, Vol 2 vs Vol 1)
                    if entry['ed_markers'] != it_ed_markers and (entry['ed_markers'] or it_ed_markers):
                        _add_other_edition(it, "Different Edition/Volume")
                        continue

                    # Conflict 5: Both have subtitles (full != base) and the full titles differ -> Different book in series!
                    entry_has_sub = norm_ft != norm_bt
                    it_has_sub = it_norm_full != it_norm_base
                    if entry_has_sub and it_has_sub and it_norm_full != norm_ft:
                        _add_other_edition(it, f"Different subtitle ({it.title})")
                        continue

                    # Conflict 6: Wishlist has a specific subtitle (or ISBN) that the library book lacks, and base title is short (<= 2 words, e.g. "Visualization")
                    if (entry_has_sub != it_has_sub) and len(bt_words) <= 2:
                        _add_other_edition(it, f"Different title/subtitle ({it.title})")
                        continue

                    _add_item(it, 'title_author' if sname else 'title')

                # If any candidate for this title/author had an explicit ISBN, ASIN, Year, or Edition conflict
                # and we only matched a sibling/duplicate row by title (e.g. a row missing its ISBN or pub_date),
                # demote the title-only match into other_edition_items so it does not cause a false positive.
                if other_edition_items and matched_items and match_type in ('title', 'title_author'):
                    first_conflict_reason = other_edition_items[0][1]
                    for m_it in list(matched_items):
                        m_isbns = set()
                        if m_it.isbn:
                            c_i = re.sub(r'[^0-9Xx]', '', str(m_it.isbn)).upper()
                            if len(c_i) in (10, 13):
                                m_isbns.add(c_i)
                                if len(c_i) == 10:
                                    m_isbns.add(_isbn10_to_isbn13(c_i))
                                elif len(c_i) == 13:
                                    m_isbns.add(_isbn13_to_isbn10(c_i))
                        has_exact_isbn = bool(entry['cand_isbns'] and m_isbns and not entry['cand_isbns'].isdisjoint(m_isbns))
                        if not has_exact_isbn:
                            matched_items.remove(m_it)
                            seen_ids.discard(m_it.id)
                            _add_other_edition(m_it, first_conflict_reason)
                    if not matched_items:
                        match_type = ''

            def _serialize_match_obj(it: CatalogItem, note: str = '') -> dict:
                files_data = []
                is_enr = False
                for fl in it.files.all():
                    if not is_enr and fl.file_path and is_file_enriched(fl.file_path):
                        is_enr = True
                    fmt = (fl.file_format or '').upper()
                    files_data.append({
                        'id': str(fl.id),
                        'file_name': fl.file_name,
                        'file_format': fmt,
                        'file_size': fl.file_size,
                        'download_url': f'/mylibrary/opds/download/{fl.id}/{fl.file_name}',
                    })
                _, text_year = _extract_edition_and_year_markers(
                    f"{it.title or ''} {' '.join(f['file_name'] or '' for f in files_data)}"
                )
                pub_date_str = str(it.pub_date) if it.pub_date else (str(text_year) if text_year else '')
                pub_year_val = it.pub_date.year if it.pub_date else text_year
                return {
                    'id': str(it.id),
                    'title': it.title,
                    'authors': it.authors,
                    'isbn': it.isbn or '',
                    'asin': it.asin or '',
                    'pub_date': pub_date_str,
                    'pub_year': pub_year_val,
                    'item_type': it.item_type,
                    'is_enriched': is_enr,
                    'note': note,
                    'formats': [f['file_format'] for f in files_data if f['file_format']],
                    'files': files_data,
                }

            # Format matched items for Chrome Extension badge & modal
            serialized_matches = []
            all_formats = []
            all_types = []
            any_enriched = False
            for it in matched_items[:6]:
                m_obj = _serialize_match_obj(it)
                if m_obj['is_enriched']:
                    any_enriched = True
                if it.item_type and it.item_type not in all_types:
                    all_types.append(it.item_type)
                for fmt in m_obj['formats']:
                    if fmt and fmt not in all_formats:
                        all_formats.append(fmt)
                # Only adopt ISBN from MyLibrary item when it was an exact ASIN/ISBN match
                if match_type in ('isbn', 'asin') and it.isbn:
                    ci_clean = re.sub(r'[^0-9Xx]', '', str(it.isbn)).upper()
                    if len(ci_clean) in (10, 13):
                        entry['cand_isbns'].add(ci_clean)
                        if len(ci_clean) == 10:
                            i13 = _isbn10_to_isbn13(ci_clean)
                            if i13:
                                entry['cand_isbns'].add(i13)
                        elif len(ci_clean) == 13:
                            i10 = _isbn13_to_isbn10(ci_clean)
                            if i10:
                                entry['cand_isbns'].add(i10)
                serialized_matches.append(m_obj)

            serialized_other_editions = [
                _serialize_match_obj(it_other, reason)
                for it_other, reason in other_edition_items[:4]
            ]

            sorted_isbns = sorted(entry['cand_isbns'], key=lambda x: (-len(x), x))
            primary_isbn = sorted_isbns[0] if sorted_isbns else ''
            title_core = entry['base_title'] or entry['full_title'] or entry['orig_title']
            annas_title_q = ' '.join(filter(None, [title_core, entry['surname']])).strip()
            search_q = title_core or entry['raw_asin']
            annas_q = primary_isbn or annas_title_q or search_q

            results.append({
                'key': entry['key'],
                'asin': entry['raw_asin'],
                'isbns': sorted_isbns,
                'primary_isbn': primary_isbn,
                'pub_year': entry['pub_year'],
                'cleaned_title': entry['full_title'],
                'base_title': entry['base_title'],
                'cleaned_author': entry['clean_author'],
                'author_surname': entry['surname'],
                'in_library': bool(serialized_matches),
                'match_type': match_type,
                'match_count': len(serialized_matches),
                'is_enriched': any_enriched,
                'formats': all_formats,
                'item_types': all_types,
                'search_query': search_q,
                'annas_query': annas_q,
                'annas_isbn_query': primary_isbn,
                'annas_title_query': annas_title_q,
                'annas_title_only_query': title_core,
                'annas_search_url': f"https://annas-archive.pk/search?q={urllib.parse.quote_plus(annas_q)}",
                'annas_isbn_search_url': f"https://annas-archive.pk/search?q={urllib.parse.quote_plus(primary_isbn)}" if primary_isbn else '',
                'annas_title_search_url': f"https://annas-archive.pk/search?q={urllib.parse.quote_plus(annas_title_q or title_core)}" if (annas_title_q or title_core) else '',
                'matches': serialized_matches,
                'other_editions': serialized_other_editions,
            })

        return results

    def get(self, request):
        book = {
            'key': request.GET.get('key') or '0',
            'asin': request.GET.get('asin') or '',
            'isbn': request.GET.get('isbn') or '',
            'title': request.GET.get('title') or '',
            'author': request.GET.get('author') or '',
            'wishlist_name': request.GET.get('wishlist_name') or '',
        }
        res = self._check_books_batch([book])
        resp = Response({'ok': True, 'timings': getattr(self, '_last_timings', {}), 'results': res, 'item': res[0] if res else None})
        resp['Access-Control-Allow-Origin'] = '*'
        return resp

    def post(self, request):
        body = request.data or {}
        books = body.get('books') or []
        if isinstance(books, dict):
            books = [books]
        res = self._check_books_batch(books)
        in_lib_cnt = sum(1 for r in res if r.get('in_library'))
        resp = Response({
            'ok': True,
            'checked_count': len(res),
            'in_library_count': in_lib_cnt,
            'missing_count': len(res) - in_lib_cnt,
            'results': res,
        })
        resp['Access-Control-Allow-Origin'] = '*'
        return resp






