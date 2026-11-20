// Unified Content Script for Book Product Pages (Amazon, Barnes & Noble, Springer Link, Goodreads, O'Reilly, No Starch, + Custom Sites)
// Replaces the 3 separate Tampermonkey scripts AND adds live MyLibrary status + Anna's Archive live checker!

(function () {
  'use strict';

  // Skip if we are on an Amazon Wishlist page (handled by content_amazon.js)
  if (/\/(?:hz\/wishlist|gp\/registry\/wishlist|wishlist)/i.test(window.location.pathname)) {
    return;
  }

  let currentSettings = {
    mylibraryBaseUrl: 'http://192.168.1.43:8080/mylibrary',
    annasBaseUrl: 'https://annas-archive.pk',
    zlibBaseUrl: 'https://singlelogin.re',
    showZlibButton: false,
    customSites: []
  };

  let currentBookState = null;
  let dismissedKeys = new Set();
  let lastCheckedHref = '';
  let pageObserver = null;
  const searchCardStateMap = new Map();

  function isExtensionContextValid() {
    try {
      return Boolean(typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.id);
    } catch (_) {
      return false;
    }
  }

  function teardownInvalidatedContext() {
    if (pageObserver) {
      try {
        pageObserver.disconnect();
      } catch (_) {}
      pageObserver = null;
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
        chrome.storage.sync.get(
          {
            mylibraryBaseUrl: 'http://192.168.1.43:8080/mylibrary',
            annasBaseUrl: 'https://annas-archive.pk',
            zlibBaseUrl: 'https://singlelogin.re',
            showZlibButton: false,
            customSites: []
          },
          (syncItems) => {
            if (!isExtensionContextValid()) {
              teardownInvalidatedContext();
              resolve();
              return;
            }
            const safeSync = syncItems || {};
            currentSettings = {
              mylibraryBaseUrl: String(safeSync.mylibraryBaseUrl || 'http://192.168.1.43:8080/mylibrary').replace(/\/+$/, ''),
              annasBaseUrl: String(safeSync.annasBaseUrl || 'https://annas-archive.pk').replace(/\/+$/, ''),
              zlibBaseUrl: String(safeSync.zlibBaseUrl || 'https://singlelogin.re').replace(/\/+$/, ''),
              showZlibButton: Boolean(safeSync.showZlibButton),
              customSites: Array.isArray(safeSync.customSites) ? safeSync.customSites : []
            };
            try {
              chrome.storage.local.get({ dismissedWishlistFalsePositives: [] }, (localItems) => {
                const safeLocal = localItems || {};
                const list = Array.isArray(safeLocal.dismissedWishlistFalsePositives)
                  ? safeLocal.dismissedWishlistFalsePositives
                  : [];
                dismissedKeys = new Set(list.map(String));
                resolve();
              });
            } catch (_) {
              resolve();
            }
          }
        );
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

  function isBookDismissed(book) {
    if (!book) return false;
    return (
      (book.isbnDigits && dismissedKeys.has(book.isbnDigits)) ||
      (book.asin && dismissedKeys.has(book.asin)) ||
      dismissedKeys.has(book.key)
    );
  }

  function renderTopTitleIndicator(book) {
    const existingPill = document.getElementById('mylib-title-status-pill');
    if (existingPill) existingPill.remove();

    if (!book || !book.libResult) return;

    // Find the main H1 title element on the page to attach a clean inline indicator badge
    const titleEl =
      document.getElementById('productTitle') ||
      document.getElementById('ebooksProductTitle') ||
      document.querySelector('#pdp-header-info > h1') ||
      document.querySelector('div.app-article-masthead--book__info > h1, h1[data-test="book-title"]') ||
      document.querySelector('h1');

    if (!titleEl) return;

    const lib = book.libResult;
    const dismissed = isBookDismissed(book);
    const inLib = Boolean(lib.in_library && !dismissed);
    const searchQ = lib.search_query || book.isbnDigits || book.title;
    const mylibUrl = `${currentSettings.mylibraryBaseUrl}/?q=${encodeURIComponent(searchQ)}`;

    const pill = document.createElement('a');
    pill.id = 'mylib-title-status-pill';
    pill.href = mylibUrl;
    pill.target = '_blank';
    pill.rel = 'noopener';

    if (inLib) {
      const fmts = (lib.formats && lib.formats.length > 0) ? lib.formats.join(', ') : 'BOOK';
      pill.className = 'mylib-title-pill mylib-title-pill-owned';
      pill.textContent = `✅ In MyLibrary (${fmts})`;
      pill.title = 'This book is in your MyLibrary catalog! Click to open in MyLibrary.';
    } else {
      const hasOtherEd = Array.isArray(lib.other_editions) && lib.other_editions.length > 0;
      pill.className = 'mylib-title-pill mylib-title-pill-missing';
      pill.textContent = hasOtherEd ? '⚠️ Different Edition in MyLibrary' : '❌ Not in MyLibrary';
      pill.title = hasOtherEd
        ? 'An older/different edition with this title is in MyLibrary, but not this edition.'
        : 'Not found in MyLibrary — click to search MyLibrary.';
    }

    titleEl.appendChild(document.createTextNode(' '));
    titleEl.appendChild(pill);
  }

  function renderBookPageWidget(book) {
    if (!book || !book.anchorEl) return;

    let container = document.getElementById('mylibrary-bookpage-widget');
    if (!container) {
      container = document.createElement('div');
      container.id = 'mylibrary-bookpage-widget';
      container.className = 'mylib-bookpage-widget';
      const safeAnchor = (book.anchorEl.closest && book.anchorEl.closest('a')) || book.anchorEl;
      if (safeAnchor.parentNode) {
        safeAnchor.parentNode.insertBefore(container, safeAnchor.nextSibling);
      } else {
        safeAnchor.appendChild(container);
      }
    }

    const lib = book.libResult;
    const annas = book.annasResult;
    const dismissed = isBookDismissed(book);

    const isbnForAnnas = book.isbnRaw || book.isbnDigits || (lib && lib.primary_isbn) || '';
    const isbnDigits = book.isbnDigits || (lib && lib.primary_isbn) || '';
    const titleForAnnas = book.title || (lib && lib.cleaned_title) || '';
    const authorForAnnas = (book.author || (lib && lib.cleaned_author) || '').replace(/^by\s*:?\s+/i, '').trim();
    const firstAuthor = authorForAnnas.split(/\s*(?:,|;|&|\band\b)\s*/i)[0].trim();
    const searchQ = (lib && lib.search_query) || isbnDigits || titleForAnnas || book.asin;
    const mylibSearchUrl = `${currentSettings.mylibraryBaseUrl}/?q=${encodeURIComponent(searchQ)}`;
    const mylibAuthorUrl = firstAuthor
      ? `${currentSettings.mylibraryBaseUrl}/?q=${encodeURIComponent('author:"' + firstAuthor + '"')}`
      : '';

    const annasIsbnUrl = isbnForAnnas
      ? `${currentSettings.annasBaseUrl}/search?index=&page=1&q=${encodeURIComponent(isbnForAnnas)}`
      : '';
    const annasTitleUrl = titleForAnnas
      ? `${currentSettings.annasBaseUrl}/search?index=&page=1&q=${encodeURIComponent(titleForAnnas)}`
      : `${currentSettings.annasBaseUrl}/`;
    const annasAuthorUrl = authorForAnnas
      ? `${currentSettings.annasBaseUrl}/search?index=&page=1&q=${encodeURIComponent(authorForAnnas)}`
      : '';
    const zlibUrl = isbnForAnnas
      ? `${currentSettings.zlibBaseUrl}/s/${encodeURIComponent(isbnForAnnas)}`
      : `${currentSettings.zlibBaseUrl}/s/${encodeURIComponent(titleForAnnas)}`;

    container.classList.remove('mylib-owned', 'mylib-missing');

    // 1. MyLibrary Live Status Indicator Banner
    let libStatusHtml = `
      <div class="mylib-bp-status-row">
        <span class="mylib-badge" style="background:rgba(148,163,184,0.2); color:#cbd5e1;">
          ⏳ Checking MyLibrary${isbnDigits ? ` (ISBN ${escapeHtml(isbnDigits)})` : ''}...
        </span>
      </div>
    `;
    let detailsHtml = '';

    if (lib) {
      if (lib.in_library && !dismissed) {
        container.classList.add('mylib-owned');
        const fmts = (lib.formats && lib.formats.length > 0) ? lib.formats.join(', ') : (lib.item_types || ['BOOK']).join(', ');
        const enrTag = lib.is_enriched ? ' · ✨ Enriched' : '';
        const matchViaLabel = (lib.match_type === 'isbn' || lib.match_type === 'asin')
          ? `Exact ${lib.match_type.toUpperCase()}`
          : (lib.match_type === 'title_author' ? 'Title/Author Match' : 'Title Only Match');

        libStatusHtml = `
          <div class="mylib-bp-status-row">
            <a href="${escapeHtml(mylibSearchUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-in-lib" style="font-size:13px; padding:5px 11px;">
              ✅ In MyLibrary (${escapeHtml(fmts)}${enrTag} · ${escapeHtml(matchViaLabel)}) ↗
            </a>
            <button type="button" class="mylib-badge mylib-badge-annas-none" id="mylib-bp-dismiss-fp" title="Mark as False Positive (e.g. different edition)">
              ✕ False Positive
            </button>
          </div>
        `;

        const matchLines = (lib.matches || []).slice(0, 3).map((m) => {
          const itemUrl = `${currentSettings.mylibraryBaseUrl}/?q=${encodeURIComponent(m.isbn || m.title)}`;
          const metaBits = [];
          if (m.pub_date) metaBits.push(`📅 ${escapeHtml(m.pub_date)}`);
          if (m.isbn) metaBits.push(`ISBN: ${escapeHtml(m.isbn)}`);
          const metaSuffix = metaBits.length ? ` <span style="color:#94a3b8;">(${metaBits.join(' · ')})</span>` : '';
          const dlLinks = (m.files || []).slice(0, 2).map((f) => {
            const dlUrl = `${currentSettings.mylibraryBaseUrl.replace(/\/mylibrary$/, '')}${f.download_url}`;
            return `<a href="${escapeHtml(dlUrl)}" target="_blank" rel="noopener">[Download ${escapeHtml(f.file_format || 'File')}]</a>`;
          }).join(' ');
          return `<div>📖 <strong>${escapeHtml(m.title)}</strong> — ${escapeHtml(m.authors)}${metaSuffix} <a href="${escapeHtml(itemUrl)}" target="_blank" rel="noopener">View</a> ${dlLinks}</div>`;
        });
        if (matchLines.length > 0) {
          detailsHtml = `<div class="mylib-match-details">${matchLines.join('')}</div>`;
        }
      } else {
        container.classList.add('mylib-missing');
        libStatusHtml = `
          <div class="mylib-bp-status-row">
            <a href="${escapeHtml(mylibSearchUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-not-in-lib" style="font-size:13px; padding:5px 11px;">
              ❌ Not in MyLibrary${dismissed ? ' (Dismissed False Positive)' : ''} ↗
            </a>
            ${dismissed ? `<button type="button" class="mylib-badge mylib-badge-annas-none" id="mylib-bp-undo-fp">↩ Undo</button>` : ''}
          </div>
        `;
        const otherEds = Array.isArray(lib.other_editions) ? lib.other_editions : [];
        if (otherEds.length > 0) {
          const otherLines = otherEds.slice(0, 2).map((oe) => {
            const oeUrl = `${currentSettings.mylibraryBaseUrl}/?q=${encodeURIComponent(oe.isbn || oe.title)}`;
            const reasonStr = oe.note ? ` — <span style="color:#fbbf24;">${escapeHtml(oe.note)}</span>` : '';
            const yrStr = oe.pub_date ? ` (${escapeHtml(oe.pub_date)})` : '';
            return `<div>ℹ️ Different edition in MyLibrary: <strong>${escapeHtml(oe.title)}</strong>${yrStr}${reasonStr} <a href="${escapeHtml(oeUrl)}" target="_blank" rel="noopener">View</a></div>`;
          });
          detailsHtml = `<div class="mylib-match-details">${otherLines.join('')}</div>`;
        }
      }
    }

    // 2. Action Buttons from the User's Tampermonkey Scripts + Live Anna's Archive Checker
    let annasLiveHtml = '';
    if (annas) {
      if (annas.checking) {
        annasLiveHtml = `<span class="mylib-bp-btn mylib-bp-btn-checking">⏳ Checking Anna's Archive...</span>`;
      } else if (annas.ok && annas.found) {
        const fmtStr = (annas.formats && annas.formats.length) ? ` (${annas.formats.join('/')})` : '';
        const topHref = (annas.topMatch && annas.topMatch.url) ? annas.topMatch.url : (annas.searchUrl || annasIsbnUrl || annasTitleUrl);
        annasLiveHtml = `
          <a href="${escapeHtml(topHref)}" target="_blank" rel="noopener" class="mylib-bp-btn mylib-bp-btn-annas-hit" title="Found on Anna's Archive via ${escapeHtml(annas.matchedBy || 'ISBN/Title')}">
            ✅ Anna's: ${annas.count} match${annas.count === 1 ? '' : 'es'}${escapeHtml(fmtStr)} ⬇
          </a>
        `;
      } else if (annas.ok && !annas.found) {
        annasLiveHtml = `
          <span class="mylib-bp-btn mylib-bp-btn-annas-miss" title="0 automatic matches on Anna's Archive">
            Anna's: 0 matches
          </span>
        `;
      }
    } else {
      annasLiveHtml = `
        <button type="button" class="mylib-bp-btn mylib-bp-btn-annas-check" id="mylib-bp-btn-check-annas" title="Check Anna's Archive in background by ISBN and Title">
          🔍 Check Anna's Live
        </button>
      `;
    }

    const isbnBtnText = isbnForAnnas
      ? `Open in Anna's Archive (${isbnForAnnas})`
      : `Open in Anna's Archive (No ISBN)`;

    const shortAuthorLabel = firstAuthor
      ? (firstAuthor.length > 24 ? `${firstAuthor.slice(0, 22)}…` : firstAuthor)
      : 'Author';

    container.innerHTML = `
      <div class="mylib-bp-header">
        <span class="mylib-bp-site-tag">📚 MyLibrary · ${escapeHtml(book.siteName)}</span>
        ${authorForAnnas ? `<span class="mylib-bp-meta-pill" title="Detected Author(s)">👤 ${escapeHtml(authorForAnnas)}</span>` : ''}
        ${book.pub_year ? `<span class="mylib-bp-meta-pill">📅 ${escapeHtml(book.pub_year)}</span>` : ''}
        ${book.edition ? `<span class="mylib-bp-meta-pill">${escapeHtml(book.edition)}</span>` : ''}
      </div>
      ${libStatusHtml}
      ${detailsHtml}
      <div class="mylib-bp-buttons-row">
        <button type="button" class="mylib-bp-btn mylib-bp-btn-annas-isbn" id="mylib-bp-open-isbn" title="${isbnForAnnas ? `Open ${escapeHtml(annasIsbnUrl)}` : 'No ISBN-13 found on this page'}">
          ${escapeHtml(isbnBtnText)}
        </button>
        <button type="button" class="mylib-bp-btn mylib-bp-btn-annas-title" id="mylib-bp-open-title" title="Search Title on ${escapeHtml(currentSettings.annasBaseUrl)}">
          Search Title in Anna's
        </button>
        <button type="button" class="mylib-bp-btn mylib-bp-btn-annas-author" id="mylib-bp-open-author" title="${authorForAnnas ? `Search Author (${escapeHtml(authorForAnnas)}) on ${escapeHtml(currentSettings.annasBaseUrl)}` : 'No Author found on this page'}">
          👤 Search Author in Anna's (${escapeHtml(shortAuthorLabel)})
        </button>
        ${mylibAuthorUrl ? `
          <button type="button" class="mylib-bp-btn mylib-bp-btn-mylib-author" id="mylib-bp-open-author-mylib" title="Search Author (${escapeHtml(firstAuthor)}) in MyLibrary">
            📚 Author in MyLibrary (${escapeHtml(shortAuthorLabel)})
          </button>
        ` : ''}
        ${annasLiveHtml}
        ${currentSettings.showZlibButton ? `
          <button type="button" class="mylib-bp-btn mylib-bp-btn-zlib" id="mylib-bp-open-zlib" title="Open in Z-Library (${escapeHtml(zlibUrl)})">
            Open in Z-Library
          </button>
        ` : ''}
      </div>
    `;

    // Wire button click handlers (matching the user's Tampermonkey scripts)
    const btnIsbn = container.querySelector('#mylib-bp-open-isbn');
    if (btnIsbn) {
      btnIsbn.addEventListener('click', () => {
        if (!isbnForAnnas) {
          alert('No ISBN-13 Found on this page. Use "Search Title in Anna\'s" to search by Title.');
        } else {
          window.open(annasIsbnUrl, '_blank', 'noopener');
        }
      });
    }

    const btnTitle = container.querySelector('#mylib-bp-open-title');
    if (btnTitle) {
      btnTitle.addEventListener('click', () => {
        if (!titleForAnnas) {
          alert('No Title Found.');
        } else {
          window.open(annasTitleUrl, '_blank', 'noopener');
        }
      });
    }

    const btnAuthor = container.querySelector('#mylib-bp-open-author');
    if (btnAuthor) {
      btnAuthor.addEventListener('click', () => {
        if (!authorForAnnas) {
          alert('No Author Found on this page.');
        } else {
          window.open(annasAuthorUrl, '_blank', 'noopener');
        }
      });
    }

    const btnAuthorMylib = container.querySelector('#mylib-bp-open-author-mylib');
    if (btnAuthorMylib) {
      btnAuthorMylib.addEventListener('click', () => {
        if (mylibAuthorUrl) {
          window.open(mylibAuthorUrl, '_blank', 'noopener');
        }
      });
    }

    const btnZlib = container.querySelector('#mylib-bp-open-zlib');
    if (btnZlib) {
      btnZlib.addEventListener('click', () => {
        if (!isbnForAnnas && !titleForAnnas) {
          alert('No ISBN-13 or Title Found.');
        } else {
          window.open(zlibUrl, '_blank', 'noopener');
        }
      });
    }

    const btnCheckAnnas = container.querySelector('#mylib-bp-btn-check-annas');
    if (btnCheckAnnas) {
      btnCheckAnnas.addEventListener('click', () => {
        checkBookOnAnnasLive(book);
      });
    }

    const btnDismiss = container.querySelector('#mylib-bp-dismiss-fp');
    if (btnDismiss) {
      btnDismiss.addEventListener('click', () => {
        if (book.isbnDigits) dismissedKeys.add(book.isbnDigits);
        if (book.asin) dismissedKeys.add(book.asin);
        dismissedKeys.add(book.key);
        saveDismissedKeys();
        renderBookPageWidget(book);
        renderTopTitleIndicator(book);
      });
    }

    const btnUndo = container.querySelector('#mylib-bp-undo-fp');
    if (btnUndo) {
      btnUndo.addEventListener('click', () => {
        if (book.isbnDigits) dismissedKeys.delete(book.isbnDigits);
        if (book.asin) dismissedKeys.delete(book.asin);
        dismissedKeys.delete(book.key);
        saveDismissedKeys();
        renderBookPageWidget(book);
        renderTopTitleIndicator(book);
      });
    }

    renderTopTitleIndicator(book);
  }

  function checkBookInMyLibrary(book) {
    const payload = [
      {
        key: book.key,
        asin: book.asin || '',
        isbn: book.isbnDigits || book.isbnRaw || '',
        pub_year: book.pub_year || null,
        edition: book.edition || '',
        title: book.title || '',
        author: book.author || '',
        wishlist_name: book.siteName || 'Book Page'
      }
    ];

    safeSendMessage({ type: 'CHECK_MYLIBRARY_BATCH', books: payload }, (resp) => {
      if (!resp || !resp.ok || !resp.data || !Array.isArray(resp.data.results) || !resp.data.results[0]) {
        book.libResult = { in_library: false, error: (resp && resp.error) || 'Offline' };
        renderBookPageWidget(book);
        return;
      }
      if (resp.mylibraryBaseUrl) currentSettings.mylibraryBaseUrl = resp.mylibraryBaseUrl;
      if (resp.annasBaseUrl) currentSettings.annasBaseUrl = resp.annasBaseUrl;

      book.libResult = resp.data.results[0];
      renderBookPageWidget(book);
    });
  }

  function checkBookOnAnnasLive(book) {
    book.annasResult = { checking: true };
    renderBookPageWidget(book);

    const lib = book.libResult || {};
    const payloadBook = {
      key: book.key,
      asin: book.asin || '',
      isbn: book.isbnDigits || book.isbnRaw || '',
      primary_isbn: lib.primary_isbn || book.isbnDigits || '',
      isbns: lib.isbns || (book.isbnDigits ? [book.isbnDigits] : []),
      title: lib.cleaned_title || book.title,
      base_title: lib.base_title || book.title,
      author: lib.cleaned_author || book.author
    };

    safeSendMessage({ type: 'CHECK_ANNAS_SINGLE', book: payloadBook }, (res) => {
      book.annasResult = res || { ok: false, error: 'No response' };
      renderBookPageWidget(book);
    });
  }

  function renderSearchCardBadge(card) {
    if (!card) return;
    const targets = card.mountEls && card.mountEls.size > 0
      ? Array.from(card.mountEls)
      : (card.mountEl ? [card.mountEl] : []);
    if (targets.length === 0) return;

    const lib = card.libResult;
    const annasBase = currentSettings.annasBaseUrl;
    const mylibBase = currentSettings.mylibraryBaseUrl;
    const searchQ = (lib && lib.search_query) || card.isbnDigits || card.title;
    const mylibUrl = `${mylibBase}/?q=${encodeURIComponent(searchQ)}`;
    const isbnQuery = card.isbnRaw || card.isbnDigits || '';
    const annasQuery = isbnQuery || card.title;
    const annasUrl = `${annasBase}/search?q=${encodeURIComponent(annasQuery)}`;
    const annasTitleUrl = (isbnQuery && card.title)
      ? `${annasBase}/search?q=${encodeURIComponent(card.title)}`
      : '';
    const firstAuthor = card.author ? card.author.split(/\s*(?:,|;|&|\band\b)\s*/i)[0].trim() : '';
    const annasAuthorUrl = firstAuthor ? `${annasBase}/search?q=${encodeURIComponent(firstAuthor)}` : '';
    const mylibAuthorUrl = firstAuthor ? `${mylibBase}/?q=${encodeURIComponent('author:"' + firstAuthor + '"')}` : '';

    let statusHtml = `<span style="color:#94a3b8; font-weight:600;">⏳ Checking MyLibrary...</span>`;
    let borderColor = 'rgba(56,189,248,0.35)';
    if (lib) {
      if (lib.error) {
        borderColor = 'rgba(250,204,21,0.5)';
        statusHtml = `<button type="button" data-retry-card="1" style="cursor:pointer; border:1px solid rgba(250,204,21,0.5); background:rgba(234,179,8,0.2); color:#fde047; border-radius:4px; padding:2px 6px; font-size:10.5px; font-weight:700;">⚠️ Retry Check ↻</button>`;
      } else if (lib.in_library) {
        const fmts = (lib.formats && lib.formats.length > 0) ? lib.formats.join(', ') : 'BOOK';
        borderColor = 'rgba(34,197,94,0.6)';
        statusHtml = `<button type="button" data-open-url="${escapeHtml(mylibUrl)}" style="cursor:pointer; border:1px solid rgba(74,222,128,0.5); background:rgba(34,197,94,0.22); color:#4ade80; border-radius:4px; padding:2px 6px; font-size:10.5px; font-weight:700;">✅ In MyLibrary (${escapeHtml(fmts)}) ↗</button>`;
      } else if (Array.isArray(lib.other_editions) && lib.other_editions.length > 0) {
        borderColor = 'rgba(250,204,21,0.55)';
        statusHtml = `<button type="button" data-open-url="${escapeHtml(mylibUrl)}" title="A different edition/year with this title is in MyLibrary" style="cursor:pointer; border:1px solid rgba(250,204,21,0.5); background:rgba(234,179,8,0.2); color:#fde047; border-radius:4px; padding:2px 6px; font-size:10.5px; font-weight:700;">⚠️ Diff. Edition in MyLibrary ↗</button>`;
      } else {
        borderColor = 'rgba(248,113,113,0.45)';
        statusHtml = `<button type="button" data-open-url="${escapeHtml(mylibUrl)}" style="cursor:pointer; border:1px solid rgba(248,113,113,0.4); background:rgba(239,68,68,0.16); color:#fca5a5; border-radius:4px; padding:2px 6px; font-size:10.5px; font-weight:700;">❌ Not in MyLibrary ↗</button>`;
      }
    }

    const annasBtnLabel = isbnQuery ? `🔍 Anna's (${escapeHtml(isbnQuery)}) ↗` : `🔍 Anna's ↗`;
    const annasBtnHtml = `<button type="button" data-open-url="${escapeHtml(annasUrl)}" title="Search ${escapeHtml(annasQuery)} on Anna's Archive" style="cursor:pointer; border:1px solid rgba(56,189,248,0.45); background:rgba(56,189,248,0.18); color:#7dd3fc; border-radius:4px; padding:2px 6px; font-size:10.5px; font-weight:700;">${annasBtnLabel}</button>`;
    const annasTitleBtnHtml = annasTitleUrl
      ? `<button type="button" data-open-url="${escapeHtml(annasTitleUrl)}" title="Search Title (${escapeHtml(card.title)}) on Anna's Archive" style="cursor:pointer; border:1px solid rgba(56,189,248,0.35); background:rgba(14,165,233,0.14); color:#bae6fd; border-radius:4px; padding:2px 6px; font-size:10.5px; font-weight:700;">📖 Title in Anna's ↗</button>`
      : '';
    const shortAuthor = firstAuthor ? (firstAuthor.length > 18 ? firstAuthor.slice(0, 16) + '…' : firstAuthor) : '';
    const authorBtnHtml = firstAuthor
      ? `<button type="button" data-open-url="${escapeHtml(annasAuthorUrl)}" title="Search Author (${escapeHtml(firstAuthor)}) on Anna's Archive" style="cursor:pointer; border:1px solid rgba(192,132,252,0.45); background:rgba(168,85,247,0.18); color:#d8b4fe; border-radius:4px; padding:2px 6px; font-size:10.5px; font-weight:700;">👤 ${escapeHtml(shortAuthor)} (Anna's) ↗</button>` +
        `<button type="button" data-open-url="${escapeHtml(mylibAuthorUrl)}" title="Search Author (${escapeHtml(firstAuthor)}) in MyLibrary" style="cursor:pointer; border:1px solid rgba(129,140,248,0.45); background:rgba(99,102,241,0.18); color:#c7d2fe; border-radius:4px; padding:2px 6px; font-size:10.5px; font-weight:700;">📚 ${escapeHtml(shortAuthor)} (MyLib) ↗</button>`
      : '';

    const badgeBoxClass = 'mylib-search-card-bar';
    targets.forEach((mountEl) => {
      if (!mountEl || !mountEl.isConnected) return;
      let bar = mountEl.querySelector(`.${badgeBoxClass}`);
      if (!bar) {
        bar = document.createElement('div');
        bar.className = badgeBoxClass;
        bar.style.cssText =
          'position:relative; z-index:10; margin-top:6px; padding:5px 7px; border-radius:6px; background:rgba(15,23,42,0.94); border:1px solid rgba(56,189,248,0.35); display:flex; flex-wrap:wrap; align-items:center; gap:5px; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; font-size:11px; line-height:1.25;';
        mountEl.appendChild(bar);
      }
      bar.style.borderColor = borderColor;
      bar.innerHTML = `${statusHtml}${annasBtnHtml}${annasTitleBtnHtml}${authorBtnHtml}`;
      bar.querySelectorAll('button[data-open-url]').forEach((btn) => {
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          e.stopPropagation();
          const target = btn.getAttribute('data-open-url');
          if (target) window.open(target, '_blank', 'noopener');
        });
      });
      bar.querySelectorAll('button[data-retry-card]').forEach((btn) => {
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          e.stopPropagation();
          card.libResult = null;
          card.requestedAt = 0;
          renderSearchCardBadge(card);
          scanSearchCardsOnPage(new URL(window.location.href));
        });
      });
    });
  }

  function scanSearchCardsOnPage(url) {
    if (!window.MyLibSiteAdapters || typeof window.MyLibSiteAdapters.extractSearchCardsFromPage !== 'function') {
      return;
    }
    const cards = window.MyLibSiteAdapters.extractSearchCardsFromPage(document, url);
    if (!cards || cards.length === 0) return;

    const toCheck = [];
    const now = Date.now();
    cards.forEach((c) => {
      const existing = searchCardStateMap.get(c.key);
      if (existing) {
        if (!existing.mountEls) existing.mountEls = new Set();
        if (c.mountEl) existing.mountEls.add(c.mountEl);
        existing.mountEl = c.mountEl;
        renderSearchCardBadge(existing);
        if (!existing.libResult && now - (existing.requestedAt || 0) > 15000) {
          existing.requestedAt = now;
          toCheck.push(existing);
        }
      } else {
        c.mountEls = new Set(c.mountEl ? [c.mountEl] : []);
        c.requestedAt = now;
        searchCardStateMap.set(c.key, c);
        renderSearchCardBadge(c);
        toCheck.push(c);
      }
    });

    if (toCheck.length > 0) {
      const batchSize = 100;
      for (let i = 0; i < toCheck.length; i += batchSize) {
        const slice = toCheck.slice(i, i + batchSize);
        const payload = slice.map((c) => ({
          key: c.key,
          isbn: c.isbnDigits || c.isbnRaw || '',
          pub_year: c.pub_year || null,
          edition: c.edition || '',
          title: c.title || '',
          author: c.author || '',
          wishlist_name: `${c.siteName || 'Publisher'} Search`
        }));

        safeSendMessage({ type: 'CHECK_MYLIBRARY_BATCH', books: payload }, (resp) => {
          if (!resp || !resp.ok || !resp.data || !Array.isArray(resp.data.results)) {
            slice.forEach((c) => {
              c.libResult = { in_library: false, error: 'Offline' };
              renderSearchCardBadge(c);
            });
            return;
          }
          if (resp.mylibraryBaseUrl) currentSettings.mylibraryBaseUrl = resp.mylibraryBaseUrl;
          if (resp.annasBaseUrl) currentSettings.annasBaseUrl = resp.annasBaseUrl;

          resp.data.results.forEach((r) => {
            const cardEntry = searchCardStateMap.get(String(r.key));
            if (cardEntry) {
              cardEntry.libResult = r;
              renderSearchCardBadge(cardEntry);
            }
          });
        });
      }
    }
  }

  function tryMountBookPage() {
    if (!isExtensionContextValid()) {
      teardownInvalidatedContext();
      return;
    }
    if (!window.MyLibSiteAdapters) return;
    const url = new URL(window.location.href);

    // Also scan multi-card search / catalog pages (Pearson+, McGraw-Hill, Cengage, Wiley, Manning)
    scanSearchCardsOnPage(url);

    const adapter = window.MyLibSiteAdapters.findMatchingSiteAdapter(document, url, currentSettings.customSites);
    if (!adapter) return;

    const extracted = window.MyLibSiteAdapters.extractBookDetailsWithAdapter(adapter, document, url);
    if (!extracted.anchorEl || (!extracted.title && !extracted.isbnDigits)) return;

    const hrefKey = `${url.origin}${url.pathname}?ean=${url.searchParams.get('ean') || ''}`;
    if (lastCheckedHref === hrefKey && document.getElementById('mylibrary-bookpage-widget')) {
      return;
    }
    lastCheckedHref = hrefKey;

    currentBookState = {
      ...extracted,
      key: extracted.isbnDigits || extracted.asin || extracted.title || 'book_page',
      libResult: null,
      annasResult: null
    };

    renderBookPageWidget(currentBookState);
    checkBookInMyLibrary(currentBookState);
  }

  async function init() {
    if (!isExtensionContextValid()) return;
    await loadSettingsAndDismissed();
    tryMountBookPage();

    // Listen for live setting changes from the extension popup (e.g. switching Anna's Archive URL)
    try {
      if (isExtensionContextValid() && chrome.storage && chrome.storage.onChanged) {
        chrome.storage.onChanged.addListener((changes, area) => {
          if (!isExtensionContextValid()) {
            teardownInvalidatedContext();
            return;
          }
          if (area === 'sync') {
            loadSettingsAndDismissed().then(() => {
              if (currentBookState) {
                renderBookPageWidget(currentBookState);
              } else {
                tryMountBookPage();
              }
            });
          }
        });
      }
    } catch (_) {}

    // Observe SPA / format-switcher updates (e.g. switching Hardcover <-> Kindle <-> eBook on B&N or Amazon)
    let debounceTimer = null;
    pageObserver = new MutationObserver(() => {
      if (!isExtensionContextValid()) {
        if (debounceTimer) clearTimeout(debounceTimer);
        teardownInvalidatedContext();
        return;
      }
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        if (!isExtensionContextValid()) {
          teardownInvalidatedContext();
          return;
        }
        tryMountBookPage();
      }, 400);
    });
    if (document.body) {
      pageObserver.observe(document.body, { childList: true, subtree: true });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

