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
      } catch (_) { }
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
    const ownedSources = Array.isArray(lib.owned_sources) ? lib.owned_sources : [];
    const searchQ = lib.search_query || book.isbnDigits || book.title;
    const mylibUrl = `${currentSettings.mylibraryBaseUrl}/?q=${encodeURIComponent(searchQ)}`;

    const pill = document.createElement('a');
    pill.id = 'mylib-title-status-pill';
    pill.href = mylibUrl;
    pill.target = '_blank';
    pill.rel = 'noopener';

    if (inLib) {
      const fmts = (lib.formats && lib.formats.length > 0) ? lib.formats.join(', ') : 'BOOK';
      const ownedExtra = ownedSources.length > 0 ? ` + Owned on ${ownedSources.join(' & ')}` : '';
      pill.className = 'mylib-title-pill mylib-title-pill-owned';
      pill.textContent = `✅ In MyLibrary (${fmts})${ownedExtra}`;
      pill.title = 'This book is in your MyLibrary catalog! Click to open in MyLibrary.';
    } else if (ownedSources.length > 0) {
      pill.className = 'mylib-title-pill mylib-title-pill-owned';
      pill.textContent = `🏛️ Owned on ${ownedSources.join(' & ')}`;
      pill.title = `You own this title on ${ownedSources.join(' & ')}! Click to search in MyLibrary.`;
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
      const ownedSources = Array.isArray(lib.owned_sources) ? lib.owned_sources : [];
      const ownedBadgeHtml = ownedSources.length > 0
        ? `<span class="mylib-badge" style="background:rgba(16,185,129,0.22); border:1px solid rgba(52,211,153,0.65); color:#6ee7b7; font-size:13px; padding:5px 11px;" title="Imported from your ${escapeHtml(ownedSources.join(' & '))} Owned Titles library">🏛️ Owned on ${escapeHtml(ownedSources.join(' & '))}</span>`
        : '';

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
            ${ownedBadgeHtml}
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
        if (ownedSources.length > 0) {
          container.classList.add('mylib-owned');
        } else {
          container.classList.add('mylib-missing');
        }
        libStatusHtml = `
          <div class="mylib-bp-status-row">
            ${ownedBadgeHtml}
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
      if (Array.isArray(resp.enrichedBooks) && resp.enrichedBooks[0]) {
        const eb = resp.enrichedBooks[0];
        if (!book.title && eb.title) book.title = eb.title;
        if (!book.author && eb.author) book.author = eb.author;
        if (!book.pub_year && eb.pub_year) book.pub_year = eb.pub_year;
      }

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
    let ownedCardHtml = '';
    let borderColor = 'rgba(56,189,248,0.35)';
    if (lib) {
      const ownedSources = Array.isArray(lib.owned_sources) ? lib.owned_sources : [];
      if (ownedSources.length > 0) {
        borderColor = 'rgba(16,185,129,0.65)';
        ownedCardHtml = `<span style="border:1px solid rgba(52,211,153,0.6); background:rgba(16,185,129,0.22); color:#6ee7b7; border-radius:4px; padding:2px 6px; font-size:10.5px; font-weight:700;" title="Owned on ${escapeHtml(ownedSources.join(' & '))}">🏛️ Owned (${escapeHtml(ownedSources.join(' & '))})</span>`;
      }
      if (lib.error && !ownedSources.length) {
        borderColor = 'rgba(250,204,21,0.5)';
        statusHtml = `<button type="button" data-retry-card="1" style="cursor:pointer; border:1px solid rgba(250,204,21,0.5); background:rgba(234,179,8,0.2); color:#fde047; border-radius:4px; padding:2px 6px; font-size:10.5px; font-weight:700;">⚠️ Retry Check ↻</button>`;
      } else if (lib.in_library) {
        const fmts = (lib.formats && lib.formats.length > 0) ? lib.formats.join(', ') : 'BOOK';
        borderColor = 'rgba(34,197,94,0.6)';
        statusHtml = `<button type="button" data-open-url="${escapeHtml(mylibUrl)}" style="cursor:pointer; border:1px solid rgba(74,222,128,0.5); background:rgba(34,197,94,0.22); color:#4ade80; border-radius:4px; padding:2px 6px; font-size:10.5px; font-weight:700;">✅ In MyLibrary (${escapeHtml(fmts)}) ↗</button>`;
      } else if (Array.isArray(lib.other_editions) && lib.other_editions.length > 0) {
        if (!ownedSources.length) borderColor = 'rgba(250,204,21,0.55)';
        statusHtml = `<button type="button" data-open-url="${escapeHtml(mylibUrl)}" title="A different edition/year with this title is in MyLibrary" style="cursor:pointer; border:1px solid rgba(250,204,21,0.5); background:rgba(234,179,8,0.2); color:#fde047; border-radius:4px; padding:2px 6px; font-size:10.5px; font-weight:700;">⚠️ Diff. Edition in MyLibrary ↗</button>`;
      } else {
        if (!ownedSources.length) borderColor = 'rgba(248,113,113,0.45)';
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
      bar.innerHTML = `${statusHtml}${ownedCardHtml}${annasBtnHtml}${annasTitleBtnHtml}${authorBtnHtml}`;
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

          resp.data.results.forEach((r, idx) => {
            const cardEntry = searchCardStateMap.get(String(r.key));
            if (cardEntry) {
              const eb = Array.isArray(resp.enrichedBooks) ? resp.enrichedBooks[idx] : null;
              if (eb) {
                if (!cardEntry.title && eb.title) cardEntry.title = eb.title;
                if (!cardEntry.author && eb.author) cardEntry.author = eb.author;
                if (!cardEntry.pub_year && eb.pub_year) cardEntry.pub_year = eb.pub_year;
              }
              cardEntry.libResult = r;
              renderSearchCardBadge(cardEntry);
            }
          });
        });
      }
    }
  }

  let isAutoSyncingOwnedPage = false;

  async function tryPacktEntitlementsApiFetch() {
    const foundBooks = [];
    try {
      // 1. First try Packt's same-origin cookie-authenticated endpoint on subscription.packtpub.com
      if (/^subscription\.packtpub\.com$/i.test(window.location.hostname)) {
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
          rows.forEach((row) => {
            const sp = row.simplifiedProduct || {};
            const isbnsObj = sp.isbns || {};
            const isbn13 = String(sp.isbn13 || isbnsObj.print || isbnsObj.ebook || row.productId || '').replace(/[^0-9Xx]/g, '');
            const linkAuthors = Array.isArray(sp.linkAuthors) ? sp.linkAuthors.map((a) => a && a.author).filter(Boolean) : [];
            const pType = String(sp.type || sp.defaultType || '').toLowerCase();
            const is_video = pType.includes('video');
            const readUrl = sp.readUrl ? (sp.readUrl.startsWith('http') ? sp.readUrl : `https://subscription.packtpub.com${sp.readUrl}`) : '';
            foundBooks.push({
              isbn: isbn13,
              title: sp.title || row.productName || 'Untitled',
              author: linkAuthors.join(', ') || '',
              is_video,
              cover_image_url: sp.coverImage || sp.smallImage || '',
              web_reader_url: readUrl
            });
          });
          if (foundBooks.length >= totalCount || rows.length < limit) break;
        }
        if (foundBooks.length > 0) return foundBooks;
      }
    } catch (_) { }
    return foundBooks;
  }

  function collectOwnedBooksFromDom(sourceLabel) {
    const url = new URL(window.location.href);
    const cards = (window.MyLibSiteAdapters && window.MyLibSiteAdapters.extractSearchCardsFromPage(document, url)) || [];
    const byKey = new Map();

    cards.forEach((c) => {
      const k = c.isbnDigits || c.title;
      if (!k) return;
      byKey.set(k, {
        isbn: c.isbnDigits || c.isbnRaw || '',
        title: c.title || '',
        author: c.author || '',
        pub_year: c.pub_year || null
      });
    });

    // Also fallback-scan any links/rows on subscription.packtpub.com/owned or library.springer.com
    if (/packt/i.test(sourceLabel)) {
      document.querySelectorAll('a[href*="/product/"], a[href*="/book/"], a[href*="/video/"], img[src*="static.packt-cdn.com/products/"]').forEach((el) => {
        const srcOrHref = el.getAttribute('href') || el.getAttribute('src') || '';
        const m = srcOrHref.match(/(97[89][0-9]{10})/);
        const isbn = m ? m[1] : '';
        const row = el.closest('tr, li, article, .product-card, [class*="card"], [class*="owned"]') || el.parentElement;
        const titleEl = row && row.querySelector('h2, h3, h4, h5, [class*="title"], strong');
        const title =
          (titleEl && titleEl.textContent.replace(/\s+/g, ' ').trim()) ||
          (el.getAttribute('alt') || el.textContent || '').replace(/\s+/g, ' ').trim();
        const k = isbn || title;
        if (k && !byKey.has(k)) {
          byKey.set(k, { isbn, title });
        }
      });
    } else if (/springer/i.test(sourceLabel)) {
      document.querySelectorAll('a[href*="link.springer.com"], a[href*="/book/"], a[href*="978-"], [data-test*="book"]').forEach((el) => {
        const href = el.getAttribute('href') || '';
        const m = href.match(/(97[89][0-9\-]{10,14})/);
        const isbn = m ? m[1].replace(/[^0-9]/g, '') : '';
        const row = el.closest('tr, li, article, .c-card, [class*="card"], [class*="book"]') || el.parentElement;
        const titleEl = (row && row.querySelector('h2, h3, h4, [class*="title"]')) || el;
        const title = (titleEl && titleEl.textContent.replace(/\s+/g, ' ').trim()) || '';
        if ((isbn || title.length >= 4) && !/download\s+pdf|download\s+epub/i.test(title)) {
          const k = isbn || title;
          if (!byKey.has(k)) {
            byKey.set(k, { isbn, title });
          }
        }
      });
    }

    return Array.from(byKey.values());
  }

  function ensureOwnedLibrarySyncToolbar(url) {
    const isPacktOwned = /(^|\.)packtpub\.com$/i.test(url.hostname) && /^\/owned/i.test(url.pathname);
    const isSpringerLibrary = /^library\.springer\.com$/i.test(url.hostname);
    if (!isPacktOwned && !isSpringerLibrary) return;

    const sourceLabel = isPacktOwned ? 'Packt' : 'Springer';
    let bar = document.getElementById('mylib-owned-sync-toolbar');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'mylib-owned-sync-toolbar';
      bar.style.cssText =
        'position:sticky; top:0; z-index:9999; margin:10px auto; max-width:1280px; padding:11px 16px; border-radius:10px; background:linear-gradient(135deg,#0f172a 0%,#064e3b 100%); color:#f8fafc; border:1px solid rgba(52,211,153,0.6); box-shadow:0 8px 24px rgba(2,6,23,0.45); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:10px;';

      bar.innerHTML = `
        <div style="display:flex; align-items:center; flex-wrap:wrap; gap:10px;">
          <span style="font-weight:800; font-size:14px; color:#6ee7b7;">🏛️ MyLibrary · ${escapeHtml(sourceLabel)} Owned Titles Importer</span>
          <span id="mylib-owned-sync-status" style="font-size:12px; color:#d1fae5; background:rgba(6,78,59,0.75); border:1px solid rgba(52,211,153,0.4); padding:3px 9px; border-radius:999px;">
            Scanning page...
          </span>
        </div>
        <div style="display:flex; align-items:center; flex-wrap:wrap; gap:8px;">
          <button type="button" id="mylib-btn-import-owned-now" style="cursor:pointer; padding:6px 12px; border-radius:6px; border:1px solid #34d399; background:rgba(16,185,129,0.28); color:#ecfdf5; font-weight:700; font-size:12px;">
            📥 Import Visible Owned Titles
          </button>
          <button type="button" id="mylib-btn-autopage-owned" style="cursor:pointer; padding:6px 12px; border-radius:6px; border:1px solid #38bdf8; background:rgba(56,189,248,0.22); color:#e0f2fe; font-weight:700; font-size:12px;" title="Automatically scroll and click through all pagination pages to import every owned title">
            ⏬ Auto-Scan All Pages &amp; Import
          </button>
        </div>
      `;

      const hostTarget = document.querySelector('main, #main, .container, body') || document.body;
      hostTarget.insertBefore(bar, hostTarget.firstChild);

      bar.querySelector('#mylib-btn-import-owned-now').addEventListener('click', () => {
        runOwnedPageImport(sourceLabel, false);
      });
      bar.querySelector('#mylib-btn-autopage-owned').addEventListener('click', () => {
        runOwnedPageImport(sourceLabel, true);
      });
    }

    // Automatically import any visible owned titles on the current page
    const visibleBooks = collectOwnedBooksFromDom(sourceLabel);
    if (visibleBooks.length > 0 && Number(bar.dataset.lastImportedCount || '0') !== visibleBooks.length) {
      bar.dataset.lastImportedCount = String(visibleBooks.length);
      safeSendMessage({ type: 'IMPORT_OWNED_TITLES', source: sourceLabel, books: visibleBooks }, (resp) => {
        const statusEl = document.getElementById('mylib-owned-sync-status');
        if (statusEl && resp && resp.ok) {
          const srcTotal = isPacktOwned ? resp.packtCount : resp.springerCount;
          statusEl.textContent = `✅ Auto-Imported ${visibleBooks.length} on page · Total ${sourceLabel} Owned: ${srcTotal}`;
        }
        scanSearchCardsOnPage(url);
      });
    } else if (visibleBooks.length === 0 && !bar.dataset.checkedInitialStats) {
      bar.dataset.checkedInitialStats = '1';
      safeSendMessage({ type: 'GET_OWNED_STATS' }, (resp) => {
        const statusEl = document.getElementById('mylib-owned-sync-status');
        if (statusEl && resp && resp.ok) {
          const srcTotal = isPacktOwned ? resp.packtCount : resp.springerCount;
          statusEl.textContent = `Total ${sourceLabel} Owned Saved: ${srcTotal} — click Import when titles load`;
        }
      });
    }
  }

  async function runOwnedPageImport(sourceLabel, autoPaginate = false) {
    const statusEl = document.getElementById('mylib-owned-sync-status');
    const autoBtn = document.getElementById('mylib-btn-autopage-owned');
    if (autoPaginate && isAutoSyncingOwnedPage) {
      isAutoSyncingOwnedPage = false;
      if (autoBtn) autoBtn.textContent = '⏬ Auto-Scan All Pages & Import';
      return;
    }

    if (autoPaginate) {
      isAutoSyncingOwnedPage = true;
      if (autoBtn) autoBtn.textContent = '⏹ Stop Auto-Scan';
    }

    const accumulated = new Map();
    const addBooks = (list) => {
      list.forEach((b) => {
        const k = b.isbn || b.title;
        if (k && !accumulated.has(k)) accumulated.set(k, b);
      });
    };

    if (/packt/i.test(sourceLabel)) {
      if (statusEl) statusEl.textContent = '⏳ Checking Packt account entitlements & page DOM...';
      const apiBooks = await tryPacktEntitlementsApiFetch();
      addBooks(apiBooks);
    }

    let pageNum = 1;
    while (true) {
      // Scroll to bottom to trigger any lazy-loaded cards
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
      await new Promise((r) => setTimeout(r, 850));
      window.scrollTo({ top: 0, behavior: 'smooth' });

      const domBooks = collectOwnedBooksFromDom(sourceLabel);
      addBooks(domBooks);

      if (statusEl) {
        statusEl.textContent = `⏳ Importing ${accumulated.size} ${sourceLabel} owned titles (page ${pageNum})...`;
      }

      await new Promise((resolve) => {
        safeSendMessage(
          { type: 'IMPORT_OWNED_TITLES', source: sourceLabel, books: Array.from(accumulated.values()) },
          (resp) => {
            if (statusEl && resp && resp.ok) {
              const srcTotal = /packt/i.test(sourceLabel) ? resp.packtCount : resp.springerCount;
              statusEl.textContent = `✅ Synced ${accumulated.size} titles (Total ${sourceLabel} Owned: ${srcTotal})`;
            }
            resolve();
          }
        );
      });

      if (!autoPaginate || !isAutoSyncingOwnedPage) break;

      // Look for an active "Next" pagination button or link
      const nextBtn = Array.from(
        document.querySelectorAll(
          'a[rel="next"], button[aria-label*="Next" i], a[aria-label*="Next" i], .pagination-next a, .page-item:not(.disabled) a.page-link, [class*="pagination"] button:not([disabled])'
        )
      ).find((el) => {
        const txt = (el.textContent || el.getAttribute('aria-label') || '').trim();
        const isDisabled =
          el.hasAttribute('disabled') ||
          el.getAttribute('aria-disabled') === 'true' ||
          /\bdisabled\b/i.test(el.className || '') ||
          (el.parentElement && /\bdisabled\b/i.test(el.parentElement.className || ''));
        return !isDisabled && (/^(?:next|›|»|>)$/i.test(txt) || /next\s*page/i.test(txt));
      });

      if (!nextBtn || pageNum >= 50) break;
      pageNum += 1;
      nextBtn.click();
      await new Promise((r) => setTimeout(r, 1800));
    }

    isAutoSyncingOwnedPage = false;
    if (autoBtn) autoBtn.textContent = '⏬ Auto-Scan All Pages & Import';
    scanSearchCardsOnPage(new URL(window.location.href));
  }

  function tryMountBookPage() {
    if (!isExtensionContextValid()) {
      teardownInvalidatedContext();
      return;
    }
    if (!window.MyLibSiteAdapters) return;
    const url = new URL(window.location.href);

    // Mount Owned Titles Importer toolbar if on subscription.packtpub.com/owned or library.springer.com
    ensureOwnedLibrarySyncToolbar(url);

    // Also scan multi-card search / catalog pages (Pearson+, McGraw-Hill, Cengage, Wiley, Manning, Packt, Springer Library)
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

    // Support automatic background sync when opened with ?mylib_autosync=1 from the extension popup
    try {
      const initUrl = new URL(window.location.href);
      if (initUrl.searchParams.get('mylib_autosync') === '1') {
        const isPacktOwned = /(^|\.)packtpub\.com$/i.test(initUrl.hostname) && /^\/owned/i.test(initUrl.pathname);
        const isSpringerLib = /^library\.springer\.com$/i.test(initUrl.hostname);
        if (isPacktOwned || isSpringerLib) {
          setTimeout(() => {
            runOwnedPageImport(isPacktOwned ? 'Packt' : 'Springer', true);
          }, 1800);
        }
      }
    } catch (_) { }

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
    } catch (_) { }

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

    // Bridge messages from the MyLibrary web UI (e.g. Amazon Only (Not on NAS) -> Send to Kindle)
    window.addEventListener('message', (ev) => {
      if (!ev || !ev.data || ev.data.source !== 'MYLIBRARY_WEB_UI') return;
      const reqId = ev.data.reqId || '';
      const action = ev.data.action || '';
      if (action === 'PING_EXTENSION') {
        window.postMessage({ source: 'MYLIBRARY_EXTENSION', reqId, ok: true, version: '1.4.0' }, '*');
        return;
      }
      if (action === 'CHECK_ANNAS_SINGLE' || action === 'OPEN_ANNAS_SESSION_TAB') {
        safeSendMessage(
          {
            type: action,
            book: ev.data.book || {}
          },
          (resp) => {
            window.postMessage(
              {
                source: 'MYLIBRARY_EXTENSION',
                reqId,
                ...(resp || { ok: false, error: 'No response from MyLibrary extension background worker' })
              },
              '*'
            );
          }
        );
        return;
      }
      if (action === 'FETCH_MYCD_DEVICES' || action === 'DELIVER_TO_KINDLE_BATCH' || action === 'DEREGISTER_MYCD_DEVICES') {
        safeSendMessage(
          {
            type: action,
            csrfToken: ev.data.csrfToken || '',
            items: ev.data.items || [],
            devicesToRemove: ev.data.devicesToRemove || [],
            keepDeviceAccountID: ev.data.keepDeviceAccountID || '',
            keepDeviceAccountIDs: ev.data.keepDeviceAccountIDs || [],
            keepDeviceNames: ev.data.keepDeviceNames || [],
            deviceAccountID: ev.data.deviceAccountID || '',
            deviceSerialNumber: ev.data.deviceSerialNumber || ''
          },
          (resp) => {
            window.postMessage(
              {
                source: 'MYLIBRARY_EXTENSION',
                reqId,
                ...(resp || { ok: false, error: 'No response from MyLibrary extension background worker' })
              },
              '*'
            );
          }
        );
      }
    });

    // If we are on Amazon Manage Your Content & Devices (/hz/mycd/digital-console/alldevices or /device-details),
    // mount a floating "MyLibrary Device & Download Cleaner" banner that drives the real page!
    if (/amazon\.com\/hz\/mycd\/digital-console/i.test(location.href)) {
      mountMycdDeviceCleanerBanner();
    }
  }

  function mountMycdDeviceCleanerBanner() {
    if (document.getElementById('mylibrary-mycd-cleaner-bar')) return;

    const KEEP_IDS = new Set([
      'A101329625FBGFO0QECXO', // JAMES's Kindle for Windows = PC (_8YLYgU)
      'A015285311F3W73S7EA31', // JAMES's 18th Android Device = Tab S11 (_RfOPZk)
      'A018607231PVOX7INFXHE', // JAMES's 12th Android Device = Galaxy S25 Ultra (_GNgiXh)
      'A0440102192GKG2EE19T3', // JAMES's 8th Android Device = Tab S8 Ultra (_vlEewv)
      'A02063483UBXG3PKOGGOX', // Jimmy's tablet = Tab A8
      'A05507311ZARZZO8ORYGN'  // JAMES's Main = Cloud Reader
    ]);
    const KEEP_NAMES = new Set([
      "james's kindle for windows",
      "jimmy's tab s11",
      "jimmy's galaxy s25 ultra",
      "jimmy's tab s8 ultra",
      "jimmy's tab a8",
      "james's kindle for web",
      "pc",
      "tab s11",
      "galaxy s25 ultra",
      "tab s8 ultra",
      "tab a8",
      "cloud reader"
    ]);

    const RENAME_BY_ID = {
      'A101329625FBGFO0QECXO': 'PC',
      'A015285311F3W73S7EA31': 'Tab S11',
      'A018607231PVOX7INFXHE': 'Galaxy S25 Ultra',
      'A0440102192GKG2EE19T3': 'Tab S8 Ultra',
      'A02063483UBXG3PKOGGOX': 'Tab A8',
      'A05507311ZARZZO8ORYGN': 'Cloud Reader'
    };
    const RENAME_BY_OLD_NAME = {
      "james's kindle for windows": 'PC',
      "james's 19th android device": 'Tab A8',
      "james's 12th android device": 'Galaxy S25 Ultra',
      "james's 8th android device": 'Tab S8 Ultra',
      "jimmy's tablet": 'Tab A8',
      "james's main": 'Cloud Reader'
    };

    const bar = document.createElement('div');
    bar.id = 'mylibrary-mycd-cleaner-bar';
    bar.style.cssText = 'position:fixed; bottom:18px; right:18px; z-index:2147483647; width:440px; background:linear-gradient(135deg,#0f172a,#1e293b); color:#f8fafc; border:2px solid #a855f7; border-radius:14px; padding:14px 16px; box-shadow:0 16px 40px rgba(0,0,0,0.65); font-family:system-ui,-apple-system,sans-serif; font-size:13px;';
    bar.innerHTML = `
      <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
        <strong style="color:#d8b4fe; font-size:14px;">📚 MyLibrary Kindle Device Manager</strong>
        <button id="ml-mycd-close" style="background:transparent; border:none; color:#94a3b8; cursor:pointer; font-size:16px;">✕</button>
      </div>
      <div style="color:#cbd5e1; font-size:12px; line-height:1.45; margin-bottom:10px;">
        <strong>Active Devices Mapping:</strong><br>
        • <code>PC</code> (JAMES's Kindle for Windows)<br>
        • <code>Tab S11</code> (18th Android) &nbsp;• <code>Galaxy S25 Ultra</code> (12th Android)<br>
        • <code>Tab S8 Ultra</code> (8th Android) &nbsp;• <code>Tab A8</code> (Jimmy's tablet) &nbsp;• <code>Cloud Reader</code>
      </div>
      <div style="display:flex; gap:8px; flex-wrap:wrap;">
        <button id="ml-mycd-rename-btn" style="flex:1; background:linear-gradient(135deg,#0284c7,#0369a1); color:#fff; border:none; border-radius:8px; padding:8px 10px; font-weight:700; cursor:pointer; font-size:12px;">
          ✏️ Rename Active Devices on Amazon
        </button>
        <button id="ml-mycd-run-btn" style="flex:1; background:linear-gradient(135deg,#9333ea,#7e22ce); color:#fff; border:none; border-radius:8px; padding:8px 10px; font-weight:700; cursor:pointer; font-size:12px;">
          🚀 Deregister Unused Devices
        </button>
      </div>
      <div id="ml-mycd-status" style="margin-top:9px; font-size:12px; color:#38bdf8; display:none; max-height:160px; overflow-y:auto;"></div>
    `;
    document.body.appendChild(bar);
    document.getElementById('ml-mycd-close').onclick = () => bar.remove();

    document.getElementById('ml-mycd-rename-btn').onclick = async () => {
      const btn = document.getElementById('ml-mycd-rename-btn');
      const st = document.getElementById('ml-mycd-status');
      btn.disabled = true;
      st.style.display = 'block';
      st.innerHTML = '⏳ Renaming active devices on Amazon...';

      let csrf = window.csrfToken || '';
      if (!csrf) {
        const m = document.documentElement.innerHTML.match(/csrfToken\s*[=:]\s*["']([^"']+)["']/i);
        if (m) csrf = m[1];
      }

      try {
        const b1 = new URLSearchParams({
          activity: 'GetDevicesOverview',
          activityInput: JSON.stringify({ surfaceType: 'Desktop' }),
          clientId: 'MYCD_WebService',
          csrfToken: csrf
        });
        const r1 = await fetch('https://www.amazon.com/hz/mycd/digital-console/ajax', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
          body: b1.toString(),
          credentials: 'include'
        });
        const j1 = await r1.json();
        const rawDevs = ((j1.GetDevicesOverview || {}).deviceList || []);
        const kindleDevs = rawDevs.filter(d => ['KINDLE_APP', 'FIRE_TABLET', 'KINDLE'].includes(d.deviceFamily));

        let renamedCount = 0;
        const logs = [];

        for (const d of kindleDevs) {
          const aid = String(d.deviceAccountID || '').trim();
          const oldName = String(d.deviceName || '').trim();
          const targetName = RENAME_BY_ID[aid] || RENAME_BY_OLD_NAME[oldName.toLowerCase()];
          if (!targetName || oldName === targetName) continue;

          const dsn = d.deviceSerialNumber || '';
          const dt = d.deviceTypeID || '';
          let ok = false;

          // 1. Try MYCD ajax EditDeviceName / RenameDevice / UpdateDeviceName
          for (const actName of ['EditDeviceName', 'UpdateDeviceName', 'RenameDevice', 'SetDeviceName']) {
            try {
              const b = new URLSearchParams({
                activity: actName,
                activityInput: JSON.stringify({
                  deviceSerialNumber: dsn,
                  deviceType: dt,
                  deviceTypeID: dt,
                  deviceAccountId: aid,
                  deviceAccountID: aid,
                  deviceName: targetName,
                  newDeviceName: targetName,
                  surfaceType: 'Desktop'
                }),
                clientId: 'MYCD_WebService',
                csrfToken: csrf
              });
              const r = await fetch('https://www.amazon.com/hz/mycd/digital-console/ajax', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
                body: b.toString(),
                credentials: 'include'
              });
              const j = await r.json();
              const sub = j[actName] || j;
              if (r.status === 200 && (sub.success === true || j.success === true)) {
                ok = true;
                break;
              }
            } catch (_) {}
          }

          // 2. Fallback: click the "Edit" link next to the device name in the DOM table!
          if (!ok) {
            const rows = Array.from(document.querySelectorAll('tr, div, li')).filter(el => {
              const t = (el.innerText || '');
              return t.includes(oldName) && /edit/i.test(t) && t.length < 600;
            }).sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);

            if (rows.length > 0) {
              const row = rows[0];
              const editLink = Array.from(row.querySelectorAll('a, button, span[role="button"], div[role="button"]'))
                .find(el => /^edit$/i.test((el.innerText || '').trim()));
              if (editLink) {
                editLink.click();
                await new Promise(r => setTimeout(r, 450));
                const input = row.querySelector('input[type="text"], input:not([type="hidden"]):not([type="checkbox"])')
                  || document.querySelector('input[value*="' + oldName.slice(0, 8) + '"], input[type="text"]');
                if (input) {
                  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                  nativeSetter.call(input, targetName);
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  await new Promise(r => setTimeout(r, 250));
                  const saveBtn = Array.from(document.querySelectorAll('button, a, span[role="button"], div[role="button"], input[type="submit"]'))
                    .find(el => /^(save|submit|update|confirm)$/i.test((el.innerText || el.value || '').trim()) && el.offsetParent !== null);
                  if (saveBtn) {
                    saveBtn.click();
                    ok = true;
                    await new Promise(r => setTimeout(r, 700));
                  }
                }
              }
            }
          }

          if (ok) {
            renamedCount++;
            logs.push(`✓ ${oldName} → <strong>${targetName}</strong>`);
          } else {
            logs.push(`⚠ Could not auto-rename ${oldName} (click Edit next to it to paste "${targetName}")`);
          }
        }

        st.innerHTML = `<strong style="color:#4ade80;">✅ Renamed ${renamedCount} device(s)!</strong><br>` + logs.join('<br>');
      } catch (err) {
        st.innerHTML = `❌ Error: ${err}`;
      }
      btn.disabled = false;
    };

    document.getElementById('ml-mycd-run-btn').onclick = async () => {
      const btn = document.getElementById('ml-mycd-run-btn');
      const st = document.getElementById('ml-mycd-status');
      btn.disabled = true;
      st.style.display = 'block';
      st.innerHTML = '⏳ Extracting CSRF token and querying registered Kindle devices...';

      let csrf = window.csrfToken || '';
      if (!csrf) {
        const m = document.documentElement.innerHTML.match(/csrfToken\s*[=:]\s*["']([^"']+)["']/i);
        if (m) csrf = m[1];
      }
      if (!csrf) {
        st.innerHTML = '❌ Could not find csrfToken on page. Please refresh the page and sign in if prompted.';
        btn.disabled = false;
        return;
      }

      try {
        const b1 = new URLSearchParams({
          activity: 'GetDevicesOverview',
          activityInput: JSON.stringify({ surfaceType: 'Desktop' }),
          clientId: 'MYCD_WebService',
          csrfToken: csrf
        });
        const r1 = await fetch('https://www.amazon.com/hz/mycd/digital-console/ajax', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
          body: b1.toString(),
          credentials: 'include'
        });
        const j1 = await r1.json();
        const rawDevs = ((j1.GetDevicesOverview || {}).deviceList || []);
        const kindleDevs = rawDevs.filter(d => ['KINDLE_APP', 'FIRE_TABLET', 'KINDLE'].includes(d.deviceFamily));

        const toRemove = kindleDevs.filter(d => {
          const aid = String(d.deviceAccountID || '').trim();
          const dn = String(d.deviceName || '').trim().toLowerCase();
          const dtStr = String(d.deviceTypeString || '').trim().toLowerCase();
          if (dtStr.includes('cloud reader')) return false;
          return !KEEP_IDS.has(aid) && !KEEP_NAMES.has(dn);
        });

        st.innerHTML = `Found ${kindleDevs.length} Kindle devices (${toRemove.length} unused to deregister, ${kindleDevs.length - toRemove.length} active to keep)...`;

        // Also inspect loaded scripts on this MYCD page to find exact activity name for deregistering
        let discoveredActivity = 'DeregisterDevice';
        const scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
        for (const u of scripts) {
          try {
            const txt = await (await fetch(u)).text();
            const actMatches = txt.match(/["']([A-Za-z0-9_]*[Dd]eregister[A-Za-z0-9_]*)["']/g);
            if (actMatches && actMatches.length) {
              console.log('[MyLibrary MYCD] Discovered deregister strings in bundle:', actMatches);
            }
          } catch (_) { }
        }

        let deregCount = 0;
        const logs = [];
        for (const d of toRemove) {
          const dsn = d.deviceSerialNumber || '';
          const dt = d.deviceTypeID || '';
          const aid = d.deviceAccountID || '';
          let ok = false;
          let lastErr = '';

          // Try candidate MYCD activity / param formats
          const attempts = [
            {
              url: 'https://www.amazon.com/hz/mycd/digital-console/ajax',
              body: new URLSearchParams({
                activity: 'DeregisterDevice',
                activityInput: JSON.stringify({
                  deviceSerialNumber: dsn,
                  deviceType: dt,
                  deviceTypeID: dt,
                  deviceAccountId: aid,
                  deviceAccountID: aid,
                  surfaceType: 'Desktop'
                }),
                clientId: 'MYCD_WebService',
                csrfToken: csrf
              })
            },
            {
              url: 'https://www.amazon.com/hz/mycd/ajax',
              body: new URLSearchParams({
                clientId: 'MYCD_WebService',
                csrfToken: csrf,
                data: JSON.stringify({
                  param: {
                    DeregisterDevice: {
                      deviceSerialNumber: dsn,
                      deviceType: dt,
                      deviceTypeID: dt,
                      deviceAccountId: aid,
                      deviceAccountID: aid
                    }
                  }
                })
              })
            },
            {
              url: 'https://www.amazon.com/hz/mycd/ajax',
              body: new URLSearchParams({
                clientId: 'MYCD_WebService',
                csrfToken: csrf,
                data: JSON.stringify({
                  param: {
                    Deregister: {
                      deviceSerialNumber: dsn,
                      deviceType: dt,
                      deviceAccountId: aid
                    }
                  }
                })
              })
            }
          ];

          for (const att of attempts) {
            try {
              const r = await fetch(att.url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
                body: att.body.toString(),
                credentials: 'include'
              });
              const j = await r.json();
              const sub = j.DeregisterDevice || j.Deregister || j;
              if (r.status === 200 && (sub.success === true || j.success === true)) {
                ok = true;
                break;
              } else {
                lastErr = JSON.stringify(j).slice(0, 120);
              }
            } catch (e) {
              lastErr = String(e);
            }
          }

          if (ok) {
            deregCount++;
            logs.push(`✓ Deregistered: ${d.deviceName}`);
          } else {
            logs.push(`⚠ ${d.deviceName}: ${lastErr}`);
          }
          st.innerHTML = `Processed ${logs.length}/${toRemove.length} (${deregCount} deregistered)...<br>` + logs.slice(-5).join('<br>');
        }

        // If API attempts returned an error, auto-click the DOM Deregister buttons on /devicedetails!
        if (deregCount === 0 && toRemove.length > 0) {
          st.innerHTML += `<br>🔍 Clicking Deregister buttons on page for unused devices...`;
          let domClicked = 0;
          const allEls = Array.from(document.querySelectorAll('div, tr, li, section, article'));
          for (const d of toRemove) {
            const dn = (d.deviceName || '').trim();
            if (!dn) continue;
            // Find the smallest container holding this deviceName AND a Deregister button
            const matchingContainers = allEls.filter(el => {
              const t = (el.innerText || '');
              return t.includes(dn) && /deregister/i.test(t) && t.length < 900;
            }).sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);

            if (matchingContainers.length > 0) {
              const box = matchingContainers[0];
              const dBtn = Array.from(box.querySelectorAll('button, a, span[role="button"], div[role="button"], input[type="button"], input[type="submit"]'))
                .find(b => /^deregister$/i.test((b.innerText || b.value || '').trim()));
              if (dBtn) {
                dBtn.click();
                await new Promise(r => setTimeout(r, 650));
                const confirmBtn = Array.from(document.querySelectorAll('button, a, span[role="button"], div[role="button"], input[type="button"], input[type="submit"]'))
                  .find(b => b !== dBtn && /^deregister$/i.test((b.innerText || b.value || '').trim()) && b.offsetParent !== null);
                if (confirmBtn) {
                  confirmBtn.click();
                  domClicked++;
                  await new Promise(r => setTimeout(r, 900));
                }
              }
            }
          }
          st.innerHTML = `<strong style="color:#4ade80;">✅ Done! Deregistered ${deregCount + domClicked} unused devices. Kept your 6 active devices!</strong><br>` + logs.join('<br>');
        } else {
          st.innerHTML = `<strong style="color:#4ade80;">✅ Done! Deregistered ${deregCount} unused devices. Kept your 6 active devices!</strong><br>` + logs.join('<br>');
        }
      } catch (err) {
        st.innerHTML = `❌ Error: ${err}`;
      }
      btn.disabled = false;
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

