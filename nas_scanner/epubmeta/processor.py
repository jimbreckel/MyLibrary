"""Core processing pipeline coordinating readers, ISBN extraction, AI, and providers."""

from __future__ import annotations
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import BookMetadata
from .readers import get_reader, BaseReader
from .isbn.validator import normalize_isbn
from .isbn.extractor import extract_isbns_from_text
from .isbn.ai_extractor import GeminiBookExtractor
from .providers.aggregator import MetadataAggregator
from .state import StateTracker, compute_file_quick_hash, normalize_cache_stem, normalize_title_author_key

logger = logging.getLogger(__name__)


def _is_text_only_cover_bytes(raw_bytes: Optional[bytes]) -> bool:
    """Return True if `raw_bytes` decodes to a plain white/monochrome text-only title page."""
    if not raw_bytes or len(raw_bytes) < 900:
        return True
    try:
        import io
        from PIL import Image

        im = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        w, h = im.size
        if w < 24 or h < 24:
            return True
        im_crop = im.crop((int(w * 0.08), int(h * 0.08), max(int(w * 0.08) + 1, int(w * 0.92)), max(int(h * 0.08) + 1, int(h * 0.92))))
        im_small = im_crop.resize((80, 112))
        px = list(im_small.get_flattened_data() if hasattr(im_small, "get_flattened_data") else im_small.getdata())
        n = len(px)
        if not n:
            return True
        white_bg = 0
        colored = 0
        dark_ink = 0
        mid_gray = 0
        buckets: Dict[Tuple[int, int, int], int] = {}
        for r, g, b in px:
            mx = max(r, g, b)
            mn = min(r, g, b)
            sat_diff = mx - mn
            if mn >= 210 and sat_diff < 20:
                white_bg += 1
            if sat_diff >= 26:
                colored += 1
            if mx <= 95:
                dark_ink += 1
            elif mx < 210 and sat_diff < 26:
                mid_gray += 1
            b_key = (r // 28, g // 28, b // 28)
            buckets[b_key] = buckets.get(b_key, 0) + 1
        white_ratio = white_bg / n
        color_ratio = colored / n
        dark_ratio = dark_ink / n
        mid_ratio = mid_gray / n
        if white_ratio >= 0.78 and color_ratio <= 0.025 and mid_ratio <= 0.16:
            return True
        if white_ratio >= 0.93 or dark_ratio >= 0.96:
            return True
        sorted_b = sorted(buckets.values(), reverse=True)
        top1_ratio = sorted_b[0] / n if sorted_b else 0.0
        top2_ratio = (sorted_b[0] + (sorted_b[1] if len(sorted_b) > 1 else 0)) / n if sorted_b else 0.0
        if top1_ratio >= 0.87 and top2_ratio >= 0.95 and len(buckets) <= 14:
            return True
        return False
    except Exception:
        return False


class BookProcessor:
    """Orchestrates end-to-end processing for a single ebook or PDF file."""

    def __init__(
        self,
        aggregator: Optional[MetadataAggregator] = None,
        ai_extractor: Optional[GeminiBookExtractor] = None,
        state_tracker: Optional[StateTracker] = None,
        quarantine_dir: Optional[Path] = None,
        backup_dir: Optional[Path] = None,
        scan_root: Optional[Path] = None,
        dry_run: bool = False,
        backup: bool = False,
        update_cover: bool = False,
        min_confidence: float = 0.50,
        pdf_require_isbn: bool = False,
        scanner_bridge: Optional[Any] = None,
        mylibrary_client: Optional[Any] = None,
    ):
        self.aggregator = aggregator or MetadataAggregator()
        self.ai_extractor = ai_extractor or GeminiBookExtractor()
        self.state_tracker = state_tracker
        self.quarantine_dir = Path(quarantine_dir).resolve() if quarantine_dir else None
        self.backup_dir = Path(backup_dir).resolve() if backup_dir else None
        self.scan_root = Path(scan_root).resolve() if scan_root else None
        self.dry_run = dry_run
        self.backup = backup
        self.update_cover = update_cover
        self.min_confidence = min_confidence
        self.pdf_require_isbn = pdf_require_isbn
        self.scanner_bridge = scanner_bridge
        self.mylibrary_client = mylibrary_client
        self._memory_cache: List[Dict[str, Any]] = []
        self._orig_hashes: Dict[str, str] = {}
        self._orig_sha256: Dict[str, str] = {}
        self._orig_sizes: Dict[str, int] = {}
        self._orig_metadata: Dict[str, BookMetadata] = {}
        self._match_scores: Dict[str, float] = {}
        self._front_texts: Dict[str, Optional[str]] = {}
        self._front_matter_isbns: set[str] = set()

    def _lookup_cache(
        self,
        orig_hash: Optional[str] = None,
        isbn: Optional[str] = None,
        norm_stem: Optional[str] = None,
        title: Optional[str] = None,
        author: Optional[str] = None,
    ) -> Optional[BookMetadata]:
        """Check SQLite state cache and in-memory run cache for duplicate/companion metadata."""
        if getattr(self, "skip_cache", False):
            return None
        if self.state_tracker:
            cached = self.state_tracker.get_cached_metadata(
                orig_hash=orig_hash,
                isbn=isbn,
                norm_stem=norm_stem,
                title=title,
                author=author,
            )
            if cached:
                return cached

        norm_ta = normalize_title_author_key(title, author) if title else ""
        for entry in reversed(self._memory_cache):
            if (
                (orig_hash and entry.get("orig_hash") == orig_hash)
                or (isbn and entry.get("isbn") == isbn)
                or (norm_stem and len(norm_stem) >= 3 and entry.get("norm_stem") == norm_stem)
                or (norm_ta and entry.get("norm_ta") == norm_ta)
            ):
                meta = BookMetadata.from_dict(entry["metadata"])
                orig_src = meta.source or "cached"
                if not orig_src.startswith("local-cache"):
                    meta.source = f"local-cache ({orig_src})"
                meta.ai_used = False
                return meta
        return None

    def _store_cache(self, file_path: Path, metadata: BookMetadata) -> None:
        """Persist resolved metadata to in-memory and SQLite caches for duplicates and companion files."""
        if not metadata or metadata.is_empty():
            return
        norm_path = str(Path(file_path).resolve())
        orig_hash = self._orig_hashes.get(norm_path) or compute_file_quick_hash(file_path)
        orig_meta = self._orig_metadata.get(norm_path)
        isbn = metadata.isbn13 or metadata.isbn10
        norm_stem = normalize_cache_stem(file_path)
        primary_author = metadata.authors[0] if metadata.authors else None
        norm_ta = normalize_title_author_key(metadata.title, primary_author)
        self._memory_cache.append(
            {
                "orig_hash": orig_hash,
                "isbn": isbn,
                "norm_stem": norm_stem,
                "norm_ta": norm_ta,
                "metadata": metadata.to_dict(),
            }
        )
        if self.state_tracker:
            self.state_tracker.save_cached_metadata(
                file_path,
                metadata,
                orig_hash=orig_hash,
                original_metadata=orig_meta,
            )

    def discover_isbn(
        self,
        reader: BaseReader,
        embedded_meta: BookMetadata,
        precomputed_ai_result: Optional[Dict[str, Any]] = None,
        excluded_isbns: Optional[set[str]] = None,
        force_ai: bool = False,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[BookMetadata]]:
        """
        Attempt to discover the ISBN through cascading strategies:
        0. Filename clues (unless excluded)
        1. Embedded metadata (unless excluded)
        2. Front matter regex extraction (unless excluded)
        3. Gemini AI analysis (text + rendered page images, or precomputed Batch API result)

        Returns: (isbn13, candidate_title, candidate_author, ai_or_cached_meta)
        """
        excluded = {x.strip().upper() for x in (excluded_isbns or set()) if x and str(x).strip()}

        # Clues from filename and embedded metadata
        from .filename_parser import parse_filename, clean_book_title
        file_clues = parse_filename(reader.file_path)
        valid_emb_authors = [
            a for a in (embedded_meta.authors or [])
            if a and a.strip().lower() not in ("unknown", "unknown author")
        ]
        emb_title = clean_book_title(embedded_meta.title)
        fn_isbn = file_clues.get("isbn")

        # Fallback candidate title and author from embedded metadata or filename
        clue_title = file_clues.get("title")
        if emb_title and clue_title:
            if (":" in clue_title or " - " in clue_title) and not (":" in emb_title or " - " in emb_title):
                fallback_title = clue_title
            elif (":" in emb_title or " - " in emb_title) and not (":" in clue_title or " - " in clue_title):
                fallback_title = emb_title
            else:
                fallback_title = clue_title if len(clue_title) > len(emb_title) else emb_title
        else:
            fallback_title = emb_title or clue_title
        fallback_author = valid_emb_authors[0] if valid_emb_authors else (file_clues["authors"][0] if file_clues["authors"] else None)

        if precomputed_ai_result is not None:
            if precomputed_ai_result:
                ai_isbn = precomputed_ai_result.get("isbn13")
                if ai_isbn and ai_isbn.upper() in excluded:
                    ai_isbn = None
                if ai_isbn:
                    self._front_matter_isbns.add(ai_isbn)
                ai_title = precomputed_ai_result.get("title") or fallback_title
                ai_authors = precomputed_ai_result.get("authors", [])
                ai_author = ai_authors[0] if ai_authors else fallback_author
                ai_pub = precomputed_ai_result.get("publisher")
                ai_year = precomputed_ai_result.get("published_year")
                ai_meta = BookMetadata(
                    title=ai_title,
                    authors=ai_authors,
                    publisher=ai_pub,
                    published_date=ai_year,
                    isbn13=ai_isbn,
                    source="gemini-ai-batch",
                    ai_used=True,
                )
                return ai_isbn, ai_title, ai_author, ai_meta
            return None, fallback_title, fallback_author, None

        if not force_ai:
            # Strategy 0 (Highest priority): Already embedded in the file's OPF/metadata (ISBN-13, ISBN-10, or Amazon ASIN)
            # Prioritized ahead of filename ISBNs because Anna's Archive filenames sometimes contain wrong/cited ISBNs.
            if embedded_meta.isbn13 and embedded_meta.isbn13.upper() not in excluded:
                logger.info("Found valid ISBN-13 in embedded metadata: %s", embedded_meta.isbn13)
                self._front_matter_isbns.add(embedded_meta.isbn13)
                return embedded_meta.isbn13, fallback_title, fallback_author, None

            if embedded_meta.isbn10:
                i13, _ = normalize_isbn(embedded_meta.isbn10)
                if i13 and i13.upper() not in excluded:
                    logger.info("Normalized embedded ISBN-10 to ISBN-13: %s", i13)
                    self._front_matter_isbns.add(i13)
                    return i13, fallback_title, fallback_author, None

            if embedded_meta.asin and embedded_meta.asin.upper() not in excluded:
                logger.info("Found valid Amazon ASIN in embedded metadata: %s", embedded_meta.asin)
                return embedded_meta.asin, fallback_title, fallback_author, None

            # Strategy 1: Clues from filename (e.g. ISBN in name)
            if fn_isbn and fn_isbn.upper() not in excluded:
                logger.info("Found valid ISBN in filename: %s", fn_isbn)
                cand_title = emb_title or file_clues["title"]
                cand_author = valid_emb_authors[0] if valid_emb_authors else (file_clues["authors"][0] if file_clues["authors"] else None)
                return fn_isbn, cand_title, cand_author, None

        # Strategy 2: Extract text from front pages and scan for ISBNs or ASINs
        norm_file_key = str(reader.file_path.resolve())
        if norm_file_key in self._front_texts:
            front_text = self._front_texts[norm_file_key]
        else:
            logger.debug("Extracting front text from %s...", reader.file_path.name)
            front_text = reader.extract_front_text(max_pages=15)
            self._front_texts[norm_file_key] = front_text

        if front_text and not force_ai:
            extracted_isbns = extract_isbns_from_text(front_text)
            for cand_isbn, label in extracted_isbns:
                if cand_isbn.upper() not in excluded:
                    logger.info("Extracted valid ISBN (%s) via text regex: %s", label, cand_isbn)
                    self._front_matter_isbns.add(cand_isbn)
                    return cand_isbn, fallback_title, fallback_author, None
            import re as _re
            m_asin = _re.search(r"\bASIN\s*[:#]?\s*(B[0-9A-Z]{9})\b", front_text, _re.IGNORECASE)
            if m_asin:
                found_asin = m_asin.group(1).upper()
                if found_asin not in excluded:
                    logger.info("Extracted valid Amazon ASIN via text regex: %s", found_asin)
                    self._front_matter_isbns.add(found_asin)
                    return found_asin, fallback_title, fallback_author, None

        # Check duplicate / cross-format cache BEFORE invoking Gemini AI
        if not force_ai and not excluded:
            orig_hash = self._orig_hashes.get(norm_file_key)
            norm_stem = normalize_cache_stem(reader.file_path)
            pre_ai_cached = self._lookup_cache(
                orig_hash=orig_hash,
                norm_stem=norm_stem,
                title=fallback_title,
                author=fallback_author,
            )
            if pre_ai_cached and not pre_ai_cached.is_empty():
                logger.info("Reusing cached metadata for '%s' (skipping Gemini AI & catalog lookups).", reader.file_path.name)
                return pre_ai_cached.isbn13 or pre_ai_cached.isbn10, fallback_title, fallback_author, pre_ai_cached

        # In Batch Mode, defer Gemini AI inspection until after checking if title/author already yields a confident catalog match
        if self.ai_extractor and getattr(self.ai_extractor, "batch_mode", False):
            return None, fallback_title, fallback_author, None

        # Strategy 3: Synchronous AI-based document inspection (Gemini)
        if self.ai_extractor and self.ai_extractor.is_available:
            logger.info("Invoking Gemini AI to inspect front matter for %s...", reader.file_path.name)
            images: List[bytes] = []
            if not front_text or len(front_text.strip()) < 100:
                logger.debug("Front text is sparse; rendering front pages for AI vision...")
                images = reader.extract_front_images(max_pages=3)

            from .exceptions import RateLimitExceededError
            try:
                ai_result = self.ai_extractor.extract(text=front_text, images=images)
            except RateLimitExceededError as exc:
                logger.warning(
                    "Gemini AI rate/quota limit reached (%s); disabling Gemini fallback for the rest of this cycle and continuing with catalog/cache lookup.",
                    exc,
                )
                self.ai_extractor = None
                ai_result = None
            if ai_result:
                ai_isbn = ai_result.get("isbn13")
                if ai_isbn and ai_isbn.upper() in excluded:
                    ai_isbn = None
                if ai_isbn:
                    self._front_matter_isbns.add(ai_isbn)
                ai_title = ai_result.get("title") or fallback_title
                ai_authors = ai_result.get("authors", [])
                ai_author = ai_authors[0] if ai_authors else fallback_author
                ai_pub = ai_result.get("publisher")
                ai_year = ai_result.get("published_year")

                ai_meta = BookMetadata(
                    title=ai_title,
                    authors=ai_authors,
                    publisher=ai_pub,
                    published_date=ai_year,
                    isbn13=ai_isbn,
                    source="gemini-ai",
                    ai_used=True,
                )

                if ai_isbn:
                    logger.info("Gemini AI successfully identified ISBN: %s", ai_isbn)
                    return ai_isbn, ai_title, ai_author, ai_meta

                if ai_title:
                    return None, ai_title, ai_author, ai_meta

        return None, fallback_title, fallback_author, None

    def _maybe_queue_for_ai_batch(
        self,
        file_path: Path,
        embedded_meta: BookMetadata,
        candidate_title: Optional[str],
        candidate_author: Optional[str],
        precomputed_ai_result: Optional[Dict[str, Any]],
        reader: Optional[BaseReader] = None,
    ) -> bool:
        """If batch_mode is active and front_text is available, queue this file for Gemini Batch API."""
        if precomputed_ai_result is not None:
            return False
        if not (
            self.ai_extractor
            and getattr(self.ai_extractor, "batch_mode", False)
            and self.ai_extractor.api_key
            and self.state_tracker
            and hasattr(self.state_tracker, "queue_ai_batch_item")
        ):
            return False
        norm_key = str(file_path.resolve())
        front_text = self._front_texts.get(norm_key)
        if front_text is None and reader is not None:
            try:
                front_text = reader.extract_front_text(max_pages=15)
                self._front_texts[norm_key] = front_text
            except Exception:
                front_text = None
        if not front_text or len(front_text.strip()) < 40:
            return False
        prompt = self.ai_extractor.build_prompt_for_text(front_text)
        if not prompt:
            return False
        self.state_tracker.queue_ai_batch_item(
            file_path=file_path,
            prompt_text=prompt,
            fallback_title=candidate_title,
            fallback_author=candidate_author,
            original_metadata=embedded_meta,
        )
        return True

    def prepare_metadata(
        self,
        file_path: Path,
        precomputed_ai_result: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[BaseReader], Optional[BookMetadata], Optional[BookMetadata], Optional[str], Optional[Any], Optional[str]]:
        """
        Inspect file, discover ISBN, query providers, and generate target metadata.
        If a filename or embedded ISBN fails or yields a low-confidence match (e.g. 0% match due to
        an inaccurate filename ISBN), automatically falls back to inspecting the book's front-matter
        text and Gemini AI before giving up.
        Returns: (reader, embedded_meta, target_meta, isbn, match_result, error_message)
        """
        file_path = Path(file_path).resolve()
        try:
            orig_hash = compute_file_quick_hash(file_path)
            self._orig_hashes[str(file_path)] = orig_hash
            self._orig_sizes[str(file_path)] = file_path.stat().st_size
            if self.scanner_bridge:
                cached_sha = self.scanner_bridge.get_cached_sha256(file_path)
                if cached_sha:
                    self._orig_sha256[str(file_path)] = cached_sha
            norm_stem = normalize_cache_stem(file_path)

            reader = get_reader(file_path)
            embedded_meta = reader.read_metadata()
            self._orig_metadata[str(file_path)] = embedded_meta

            from .matcher import calculate_match_confidence
            from .filename_parser import parse_filename
            import re as _re_t

            fn_info = parse_filename(file_path.name)
            is_pdf = file_path.suffix.lower() == ".pdf"

            excluded_isbns: set[str] = set()
            force_ai = False
            best_target_meta: Optional[BookMetadata] = None
            best_isbn: Optional[str] = None
            best_match_result: Optional[Any] = None
            last_candidate_title: Optional[str] = None
            last_candidate_author: Optional[str] = None

            for attempt in range(4):
                isbn, candidate_title, candidate_author, ai_or_cached_meta = self.discover_isbn(
                    reader,
                    embedded_meta,
                    precomputed_ai_result=precomputed_ai_result,
                    excluded_isbns=excluded_isbns,
                    force_ai=force_ai,
                )
                last_candidate_title = candidate_title or last_candidate_title
                last_candidate_author = candidate_author or last_candidate_author

                # Check local duplicate/cross-format cache before hitting external providers
                fetched_meta: Optional[BookMetadata] = None
                if ai_or_cached_meta and ai_or_cached_meta.source and ai_or_cached_meta.source.startswith("local-cache"):
                    fetched_meta = ai_or_cached_meta
                    ai_meta = None
                else:
                    ai_meta = ai_or_cached_meta
                    if not excluded_isbns and not force_ai:
                        cached_meta = self._lookup_cache(
                            orig_hash=orig_hash,
                            isbn=isbn,
                            norm_stem=norm_stem,
                            title=candidate_title,
                            author=candidate_author,
                        )
                        if cached_meta and not cached_meta.is_empty():
                            logger.info("Cache hit for '%s' (ISBN=%s) — reusing local cached metadata.", file_path.name, isbn)
                            fetched_meta = cached_meta

                # Fetch rich metadata from external providers if not in local cache
                if not fetched_meta or fetched_meta.is_empty():
                    if isbn:
                        fetched_meta = self.aggregator.fetch_by_isbn(isbn)
                        if not fetched_meta or fetched_meta.is_empty():
                            # This ISBN wasn't found in any catalog; if it came from filename/OPF, reject it and inspect book content
                            if isbn.upper() not in excluded_isbns and str(file_path) not in self._front_texts:
                                logger.info(
                                    "ISBN %s not found in catalog providers; inspecting book front-matter content for %s...",
                                    isbn,
                                    file_path.name,
                                )
                                excluded_isbns.add(isbn.upper())
                                continue

                    if is_pdf and self.pdf_require_isbn and not isbn and (not fetched_meta or fetched_meta.is_empty()):
                        break

                    # Fallback: if ISBN lookup had no match, search by title and author
                    if (not fetched_meta or fetched_meta.is_empty()) and candidate_title and not (is_pdf and self.pdf_require_isbn):
                        logger.info("ISBN lookup yielded no results; searching by title '%s'...", candidate_title)
                        fallback_meta = self.aggregator.fetch_by_query(candidate_title, candidate_author)
                        if fallback_meta and not fallback_meta.is_empty():
                            fetched_meta = fallback_meta
                        elif ":" in candidate_title:
                            short_title = candidate_title.split(":", 1)[0].strip()
                            logger.info("Searching with short title '%s'...", short_title)
                            short_meta = self.aggregator.fetch_by_query(short_title, candidate_author)
                            if short_meta and not short_meta.is_empty():
                                fetched_meta = short_meta

                    # Fallback 2: if external search had no results, use Gemini AI discovered metadata
                    if (not fetched_meta or fetched_meta.is_empty()) and ai_meta and not ai_meta.is_empty():
                        logger.info("External providers have no match; using Gemini AI metadata for %s", file_path.name)
                        fetched_meta = ai_meta

                if not fetched_meta or fetched_meta.is_empty():
                    if isbn and isbn.upper() not in excluded_isbns:
                        excluded_isbns.add(isbn.upper())
                        continue
                    if (
                        not force_ai
                        and not ai_meta
                        and precomputed_ai_result is None
                        and self.ai_extractor
                        and self.ai_extractor.is_available
                        and not getattr(self.ai_extractor, "batch_mode", False)
                    ):
                        force_ai = True
                        continue
                    break

                # Merge fetched metadata with existing file metadata
                if fetched_meta.title and _re_t.match(r"^untitled\b", fetched_meta.title.strip(), _re_t.IGNORECASE):
                    if embedded_meta.title and not _re_t.match(r"^untitled\b", embedded_meta.title.strip(), _re_t.IGNORECASE):
                        fetched_meta.title = embedded_meta.title
                    elif candidate_title and not _re_t.match(r"^untitled\b", candidate_title.strip(), _re_t.IGNORECASE):
                        fetched_meta.title = candidate_title

                # Pre-publication / working-title ISBN check (e.g., Manning MEAP working title 'Domain-Specific Languages Made Easy'
                # registered under ISBN 9781617296475 before final publication as 'Building User-Friendly DSLs'):
                # If the catalog record matches the book's embedded ISBN AND author (>= 80%), but the catalog title differs
                # while the embedded OPF title and filename title agree (>= 80%), try a title+author query for the final title
                # and preserve the book's final published title & newer publication date.
                from .matcher import score_title, score_authors
                emb_isbn_match = bool(
                    isbn
                    and embedded_meta.isbn13
                    and isbn.strip() == embedded_meta.isbn13.strip()
                )
                if (
                    emb_isbn_match
                    and embedded_meta.title
                    and fetched_meta.title
                    and score_title(fetched_meta.title, embedded_meta.title) < 0.50
                    and score_authors(fetched_meta.authors or [], embedded_meta.authors or []) >= 0.80
                    and (not fn_info.get("title") or score_title(embedded_meta.title, fn_info.get("title")) >= 0.75)
                ):
                    logger.info(
                        "ISBN %s has matching author (%s) but catalog working title '%s' differs from verified book title '%s'; preserving book title.",
                        isbn,
                        fetched_meta.authors,
                        fetched_meta.title,
                        embedded_meta.title,
                    )
                    try:
                        q_meta = self.aggregator.fetch_by_query(
                            embedded_meta.title,
                            embedded_meta.authors[0] if embedded_meta.authors else candidate_author,
                        )
                        if q_meta and not q_meta.is_empty() and score_title(q_meta.title, embedded_meta.title) >= 0.75:
                            fetched_meta = q_meta.merge_with(fetched_meta, combine_lists=True)
                    except Exception:
                        pass
                    fetched_meta.title = embedded_meta.title
                    if embedded_meta.published_date and (
                        not fetched_meta.published_date
                        or str(embedded_meta.published_date)[:4] > str(fetched_meta.published_date)[:4]
                    ):
                        fetched_meta.published_date = embedded_meta.published_date

                target_meta = fetched_meta.merge_with(embedded_meta, combine_lists=False)

                if ai_meta and (ai_meta.isbn13 == isbn or fetched_meta == ai_meta or force_ai):
                    target_meta.ai_used = True

                # Preserve subtitle: if target_meta.title is just the short title but clue/filename has full subtitle
                if target_meta.title and candidate_title:
                    t_lower = target_meta.title.strip().lower()
                    c_lower = candidate_title.strip().lower()
                    if c_lower.startswith(t_lower) and len(candidate_title) > len(target_meta.title):
                        target_meta.title = candidate_title

                was_cached = bool(target_meta.source and target_meta.source.startswith("local-cache"))
                from_front_matter = bool(
                    (isbn and isbn in self._front_matter_isbns)
                    or was_cached
                )
                match_result = calculate_match_confidence(
                    candidate=target_meta,
                    filename_info=fn_info,
                    existing_meta=embedded_meta,
                    isbn_matched_in_front_matter=from_front_matter,
                    min_confidence=self.min_confidence,
                    content_meta=ai_meta,
                )

                if best_match_result is None or match_result.score > best_match_result.score:
                    best_target_meta = target_meta
                    best_isbn = isbn
                    best_match_result = match_result
                    self._match_scores[str(file_path)] = match_result.score

                if match_result.is_confident:
                    # If the confident match (e.g., from a specific edition ISBN) is missing synopsis, publisher, or cover,
                    # supplement missing fields via a title+author query or the next front-matter ISBN before finishing.
                    if (
                        not best_target_meta.description
                        or not best_target_meta.publisher
                        or not best_target_meta.cover_url
                        or not best_target_meta.subjects
                    ):
                        q_title = best_target_meta.title or candidate_title
                        q_author = (best_target_meta.authors[0] if best_target_meta.authors else candidate_author)
                        if q_title:
                            try:
                                supp_meta = self.aggregator.fetch_by_query(q_title, q_author)
                                if supp_meta and not supp_meta.is_empty():
                                    best_target_meta = best_target_meta.merge_with(supp_meta, combine_lists=True)
                            except Exception as supp_err:
                                logger.debug("Supplemental query lookup failed: %s", supp_err)
                    break

                # Low confidence (e.g. 0% match because filename/OPF ISBN is inaccurate):
                # Reject that ISBN and inspect actual book front-matter content / Gemini AI before giving up!
                if isbn and isbn.upper() not in excluded_isbns:
                    logger.info(
                        "ISBN %s yielded low match confidence (%d%% < %d%%) for '%s'; rejecting ISBN and inspecting book content...",
                        isbn,
                        match_result.percentage,
                        int(self.min_confidence * 100),
                        file_path.name,
                    )
                    excluded_isbns.add(isbn.upper())
                    continue

                if (
                    not force_ai
                    and not ai_meta
                    and precomputed_ai_result is None
                    and self.ai_extractor
                    and self.ai_extractor.is_available
                    and not getattr(self.ai_extractor, "batch_mode", False)
                ):
                    logger.info(
                        "Catalog lookup yielded low confidence (%d%%) for '%s'; invoking Gemini AI on book content...",
                        match_result.percentage,
                        file_path.name,
                    )
                    force_ai = True
                    continue

                break

            # If all ISBN attempts yielded no result or a low-confidence match (e.g., an inaccurate filename/cited ISBN
            # matched a different book by the same author), try a direct Title + Author catalog search before giving up!
            if (
                (best_match_result is None or not best_match_result.is_confident)
                and last_candidate_title
                and not (is_pdf and self.pdf_require_isbn)
            ):
                logger.info(
                    "ISBN attempts did not yield a confident match (best=%s); searching catalog by title '%s' (author='%s')...",
                    f"{best_match_result.percentage}%" if best_match_result else "none",
                    last_candidate_title,
                    last_candidate_author,
                )
                query_meta = self.aggregator.fetch_by_query(last_candidate_title, last_candidate_author)
                if (not query_meta or query_meta.is_empty()) and ":" in last_candidate_title:
                    short_t = last_candidate_title.split(":", 1)[0].strip()
                    query_meta = self.aggregator.fetch_by_query(short_t, last_candidate_author)
                if query_meta and not query_meta.is_empty():
                    q_target = query_meta.merge_with(embedded_meta, combine_lists=False)
                    q_match = calculate_match_confidence(
                        candidate=q_target,
                        filename_info=fn_info,
                        existing_meta=embedded_meta,
                        isbn_matched_in_front_matter=False,
                        min_confidence=self.min_confidence,
                    )
                    if best_match_result is None or q_match.score > best_match_result.score:
                        best_target_meta = q_target
                        best_isbn = q_target.isbn13 or q_target.isbn10 or best_isbn
                        best_match_result = q_match
                        self._match_scores[str(file_path)] = q_match.score

            # Guard against replacing a book's valid embedded metadata (title + authors + ISBN or publisher/description)
            # with a low-confidence match from a cited 'About the Author'/'Also By' or wrong filename ISBN
            # (e.g. 'Data Fabric and Data Mesh Approaches with AI', 'Helidon' by Nikki Mottram, or 'Java Basics' by Madison Giroux).
            from .matcher import score_title
            emb_has_rich_meta = bool(
                embedded_meta.isbn13
                or (embedded_meta.description and len(embedded_meta.description.strip()) >= 30)
                or (embedded_meta.publisher and embedded_meta.published_date)
            )
            if (
                (best_match_result is None or not best_match_result.is_confident or best_match_result.title_score < 0.50)
                and embedded_meta.title
                and embedded_meta.authors
                and emb_has_rich_meta
                and (not fn_info.get("title") or score_title(embedded_meta.title, fn_info.get("title")) >= 0.70)
            ):
                logger.info(
                    "External catalogs did not match '%s' (ISBN %s); preserving verified embedded OPF metadata instead of low-confidence candidate '%s'.",
                    embedded_meta.title,
                    embedded_meta.isbn13 or "none",
                    best_target_meta.title if best_target_meta else "none",
                )
                verified_self_meta = BookMetadata(
                    title=embedded_meta.title,
                    authors=list(embedded_meta.authors),
                    publisher=embedded_meta.publisher,
                    published_date=embedded_meta.published_date,
                    isbn13=embedded_meta.isbn13,
                    isbn10=embedded_meta.isbn10,
                    description=embedded_meta.description,
                    subjects=list(embedded_meta.subjects or []),
                    language=embedded_meta.language,
                    source="embedded-opf-verified",
                )
                # If description is missing and synchronous Gemini AI is available, extract synopsis from front matter
                if (
                    not verified_self_meta.description
                    and self.ai_extractor
                    and self.ai_extractor.is_available
                    and not getattr(self.ai_extractor, "batch_mode", False)
                ):
                    norm_key = str(file_path.resolve())
                    ft = self._front_texts.get(norm_key) or reader.extract_front_text(max_pages=15)
                    if ft:
                        try:
                            ai_res = self.ai_extractor.extract(text=ft, file_path_hint=str(file_path))
                            if ai_res:
                                if ai_res.get("description"):
                                    verified_self_meta.description = ai_res["description"]
                                if ai_res.get("subjects") and not verified_self_meta.subjects:
                                    verified_self_meta.subjects = ai_res["subjects"]
                                if ai_res.get("published_year") and not verified_self_meta.published_date:
                                    verified_self_meta.published_date = str(ai_res["published_year"])
                        except Exception:
                            pass
                best_target_meta = verified_self_meta
                best_isbn = embedded_meta.isbn13
                best_match_result = calculate_match_confidence(
                    candidate=best_target_meta,
                    filename_info=fn_info,
                    existing_meta=embedded_meta,
                    isbn_matched_in_front_matter=True,
                    min_confidence=self.min_confidence,
                )
                self._match_scores[str(file_path)] = best_match_result.score

            if not best_target_meta or best_target_meta.is_empty():
                if self._maybe_queue_for_ai_batch(
                    file_path,
                    embedded_meta,
                    last_candidate_title,
                    last_candidate_author,
                    precomputed_ai_result,
                    reader=reader,
                ):
                    msg = f"Queued for Gemini Batch API (50% cost reduction): {file_path.name}"
                    return reader, embedded_meta, None, None, None, msg
                if is_pdf and self.pdf_require_isbn:
                    msg = f"Skipped PDF without verifiable ISBN (--pdf-require-isbn): {file_path.name}"
                    logger.info(msg)
                    return reader, embedded_meta, None, None, None, msg
                msg = f"No metadata found for {file_path.name}"
                return reader, embedded_meta, None, best_isbn, None, msg

            if best_match_result and not best_match_result.is_confident:
                if self._maybe_queue_for_ai_batch(
                    file_path,
                    embedded_meta,
                    last_candidate_title,
                    last_candidate_author,
                    precomputed_ai_result,
                    reader=reader,
                ):
                    msg = f"Queued for Gemini Batch API (50% cost reduction): {file_path.name}"
                    return reader, embedded_meta, None, None, None, msg

            # Download cover image for the winning target_meta if available
            if best_target_meta.cover_url and not best_target_meta.cover_bytes:
                try:
                    import requests
                    logger.info("Downloading cover image from %s...", best_target_meta.cover_url)
                    resp = requests.get(best_target_meta.cover_url, timeout=10, headers={"User-Agent": "epubmeta/1.0"})
                    if resp.status_code == 200 and len(resp.content) > 1500:
                        best_target_meta.cover_bytes = resp.content
                        logger.info("Downloaded cover image (%d bytes)", len(resp.content))
                except Exception as cover_err:
                    logger.debug("Failed to download cover image: %s", cover_err)

            return reader, embedded_meta, best_target_meta, best_isbn, best_match_result, None

        except Exception as exc:
            import shutil
            import zipfile
            from .exceptions import DrmProtectedError, InvalidDocumentError, RateLimitExceededError

            if isinstance(exc, RateLimitExceededError):
                raise

            if isinstance(exc, DrmProtectedError):
                msg = f"Skipped DRM-protected file: {exc}"
                logger.info(msg)
                if self.state_tracker:
                    self.state_tracker.record_failure(file_path, msg, status="HAS_DRM")
                return None, None, None, None, None, msg

            is_corrupt = isinstance(exc, (InvalidDocumentError, zipfile.BadZipFile))

            if is_corrupt and self.quarantine_dir:
                from .file_utils import resolve_mirrored_destination
                dest = resolve_mirrored_destination(file_path, self.quarantine_dir, self.scan_root)
                if self.dry_run:
                    msg = f"[DRY RUN] Invalid document ({exc}). Would move to quarantine: {dest}"
                    logger.warning(msg)
                    if self.state_tracker:
                        self.state_tracker.record_failure(file_path, msg, status="QUARANTINED")
                    return None, None, None, None, None, msg
                else:
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        from .file_utils import get_file_timestamps, restore_file_timestamps
                        orig_ts = get_file_timestamps(file_path)
                        shutil.move(str(file_path), str(dest))
                        restore_file_timestamps(dest, orig_ts)
                        msg = f"Invalid/corrupt file moved to quarantine: {dest}"
                        logger.warning(msg)
                        if self.state_tracker:
                            self.state_tracker.record_failure(file_path, str(exc), status="QUARANTINED")
                        return None, None, None, None, None, msg
                    except Exception as move_err:
                        logger.error("Failed moving %s to quarantine: %s", file_path.name, move_err)

            msg = f"Error inspecting {file_path.name}: {exc}"
            logger.exception(msg)
            return None, None, None, None, None, msg

    def compute_match_confidence(self, existing_meta: BookMetadata, candidate: BookMetadata):
        from .matcher import calculate_match_confidence
        return calculate_match_confidence(
            candidate=candidate,
            filename_info={},
            existing_meta=existing_meta,
            min_confidence=self.min_confidence,
        )

    def apply_metadata(
        self,
        file_path: Path,
        reader: BaseReader,
        target_meta: BookMetadata,
        isbn: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Apply target metadata to file and update state tracker."""
        file_path = Path(file_path).resolve()
        author_str = ", ".join(target_meta.authors) if target_meta.authors else None
        result: Dict[str, Any] = {
            "file": str(file_path),
            "status": "FAILED",
            "isbn": target_meta.isbn13 or isbn,
            "title": target_meta.title,
            "author": author_str,
            "message": "",
        }

        try:
            self._store_cache(file_path, target_meta)
            if self.dry_run:
                result["status"] = "DRY_RUN"
                result["message"] = (
                    f"[DRY RUN] Would update: Title='{target_meta.title}', "
                    f"Authors={target_meta.authors}, Tags={len(target_meta.subjects)}, "
                    f"DescLen={len(target_meta.description or '')}"
                )
                logger.info(result["message"])
            else:
                from .external_dbs import compute_full_sha256, generate_cover_thumbnail
                from .file_utils import resolve_mirrored_destination
                from .readers.epub import EpubReader
                norm_key = str(file_path)
                needs_db_sync = bool(self.state_tracker or self.scanner_bridge or self.mylibrary_client)
                orig_sha256 = (
                    (self._orig_sha256.get(norm_key) or compute_full_sha256(file_path))
                    if needs_db_sync
                    else None
                )
                orig_meta = self._orig_metadata.get(norm_key)

                # Check if embedded metadata already matches target_meta so we don't rewrite unchanged files
                unchanged = (
                    not self.update_cover
                    and orig_meta is not None
                    and (orig_meta.title or "") == (target_meta.title or "")
                    and (orig_meta.authors or []) == (target_meta.authors or [])
                    and (orig_meta.isbn13 or "") == (target_meta.isbn13 or "")
                    and (orig_meta.publisher or "") == (target_meta.publisher or "")
                    and (orig_meta.description or "") == (target_meta.description or "")
                    and (orig_meta.subjects or []) == (target_meta.subjects or [])
                )

                if unchanged:
                    new_sha256 = orig_sha256
                    result["status"] = "SUCCESS"
                    result["unchanged"] = True
                    result["message"] = f"Metadata already up-to-date for {file_path.name} (no disk write needed)"
                    logger.info(result["message"])
                else:
                    backup_dest = (
                        resolve_mirrored_destination(file_path, self.backup_dir, self.scan_root)
                        if (self.backup and self.backup_dir)
                        else None
                    )
                    try:
                        if isinstance(reader, EpubReader):
                            reader.write_metadata(
                                target_meta,
                                backup=self.backup,
                                update_cover=self.update_cover,
                                backup_path=backup_dest,
                            )
                        else:
                            reader.write_metadata(
                                target_meta,
                                backup=self.backup,
                                backup_path=backup_dest,
                            )
                        new_sha256 = compute_full_sha256(file_path) if needs_db_sync else None
                        result["status"] = "SUCCESS"
                        result["unchanged"] = False
                        result["message"] = f"Successfully updated metadata for {file_path.name}"
                        logger.info(result["message"])
                    except Exception as write_exc:
                        logger.warning(
                            "Embedded file write skipped for %s (%s); saving enriched metadata to database only",
                            file_path.name,
                            write_exc,
                        )
                        new_sha256 = orig_sha256
                        result["status"] = "SUCCESS"
                        result["unchanged"] = True
                        result["message"] = (
                            f"Enriched catalog metadata for {file_path.name} "
                            f"(embedded archive write skipped: {write_exc})"
                        )

                cover_thumbnail = None
                if needs_db_sync:
                    orig_cover_bytes = None
                    try:
                        orig_cover_bytes = reader.extract_cover_bytes()
                    except Exception:
                        orig_cover_bytes = None
                    if orig_cover_bytes and len(orig_cover_bytes) >= 1000:
                        if target_meta.cover_bytes and len(target_meta.cover_bytes) >= 1000 and _is_text_only_cover_bytes(orig_cover_bytes) and not _is_text_only_cover_bytes(target_meta.cover_bytes):
                            cover_bytes = target_meta.cover_bytes
                        else:
                            cover_bytes = orig_cover_bytes
                    else:
                        cover_bytes = target_meta.cover_bytes or orig_cover_bytes
                    cover_thumbnail = generate_cover_thumbnail(cover_bytes)

                result["file_sha256"] = new_sha256
                result["orig_sha256"] = orig_sha256
                result["cover_thumbnail"] = cover_thumbnail
                result["metadata"] = target_meta

                if self.state_tracker:
                    self.state_tracker.record_success(
                        file_path=file_path,
                        isbn=target_meta.isbn13 or isbn,
                        title=target_meta.title,
                        author=author_str,
                        orig_hash=self._orig_hashes.get(norm_key),
                        orig_sha256=orig_sha256,
                        orig_size=self._orig_sizes.get(norm_key),
                        file_sha256=new_sha256,
                        original_metadata=self._orig_metadata.get(norm_key),
                        updated_metadata=target_meta,
                        match_confidence=self._match_scores.get(norm_key),
                    )
                if self.scanner_bridge and new_sha256:
                    self.scanner_bridge.update_file_state(file_path, sha256_hash=new_sha256)
                if self.mylibrary_client and new_sha256:
                    pushed = self.mylibrary_client.push_updated_file(
                        file_path=file_path,
                        metadata=target_meta,
                        file_sha256=new_sha256,
                        orig_sha256=orig_sha256,
                        cover_thumbnail=cover_thumbnail,
                    )
                    result["pushed_to_mylibrary"] = pushed
            return result
        except Exception as exc:
            msg = f"Error updating {file_path.name}: {exc}"
            logger.exception(msg)
            result["status"] = "FAILED"
            result["message"] = msg
            if self.state_tracker:
                norm_key = str(file_path)
                self.state_tracker.record_failure(
                    file_path,
                    str(exc),
                    status="FAILED",
                    original_metadata=self._orig_metadata.get(norm_key),
                    candidate_metadata=target_meta,
                    match_confidence=self._match_scores.get(norm_key),
                )
            return result

    def process_file(self, file_path: Path) -> Dict[str, Any]:
        """Process a single book file automatically."""
        file_path = Path(file_path).resolve()
        reader, embedded_meta, target_meta, isbn, match_result, err = self.prepare_metadata(file_path)

        if not target_meta or not reader:
            msg = err or f"No metadata found for {file_path.name}"
            if err and "quarantine" in err.lower():
                status = "QUARANTINED"
            elif err and "drm-protected" in err.lower():
                status = "HAS_DRM"
            else:
                status = "NOT_FOUND"
            if self.state_tracker and status not in ("QUARANTINED", "HAS_DRM"):
                self.state_tracker.record_failure(
                    file_path,
                    msg,
                    status=status,
                    original_metadata=embedded_meta,
                )
            return {
                "file": str(file_path),
                "status": status,
                "isbn": isbn,
                "title": None,
                "author": None,
                "message": msg,
            }

        # Check match confidence threshold
        if match_result and not match_result.is_confident:
            msg = (
                f"Low match confidence ({match_result.percentage}% < {int(self.min_confidence * 100)}%). "
                f"Candidate ('{target_meta.title}' by {target_meta.authors}) differs significantly from file clues. Deferring for manual checking."
            )
            logger.warning(msg)
            if self.state_tracker:
                self.state_tracker.record_failure(
                    file_path,
                    msg,
                    status="NEEDS_REVIEW",
                    original_metadata=embedded_meta,
                    candidate_metadata=target_meta,
                    match_confidence=match_result.score,
                )
            return {
                "file": str(file_path),
                "status": "DEFERRED",
                "isbn": isbn,
                "title": target_meta.title,
                "author": ", ".join(target_meta.authors) if target_meta.authors else None,
                "message": msg,
            }

        return self.apply_metadata(file_path, reader, target_meta, isbn)
