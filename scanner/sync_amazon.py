#!/usr/bin/env python3
"""
MyLibrary Direct Automated Amazon Kindle, Kindle+ & Audible Sync (`scanner/sync_amazon.py`)
===========================================================================================
Automatically fetches:
  1. Purchased Amazon Kindle books & Kindle+ (Kindle Unlimited / Prime Reading) titles
     directly from `https://read.amazon.com/kindle-library` (including reading progress % and recency).
  2. Amazon Kindle Bookmarks, Highlights & Notes from `https://read.amazon.com/notebook`
     (including last-read/annotated dates).
  3. Purchased Audible Audiobooks & Audible Plus titles from `https://www.audible.com/library/titles`
     (or via the `audible` API).

Uses a persistent local browser profile (`.amazon_session_profile`) via Playwright (Microsoft Edge / Chrome)
so you only log in to Amazon once; subsequent refreshes run automatically.

Usage:
  python scanner/sync_amazon.py                  # Sync both Kindle (books + bookmarks) & Audible
  python scanner/sync_amazon.py kindle           # Sync Kindle Cloud Library & Kindle Notebook bookmarks
  python scanner/sync_amazon.py audible          # Sync Audible library
  python scanner/sync_amazon.py import-file FILE # Import from a saved JSON/CSV file
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

DEFAULT_SERVER_URL = os.environ.get("MYLIBRARY_URL", "http://192.168.1.43:8080/mylibrary").rstrip("/")
DEFAULT_API_TOKEN = os.environ.get("SCANNER_API_TOKEN", "super-secret-sync-token-12345")
PROFILE_DIR = Path(__file__).resolve().parent.parent / ".amazon_session_profile"
EXPORT_JSON_PATH = Path(__file__).resolve().parent.parent / "amazon_library_export.json"


def _clean_author(raw: Any) -> str:
    if isinstance(raw, list):
        cleaned = [str(a).strip().rstrip(":").strip() for a in raw if a]
        return ", ".join(c for c in cleaned if c) or "Unknown Author"
    s = str(raw or "").strip().rstrip(":").strip()
    return s or "Unknown Author"


def _classify_kindle_origin(origin_type: str) -> Dict[str, Any]:
    """
    Map Amazon's `originType` from `read.amazon.com/kindle-library/search` into
    clear user-facing badges for Purchased on Amazon vs Kindle+ (Kindle Unlimited / Prime).
    """
    ot = (origin_type or "PURCHASE").strip().upper()
    if ot in ("KINDLE_UNLIMITED", "KU"):
        return {
            "origin_type": "KINDLE_UNLIMITED",
            "is_kindle_plus": True,
            "badge_label": "Kindle+ (Kindle Unlimited)",
            "short_badge": "Kindle+",
        }
    if ot in ("PRIME", "PRIME_READING", "KOLL"):
        return {
            "origin_type": "PRIME_READING",
            "is_kindle_plus": True,
            "badge_label": "Kindle+ (Prime Reading)",
            "short_badge": "Kindle+",
        }
    if ot in ("COMIXOLOGY", "COMIXOLOGY_UNLIMITED"):
        return {
            "origin_type": "COMIXOLOGY",
            "is_kindle_plus": True,
            "badge_label": "Kindle+ (ComiXology)",
            "short_badge": "Kindle+",
        }
    if ot == "SAMPLE":
        return {
            "origin_type": "SAMPLE",
            "is_kindle_plus": False,
            "badge_label": "Kindle Sample",
            "short_badge": "Kindle Sample",
        }
    return {
        "origin_type": "PURCHASE",
        "is_kindle_plus": False,
        "badge_label": "Purchased on Amazon",
        "short_badge": "Purchased on Amazon",
    }


def _parse_human_date(date_str: str) -> str:
    """Convert 'October 20, 2026' or 'Tuesday, October 20, 2026' into '2026-10-20'."""
    if not date_str:
        return ""
    s = str(date_str).strip()
    if "," in s and s.count(",") >= 2:
        # Strip leading weekday: "Tuesday, October 20, 2026" -> "October 20, 2026"
        s = s.split(",", 1)[1].strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def _wait_for_stable_page(page: Any, settle_sec: float = 1.5) -> None:
    """Wait for any in-flight navigation or client-side redirect to settle."""
    for state in ("domcontentloaded", "load"):
        try:
            page.wait_for_load_state(state, timeout=15000)
        except Exception:
            pass
    try:
        page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        pass
    if settle_sec > 0:
        time.sleep(settle_sec)


def _safe_evaluate(page: Any, expression: str, arg: Any = None, retries: int = 6) -> Any:
    """
    Run page.evaluate() resiliently, retrying if a late post-login or SPA navigation
    destroys the frame execution context mid-call.
    """
    last_err: Optional[Exception] = None
    for _ in range(retries):
        try:
            if arg is None:
                return page.evaluate(expression)
            return page.evaluate(expression, arg)
        except Exception as exc:
            last_err = exc
            msg = str(exc).lower()
            if (
                "execution context was destroyed" in msg
                or "navigation" in msg
                or "cannot find context" in msg
            ):
                _wait_for_stable_page(page, settle_sec=2.0)
                continue
            raise
    if last_err:
        raise last_err
    return None


def fetch_amazon_via_browser(
    fetch_kindle: bool = True,
    fetch_audible: bool = True,
    headless: bool = False,
    browser_channel: str = "chrome",
    max_notebook_details: int = 75,
) -> List[Dict[str, Any]]:
    """
    Launch Google Chrome (preferred) or Microsoft Edge with a persistent profile, ensure the user is signed in
    to Amazon, and fetch:
      1. Full Kindle Cloud Library (`read.amazon.com/kindle-library/search`)
      2. Kindle Notebook Bookmarks & Highlights (`read.amazon.com/notebook`)
      3. Full Audible Audiobook Library (`www.audible.com/library/titles`)
    """
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    all_items: Dict[str, Dict[str, Any]] = {}

    print("=" * 88)
    print(" DIRECT AUTOMATED AMAZON KINDLE, KINDLE+ & AUDIBLE LIBRARY FETCH")
    print("=" * 88)
    print(f" Persistent Browser Profile : {PROFILE_DIR}")

    channels = [browser_channel] + [c for c in ("chrome", "msedge") if c != browser_channel]

    with sync_playwright() as p:
        context = None
        for ch in channels:
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    channel=ch,
                    headless=headless,
                    viewport={"width": 1280, "height": 900},
                )
                print(f" Browser Engine             : {ch} ({'headless' if headless else 'interactive window'})")
                break
            except Exception:
                continue

        if context is None:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=headless,
                viewport={"width": 1280, "height": 900},
            )

        page = context.pages[0] if context.pages else context.new_page()

        if fetch_kindle:
            print("\n[1/3] Connecting to Amazon Kindle Cloud Reader (https://read.amazon.com/kindle-library)...")
            try:
                page.goto("https://read.amazon.com/kindle-library", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            time.sleep(3)

            # Check if we landed on `read.amazon.com/landing` and click its Sign-In button if present
            if "landing" in page.url.lower():
                try:
                    sign_in_btn = page.locator("#top-sign-in-btn, a:has-text('Sign in'), button:has-text('Sign in')").first
                    if sign_in_btn.count() > 0:
                        sign_in_btn.click()
                        time.sleep(3)
                except Exception:
                    pass

            # Verify whether the Kindle Cloud Reader API is authenticated yet
            def _is_kindle_api_ready() -> bool:
                if "read.amazon.com" not in page.url or "signin" in page.url.lower() or "landing" in page.url.lower():
                    return False
                try:
                    return bool(
                        page.evaluate(
                            """async () => {
                                try {
                                    const r = await fetch('https://read.amazon.com/kindle-library/search?query=&libraryType=BOOKS&sortType=recency&querySize=1', { credentials: 'include' });
                                    return r.ok;
                                } catch (e) {
                                    return false;
                                }
                            }"""
                        )
                    )
                except Exception:
                    return False

            if not _is_kindle_api_ready():
                print("\n" + "!" * 80)
                print(" AMAZON SIGN-IN REQUIRED IN THE OPENED BROWSER WINDOW")
                print(" Please sign in to your Amazon account in the Chrome window that just opened.")
                print(" (Your session will be saved in `.amazon_session_profile` for future automatic runs!)")
                print("!" * 80 + "\n")
                for _ in range(300):
                    time.sleep(2)
                    if "landing" in page.url.lower():
                        try:
                            btn = page.locator("#top-sign-in-btn, a:has-text('Sign in'), button:has-text('Sign in')").first
                            if btn.count() > 0:
                                btn.click()
                                time.sleep(2)
                        except Exception:
                            pass
                    if _is_kindle_api_ready():
                        break
                _wait_for_stable_page(page, settle_sec=2.5)

            # Ensure post-login redirects have settled on `read.amazon.com`
            _wait_for_stable_page(page, settle_sec=1.5)

            # Paginate page-by-page so any late SPA reload/redirect only retries a single batch
            print("  Fetching purchased Kindle & Kindle+ books via Kindle Cloud Reader API...")
            raw_kindle_books: List[Dict[str, Any]] = []
            pagination_token: Optional[str] = None
            for page_idx in range(1, 101):
                if "read.amazon.com" not in page.url:
                    try:
                        page.goto("https://read.amazon.com/kindle-library", wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    _wait_for_stable_page(page, settle_sec=2.0)

                batch = None
                for fetch_attempt in range(3):
                    batch = _safe_evaluate(
                        page,
                        """async (token) => {
                            let url = 'https://read.amazon.com/kindle-library/search?query=&libraryType=BOOKS&sortType=recency&querySize=50';
                            if (token) {
                                url += '&paginationToken=' + encodeURIComponent(token);
                            }
                            try {
                                const resp = await fetch(url, { credentials: 'include' });
                                if (!resp.ok) return { ok: false, status: resp.status, items: [], nextToken: null };
                                const data = await resp.json();
                                return {
                                    ok: true,
                                    status: resp.status,
                                    items: data.itemsList || [],
                                    nextToken: data.paginationToken || null,
                                };
                            } catch (err) {
                                return { ok: false, status: 0, items: [], nextToken: null };
                            }
                        }""",
                        pagination_token,
                    )
                    if batch and batch.get("ok"):
                        break
                    if fetch_attempt < 2:
                        _wait_for_stable_page(page, settle_sec=2.0)

                if not batch or not batch.get("ok"):
                    break
                page_items = batch.get("items") or []
                raw_kindle_books.extend(page_items)
                pagination_token = batch.get("nextToken")
                if page_idx == 1 or page_idx % 5 == 0 or not pagination_token:
                    print(f"    -> Fetched {len(raw_kindle_books):,} Kindle books so far...")
                if not pagination_token or not page_items:
                    break

            print(f"  Discovered {len(raw_kindle_books):,} Kindle / Kindle+ books in your Amazon library.")
            for idx, kb in enumerate(raw_kindle_books, start=1):
                asin = str(kb.get("asin") or "").strip()
                if not asin:
                    continue
                title = str(kb.get("title") or "Untitled").strip()
                authors = _clean_author(kb.get("authors"))
                cover_url = str(kb.get("productUrl") or kb.get("imageUrl") or "").strip()
                pct_read = kb.get("percentageRead")
                try:
                    pct_int = int(pct_read) if pct_read is not None else 0
                except (ValueError, TypeError):
                    pct_int = 0

                origin_info = _classify_kindle_origin(kb.get("originType") or "PURCHASE")
                all_items[asin] = {
                    "asin": asin,
                    "title": title,
                    "authors": authors,
                    "item_type": "KINDLE",
                    "origin_type": origin_info["origin_type"],
                    "is_kindle_plus": origin_info["is_kindle_plus"],
                    "badge_label": origin_info["badge_label"],
                    "short_badge": origin_info["short_badge"],
                    "cover_image_url": cover_url,
                    "percentage_read": pct_int,
                    "recency_rank": idx,
                    "has_bookmarks": pct_int > 0,
                    "annotations_count": 0,
                    "last_annotated_date": "",
                    "notes_preview": "",
                    "web_reader_url": str(kb.get("webReaderUrl") or f"https://read.amazon.com/?asin={asin}"),
                }

            # Step 2: Fetch Kindle Notebook Highlights & Bookmarks (`https://read.amazon.com/notebook`)
            print("\n[2/3] Fetching Kindle Bookmarks, Highlights & Notes (https://read.amazon.com/notebook)...")
            try:
                try:
                    page.goto("https://read.amazon.com/notebook", wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    time.sleep(3)
                _wait_for_stable_page(page, settle_sec=2.0)
                if "signin" in page.url.lower():
                    print("  Waiting for Kindle Notebook SSO / sign-in redirect...")
                    for _ in range(90):
                        time.sleep(1)
                        if "read.amazon.com/notebook" in page.url and "signin" not in page.url.lower():
                            break
                    _wait_for_stable_page(page, settle_sec=2.0)
                notebook_books = _safe_evaluate(
                    page,
                    """() => {
                        const rows = [];
                        const els = document.querySelectorAll('.kp-notebook-library-each-book');
                        els.forEach((el, idx) => {
                            const asin = (el.getAttribute('id') || '').trim();
                            const titleEl = el.querySelector('h2.kp-notebook-searchable');
                            const authorEl = el.querySelector('p.kp-notebook-searchable');
                            const dateInput = el.querySelector('input[id^="kp-notebook-annotated-date-"]');
                            rows.push({
                                asin: asin,
                                title: titleEl ? titleEl.innerText.trim() : '',
                                author: authorEl ? authorEl.innerText.replace(/^By:\\s*/i, '').trim() : '',
                                annotated_date: dateInput ? dateInput.value.trim() : '',
                                notebook_rank: idx + 1,
                            });
                        });
                        return rows;
                    }""",
                ) or []
                print(f"  Found {len(notebook_books):,} Kindle books with saved Highlights / Notes / Bookmarks.")

                for nb in notebook_books[:max_notebook_details]:
                    asin = nb.get("asin") or ""
                    if not asin:
                        continue
                    iso_date = _parse_human_date(nb.get("annotated_date") or "")
                    rec = all_items.get(asin)
                    if not rec:
                        rec = {
                            "asin": asin,
                            "title": nb.get("title") or "Untitled",
                            "authors": _clean_author(nb.get("author")),
                            "item_type": "KINDLE",
                            "origin_type": "PURCHASE",
                            "is_kindle_plus": False,
                            "badge_label": "Purchased on Amazon",
                            "short_badge": "Purchased on Amazon",
                            "cover_image_url": "",
                            "percentage_read": 0,
                            "recency_rank": nb.get("notebook_rank", 9999),
                            "has_bookmarks": True,
                            "annotations_count": 1,
                            "last_annotated_date": iso_date,
                            "notes_preview": "",
                            "web_reader_url": f"https://read.amazon.com/?asin={asin}",
                        }
                        all_items[asin] = rec
                    else:
                        rec["has_bookmarks"] = True
                        rec["last_annotated_date"] = iso_date
                        if rec["annotations_count"] == 0:
                            rec["annotations_count"] = 1
            except Exception as e:
                print(f"  [WARN] Could not inspect Kindle Notebook page: {e}")

        if fetch_audible:
            print("\n[3/3] Fetching Purchased Audible Audiobooks (https://www.audible.com/library/titles)...")
            try:
                page.goto("https://www.audible.com/library/titles?pageSize=50", wait_until="domcontentloaded", timeout=60000)
                _wait_for_stable_page(page, settle_sec=2.0)
                if "ap/signin" in page.url or "signin" in page.url.lower():
                    print("  Waiting for Audible SSO / sign-in redirect (complete sign-in in Chrome if prompted)...")
                    for _ in range(180):
                        time.sleep(1)
                        if "audible.com/library" in page.url and "signin" not in page.url.lower():
                            break
                    _wait_for_stable_page(page, settle_sec=2.0)

                audible_count = 0
                for page_num in range(1, 40):
                    if page_num > 1:
                        page.goto(
                            f"https://www.audible.com/library/titles?pageSize=50&page={page_num}",
                            wait_until="domcontentloaded",
                            timeout=45000,
                        )
                        _wait_for_stable_page(page, settle_sec=1.5)

                    rows = _safe_evaluate(
                        page,
                        """() => {
                            const items = [];
                            const bookRows = document.querySelectorAll('div[id^="adbl-library-content-row-"], .adbl-library-content-row');
                            bookRows.forEach((row) => {
                                const asinInput = row.querySelector('[data-asin]');
                                const asin = (row.getAttribute('data-asin') || (asinInput ? asinInput.getAttribute('data-asin') : '') || '').trim();
                                const titleEl = row.querySelector('.bc-size-headline3, a[href*="/pd/"], .bc-text-bold');
                                const authorEl = row.querySelector('.authorLabel a, li.authorLabel');
                                const imgEl = row.querySelector('img.bc-pub-block, img');
                                const rowText = row.innerText || '';
                                const isPlus = /Included in Plus|Audible Plus/i.test(rowText);
                                const isFinished = /Finished/i.test(rowText);
                                const timeLeftMatch = rowText.match(/(\\d+h\\s*\\d*m|\\d+m)\\s+left/i);
                                if (titleEl && titleEl.innerText.trim()) {
                                    items.push({
                                        asin: asin,
                                        title: titleEl.innerText.trim(),
                                        author: authorEl ? authorEl.innerText.replace(/^By:\\s*/i, '').trim() : 'Unknown Author',
                                        cover_url: imgEl ? (imgEl.getAttribute('src') || '') : '',
                                        is_plus: isPlus,
                                        is_finished: isFinished,
                                        time_left: timeLeftMatch ? timeLeftMatch[0] : '',
                                    });
                                }
                            });
                            return items;
                        }""",
                    ) or []
                    if not rows:
                        break
                    new_on_page = 0
                    for r in rows:
                        key = r.get("asin") or f"AUDIBLE:{r.get('title')}"
                        if key in all_items:
                            continue
                        new_on_page += 1
                        audible_count += 1
                        is_plus = bool(r.get("is_plus"))
                        is_fin = bool(r.get("is_finished"))
                        t_left = r.get("time_left") or ""
                        all_items[key] = {
                            "asin": r.get("asin") or "",
                            "title": r.get("title") or "Untitled",
                            "authors": _clean_author(r.get("author")),
                            "item_type": "AUDIBLE",
                            "origin_type": "AUDIBLE_PLUS" if is_plus else "AUDIBLE_PURCHASE",
                            "is_kindle_plus": is_plus,
                            "badge_label": "Audible+ (Plus Catalog)" if is_plus else "Purchased on Audible",
                            "short_badge": "Audible+" if is_plus else "Purchased on Audible",
                            "cover_image_url": r.get("cover_url") or "",
                            "percentage_read": 100 if is_fin else (50 if t_left else 0),
                            "recency_rank": audible_count,
                            "has_bookmarks": bool(is_fin or t_left),
                            "annotations_count": 0,
                            "last_annotated_date": "",
                            "notes_preview": "Finished" if is_fin else t_left,
                            "web_reader_url": f"https://www.audible.com/pd/{r.get('asin')}" if r.get("asin") else "",
                        }
                    if new_on_page == 0 or len(rows) < 50:
                        break
                print(f"  Discovered {audible_count:,} Audible audiobooks.")
            except Exception as e:
                print(f"  [WARN] Could not inspect Audible library page: {e}")

        context.close()

    items_list = list(all_items.values())
    EXPORT_JSON_PATH.write_text(json.dumps(items_list, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved local snapshot of {len(items_list):,} Amazon/Kindle/Audible items to: {EXPORT_JSON_PATH}")
    return items_list


def push_items_to_mylibrary(server_url: str, api_token: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Push the batch of Amazon Kindle / Kindle+ / Audible items & bookmarks to MyLibrary in safe chunks."""
    url = f"{server_url.rstrip('/')}/api/sync/amazon/"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    print(f"Syncing {len(items):,} Amazon/Kindle/Audible records to {url}...")
    chunk_size = 500
    last_res: Dict[str, Any] = {}

    for start_idx in range(0, len(items), chunk_size):
        chunk = items[start_idx : start_idx + chunk_size]
        if len(items) > chunk_size:
            print(f"  -> Uploading batch {start_idx + 1:,}..{start_idx + len(chunk):,} of {len(items):,}...")
        resp = requests.post(url, json={"items": chunk}, headers=headers, timeout=120)
        if resp.status_code in (200, 201):
            last_res = resp.json()
        else:
            print(f"Error from server ({resp.status_code}): {resp.text[:500]}", file=sys.stderr)
            return {"ok": False, "status_code": resp.status_code}

    print("=" * 88)
    print(" AMAZON LIBRARY & BOOKMARK SYNC COMPLETE")
    print("=" * 88)
    print(f" Total Synced             : {last_res.get('total_synced', len(items)):,}")
    print(f" Purchased on Amazon      : {last_res.get('kindle_purchased', 0):,}")
    print(f" Kindle+ (KU / Prime)     : {last_res.get('kindle_plus', 0):,}")
    print(f" Audible Audiobooks       : {last_res.get('audible_total', 0):,}")
    print(f" With Kindle Bookmarks    : {last_res.get('with_bookmarks', 0):,}")
    print(f" Matched to NAS Books     : {last_res.get('matched_catalog_items', 0):,}")
    print("=" * 88)
    return last_res


