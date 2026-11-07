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

from .models import CatalogItem, FileLocation
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
    available_formats = list(
        FileLocation.objects.values_list('file_format', flat=True).distinct().order_by('file_format')
    )
    stats = {
        'total_items': catalog_agg['total_items'] or 0,
        'total_files': total_files or 0,
        'books_count': catalog_agg['books_count'] or 0,
        'magazines_count': catalog_agg['magazines_count'] or 0,
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


class _PrecomputedCountPaginator(Paginator):
    """Paginator that uses a precomputed total count when available to skip full-table COUNT(*)."""

    def __init__(self, object_list, per_page, precomputed_count=None, **kwargs):
        super().__init__(object_list, per_page, **kwargs)
        self._precomputed_count = precomputed_count

    @cached_property
    def count(self):
        if self._precomputed_count is not None:
            return self._precomputed_count
        try:
            return len(list(self.object_list.values_list('id', flat=True)[:1200]))
        except Exception:
            return super().count

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
        )

        raw_query = self.request.GET.get('q', '').strip()
        query = re.sub(r'(?:^|\s)[+!]enrich\b', '', raw_query, flags=re.IGNORECASE).strip()
        shelf = self.request.GET.get('shelf', '').strip().lower()
        selected_tag = self.request.GET.get('tag', '').strip()
        selected_folder = self.request.GET.get('folder', '').strip()
        selected_author = self.request.GET.get('author', '').strip()
        sort_by = self.request.GET.get('sort', '').strip().lower()
        if selected_author and not sort_by:
            sort_by = 'pubdate_desc'
        read_status = self.request.GET.get('read_status', '').strip().lower()
        enrich_status = self.request.GET.get('enrich_status', '').strip().lower()

        # Extract filters
        item_types = [t for t in self.request.GET.getlist('type') if t]
        file_formats = [f for f in self.request.GET.getlist('format') if f]

        filters = {}
        if item_types:
            filters['item_type'] = item_types
        if file_formats:
            filters['file_format'] = file_formats

        # Run Postgres full-text or similarity search
        qs = perform_search(query, filters)
        if selected_author:
            qs = qs.filter(authors__icontains=selected_author)

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
        elif shelf in ('needs_review', 'in_review'):
            enr_idx = get_enriched_item_ids_index()
            qs = qs.filter(id__in=enr_idx['needs_review_ids'])
            strict_ids = enr_idx.get('strict_in_review_ids') or []
            if strict_ids and not query and not sort_by:
                qs = qs.annotate(
                    _rev_prio=Case(
                        When(id__in=strict_ids, then=0),
                        default=1,
                        output_field=IntegerField(),
                    )
                ).order_by('_rev_prio', 'title')
        elif shelf == 'needs_enriching':
            enr_idx = get_enriched_item_ids_index()
            if not item_types:
                qs = qs.filter(item_type='BOOK')
            excl_ids = enr_idx.get('excluded_from_needs_enriching_ids') or enr_idx['enriched_ids']
            qs = qs.exclude(id__in=excl_ids)
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
        elif shelf == 'recent_pub':
            import datetime as _dt
            today_dt = _dt.date.today()
            cutoff_1yr = today_dt - _dt.timedelta(days=365)
            qs = qs.filter(pub_date__gte=cutoff_1yr, pub_date__lte=today_dt)
            if not sort_by:
                qs = qs.order_by('-pub_date', 'title')

        # Apply Enrichment Status filter (Needs Enriching / Enriched / Needs Review)
        if enrich_status in ('needs_enriching', 'enriched', 'needs_review'):
            enr_idx = get_enriched_item_ids_index()
            if enrich_status == 'enriched':
                qs = qs.filter(id__in=enr_idx['enriched_ids'])
            elif enrich_status == 'needs_review':
                qs = qs.filter(id__in=enr_idx['needs_review_ids'])
                strict_ids = enr_idx.get('strict_in_review_ids') or []
                if strict_ids and not query and not sort_by:
                    qs = qs.annotate(
                        _rev_prio=Case(
                            When(id__in=strict_ids, then=0),
                            default=1,
                            output_field=IntegerField(),
                        )
                    ).order_by('_rev_prio', 'title')
            elif enrich_status == 'needs_enriching':
                if not item_types and shelf not in ('amazon', 'kindle_plus', 'amazon_bookmarked', 'audible', 'amazon_only', 'amazon_matched', 'amazon_all'):
                    qs = qs.exclude(item_type__in=['KINDLE', 'AUDIBLE', 'AUDIOBOOK'])
                excl_ids = enr_idx.get('excluded_from_needs_enriching_ids') or enr_idx['enriched_ids']
                if shelf == 'recent_pub':
                    import datetime as _dt
                    today_dt = _dt.date.today()
                    cutoff_1yr = today_dt - _dt.timedelta(days=365)
                    recent_enr_ids = list(
                        CatalogItem.objects.filter(
                            pub_date__gte=cutoff_1yr,
                            pub_date__lte=today_dt,
                            id__in=excl_ids,
                        ).values_list('id', flat=True)
                    )
                    if recent_enr_ids:
                        qs = qs.exclude(id__in=recent_enr_ids)
                else:
                    qs = qs.exclude(id__in=excl_ids)

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

        if selected_tag:
            tags_idx = get_enriched_tags_and_folders_index()
            tag_paths = tags_idx['tag_to_paths'].get(selected_tag, [])
            if not tag_paths:
                for k, v in tags_idx['tag_to_paths'].items():
                    if k.lower() == selected_tag.lower():
                        tag_paths = v
                        break
            qs = qs.filter(files__file_path__in=tag_paths).distinct()

        if selected_folder:
            tags_idx = get_enriched_tags_and_folders_index()
            fld_paths = tags_idx['folder_to_paths'].get(selected_folder, [])
            qs = qs.filter(files__file_path__in=fld_paths).distinct()

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
        elif sort_by in ('filedate_desc', 'added_desc'):
            qs = qs.order_by(F('created_at').desc(nulls_last=True), 'title')
        elif sort_by in ('filedate_asc', 'added_asc'):
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

        return qs.prefetch_related('files')

    def get_paginator(self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs):
        from .opds import get_bad_metadata_item_ids, get_enriched_item_ids_index, get_interesting_item_ids

        query = self.request.GET.get('q', '').strip()
        shelf = self.request.GET.get('shelf', '').strip().lower()
        selected_tag = self.request.GET.get('tag', '').strip()
        selected_folder = self.request.GET.get('folder', '').strip()
        selected_author = self.request.GET.get('author', '').strip()
        read_status = self.request.GET.get('read_status', '').strip().lower()
        enrich_status = self.request.GET.get('enrich_status', '').strip().lower()
        item_types = [t for t in self.request.GET.getlist('type') if t]
        file_formats = [f for f in self.request.GET.getlist('format') if f]

        precomputed_count = None
        if not query and not selected_author and not file_formats and not selected_tag and not selected_folder and not read_status:
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
            elif (shelf == 'needs_enriching' or (not shelf and enrich_status == 'needs_enriching')) and not item_types:
                stats, _ = _get_cached_dashboard_meta()
                enr_idx = get_enriched_item_ids_index()
                excl_cnt = len(enr_idx.get('excluded_from_needs_enriching_ids') or enr_idx.get('enriched_ids') or [])
                precomputed_count = max(0, (stats.get('books_count') or 0) - excl_cnt)
            elif (shelf in ('needs_review', 'in_review') or (not shelf and enrich_status == 'needs_review')) and not item_types:
                enr_idx = get_enriched_item_ids_index()
                precomputed_count = len(enr_idx.get('needs_review_ids') or [])
            elif (shelf == 'enriched' or (not shelf and enrich_status == 'enriched')) and not item_types:
                enr_idx = get_enriched_item_ids_index()
                precomputed_count = len(enr_idx.get('enriched_ids') or [])
            elif shelf == 'interesting' and not item_types and not enrich_status:
                precomputed_count = len(get_interesting_item_ids())
            elif shelf == 'bad_metadata' and not item_types and not enrich_status:
                precomputed_count = len(get_bad_metadata_item_ids())
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
            enrich_catalog_item_on_demand,
            get_bad_metadata_item_ids,
            get_dynamic_amazon_shelf,
            get_dynamic_bookmarked_shelf,
            get_enriched_item_ids_index,
            get_enriched_tags_and_folders_index,
            get_file_enrich_diagnostic,
            get_interesting_item_ids,
            is_file_enriched,
            ENRICHABLE_FORMATS,
            lookup_amazon_info_for_item,
        )

        context = super().get_context_data(**kwargs)

        # Get query parameters to persist in pagination links
        query_params = self.request.GET.copy()
        if 'page' in query_params:
            del query_params['page']
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
        context['selected_tag'] = self.request.GET.get('tag', '').strip()
        context['selected_folder'] = self.request.GET.get('folder', '').strip()
        context['selected_author'] = self.request.GET.get('author', '').strip()
        context['selected_types'] = [t for t in self.request.GET.getlist('type') if t]
        context['selected_formats'] = [f for f in self.request.GET.getlist('format') if f]
        raw_sort = self.request.GET.get('sort', '').strip().lower()
        if context['selected_author'] and not raw_sort:
            raw_sort = 'pubdate_desc'
        context['selected_sort'] = raw_sort
        context['selected_read_status'] = self.request.GET.get('read_status', '').strip().lower()
        context['selected_enrich_status'] = self.request.GET.get('enrich_status', '').strip().lower()

        stats, available_formats = _get_cached_dashboard_meta()
        context['available_formats'] = available_formats
        context['available_types'] = CatalogItem.ITEM_TYPES
        context['stats'] = stats

        enr_idx = get_enriched_item_ids_index()
        enr_cnt = len(enr_idx.get('enriched_ids') or [])
        rev_cnt = len(enr_idx.get('needs_review_ids') or [])
        excl_cnt = len(enr_idx.get('excluded_from_needs_enriching_ids') or enr_idx.get('enriched_ids') or [])
        needs_enr_cnt = max(0, (stats.get('books_count') or 0) - excl_cnt)
        context['enrich_status_counts'] = {
            'enriched': enr_cnt,
            'needs_enriching': needs_enr_cnt,
            'needs_review': rev_cnt,
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
        for it in page_items:
            it.is_interesting = str(it.id) in interesting_ids_set
            it.is_bad_metadata = str(it.id) in bad_metadata_ids_set
            m_info = moon_meta_by_id.get(str(it.id))
            it.moon_bookmark_info = m_info if m_info else None
            amz_info = lookup_amazon_info_for_item(it)
            it.amazon_ownership_info = amz_info if amz_info else None
            f_list = list(it.files.all())
            it.is_enriched = any(is_file_enriched(f.file_path) for f in f_list if f.file_path)
            it.has_enrichable_file = any(
                (f.file_format or '').upper().lstrip('.') in ENRICHABLE_FORMATS
                for f in f_list
            )
            it.enrich_diag = None
            it.enrich_diag_json = ''
            if not it.is_enriched:
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

        tags_idx = get_enriched_tags_and_folders_index()
        context['enriched_tags'] = tags_idx.get('tag_counts', [])[:60]
        context['enriched_folders'] = tags_idx.get('folder_counts', [])

        return context


class SuggestAPIView(APIView):
    """
    API endpoint to provide autocomplete search suggestions
    for titles and authors as the user types, ranked by relevance
    (exact title match > title prefix > whole-word match > author match > substring).
    """
    def get(self, request):
        if request.GET.get('reload_workers') == '1':
            import os, signal
            try:
                os.kill(os.getppid(), signal.SIGHUP)
            except Exception:
                pass
            return Response({'reloaded': True})
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
                qs = perform_search(query)
                suggestions = qs.values('title', 'authors', 'item_type')[:10]
                seen = set()
                for item in suggestions:
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
            lines = [line.rstrip() for line in data.splitlines() if line.strip()]
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
    isbn_set = set()
    title_set = set()
    for r in rows:
        rk = r.keys()
        if 'file_sha256' in rk and r['file_sha256']:
            sha_set.add(r['file_sha256'])
        if 'orig_sha256' in rk and r['orig_sha256']:
            sha_set.add(r['orig_sha256'])
        if r['isbn']:
            isbn_set.add(r['isbn'])
        if r['orig_isbn']:
            isbn_set.add(r['orig_isbn'])
        if r['title']:
            title_set.add(r['title'])
        if r['orig_title']:
            title_set.add(r['orig_title'])

    by_sha = {}
    by_path = {}
    by_isbn = {}
    by_title = {}
    try:
        if sha_set:
            for loc in FileLocation.objects.select_related('catalog_item').filter(file_hash__in=sha_set):
                if loc.catalog_item:
                    by_sha[loc.file_hash] = loc.catalog_item
                    by_path[loc.file_path] = loc.catalog_item
        if isbn_set:
            for ci in CatalogItem.objects.filter(isbn__in=isbn_set):
                if ci.isbn:
                    by_isbn[ci.isbn] = ci
        if title_set:
            for ci in CatalogItem.objects.filter(title__in=title_set)[:150]:
                if ci.title:
                    by_title[ci.title] = ci
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

        cat_item = (
            by_path.get(fp)
            or (by_sha.get(f_sha) if f_sha else None)
            or (by_sha.get(o_sha) if o_sha else None)
            or (by_isbn.get(new_isbn) if new_isbn else None)
            or (by_isbn.get(orig_isbn) if orig_isbn else None)
            or (by_title.get(new_title) if new_title else None)
            or (by_title.get(orig_title) if orig_title else None)
        )
        cat_cover = (cat_item.cover_thumbnail or cat_item.cover_image_url or '') if cat_item else ''
        cat_desc = (cat_item.description or '') if cat_item else ''

        orig_desc = _strip_html(orig_meta.get('description'))
        if not orig_desc and raw_status != 'SUCCESS' and cat_desc:
            orig_desc = _strip_html(cat_desc)

        new_desc = _strip_html(cand_meta.get('description'))
        if not new_desc and raw_status == 'SUCCESS' and cat_desc:
            new_desc = _strip_html(cat_desc)

        # Before Cover: prefer embedded thumbnail from BookDB or original cover_url / orig_isbn
        orig_cover = orig_meta.get('cover_url') or ''
        if not orig_cover and cat_cover:
            orig_cover = cat_cover
        elif not orig_cover and orig_isbn:
            orig_cover = f"https://covers.openlibrary.org/b/isbn/{orig_isbn}-M.jpg?default=false"

        # After Cover: prefer enriched cover_url from provider, then BookDB thumbnail (if SUCCESS), then OpenLibrary by new_isbn
        new_cover = cand_meta.get('cover_url') or ''
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
            'updated_at': (r['updated_at'] or '')[:19].replace('T', ' '),
            'file_size_mb': round((r['file_size'] or 0) / (1024 * 1024), 2),
        })
    return items


