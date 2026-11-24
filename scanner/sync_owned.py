#!/usr/bin/env python3
"""
MyLibrary Direct Automated Packt & Springer Owned Titles Sync (`scanner/sync_owned.py`)
======================================================================================
Automatically fetches:
  1. Owned Packt books & videos from `https://subscription.packtpub.com/owned`
     via `https://subscription.packtpub.com/api/entitlements/users/me/owned` (includes both print & eBook ISBNs,
     full author names, covers, and Book/Video types).
  2. Owned Springer books from `https://library.springer.com/` (My Bookshelf).

Launches native Google Chrome (or Microsoft Edge) with `.amazon_session_profile` and talks directly
to the page target over Chrome DevTools WebSocket (`http://127.0.0.1:9333/json/list`) so it never hangs
on Chrome's internal `chrome://omnibox-popup.top-chrome` UI targets or Cloudflare Turnstile checks.

Usage:
  sync_owned.bat                        # Sync both Packt Owned & Springer Owned
  sync_owned.bat packt                  # Sync only Packt Owned (https://subscription.packtpub.com/owned)
  sync_owned.bat springer               # Sync only Springer Owned (https://library.springer.com/)
  sync_owned.bat import-file FILE.json  # Import from a saved JSON/CSV file
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import websockets

DEFAULT_SERVER_URL = os.environ.get("MYLIBRARY_URL", "http://192.168.1.43:8080/mylibrary").rstrip("/")
DEFAULT_API_TOKEN = os.environ.get("SCANNER_API_TOKEN", "super-secret-sync-token-12345")
PROFILE_DIR = Path(__file__).resolve().parent.parent / ".amazon_session_profile"
EXPORT_JSON_PATH = Path(__file__).resolve().parent.parent / "publisher_owned_export.json"


def _clean_author(raw: Any) -> str:
    if isinstance(raw, list):
        cleaned = [str(a).strip().rstrip(":").strip() for a in raw if a and not str(a).strip().isdigit()]
        return ", ".join(c for c in cleaned if c) or "Unknown Author"
    s = str(raw or "").strip().rstrip(":").strip()
    return s or "Unknown Author"


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_browser_exe(channel_pref: str = "chrome") -> str:
    candidates: List[str] = []
    if channel_pref == "msedge":
        candidates.extend([
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ])
    candidates.extend([
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ])
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return shutil.which("chrome") or shutil.which("msedge") or ""


async def _cdp_eval(ws_url: str, expression: str, await_promise: bool = True, timeout: float = 120.0) -> Any:
    async with websockets.connect(ws_url, max_size=100_000_000, open_timeout=15) as ws:
        msg = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        }
        await ws.send(json.dumps(msg))
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        resp = json.loads(raw)
        res = (resp.get("result") or {}).get("result") or {}
        if "value" in res:
            val = res["value"]
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except Exception:
                    return val
            return val
        return None


async def _cdp_navigate(ws_url: str, target_url: str) -> None:
    async with websockets.connect(ws_url, max_size=50_000_000, open_timeout=15) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
        await ws.recv()
        await ws.send(json.dumps({"id": 2, "method": "Page.navigate", "params": {"url": target_url}}))
        await ws.recv()


def _get_active_content_tab(debug_port: int, preferred_domain: str = "") -> Optional[Dict[str, Any]]:
    try:
        tabs = requests.get(f"http://127.0.0.1:{debug_port}/json/list", timeout=5).json()
    except Exception:
        return None
    page_tabs = [
        t for t in tabs
        if t.get("type") == "page"
        and not str(t.get("url") or "").startswith(("chrome://", "devtools://", "chrome-extension://"))
        and t.get("webSocketDebuggerUrl")
    ]
    if preferred_domain:
        for t in page_tabs:
            if preferred_domain.lower() in str(t.get("url") or "").lower():
                return t
    if page_tabs:
        return page_tabs[0]
    # Fallback to any page tab that has a webSocketDebuggerUrl
    any_pages = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    return any_pages[-1] if any_pages else None


async def _fetch_owned_async(
    fetch_packt: bool = True,
    fetch_springer: bool = True,
    headless: bool = False,
    browser_channel: str = "chrome",
    debug_port: int = 9333,
) -> List[Dict[str, Any]]:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    all_items: Dict[str, Dict[str, Any]] = {}

    print("=" * 88)
    print(" DIRECT AUTOMATED PACKT & SPRINGER OWNED TITLES FETCH")
    print("=" * 88)
    print(f" Persistent Browser Profile : {PROFILE_DIR}")

    browser_proc: Optional[subprocess.Popen] = None
    already_running = _is_port_open(debug_port)
    if not already_running:
        exe_path = _find_browser_exe(browser_channel)
        if not exe_path:
            raise RuntimeError("Could not find Google Chrome or Microsoft Edge executable.")
        start_url = "https://subscription.packtpub.com/owned" if fetch_packt else "https://library.springer.com/"
        cmd = [
            exe_path,
            f"--user-data-dir={PROFILE_DIR}",
            f"--remote-debugging-port={debug_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1340,920",
            start_url,
        ]
        if headless:
            cmd.append("--headless=new")
        browser_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            if _is_port_open(debug_port):
                break
            time.sleep(0.3)

    print(f" Browser Engine             : Native Chrome via Direct CDP WebSocket (port {debug_port})")

    # =========================================================================
    # 1. PACKT OWNED TITLES (https://subscription.packtpub.com/owned)
    # =========================================================================
    if fetch_packt:
        print("\n[1/2] Connecting to Packt Owned Library (https://subscription.packtpub.com/owned)...")
        tab = _get_active_content_tab(debug_port, "packtpub.com") or _get_active_content_tab(debug_port)
        if tab and "packtpub.com/owned" not in str(tab.get("url") or "").lower():
            await _cdp_navigate(tab["webSocketDebuggerUrl"], "https://subscription.packtpub.com/owned")
            await asyncio.sleep(3.0)

        prompted_packt = False
        for _ in range(300):
            tab = _get_active_content_tab(debug_port, "packtpub.com") or _get_active_content_tab(debug_port)
            if not tab:
                await asyncio.sleep(1.5)
                continue
            ws_url = tab["webSocketDebuggerUrl"]
            probe = await _cdp_eval(
                ws_url,
                r"""(async () => {
                    if (!location.hostname.includes('packtpub.com')) {
                        return JSON.stringify({ ready: false, reason: 'not_on_packt' });
                    }
                    try {
                        const r = await fetch('https://subscription.packtpub.com/api/entitlements/users/me/owned?sort=createdAt%3Adesc&search=&limit=1&offset=0', { credentials: 'include' });
                        if (r.ok) {
                            const d = await r.json();
                            if (d && (typeof d.count === 'number' || Array.isArray(d.data))) {
                                return JSON.stringify({ ready: true, count: d.count || (d.data ? d.data.length : 0) });
                            }
                        }
                        return JSON.stringify({ ready: false, status: r.status });
                    } catch (e) {
                        return JSON.stringify({ ready: false, err: String(e) });
                    }
                })()""",
            )
            if isinstance(probe, dict) and probe.get("ready"):
                print(f"  Packt session verified ({probe.get('count', 0):,} owned titles in account). Fetching all records...")
                break
            if not prompted_packt:
                prompted_packt = True
                print("\n" + "!" * 80)
                print(" PACKT SIGN-IN / VERIFICATION REQUIRED IN THE OPENED CHROME WINDOW")
                print(" Please complete verification and sign in to Packt in the open Chrome window.")
                print(" Once signed in, the sync will automatically fetch all your owned titles!")
                print("!" * 80 + "\n")
            await asyncio.sleep(2.5)

        tab = _get_active_content_tab(debug_port, "packtpub.com") or _get_active_content_tab(debug_port)
        if tab:
            packt_res = await _cdp_eval(
                tab["webSocketDebuggerUrl"],
                r"""(async () => {
                    const items = [];
                    let totalCount = 1500;
                    const limit = 100;
                    for (let offset = 0; offset < totalCount + limit; offset += limit) {
                        const url = `https://subscription.packtpub.com/api/entitlements/users/me/owned?sort=createdAt%3Adesc&search=&limit=${limit}&offset=${offset}`;
                        const r = await fetch(url, { credentials: 'include' });
                        if (!r.ok) break;
                        const d = await r.json();
                        if (typeof d.count === 'number' && d.count > 0) totalCount = d.count;
                        const rows = Array.isArray(d.data) ? d.data : [];
                        if (rows.length === 0) break;
                        rows.forEach((row, idx) => {
                            const sp = row.simplifiedProduct || {};
                            const isbnsObj = sp.isbns || {};
                            const isbn13 = String(sp.isbn13 || isbnsObj.print || isbnsObj.ebook || row.productId || '').replace(/[^0-9Xx]/g, '');
                            const ebookIsbn = String(isbnsObj.ebook || '').replace(/[^0-9Xx]/g, '');
                            const printIsbn = String(isbnsObj.print || '').replace(/[^0-9Xx]/g, '');
                            const linkAuthors = Array.isArray(sp.linkAuthors) ? sp.linkAuthors.map(a => a && a.author).filter(Boolean) : [];
                            const pType = String(sp.type || sp.defaultType || '').toLowerCase();
                            const isVideo = pType.includes('video');
                            const readUrl = sp.readUrl ? (sp.readUrl.startsWith('http') ? sp.readUrl : `https://subscription.packtpub.com${sp.readUrl}`) : '';
                            items.push({
                                source: 'PACKT',
                                isbn: isbn13,
                                ebook_isbn: ebookIsbn,
                                print_isbn: printIsbn,
                                title: sp.title || row.productName || 'Untitled',
                                authors: linkAuthors.join(', ') || 'Unknown Author',
                                is_video: isVideo,
                                cover_image_url: sp.coverImage || sp.smallImage || '',
                                web_reader_url: readUrl || (isbn13 ? `https://subscription.packtpub.com/book/programming/${isbn13}/1` : 'https://subscription.packtpub.com/owned'),
                                recency_rank: offset + idx + 1
                            });
                        });
                        if (items.length >= totalCount || rows.length < limit) break;
                    }
                    return JSON.stringify({ totalCount, fetched: items.length, items });
                })()""",
                timeout=180.0,
            )
            if isinstance(packt_res, dict) and isinstance(packt_res.get("items"), list):
                for p_it in packt_res["items"]:
                    k = f"PACKT:{p_it.get('isbn') or p_it.get('title')}:{p_it.get('recency_rank')}"
                    all_items[k] = p_it
                print(f"  Fetched {len(packt_res['items']):,} Packt Owned titles!")

    # =========================================================================
    # 2. SPRINGER OWNED TITLES (https://library.springer.com/)
    # =========================================================================
    if fetch_springer:
        print("\n[2/2] Connecting to Springer My Bookshelf (https://library.springer.com/)...")
        tab = _get_active_content_tab(debug_port, "springer") or _get_active_content_tab(debug_port)
        if tab:
            await _cdp_navigate(tab["webSocketDebuggerUrl"], "https://library.springer.com/")
            await asyncio.sleep(3.5)

        prompted_springer = False
        for _ in range(300):
            tab = _get_active_content_tab(debug_port, "springer") or _get_active_content_tab(debug_port)
            if not tab:
                await asyncio.sleep(1.5)
                continue
            cur_u = str(tab.get("url") or "").lower()
            ws_url = tab["webSocketDebuggerUrl"]
            if "library.springer.com" in cur_u and "idp" not in cur_u and "login" not in cur_u:
                state = await _cdp_eval(
                    ws_url,
                    r"""(() => {
                        const txt = (document.body && document.body.innerText) || '';
                        const hasLogin = Boolean(document.querySelector('input[type="password"], input[type="email"], form[action*="login"]')) || /log\s*in\s+or\s+register/i.test(document.title || '');
                        const cardsCount = document.querySelectorAll('a[href*="/book/"], a[href*="978-"], img[src*="978"], article, .c-card, [class*="card"]').length;
                        return JSON.stringify({ hasLogin, cardsCount, title: document.title, len: txt.length });
                    })()""",
                    await_promise=False,
                )
                if isinstance(state, dict) and not state.get("hasLogin") and (state.get("cardsCount", 0) > 0 or state.get("len", 0) > 300):
                    break
            if not prompted_springer:
                prompted_springer = True
                print("\n" + "!" * 80)
                print(" SPRINGER SIGN-IN REQUIRED IN THE OPENED CHROME WINDOW")
                print(" Please sign in to your Springer account in the open Chrome window.")
                print(" Once https://library.springer.com/ loads your bookshelf, sync will continue automatically!")
                print("!" * 80 + "\n")
            await asyncio.sleep(2.5)

        springer_count = 0
        for _page_idx in range(1, 51):
            tab = _get_active_content_tab(debug_port, "library.springer.com") or _get_active_content_tab(debug_port)
            if not tab:
                break
            ws_url = tab["webSocketDebuggerUrl"]
            sp_page = await _cdp_eval(
                ws_url,
                r"""(async () => {
                    window.scrollTo(0, document.body.scrollHeight);
                    await new Promise(r => setTimeout(r, 700));
                    window.scrollTo(0, 0);

                    const out = [];
                    const seen = new Set();
                    const candidates = Array.from(document.querySelectorAll(
                        '[data-test*="book"], .app-card-open, .c-card-open, .c-card, article, li, tr, [class*="book-card"], [class*="BookCard"], a[href*="/book/10."], a[href*="978-"], a[href*="/book/"]'
                    ));
                    candidates.forEach((node) => {
                        const card = node.closest('article, tr, li, .app-card-open, .c-card-open, .c-card, [class*="book-card"], [class*="BookCard"]') || node;
                        if (!card || seen.has(card)) return;
                        if (card.querySelectorAll('h2, h3').length > 2) return;

                        const link = card.querySelector('a[href*="/book/"], a[href*="/referencework/"], a[href*="link.springer.com"], a[href*="10.1007/"], a[href*="978"]') || (node.tagName === 'A' ? node : null);
                        const img = card.querySelector('img[src*="978"], img[alt], img');
                        const titleEl = card.querySelector('h2, h3, h4, [class*="title"], [data-test*="title"]');

                        let isbn = '';
                        let web_url = '';
                        if (link) {
                            web_url = link.href || link.getAttribute('href') || '';
                            const m = web_url.match(/(97[89][0-9\-]{10,14})/);
                            if (m) isbn = m[1].replace(/[^0-9]/g, '');
                        }
                        let cover_url = '';
                        if (img) {
                            cover_url = img.src || img.getAttribute('src') || '';
                            const mImg = cover_url.match(/(97[89][0-9\-]{10,14})/);
                            if (mImg && !isbn) isbn = mImg[1].replace(/[^0-9]/g, '');
                        }
                        if (!isbn) {
                            const mTxt = (card.innerText || '').match(/\b(97[89][0-9\-]{10,14})\b/);
                            if (mTxt) isbn = mTxt[1].replace(/[^0-9]/g, '');
                        }

                        const title = (
                            (titleEl && titleEl.innerText) ||
                            (link && link.innerText) ||
                            (img && img.getAttribute('alt')) ||
                            ''
                        ).replace(/\s+/g, ' ').trim();

                        if ((!title || title.length < 3 || /^(download|pdf|epub|read|my bookshelf|log\s*out|sign\s*out|log\s*in|sign\s*in|my\s*account|profile)$/i.test(title)) && !isbn) return;
                        if (/^(log\s*out|sign\s*out|log\s*in|sign\s*in|my\s*bookshelf)$/i.test(title)) return;
                        seen.add(card);

                        const authEl = card.querySelector('[class*="author"], [data-test*="author"], .c-author-list');
                        const author = authEl ? authEl.innerText.replace(/\s+/g, ' ').trim() : '';

                        out.push({ isbn, title, author, cover_url, web_url });
                    });

                    // Check for next page button
                    const nextCandidates = Array.from(document.querySelectorAll(
                        'a[rel="next"], button[aria-label*="Next" i], a[aria-label*="Next" i], .c-pagination__link--next, [data-test*="next"], [class*="pagination"] a, [class*="pagination"] button'
                    ));
                    const nextBtn = nextCandidates.find((el) => {
                        const txt = (el.innerText || el.getAttribute('aria-label') || '').trim();
                        const dis = el.disabled || el.getAttribute('aria-disabled') === 'true' || /disabled/i.test(el.className || '');
                        return !dis && (/^(next|›|»|>)$/i.test(txt) || /next\s*page/i.test(txt));
                    });
                    const curUrl = location.href;
                    let nextHref = '';
                    if (nextBtn && out.length > 0) {
                        nextHref = nextBtn.href || nextBtn.getAttribute('href') || '';
                        if (!nextHref || nextHref.startsWith('#') || nextHref.startsWith('javascript:')) {
                            nextBtn.click();
                        }
                    }
                    return JSON.stringify({ rows: out, clickedNext: Boolean(nextBtn && out.length > 0), nextHref, curUrl });
                })()""",
            )
            if not isinstance(sp_page, dict):
                break
            new_sp = 0
            for r in sp_page.get("rows") or []:
                isbn_clean = re.sub(r"[^0-9Xx]", "", str(r.get("isbn") or "")).upper()
                title = str(r.get("title") or "").strip()
                key = f"SPRINGER:{isbn_clean or title}"
                if not (isbn_clean or title) or key in all_items:
                    continue
                new_sp += 1
                springer_count += 1
                all_items[key] = {
                    "source": "SPRINGER",
                    "isbn": isbn_clean,
                    "title": title or "Untitled",
                    "authors": _clean_author(r.get("author")),
                    "is_video": False,
                    "badge_label": "Owned on Springer (My Bookshelf)",
                    "short_badge": "Owned on Springer",
                    "cover_image_url": r.get("cover_url") or "",
                    "web_reader_url": r.get("web_url") or (f"https://link.springer.com/book/10.1007/{isbn_clean}" if isbn_clean else "https://library.springer.com/"),
                    "recency_rank": len(all_items) + 1,
                }
            if not sp_page.get("clickedNext") or new_sp == 0:
                break
            next_href = str(sp_page.get("nextHref") or "").strip()
            if next_href and not next_href.startswith(("#", "javascript:")):
                if next_href.startswith("/"):
                    next_href = f"https://library.springer.com{next_href}"
                await _cdp_navigate(ws_url, next_href)
            await asyncio.sleep(2.5)

        print(f"  Discovered {springer_count:,} Springer Owned titles.")

    if browser_proc is not None:
        try:
            browser_proc.terminate()
        except Exception:
            pass

    # Merge with any previously exported items from the other publisher so running `sync_owned.bat springer`
    # preserves Packt items in publisher_owned_export.json (and vice versa).
    existing_export: List[Dict[str, Any]] = []
    if EXPORT_JSON_PATH.exists():
        try:
            existing_export = json.loads(EXPORT_JSON_PATH.read_text(encoding="utf-8")) or []
        except Exception:
            existing_export = []

    fetched_sources = {str(v.get("source") or "").upper() for v in all_items.values()}
    preserved = [
        r for r in existing_export
        if isinstance(r, dict) and str(r.get("source") or "").upper() not in fetched_sources
    ]
    items_list = preserved + list(all_items.values())
    EXPORT_JSON_PATH.write_text(json.dumps(items_list, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved local snapshot of {len(items_list):,} Packt/Springer owned titles to: {EXPORT_JSON_PATH}")
    return items_list


def fetch_owned_via_browser(
    fetch_packt: bool = True,
    fetch_springer: bool = True,
    headless: bool = False,
    browser_channel: str = "chrome",
    debug_port: int = 9333,
) -> List[Dict[str, Any]]:
    return asyncio.run(
        _fetch_owned_async(
            fetch_packt=fetch_packt,
            fetch_springer=fetch_springer,
            headless=headless,
            browser_channel=browser_channel,
            debug_port=debug_port,
        )
    )


def push_owned_items_to_mylibrary(server_url: str, api_token: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Push the batch of Packt & Springer owned titles to `/api/sync/owned/`."""
    url = f"{server_url.rstrip('/')}/api/sync/owned/"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    print(f"Syncing {len(items):,} Packt & Springer owned records to {url}...")
    chunk_size = 500
    last_res: Dict[str, Any] = {}
    synced_sources = sorted({str(it.get("source") or "PACKT").upper() for it in items})

    for start_idx in range(0, len(items), chunk_size):
        chunk = items[start_idx : start_idx + chunk_size]
        is_final = (start_idx + chunk_size) >= len(items)
        resp = requests.post(
            url,
            json={
                "items": chunk,
                "synced_sources": synced_sources if is_final else [],
                "is_final_chunk": is_final,
            },
            headers=headers,
            timeout=120,
        )
        if resp.status_code in (200, 201):
            last_res = resp.json()
        else:
            print(f"Error from server ({resp.status_code}): {resp.text[:500]}", file=sys.stderr)
            return {"ok": False, "status_code": resp.status_code}

    print("=" * 88)
    print(" PACKT & SPRINGER OWNED LIBRARY SYNC COMPLETE")
    print("=" * 88)
    print(f" Total Synced             : {last_res.get('total_synced', len(items)):,}")
    print(f" Owned on Packt           : {last_res.get('packt_owned', 0):,} (Not on NAS: {last_res.get('packt_only', 0):,})")
    print(f" Owned on Springer        : {last_res.get('springer_owned', 0):,} (Not on NAS: {last_res.get('springer_only', 0):,})")
    print(f" Missing Owned (Removed)  : {last_res.get('missing_owned', 0):,}")
    print(f" Matched to NAS Books     : {last_res.get('matched_catalog_items', 0):,}")
    print("=" * 88)
    return last_res