def sync_kindle_from_file(filepath: str, server_url: str, api_token: str) -> None:
    """Import Kindle/Audible items from an exported JSON or CSV file and push to `/api/sync/amazon/`."""
    p = Path(filepath)
    if not p.exists():
        print(f"Error: File does not exist: {filepath}", file=sys.stderr)
        return

    items: List[Dict[str, Any]] = []
    if p.suffix.lower() == ".json":
        raw = json.loads(p.read_text(encoding="utf-8"))
        for idx, r in enumerate(raw, start=1):
            origin_info = _classify_kindle_origin(r.get("origin_type") or r.get("originType") or "PURCHASE")
            itype = (r.get("item_type") or "KINDLE").upper()
            items.append({
                "asin": r.get("asin") or "",
                "title": r.get("title") or "Untitled",
                "authors": _clean_author(r.get("authors") or r.get("author")),
                "item_type": itype,
                "origin_type": r.get("origin_type") or origin_info["origin_type"],
                "is_kindle_plus": bool(r.get("is_kindle_plus", origin_info["is_kindle_plus"])),
                "badge_label": r.get("badge_label") or origin_info["badge_label"],
                "short_badge": r.get("short_badge") or origin_info["short_badge"],
                "cover_image_url": r.get("cover_image_url") or r.get("productUrl") or "",
                "percentage_read": int(r.get("percentage_read") or r.get("percentageRead") or 0),
                "recency_rank": int(r.get("recency_rank") or idx),
                "has_bookmarks": bool(r.get("has_bookmarks") or r.get("last_annotated_date") or (r.get("percentage_read") or 0) > 0),
                "annotations_count": int(r.get("annotations_count") or 0),
                "last_annotated_date": r.get("last_annotated_date") or "",
                "notes_preview": r.get("notes_preview") or "",
                "web_reader_url": r.get("web_reader_url") or "",
            })
    elif p.suffix.lower() == ".csv":
        with p.open("r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for idx, row in enumerate(reader, start=1):
                title = row.get("Title") or row.get("title")
                if not title:
                    continue
                origin_info = _classify_kindle_origin(row.get("OriginType") or row.get("origin_type") or "PURCHASE")
                items.append({
                    "asin": row.get("ASIN") or row.get("asin") or "",
                    "title": title.strip(),
                    "authors": _clean_author(row.get("Author") or row.get("authors")),
                    "item_type": (row.get("ItemType") or "KINDLE").upper(),
                    "origin_type": origin_info["origin_type"],
                    "is_kindle_plus": origin_info["is_kindle_plus"],
                    "badge_label": origin_info["badge_label"],
                    "short_badge": origin_info["short_badge"],
                    "cover_image_url": row.get("CoverUrl") or "",
                    "percentage_read": int(row.get("PercentageRead") or 0),
                    "recency_rank": idx,
                    "has_bookmarks": bool(row.get("LastAnnotatedDate")),
                    "annotations_count": 0,
                    "last_annotated_date": row.get("LastAnnotatedDate") or "",
                    "notes_preview": "",
                    "web_reader_url": "",
                })

    if items:
        push_items_to_mylibrary(server_url, api_token, items)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Direct Automated Amazon Kindle, Kindle+ & Audible Library & Bookmark Sync"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="sync",
        choices=["sync", "kindle", "kindle-cloud", "audible", "import-file"],
        help="Sync mode: 'sync' (both Kindle + Audible, default), 'kindle', 'audible', or 'import-file'",
    )
    parser.add_argument("file", nargs="?", default="", help="JSON/CSV file path when using 'import-file'")
    parser.add_argument("--server", default=DEFAULT_SERVER_URL, help=f"MyLibrary base URL (default: {DEFAULT_SERVER_URL})")
    parser.add_argument("--token", default=DEFAULT_API_TOKEN, help="Scanner API token")
    parser.add_argument("--browser", default="chrome", choices=["chrome", "msedge"], help="Browser to use (default: chrome)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode (after initial login)")
    args = parser.parse_args()

    if args.command == "import-file":
        target_file = args.file or str(EXPORT_JSON_PATH)
        sync_kindle_from_file(target_file, args.server, args.token)
        return 0

    do_kindle = args.command in ("sync", "kindle", "kindle-cloud")
    do_audible = args.command in ("sync", "audible")

    items = fetch_amazon_via_browser(
        fetch_kindle=do_kindle,
        fetch_audible=do_audible,
        headless=args.headless,
        browser_channel=args.browser,
    )
    if items:
        push_items_to_mylibrary(args.server, args.token, items)
    return 0


if __name__ == "__main__":
    sys.exit(main())