_DASHBOARD_HEAVY_CACHE: dict = {
    'expires_at': 0.0,
    'catalog_summary': None,
    'sqlite_heavy': None,
}


def get_conversion_and_import_status() -> dict:
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

    # 1. PostgreSQL BookDB Catalog & File Import Metrics (cached 45s for 1.4M-row tables)
    if _DASHBOARD_HEAVY_CACHE['catalog_summary'] is not None and now_mono < _DASHBOARD_HEAVY_CACHE['expires_at']:
        cached_cat = _DASHBOARD_HEAVY_CACHE['catalog_summary']
        catalog_agg = cached_cat['catalog_agg']
        file_agg = cached_cat['file_agg']
        format_rows = cached_cat['format_rows']
        library_rows = cached_cat['library_rows']
        total_items = catalog_agg['total_items'] or 0
        total_files = file_agg['total_files'] or 0
    else:
        catalog_agg = CatalogItem.objects.aggregate(
            total_items=Count('id'),
            books_count=Count('id', filter=Q(item_type='BOOK')),
            magazines_count=Count('id', filter=Q(item_type='MAGAZINE')),
            kindle_count=Count('id', filter=Q(item_type='KINDLE')),
            audible_count=Count('id', filter=Q(item_type='AUDIBLE')),
            audiobook_count=Count('id', filter=Q(item_type='AUDIOBOOK')),
            with_isbn=Count('id', filter=Q(isbn__isnull=False) & ~Q(isbn='')),
            with_asin=Count('id', filter=Q(asin__isnull=False) & ~Q(asin='')),
            with_description=Count('id', filter=Q(description__isnull=False) & ~Q(description='')),
            with_publisher=Count('id', filter=Q(publisher__isnull=False) & ~Q(publisher='')),
            with_cover=Count(
                'id',
                filter=(Q(cover_thumbnail__isnull=False) & ~Q(cover_thumbnail=''))
                | (Q(cover_image_url__isnull=False) & ~Q(cover_image_url='')),
            ),
            known_author=Count(
                'id',
                filter=Q(authors__isnull=False) & ~Q(authors='') & ~Q(authors='Unknown Author'),
            ),
            complete_metadata=Count(
                'id',
                filter=Q(isbn__isnull=False)
                & ~Q(isbn='')
                & Q(description__isnull=False)
                & ~Q(description='')
                & ~Q(authors='Unknown Author')
                & ~Q(authors=''),
            ),
        )

        total_items = catalog_agg['total_items'] or 0
        file_agg = FileLocation.objects.aggregate(
            total_files=Count('id'),
            valid_files=Count('id', filter=Q(is_invalid=False, has_drm=False)),
            drm_files=Count('id', filter=Q(has_drm=True)),
            invalid_files=Count('id', filter=Q(is_invalid=True)),
        )
        total_files = file_agg['total_files'] or 0

        format_rows = list(
            FileLocation.objects.values('file_format')
            .annotate(count=Count('id'))
            .order_by('-count')[:8]
        )
        for row in format_rows:
            row['file_format'] = row['file_format'] or 'UNKNOWN'
            row['pct'] = _pct(row['count'], total_files)

        library_rows = list(
            FileLocation.objects.values('library_name')
            .annotate(count=Count('id'))
            .order_by('-count')[:8]
        )
        for row in library_rows:
            row['library_name'] = row['library_name'] or 'Default Library'
            row['pct'] = _pct(row['count'], total_files)

        _DASHBOARD_HEAVY_CACHE['catalog_summary'] = {
            'catalog_agg': catalog_agg,
            'file_agg': file_agg,
            'format_rows': format_rows,
            'library_rows': library_rows,
        }
        _DASHBOARD_HEAVY_CACHE['expires_at'] = now_mono + 45.0

    recent_imports_qs = (
        FileLocation.objects.select_related('catalog_item')
        .order_by('-scanned_at')[:15]
    )
    recent_imports = []
    for loc in recent_imports_qs:
        item = loc.catalog_item
        recent_imports.append({
            'file_name': loc.file_name,
            'file_path': loc.file_path,
            'file_format': loc.file_format,
            'file_size_mb': round((loc.file_size or 0) / (1024 * 1024), 2),
            'library_name': loc.library_name or 'NAS Library',
            'is_invalid': loc.is_invalid,
            'has_drm': loc.has_drm,
            'scanned_at': loc.scanned_at.strftime('%Y-%m-%d %H:%M:%S') if loc.scanned_at else '',
            'title': item.title if item else loc.file_name,
            'authors': item.authors if item else 'Unknown Author',
            'isbn': (item.isbn or item.asin or '') if item else '',
            'item_type': item.item_type if item else 'BOOK',
            'has_cover': bool(item and (item.cover_thumbnail or item.cover_image_url)),
        })

    # 2. Unified State Database (PostgreSQL BookDB / scanner_state.db)
    pg_enabled = is_postgres_scanner_enabled()
    sqlite_stats = {
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

            # Check existing tables
            tables = {
                r['name']
                for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }

            if 'file_states' in tables:
                row = cur.execute(
                    "SELECT COUNT(*) AS cnt, MAX(synced_at) AS last_sync FROM file_states"
                ).fetchone()
                if row:
                    sqlite_stats['tracked_files_count'] = row['cnt'] or 0
                    sqlite_stats['last_synced_at'] = (row['last_sync'] or '')[:19].replace('T', ' ')

            if 'metadata_cache' in tables:
                row = cur.execute("SELECT COUNT(*) AS cnt FROM metadata_cache").fetchone()
                if row:
                    sqlite_stats['metadata_cache_count'] = row['cnt'] or 0

            if 'processed_files' in tables:
                for row in cur.execute(
                    "SELECT status, COUNT(*) AS cnt FROM processed_files GROUP BY status"
                ).fetchall():
                    st = (row['status'] or 'UNKNOWN').upper()
                    if st == 'NOT_FOUND':
                        st = 'NO_ISBN'
                    cnt = row['cnt'] or 0
                    sqlite_stats['status_counts'][st] = sqlite_stats['status_counts'].get(st, 0) + cnt
                    sqlite_stats['total_processed'] += cnt
                sqlite_stats['status_counts']['NOT_FOUND'] = sqlite_stats['status_counts']['NO_ISBN']

                tot_proc = sqlite_stats['total_processed']
                for st, cnt in sqlite_stats['status_counts'].items():
                    sqlite_stats['status_pcts'][st] = _pct(cnt, tot_proc)

                sqlite_stats['success_rate'] = _pct(
                    sqlite_stats['status_counts'].get('SUCCESS', 0), tot_proc
                )

                # Recent conversions / enrichments
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
                sqlite_stats['recent_conversions'] = _serialize_conversion_rows(recent_rows)

                # Needs review / Quarantined / Failed / Not found
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
                    sqlite_stats['attention_items'].append({
                        'file_name': r['file_name'] or os.path.basename(r['file_path'] or ''),
                        'file_path': r['file_path'] or '',
                        'file_ext': (r['file_ext'] or '').upper().lstrip('.'),
                        'status': r['status'] or 'UNKNOWN',
                        'isbn': r['isbn'] or '',
                        'title': r['title'] or r['orig_title'] or '',
                        'author': r['author'] or r['orig_author'] or '',
                        'match_confidence': round(float(conf) * 100) if conf is not None else None,
                        'error_message': r['error_message'] or '',
                        'updated_at': (r['updated_at'] or '')[:19].replace('T', ' '),
                    })
        except Exception as e:
            sqlite_stats['error'] = str(e)
        finally:
            if conn is not None:
                conn.close()

    # 3. Scanner Live Activity & Log Tail
    scanner_lines = _tail_log_file(scanner_log_path, max_lines=40)
    epubmeta_lines = _tail_log_file(epubmeta_log_path, max_lines=30)

    lock_exists = (scanner_dir / 'nas_scanner.lock').exists() or (scanner_dir / 'scanner.lock').exists()
    loop_lock_exists = (scanner_dir / 'nas_scanner_loop.lock').exists()

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
    now_dt = datetime.now(tz=timezone.utc)
    seconds_since_log = None
    if scanner_log_path.exists():
        try:
            mtime = scanner_log_path.stat().st_mtime
            log_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
            seconds_since_log = max(0, int((now_dt - log_dt).total_seconds()))
            log_mtime_str = log_dt.strftime('%Y-%m-%d %H:%M:%S UTC')
        except Exception:
            pass

    current_activity = 'Idle — Waiting for next scan cycle'
    current_library = ''
    is_scanning = False
    is_sleeping = False

    if scanner_lines:
        last_line = scanner_lines[-1]
        for line in reversed(scanner_lines[-5:]):
            m_int = re.search(r'Next scan in\s+([\d.]+)\s+hours', line, re.IGNORECASE)
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
        elif lock_exists or (seconds_since_log is not None and seconds_since_log < 600):
            is_scanning = True
            current_activity = last_line

        for line in reversed(scanner_lines):
            if 'Scanning library:' in line:
                current_library = line.split('Scanning library:', 1)[1].strip()
                break
            if 'Scanning:' in line:
                current_library = line.split('Scanning:', 1)[1].strip()
                break

    # Compute Next Scan timestamp & countdown
    next_scan_at = ''
    next_scan_iso = ''
    next_scan_in_seconds = None
    next_scan_countdown = ''

    if is_scanning:
        next_scan_at = f'After current scan (+{scan_interval_hours:g}h interval)'
        next_scan_countdown = 'Scanning now'
    elif log_dt is not None:
        next_scan_dt = log_dt + timedelta(hours=scan_interval_hours)
        next_scan_at = next_scan_dt.strftime('%Y-%m-%d %H:%M:%S UTC')
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
            current_activity = f'All scans finished — Next scan {next_scan_countdown} (every {scan_interval_hours:g}h)'

    max_standby_age = int(scan_interval_hours * 3600 + 1800)
    daemon_active = (
        loop_lock_exists
        or lock_exists
        or is_sleeping
        or (seconds_since_log is not None and seconds_since_log < max_standby_age)
    )

    # 4. Remaining to Catalog & Remaining to Enrich Queue Metrics
    remaining_to_catalog = 0
    catalog_sync_chunk_info = ''
    discovered_files_total = max(sqlite_stats['tracked_files_count'], total_files)
    eligible_to_enrich_initial = 0
    remaining_to_enrich = 0
    skipped_complete_bookdb = catalog_agg['complete_metadata'] or 0
    cycle_inspected = 0
    cycle_net_used = 0
    cycle_net_cap = 4000
    if config_file.exists():
        try:
            with open(config_file, 'r', encoding='utf-8') as cf:
                cfg_data = json.load(cf)
                em_cfg = cfg_data.get('epubmeta') or {}
                if em_cfg.get('max_files'):
                    cycle_net_cap = max(4000, int(em_cfg['max_files'])) if int(em_cfg['max_files']) == 2500 else int(em_cfg['max_files'])
        except Exception:
            pass

    if scanner_log_path.exists():
        try:
            with scanner_log_path.open('rb') as lf:
                lf.seek(0, os.SEEK_END)
                fsize = lf.tell()
                lf.seek(max(0, fsize - 512 * 1024), os.SEEK_SET)
                tail_text = lf.read().decode('utf-8', errors='replace')

            # Enrichment eligibility header from current/latest scan pass
            elig_matches = list(
                re.finditer(
                    r'out of (\d+) eligible:\s*(\d+) new/changed,\s*(\d+) backfill;\s*'
                    r'skipped (\d+) previously processed,\s*(\d+) complete/DRM/invalid',
                    tail_text,
                )
            )
            last_elig_pos = -1
            new_changed_in_walk = 0
            if elig_matches:
                m_el = elig_matches[-1]
                last_elig_pos = m_el.start()
                eligible_to_enrich_initial = int(m_el.group(1))
                new_changed_in_walk = int(m_el.group(2))
                skipped_complete_bookdb = int(m_el.group(5))

            # Progress inside current enrichment pass: [275/597078 | net:199/2500]
            prog_matches = list(
                re.finditer(r'\[(\d+)/(\d+)\s*\|\s*net:(\d+)/(\d+|all)\]\s*Inspecting', tail_text)
            )
            if prog_matches and prog_matches[-1].start() > last_elig_pos:
                m_pr = prog_matches[-1]
                cycle_inspected = int(m_pr.group(1))
                eligible_to_enrich_initial = int(m_pr.group(2))
                cycle_net_used = int(m_pr.group(3))
                if m_pr.group(4).isdigit() and is_scanning:
                    logged_cap = int(m_pr.group(4))
                    cycle_net_cap = 4000 if logged_cap == 2500 else logged_cap

            if eligible_to_enrich_initial > 0:
                remaining_to_enrich = max(0, eligible_to_enrich_initial - cycle_inspected)
            else:
                # Fallback estimate from tracked files minus complete & processed
                est_epubs = next(
                    (r['count'] for r in format_rows if (r.get('file_format') or '').upper() == 'EPUB'),
                    total_files,
                )
                remaining_to_enrich = max(
                    0, est_epubs - skipped_complete_bookdb - sqlite_stats['total_processed']
                )

            # Cataloging queue (files needing metadata extraction & syncing)
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

            last_parse_pos = parse_matches[-1].start() if parse_matches else -1
            last_fin_pos = fin_matches[-1].start() if fin_matches else -1

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
            pass

    cycle_net_remaining = max(0, cycle_net_cap - cycle_net_used)
    enrich_total_scope = remaining_to_enrich + sqlite_stats['total_processed']
    enrich_progress_pct = _pct(sqlite_stats['total_processed'], enrich_total_scope) if enrich_total_scope > 0 else 0.0

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

    return {
        'generated_at': now_dt.strftime('%Y-%m-%d %H:%M:%S UTC'),
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
            'scanner_log_lines': scanner_lines,
            'epubmeta_log_lines': epubmeta_lines,
        },
        'catalog': {
            'total_items': total_items,
            'total_files': total_files,
            'books_count': catalog_agg['books_count'] or 0,
            'magazines_count': catalog_agg['magazines_count'] or 0,
            'kindle_count': catalog_agg['kindle_count'] or 0,
            'audible_count': catalog_agg['audible_count'] or 0,
            'audiobook_count': catalog_agg['audiobook_count'] or 0,
            'valid_files': file_agg['valid_files'] or 0,
            'drm_files': file_agg['drm_files'] or 0,
            'invalid_files': file_agg['invalid_files'] or 0,
            'valid_files_pct': _pct(file_agg['valid_files'] or 0, total_files),
            'with_isbn': catalog_agg['with_isbn'] or 0,
            'with_isbn_pct': _pct(catalog_agg['with_isbn'] or 0, total_items),
            'with_description': catalog_agg['with_description'] or 0,
            'with_description_pct': _pct(catalog_agg['with_description'] or 0, total_items),
            'with_publisher': catalog_agg['with_publisher'] or 0,
            'with_publisher_pct': _pct(catalog_agg['with_publisher'] or 0, total_items),
            'with_cover': catalog_agg['with_cover'] or 0,
            'with_cover_pct': _pct(catalog_agg['with_cover'] or 0, total_items),
            'known_author': catalog_agg['known_author'] or 0,
            'known_author_pct': _pct(catalog_agg['known_author'] or 0, total_items),
            'complete_metadata': catalog_agg['complete_metadata'] or 0,
            'complete_metadata_pct': _pct(catalog_agg['complete_metadata'] or 0, total_items),
            'formats': format_rows,
            'libraries': library_rows,
            'recent_imports': recent_imports,
        },
        'conversion': sqlite_stats,
    }


