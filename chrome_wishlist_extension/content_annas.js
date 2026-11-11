// Content script injected on Anna's Archive (annas-archive.pk, annas-archive.gl, annas-archive.li, annas-archive.se, annas-archive.org)
// 1) Acts as a same-origin fetch helper for background search checks
// 2) Checks Anna's Archive book detail pages (/md5/<hash>) AND search result pages (/search?q=...) against MyLibrary!

(function () {
  'use strict';

  // --- Part 1: Same-Origin Search Fetch Helper for Background Service Worker ---
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || msg.type !== 'ANNAS_TAB_FETCH_SEARCH') return false;

    const searchPath = `/search?q=${encodeURIComponent(msg.query || '')}`;
    fetch(searchPath, {
      method: 'GET',
      credentials: 'include',
      headers: {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
      }
    })
      .then(async (resp) => {
        const html = await resp.text();
        sendResponse({
          ok: resp.ok,
          status: resp.status,
          origin: window.location.origin,
          searchUrl: `${window.location.origin}${searchPath}`,
          html
        });
      })
      .catch((err) => {
        sendResponse({
          ok: false,
          status: 0,
          error: err ? err.message : 'Fetch error'
        });
      });

    return true; // async response
  });

  // --- Part 2: Check Anna's Archive /md5/<hash> & /search Pages Against MyLibrary ---
  let mylibraryBaseUrl = 'http://192.168.1.43:8080/mylibrary';
  const dismissedKeys = new Set();
  const searchResultMap = new Map();
  let annasObserver = null;

  function isExtensionContextValid() {
    try {
      return Boolean(typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.id);
    } catch (_) {
      return false;
    }
  }

  function teardownInvalidatedContext() {
    if (annasObserver) {
      try {
        annasObserver.disconnect();
      } catch (_) {}
      annasObserver = null;
    }
  }

  function safeSendMessage(message, callback) {
    if (!isExtensionContextValid()) {
      teardownInvalidatedContext();
      return;
    }
    try {
      chrome.runtime.sendMessage(message, (resp) => {
        try {
          const lastErr = chrome.runtime && chrome.runtime.lastError;
          if (lastErr && /context invalidated/i.test(lastErr.message || '')) {
            teardownInvalidatedContext();
            return;
          }
        } catch (_) {
          teardownInvalidatedContext();
          return;
        }
        if (typeof callback === 'function') callback(resp);
      });
    } catch (_) {
      teardownInvalidatedContext();
    }
  }

  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function loadSettingsAndDismissed() {
    return new Promise((resolve) => {
      if (!isExtensionContextValid()) {
        teardownInvalidatedContext();
        resolve();
        return;
      }
      try {
        chrome.storage.sync.get({ mylibraryBaseUrl: 'http://192.168.1.43:8080/mylibrary' }, (syncItems) => {
          if (!isExtensionContextValid()) {
            teardownInvalidatedContext();
            resolve();
            return;
          }
          if (syncItems && syncItems.mylibraryBaseUrl) {
            mylibraryBaseUrl = String(syncItems.mylibraryBaseUrl).replace(/\/+$/, '');
          }
          try {
            chrome.storage.local.get({ dismissedWishlistFalsePositives: [] }, (localItems) => {
              const safeLocal = localItems || {};
              const list = Array.isArray(safeLocal.dismissedWishlistFalsePositives)
                ? safeLocal.dismissedWishlistFalsePositives
                : [];
              list.forEach((k) => dismissedKeys.add(String(k)));
              resolve();
            });
          } catch (_) {
            resolve();
          }
        });
      } catch (_) {
        teardownInvalidatedContext();
        resolve();
      }
    });
  }

  function saveDismissedKeys() {
    if (!isExtensionContextValid()) {
      teardownInvalidatedContext();
      return;
    }
    try {
      chrome.storage.local.set({ dismissedWishlistFalsePositives: Array.from(dismissedKeys) });
    } catch (_) {
      teardownInvalidatedContext();
    }
  }

  function stripEmojiAndIcons(str) {
    return String(str || '')
      .replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE00}-\u{FE0F}🔍🔎👤📗📘📙📕📄📚🏢🗓📅🌐]/gu, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function extractIsbnFromText(text) {
    const s = String(text || '');
    const m13 = s.match(/(?:ISBN[-:\s13]*|isbn13[:\s]*|\b)(97[89][0-9\-]{10,14})\b/i);
    if (m13) {
      const d13 = m13[1].replace(/[^0-9]/g, '');
      if (d13.length === 13) return d13;
    }
    const m10 = s.match(/(?:ISBN[-:\s10]*|isbn10[:\s]*)\b([0-9]{9}[0-9Xx])\b/i);
    if (m10) {
      return m10[1].toUpperCase();
    }
    return '';
  }

  function extractPubYearFromCardText(text) {
    const s = String(text || '');
    // Prefer year on the publisher/metadata line (e.g. "No Starch Press, Incorporated, 2003" or "EPUB · 0.7MB · 2003")
    const mMeta = s.match(/(?:,\s*|·\s*)(19[7-9]\d|20[0-3]\d)\b/);
    if (mMeta) return parseInt(mMeta[1], 10);
    const ym = s.match(/\b(19[7-9]\d|20[0-3]\d)\b/g);
    if (ym && ym.length) {
      return parseInt(ym[0], 10);
    }
    return null;
  }

  function extractEditionFromText(text) {
    const s = String(text || '');
    const mEd = s.match(/\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th))\s+(?:ed\.?|edition)\b/i);
    return mEd ? mEd[0].trim() : '';
  }

  // ===========================================================================
  // A. Single Book Detail Page (/md5/<32-hex>)
  // ===========================================================================
  function extractAnnasMd5PageBook() {
    const mPath = window.location.pathname.match(/\/md5\/([a-f0-9]{32})/i);
    if (!mPath) return null;
    const md5 = mPath[1].toLowerCase();

    const mainEl = document.querySelector('main') || document.body;

    // Locate the book title element inside the left book info column (never the site header)
    const candidates = Array.from(mainEl.querySelectorAll('div.text-3xl, div.text-2xl, h1, h2'));
    let titleEl = candidates.find((el) => !el.closest('header') && (el.textContent || '').trim().length > 2) || null;
    if (!titleEl) return null;

    const cloneTitle = titleEl.cloneNode(true);
    cloneTitle.querySelectorAll('a[href^="/search"], #mylib-title-status-pill').forEach((n) => n.remove());
    let rawTitle = stripEmojiAndIcons(cloneTitle.textContent || '');
    if (!rawTitle && document.title) {
      rawTitle = document.title.split(/\s*[—\-|]\s*Anna's Archive/i)[0].trim();
    }

    // Scope metadata extraction strictly to the book info column (`titleEl.parentElement`)
    // so we don't pick up years from the top "Recent downloads" ticker!
    const bookCol = titleEl.parentElement || mainEl;
    const colText = bookCol.innerText || bookCol.textContent || '';
    const fullMainText = mainEl.innerText || mainEl.textContent || '';

    let rawAuthor = '';
    const italicEl = bookCol.querySelector('div.italic, a.italic');
    if (italicEl) {
      const cloneAuth = italicEl.cloneNode(true);
      cloneAuth.querySelectorAll('a[href^="/search"]').forEach((a) => {
        if ((a.textContent || '').trim().length <= 3) a.remove();
      });
      // Deduplicate repeated author names like "Jon Erickson, Jon Erickson"
      const parts = stripEmojiAndIcons(cloneAuth.textContent || '')
        .split(/\s*,\s*/)
        .map((p) => p.trim())
        .filter(Boolean);
      rawAuthor = Array.from(new Set(parts)).join(', ');
    }

    const isbn = extractIsbnFromText(colText) || extractIsbnFromText(fullMainText);
    const pub_year = extractPubYearFromCardText(colText);
    const edition = extractEditionFromText(`${rawTitle} ${colText.slice(0, 600)}`);

    // Insert the compact bar right below the title (inside the left column, not as a sibling in the outer flex row!)
    return {
      key: `annas_md5_${md5}`,
      md5,
      titleEl,
      anchorEl: italicEl || titleEl,
      title: rawTitle,
      author: rawAuthor,
      isbn,
      pub_year,
      edition,
      libResult: null
    };
  }

  function renderAnnasMd5PageWidget(book) {
    if (!book || !book.anchorEl) return;

    const existingPill = document.getElementById('mylib-title-status-pill');
    if (existingPill) existingPill.remove();

    const lib = book.libResult;
    const dismissed = dismissedKeys.has(book.key) || (book.isbn && dismissedKeys.has(book.isbn));
    const searchQ = (lib && lib.search_query) || book.isbn || book.title;
    const mylibSearchUrl = `${mylibraryBaseUrl}/?q=${encodeURIComponent(searchQ)}`;

    // Compact inline bar placed directly below the author/title inside the book info card
    let container = document.getElementById('mylibrary-annas-md5-widget');
    if (!container) {
      container = document.createElement('div');
      container.id = 'mylibrary-annas-md5-widget';
      container.className = 'mylib-bookpage-widget';
      if (book.anchorEl.parentNode) {
        book.anchorEl.parentNode.insertBefore(container, book.anchorEl.nextSibling);
      } else {
        book.anchorEl.appendChild(container);
      }
    }

    container.classList.remove('mylib-owned', 'mylib-missing');

    let badgeHtml = `
      <span class="mylib-badge" style="background:rgba(148,163,184,0.2); color:#cbd5e1;">
        ⏳ Checking MyLibrary${book.isbn ? ` (ISBN ${escapeHtml(book.isbn)})` : ''}...
      </span>
    `;
    let detailsHtml = '';

    if (lib) {
      if (lib.in_library && !dismissed) {
        container.classList.add('mylib-owned');
        const fmts = (lib.formats && lib.formats.length > 0) ? lib.formats.join(', ') : (lib.item_types || ['BOOK']).join(', ');
        const enrTag = lib.is_enriched ? ' · ✨ Enriched' : '';
        const matchViaLabel = (lib.match_type === 'isbn' || lib.match_type === 'asin')
          ? `Exact ${lib.match_type.toUpperCase()}`
          : 'Title/Author';

        badgeHtml = `
          <a href="${escapeHtml(mylibSearchUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-in-lib">
            ✅ In MyLibrary (${escapeHtml(fmts)}${enrTag} · ${escapeHtml(matchViaLabel)}) ↗
          </a>
          <button type="button" class="mylib-badge mylib-badge-annas-none" id="mylib-annas-dismiss-fp" title="Mark as False Positive">
            ✕ False Positive
          </button>
        `;

        const matchLines = (lib.matches || []).slice(0, 2).map((m) => {
          const itemUrl = `${mylibraryBaseUrl}/?q=${encodeURIComponent(m.isbn || m.title)}`;
          const metaBits = [];
          if (m.pub_date) metaBits.push(`📅 ${escapeHtml(m.pub_date)}`);
          if (m.isbn) metaBits.push(`ISBN: ${escapeHtml(m.isbn)}`);
          const metaSuffix = metaBits.length ? ` <span style="color:#94a3b8;">(${metaBits.join(' · ')})</span>` : '';
          const dlLinks = (m.files || []).slice(0, 2).map((f) => {
            const dlUrl = `${mylibraryBaseUrl.replace(/\/mylibrary$/, '')}${f.download_url}`;
            return `<a href="${escapeHtml(dlUrl)}" target="_blank" rel="noopener">[Download ${escapeHtml(f.file_format || 'File')}]</a>`;
          }).join(' ');
          return `<div>📖 <strong>${escapeHtml(m.title)}</strong> — ${escapeHtml(m.authors)}${metaSuffix} <a href="${escapeHtml(itemUrl)}" target="_blank" rel="noopener">View</a> ${dlLinks}</div>`;
        });
        if (matchLines.length > 0) {
          detailsHtml = `<div class="mylib-match-details">${matchLines.join('')}</div>`;
        }
      } else {
        container.classList.add('mylib-missing');
        badgeHtml = `
          <a href="${escapeHtml(mylibSearchUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-not-in-lib">
            ❌ Not in MyLibrary${dismissed ? ' (Dismissed)' : ''} — Search MyLibrary ↗
          </a>
          ${dismissed ? `<button type="button" class="mylib-badge mylib-badge-annas-none" id="mylib-annas-undo-fp">↩ Undo</button>` : ''}
        `;
        const otherEds = Array.isArray(lib.other_editions) ? lib.other_editions : [];
        if (otherEds.length > 0) {
          const otherLines = otherEds.slice(0, 2).map((oe) => {
            const oeUrl = `${mylibraryBaseUrl}/?q=${encodeURIComponent(oe.isbn || oe.title)}`;
            const reasonStr = oe.note ? ` — <span style="color:#fbbf24;">${escapeHtml(oe.note)}</span>` : '';
            const yrStr = oe.pub_date ? ` (${escapeHtml(oe.pub_date)})` : '';
            return `<div>ℹ️ Different edition in MyLibrary: <strong>${escapeHtml(oe.title)}</strong>${yrStr}${reasonStr} <a href="${escapeHtml(oeUrl)}" target="_blank" rel="noopener">View</a></div>`;
          });
          detailsHtml = `<div class="mylib-match-details">${otherLines.join('')}</div>`;
        }
      }
    }

    container.innerHTML = `
      <div class="mylib-badges-row">
        ${badgeHtml}
        ${book.isbn ? `<span class="mylib-bp-meta-pill">ISBN: ${escapeHtml(book.isbn)}</span>` : ''}
        ${book.pub_year ? `<span class="mylib-bp-meta-pill">📅 ${escapeHtml(book.pub_year)}</span>` : ''}
        ${book.edition ? `<span class="mylib-bp-meta-pill">${escapeHtml(book.edition)}</span>` : ''}
      </div>
      ${detailsHtml}
    `;

    const btnDismiss = container.querySelector('#mylib-annas-dismiss-fp');
    if (btnDismiss) {
      btnDismiss.addEventListener('click', () => {
        dismissedKeys.add(book.key);
        if (book.isbn) dismissedKeys.add(book.isbn);
        saveDismissedKeys();
        renderAnnasMd5PageWidget(book);
      });
    }

    const btnUndo = container.querySelector('#mylib-annas-undo-fp');
    if (btnUndo) {
      btnUndo.addEventListener('click', () => {
        dismissedKeys.delete(book.key);
        if (book.isbn) dismissedKeys.delete(book.isbn);
        saveDismissedKeys();
        renderAnnasMd5PageWidget(book);
      });
    }
  }

  // ===========================================================================
  // B. Anna's Archive Search Results Page (/search?q=...)
  // ===========================================================================
  function scanAnnasSearchResults() {
    if (!window.location.pathname.startsWith('/search')) return;

    const anchors = Array.from(document.querySelectorAll('a[href^="/md5/"]'));
    const newlyFound = [];

    anchors.forEach((a) => {
      const m = (a.getAttribute('href') || '').match(/\/md5\/([a-f0-9]{32})/i);
      if (!m) return;
      const md5 = m[1].toLowerCase();
      if (searchResultMap.has(md5)) return;

      const cardText = (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim();
      if (cardText.length < 5) return;

      const h3 = a.querySelector('h3, .text-xl, .text-lg, .font-bold') || (a.parentElement && a.parentElement.querySelector('h3, .text-xl, .text-lg, .font-bold'));
      const rawTitle = stripEmojiAndIcons(h3 ? h3.textContent : cardText.slice(0, 140));
      if (!rawTitle) return;

      const italicEl = a.querySelector('.italic') || (a.parentElement && a.parentElement.querySelector('.italic'));
      const rawAuthor = italicEl ? stripEmojiAndIcons(italicEl.textContent) : '';

      const fullSnippet = (a.parentElement ? a.parentElement.textContent : cardText) || '';
      const isbn = extractIsbnFromText(fullSnippet);
      const pub_year = extractPubYearFromCardText(fullSnippet);
      const edition = extractEditionFromText(`${rawTitle} ${fullSnippet}`);

      const entry = {
        key: `annas_sr_${md5}`,
        md5,
        anchorEl: h3 || a,
        title: rawTitle,
        author: rawAuthor,
        isbn,
        pub_year,
        edition,
        libResult: null
      };
      searchResultMap.set(md5, entry);
      newlyFound.push(entry);
    });

    if (newlyFound.length > 0) {
      const payload = newlyFound.map((e) => ({
        key: e.key,
        isbn: e.isbn || '',
        pub_year: e.pub_year || null,
        edition: e.edition || '',
        title: e.title,
        author: e.author,
        wishlist_name: "Anna's Archive Search"
      }));

      safeSendMessage({ type: 'CHECK_MYLIBRARY_BATCH', books: payload }, (resp) => {
        if (!resp || !resp.ok || !resp.data || !Array.isArray(resp.data.results)) return;
        if (resp.mylibraryBaseUrl) mylibraryBaseUrl = resp.mylibraryBaseUrl;

        resp.data.results.forEach((r) => {
          const md5 = String(r.key || '').replace(/^annas_sr_/, '');
          const entry = searchResultMap.get(md5);
          if (!entry) return;
          entry.libResult = r;
          renderSearchResultBadge(entry);
        });
      });
    }
  }

  function renderSearchResultBadge(entry) {
    if (!entry || !entry.anchorEl || !entry.libResult) return;
    const badgeId = `mylib-annas-sr-badge-${entry.md5}`;
    let badge = document.getElementById(badgeId);
    if (badge) badge.remove();

    const lib = entry.libResult;
    const searchQ = lib.search_query || entry.isbn || entry.title;
    const mylibUrl = `${mylibraryBaseUrl}/?q=${encodeURIComponent(searchQ)}`;

    badge = document.createElement('a');
    badge.id = badgeId;
    badge.href = mylibUrl;
    badge.target = '_blank';
    badge.rel = 'noopener';
    badge.addEventListener('click', (e) => e.stopPropagation());

    if (lib.in_library) {
      const fmts = (lib.formats && lib.formats.length > 0) ? lib.formats.join(', ') : 'BOOK';
      badge.className = 'mylib-title-pill mylib-title-pill-owned';
      badge.textContent = `✅ In MyLibrary (${fmts}) ↗`;
    } else {
      const hasOtherEd = Array.isArray(lib.other_editions) && lib.other_editions.length > 0;
      badge.className = 'mylib-title-pill mylib-title-pill-missing';
      badge.textContent = hasOtherEd ? '⚠️ Other Edition in MyLibrary' : '❌ Not in MyLibrary';
    }

    entry.anchorEl.appendChild(document.createTextNode(' '));
    entry.anchorEl.appendChild(badge);
  }

  async function initAnnasChecker() {
    if (!isExtensionContextValid()) return;
    await loadSettingsAndDismissed();

    if (/\/md5\/[a-f0-9]{32}/i.test(window.location.pathname)) {
      const book = extractAnnasMd5PageBook();
      if (book) {
        renderAnnasMd5PageWidget(book);
        safeSendMessage(
          {
            type: 'CHECK_MYLIBRARY_BATCH',
            books: [
              {
                key: book.key,
                isbn: book.isbn || '',
                pub_year: book.pub_year || null,
                edition: book.edition || '',
                title: book.title || '',
                author: book.author || '',
                wishlist_name: "Anna's Archive Book Page"
              }
            ]
          },
          (resp) => {
            if (resp && resp.mylibraryBaseUrl) mylibraryBaseUrl = resp.mylibraryBaseUrl;
            if (resp && resp.ok && resp.data && Array.isArray(resp.data.results) && resp.data.results[0]) {
              book.libResult = resp.data.results[0];
            } else {
              book.libResult = { in_library: false, error: (resp && resp.error) || 'Offline' };
            }
            renderAnnasMd5PageWidget(book);
          }
        );
      }
    }

    if (window.location.pathname.startsWith('/search')) {
      scanAnnasSearchResults();
      let debounce = null;
      annasObserver = new MutationObserver(() => {
        if (!isExtensionContextValid()) {
          if (debounce) clearTimeout(debounce);
          teardownInvalidatedContext();
          return;
        }
        if (debounce) clearTimeout(debounce);
        debounce = setTimeout(() => {
          if (!isExtensionContextValid()) {
            teardownInvalidatedContext();
            return;
          }
          scanAnnasSearchResults();
        }, 400);
      });
      if (document.body) {
        annasObserver.observe(document.body, { childList: true, subtree: true });
      }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAnnasChecker);
  } else {
    initAnnasChecker();
  }
})();