def sync_owned_from_file(filepath: str, server_url: str, api_token: str) -> None:
    p = Path(filepath)
    if not p.exists():
        print(f"Error: File does not exist: {filepath}", file=sys.stderr)
        return

    items: List[Dict[str, Any]] = []
    if p.suffix.lower() == ".json":
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("items") or list((raw.get("ownedPublisherTitles") or {}).get("items", {}).values())
        for idx, r in enumerate(raw or [], start=1):
            src = str(r.get("source") or (r.get("sources") or ["PACKT"])[0]).upper()
            isbn = re.sub(r"[^0-9Xx]", "", str(r.get("isbn") or r.get("isbn13") or "")).upper()
            items.append({
                "source": "SPRINGER" if "SPRINGER" in src else "PACKT",
                "isbn": isbn,
                "ebook_isbn": re.sub(r"[^0-9Xx]", "", str(r.get("ebook_isbn") or "")).upper(),
                "print_isbn": re.sub(r"[^0-9Xx]", "", str(r.get("print_isbn") or "")).upper(),
                "title": r.get("title") or "Untitled",
                "authors": _clean_author(r.get("authors") or r.get("author")),
                "is_video": bool(r.get("is_video")),
                "cover_image_url": r.get("cover_image_url") or "",
                "web_reader_url": r.get("web_reader_url") or "",
                "recency_rank": int(r.get("recency_rank") or idx),
            })
    elif p.suffix.lower() == ".csv":
        with p.open("r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for idx, row in enumerate(reader, start=1):
                title = row.get("Title") or row.get("title") or ""
                isbn = re.sub(r"[^0-9Xx]", "", str(row.get("ISBN") or row.get("isbn") or "")).upper()
                if not title and not isbn:
                    continue
                src = str(row.get("Source") or row.get("source") or "PACKT").upper()
                items.append({
                    "source": "SPRINGER" if "SPRINGER" in src else "PACKT",
                    "isbn": isbn,
                    "title": title.strip() or "Untitled",
                    "authors": _clean_author(row.get("Author") or row.get("authors")),
                    "is_video": False,
                    "cover_image_url": row.get("CoverUrl") or "",
                    "web_reader_url": row.get("Url") or "",
                    "recency_rank": idx,
                })

    if items:
        push_owned_items_to_mylibrary(server_url, api_token, items)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Direct Automated Packt & Springer Owned Titles Sync"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="sync",
        choices=["sync", "packt", "springer", "import-file"],
        help="Sync mode: 'sync' (both Packt + Springer, default), 'packt', 'springer', or 'import-file'",
    )
    parser.add_argument("file", nargs="?", default="", help="JSON/CSV file path when using 'import-file'")
    parser.add_argument("--server", default=DEFAULT_SERVER_URL, help=f"MyLibrary base URL (default: {DEFAULT_SERVER_URL})")
    parser.add_argument("--token", default=DEFAULT_API_TOKEN, help="Scanner API token")
    parser.add_argument("--browser", default="chrome", choices=["chrome", "msedge"], help="Browser to use (default: chrome)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode (after initial login)")
    args = parser.parse_args()

    if args.command == "import-file":
        target_file = args.file or str(EXPORT_JSON_PATH)
        sync_owned_from_file(target_file, args.server, args.token)
        return 0

    do_packt = args.command in ("sync", "packt")
    do_springer = args.command in ("sync", "springer")

    items = fetch_owned_via_browser(
        fetch_packt=do_packt,
        fetch_springer=do_springer,
        headless=args.headless,
        browser_channel=args.browser,
    )
    if items:
        push_owned_items_to_mylibrary(args.server, args.token, items)
    return 0


if __name__ == "__main__":
    sys.exit(main())