class StatusDashboardView(TemplateView):
    """
    Dashboard page displaying live Conversion (epubmeta) & Import (MyLibrary Scanner) status.
    """
    template_name = 'catalog/status_dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['dashboard'] = get_conversion_and_import_status()
        return context


class StatusDashboardAPIView(APIView):
    """
    JSON API endpoint for live auto-refresh on the Conversion & Import Status Dashboard.
    """
    def get(self, request):
        return Response(get_conversion_and_import_status())


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
                    from .opds import _ENRICHED_PATHS_CACHE
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
                    if req_item_id:
                        cat_item = CatalogItem.objects.filter(id=req_item_id).first()
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
                                cat_item.cover_thumbnail = final_cover
                            elif 'cover' in sel and final_cover != (orig_meta.get('cover_url') or ''):
                                cat_item.cover_image_url = final_cover
                                if not cat_item.cover_thumbnail:
                                    try:
                                        from .opds import apply_book_cover_to_item
                                        apply_book_cover_to_item(cat_item, final_cover)
                                    except Exception:
                                        pass
                            elif not cat_item.cover_thumbnail and not cat_item.cover_image_url:
                                cat_item.cover_image_url = final_cover
                        cat_item.save()
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
                        if isinstance(_ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids_set'), set):
                            if cat_item.id not in _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids_set']:
                                _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids_set'].add(cat_item.id)
                                if isinstance(_ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids'), list):
                                    _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids'].append(cat_item.id)
                        from .opds import clear_book_bad_metadata, merge_duplicate_catalog_items_for_item
                        clear_book_bad_metadata(cat_item.id)
                        merged_item_ids = merge_duplicate_catalog_items_for_item(cat_item)
                except Exception:
                    merged_item_ids = []

                return Response({
                    'ok': True,
                    'action': 'approve',
                    'file_path': file_path,
                    'title': final_title,
                    'authors': final_author,
                    'publisher': final_pub,
                    'isbn': final_isbn,
                    'description': final_desc,
                    'cover_url': final_cover,
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
                f"""
                SELECT substr(updated_at, 1, {substr_len}) AS bucket,
                       status,
                       COUNT(*) AS cnt
                FROM processed_files
                WHERE updated_at IS NOT NULL AND updated_at != ''
                GROUP BY bucket, status
                ORDER BY bucket ASC
                """
            ).fetchall()

            for r in rows:
                b = (r['bucket'] or '').replace('T', ' ')
                if granularity == 'hour' and len(b) == 13:
                    b = f"{b}:00"
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
                    f"""
                    SELECT substr(updated_at, 1, {substr_len}) AS bucket,
                           COUNT(*) AS cnt
                    FROM metadata_cache
                    WHERE updated_at IS NOT NULL AND updated_at != ''
                    GROUP BY bucket
                    ORDER BY bucket ASC
                    """
                ).fetchall()
                for r in c_rows:
                    b = (r['bucket'] or '').replace('T', ' ')
                    if granularity == 'hour' and len(b) == 13:
                        b = f"{b}:00"
                    if b not in bucket_map:
                        bucket_map[b] = {k: 0 for k in statuses + ['CACHE', 'TRACKED']}
                    bucket_map[b]['CACHE'] += int(r['cnt'] or 0)
            except Exception:
                pass

            # Also include file_states (Tracked File States in SQLite) cached for 120s
            try:
                now_m = _time.monotonic()
                cache_entry = _TRACKED_HISTORY_CACHE.get(granularity)
                if cache_entry and now_m < cache_entry['expires_at']:
                    t_rows = cache_entry['rows']
                else:
                    raw_t = cur.execute(
                        f"""
                        SELECT substr(synced_at, 1, {substr_len}) AS bucket,
                               COUNT(*) AS cnt
                        FROM file_states
                        WHERE synced_at IS NOT NULL AND synced_at != ''
                        GROUP BY bucket
                        ORDER BY bucket ASC
                        """
                    ).fetchall()
                    t_rows = [(r['bucket'], int(r['cnt'] or 0)) for r in raw_t]
                    _TRACKED_HISTORY_CACHE[granularity] = {
                        'expires_at': now_m + 120.0,
                        'rows': t_rows,
                    }
                for raw_b, cnt in t_rows:
                    b = (raw_b or '').replace('T', ' ')
                    if granularity == 'hour' and len(b) == 13:
                        b = f"{b}:00"
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
            qs = qs.filter(Q(isbn__iexact=isbn_filter) | Q(asin__iexact=isbn_filter))
        if author_filter:
            qs = qs.filter(authors__icontains=author_filter)

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

        item = CatalogItem.objects.prefetch_related('files').filter(
            Q(isbn__iexact=ident) | Q(asin__iexact=ident)
        ).first()
        if item:
            return item

        loc = FileLocation.objects.select_related('catalog_item').filter(
            Q(file_hash=ident) | Q(file_path=ident) | Q(file_name__iexact=ident)
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

    def post(self, request):
        from .opds import enrich_catalog_item_on_demand, enrich_file_location_on_demand

        body = request.data or {}
        item_id = (body.get('item_id') or request.GET.get('item_id') or '').strip()
        file_id = (body.get('file_id') or request.GET.get('file_id') or '').strip()
        file_path = (body.get('file_path') or request.GET.get('file_path') or '').strip()
        force = str(body.get('force', request.GET.get('force', 'false'))).lower() in ('1', 'true', 'yes', 'force')
        re_enrich = str(body.get('re_enrich', request.GET.get('re_enrich', 'false'))).lower() in ('1', 'true', 'yes')
        hint_title = (body.get('hint_title') or request.GET.get('hint_title') or '').strip()
        hint_authors = (body.get('hint_authors') or request.GET.get('hint_authors') or '').strip()
        hint_isbn = (body.get('hint_isbn') or request.GET.get('hint_isbn') or '').strip()
        force_ai_vision = str(body.get('force_ai_vision', request.GET.get('force_ai_vision', 'false'))).lower() in ('1', 'true', 'yes')
        review_only = str(body.get('review_only', request.GET.get('review_only', 'false'))).lower() in ('1', 'true', 'yes')
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
                )
                return Response(res, status=200 if res.get('ok') else 422)

        if item_id:
            item = CatalogItem.objects.prefetch_related('files').filter(id=item_id).first()
            if not item:
                fb_file_id = (body.get('fallback_file_id') or '').strip()
                if fb_file_id:
                    fb_loc = FileLocation.objects.select_related('catalog_item').filter(id=fb_file_id).first()
                    if fb_loc and fb_loc.catalog_item:
                        item = fb_loc.catalog_item
                if not item:
                    fb_title = (body.get('title') or '').strip()
                    fb_authors = (body.get('authors') or '').strip()
                    if fb_title and fb_authors:
                        item = CatalogItem.objects.prefetch_related('files').filter(
                            title__iexact=fb_title, authors__iexact=fb_authors
                        ).first()
                    elif fb_title:
                        item = CatalogItem.objects.prefetch_related('files').filter(title__iexact=fb_title).first()
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
            )
            res['requested_item_id'] = item_id
            return Response(res, status=200 if res.get('ok') else 422)

        return Response({'ok': False, 'error': 'Please provide item_id or file_id.'}, status=400)


class RevertEnrichAPIView(APIView):
    """
    Revert an enriched or in-review `CatalogItem` / `FileLocation` back to its original
    pre-enrichment metadata (`POST /mylibrary/api/revert-enrich/`).
    """

    def post(self, request):
        from .opds import revert_catalog_item_enrichment

        body = request.data or {}
        item_id = (body.get('item_id') or request.GET.get('item_id') or '').strip()
        file_id = (body.get('file_id') or request.GET.get('file_id') or '').strip()
        file_path = (body.get('file_path') or request.GET.get('file_path') or '').strip()

        loc = None
        item = None
        if file_id:
            loc = FileLocation.objects.select_related('catalog_item').filter(id=file_id).first()
            if loc:
                item = loc.catalog_item
        if not item and item_id:
            item = CatalogItem.objects.prefetch_related('files').filter(id=item_id).first()

        if not item and not loc and not file_path:
            return Response({'ok': False, 'error': 'Please provide item_id, file_id, or file_path.'}, status=400)

        res = revert_catalog_item_enrichment(item=item, file_loc=loc, file_path=file_path)
        return Response(res, status=200 if res.get('ok') else 422)


class MoveToMagAPIView(APIView):
    """
    Move a Magazine / Periodical `CatalogItem` or `FileLocation` from `/books` to `/toMags`
    (`POST /mylibrary/api/move-to-mag/`), cleaning up `scanner_state.db` and `BookDB`.
    """

    def post(self, request):
        from .opds import move_catalog_item_to_tomags, move_file_location_to_tomags

        body = request.data or {}
        item_id = (body.get('item_id') or request.GET.get('item_id') or '').strip()
        file_id = (body.get('file_id') or request.GET.get('file_id') or '').strip()

        if file_id:
            loc = FileLocation.objects.select_related('catalog_item').filter(id=file_id).first()
            if not loc:
                return Response({'ok': False, 'error': f'FileLocation not found: {file_id}'}, status=404)
            res = move_file_location_to_tomags(loc)
            return Response(res, status=200 if res.get('ok') else 422)

        if item_id:
            item = CatalogItem.objects.prefetch_related('files').filter(id=item_id).first()
            if not item:
                return Response({'ok': False, 'error': f'CatalogItem not found: {item_id}'}, status=404)
            res = move_catalog_item_to_tomags(item, only_detected_mags=False)
            return Response(res, status=200 if res.get('ok') else 422)

        return Response({'ok': False, 'error': 'Please provide item_id or file_id.'}, status=400)


class ToggleInterestingAPIView(APIView):
    """
    Toggle or set the 'Interesting' flag for a `CatalogItem` (`POST /mylibrary/api/interesting/`).
    """

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


class CoverSearchAPIView(APIView):
    """
    Web Cover Search API (`POST /mylibrary/api/cover-search/`), modeled after Moon+ Reader's
    'Download cover from internet' feature. Queries Apple Books, Google Books, and Open Library
    in parallel and returns candidate cover thumbnails (with the book's Original Cover first unless missing).
    """

    def post(self, request):
        from .opds import search_book_covers_web

        body = request.data or {}
        query = (body.get('query') or request.GET.get('query') or '').strip()
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

    def post(self, request):
        from .opds import apply_book_cover_to_item

        body = request.data or {}
        item_id = (body.get('item_id') or '').strip()
        cover_url = (body.get('cover_url') or '').strip()
        fallback_url = (body.get('fallback_url') or '').strip()
        if not item_id or not cover_url:
            return Response({'ok': False, 'error': 'item_id and cover_url are required.'}, status=400)

        res = apply_book_cover_to_item(item_id, cover_url, fallback_url=fallback_url)
        return Response(res, status=200 if res.get('ok') else 422)


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




