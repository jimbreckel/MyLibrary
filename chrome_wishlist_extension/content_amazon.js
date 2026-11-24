// Content script for Amazon Wishlists (Books (Kindle), Print Books, etc.) & Book Product Pages

(function () {
  'use strict';

  const TARGET_WISHLIST_NAMES = ['books (kindle)', 'print books'];
  const itemStateByKey = new Map(); // key -> { key, el, asin, isbn, pub_year, title, author, wishlist_name, libResult, annasResult, dpVerified }
  const dismissedKeys = new Set();
  const dpMetaCache = new Map(); // asin -> { isbn, pub_year, full_title }
  let activeFilter = 'all'; // 'all' | 'in_lib' | 'missing' | 'on_annas'
  let mylibraryBaseUrl = 'http://192.168.1.43:8080/mylibrary';
  let annasBaseUrl = 'https://annas-archive.pk';
  let isAutoScrolling = false;
  let isCheckingAnnasBatch = false;
  let pendingScanTimer = null;
  let wishlistObserver = null;

  function isExtensionContextValid() {
    try {
      return Boolean(typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.id);
    } catch (_) {
      return false;
    }
  }

  function teardownInvalidatedContext() {
    isAutoScrolling = false;
    if (pendingScanTimer) {
      clearTimeout(pendingScanTimer);
      pendingScanTimer = null;
    }
    if (wishlistObserver) {
      try {
        wishlistObserver.disconnect();
      } catch (_) {}
      wishlistObserver = null;
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

  function loadDismissedKeys() {
    return new Promise((resolve) => {
      if (!isExtensionContextValid() || !chrome.storage || !chrome.storage.local) {
        resolve();
        return;
      }
      try {
        chrome.storage.local.get({ dismissedWishlistFalsePositives: [] }, (res) => {
          const safeRes = res || {};
          const list = Array.isArray(safeRes.dismissedWishlistFalsePositives) ? safeRes.dismissedWishlistFalsePositives : [];
          list.forEach((k) => dismissedKeys.add(String(k)));
          resolve();
        });
      } catch (_) {
        teardownInvalidatedContext();
        resolve();
      }
    });
  }

  function saveDismissedKeys() {
    if (!isExtensionContextValid() || !chrome.storage || !chrome.storage.local) return;
    try {
      chrome.storage.local.set({ dismissedWishlistFalsePositives: Array.from(dismissedKeys) });
    } catch (_) {
      teardownInvalidatedContext();
    }
  }

  function isEntryDismissed(entry) {
    return dismissedKeys.has(entry.asin || entry.key) || dismissedKeys.has(entry.key);
  }

  function isEntryInLibrary(entry) {
    if (isEntryDismissed(entry)) return false;
    return Boolean(
      entry.libResult &&
      (entry.libResult.in_library || (Array.isArray(entry.libResult.owned_sources) && entry.libResult.owned_sources.length > 0))
    );
  }

  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function detectActiveWishlistName() {
    const candidates = [
      document.querySelector('#profile-list-name'),
      document.querySelector('[id^="wl-list-title"]'),
      document.querySelector('.wl-list-selected'),
      document.querySelector('#your-lists-nav .a-text-bold')
    ];
    for (const el of candidates) {
      if (el && el.textContent && el.textContent.trim()) {
        return el.textContent.trim();
      }
    }
    return document.title.replace(/Amazon\.com\s*:\s*/i, '').trim() || 'Amazon Wishlist';
  }

  function highlightTargetWishlistsInSidebar() {
    const links = document.querySelectorAll('#your-lists-nav a, [id^="wl-list-entry"] a, a[href*="/hz/wishlist/ls/"]');
    links.forEach((a) => {
      const txt = (a.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
      if (TARGET_WISHLIST_NAMES.some((t) => txt.includes(t))) {
        a.classList.add('mylib-wishlist-nav-highlight');
        a.title = 'Target Book Wishlist monitored by MyLibrary Checker';
      }
    });
  }

  function extractAsinFromUrl(href) {
    if (!href) return '';
    const m = String(href).match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})(?:[/?&#]|$)/i);
    return m ? m[1].toUpperCase() : '';
  }

  function extractIsbnFromContainer(container, asin) {
    if (asin && /^[0-9]{9}[0-9X]$/.test(asin)) {
      return asin;
    }
    const text = (container && container.textContent) || '';
    const m13 = text.match(/\b(97[89][0-9\- ]{10,14})\b/);
    if (m13) {
      const c13 = m13[1].replace(/[^0-9]/g, '');
      if (c13.length === 13) return c13;
    }
    const m10 = text.match(/ISBN(?:-10)?\s*[:#]?\s*([0-9]{9}[0-9Xx])\b/i);
    if (m10) {
      return m10[1].toUpperCase();
    }
    return '';
  }

  function parseCurrencyNumber(str) {
    if (!str) return null;
    const cleaned = String(str).replace(/[^0-9.,]/g, '').trim();
    if (!cleaned) return null;
    const normalized =
      cleaned.includes(',') && cleaned.includes('.')
        ? cleaned.replace(/,/g, '')
        : cleaned.replace(',', '.');
    const val = parseFloat(normalized);
    return Number.isFinite(val) && val > 0 ? val : null;
  }

  function extractSaleInfoFromContainer(container) {
    if (!container) return { onSale: false, currentPrice: '', oldPrice: '', dropText: '', discountPct: null, summary: '' };

    // 1. Current active price on the wishlist row
    let currentPrice = '';
    const curPriceEl =
      container.querySelector('span[id^="itemPrice_"] .a-offscreen') ||
      container.querySelector('.a-price:not([data-a-strike="true"]) .a-offscreen') ||
      container.querySelector('span[id^="itemPrice_"] .a-price-whole');
    if (curPriceEl && curPriceEl.textContent) {
      const txt = curPriceEl.textContent.replace(/\s+/g, ' ').trim();
      if (/\d/.test(txt)) {
        const fracEl =
          curPriceEl.classList && curPriceEl.classList.contains('a-price-whole')
            ? container.querySelector('span[id^="itemPrice_"] .a-price-fraction')
            : null;
        const symEl =
          curPriceEl.classList && curPriceEl.classList.contains('a-price-whole')
            ? container.querySelector('span[id^="itemPrice_"] .a-price-symbol')
            : null;
        if (fracEl) {
          const sym = (symEl && symEl.textContent.trim()) || '$';
          const whole = txt.replace(/[^0-9]/g, '');
          const frac = (fracEl.textContent || '00').replace(/[^0-9]/g, '');
          currentPrice = `${sym}${whole}.${frac}`;
        } else {
          currentPrice = txt;
        }
      }
    }
    if (!currentPrice) {
      const dataPrice = container.getAttribute('data-price');
      const dpNum = parseFloat(dataPrice || '');
      if (Number.isFinite(dpNum) && dpNum > 0) {
        currentPrice = `$${dpNum.toFixed(2)}`;
      }
    }

    // 2. Strikethrough / Was / List price on the wishlist row
    let oldPrice = '';
    const strikeEl =
      container.querySelector('.a-price[data-a-strike="true"] .a-offscreen') ||
      container.querySelector('span.a-text-strike') ||
      container.querySelector('.a-text-strike');
    if (strikeEl && strikeEl.textContent) {
      const stTxt = strikeEl.textContent.replace(/\s+/g, ' ').trim();
      if (/\d/.test(stTxt)) {
        oldPrice = stTxt;
      }
    }

    // 3. Amazon's "Price dropped X% / $Y since added" callout or Deal badge
    let dropText = '';
    const priceDropEl = container.querySelector(
      '.itemPriceDrop, [id^="itemPriceDrop_"], [class*="itemPriceDrop"], [class*="priceDrop"]'
    );
    if (priceDropEl && priceDropEl.textContent) {
      dropText = priceDropEl.textContent.replace(/\s+/g, ' ').trim();
    } else {
      const rowText = (container.innerText || container.textContent || '').replace(/\s+/g, ' ');
      const mDrop = rowText.match(/\b(Price\s+dropped\s+[^.\n\r]+?)(?:\s+since\s+added[^.\n\r]*|\s*\.|$)/i);
      if (mDrop) {
        dropText = mDrop[1].trim();
      }
    }

    let dealBadgeText = '';
    const dealEl = container.querySelector('[id^="dealBadge"], .wl-deal-badge, [data-csa-c-badge-type], .a-badge-text');
    if (dealEl && dealEl.textContent) {
      const dTxt = dealEl.textContent.replace(/\s+/g, ' ').trim();
      if (/deal|save|off|discount|coupon|sale/i.test(dTxt)) {
        dealBadgeText = dTxt;
      }
    } else {
      const rowText = (container.innerText || container.textContent || '').replace(/\s+/g, ' ');
      const mDeal = rowText.match(/\b(Limited\s+time\s+deal|Lightning\s+Deal|Deal\s+of\s+the\s+Day|Save\s+\d+%)\b/i);
      if (mDeal) {
        dealBadgeText = mDeal[1].trim();
      }
    }

    const curNum = parseCurrencyNumber(currentPrice);
    const oldNum = parseCurrencyNumber(oldPrice);
    let discountPct = null;

    if (curNum && oldNum && oldNum > curNum) {
      discountPct = Math.round(((oldNum - curNum) / oldNum) * 100);
    } else if (dropText) {
      const mPct = dropText.match(/(\d+)\s*%/);
      if (mPct) discountPct = parseInt(mPct[1], 10);
    } else if (dealBadgeText) {
      const mPct2 = dealBadgeText.match(/(\d+)\s*%/);
      if (mPct2) discountPct = parseInt(mPct2[1], 10);
    }

    const hasValidStrikeDiscount = Boolean(oldPrice && (!curNum || !oldNum || oldNum > curNum));
    const onSale = Boolean(dropText || hasValidStrikeDiscount || dealBadgeText);
    if (!onSale) {
      return { onSale: false, currentPrice, oldPrice: '', dropText: '', discountPct: null, summary: '' };
    }

    const parts = [];
    if (currentPrice) parts.push(currentPrice);
    if (oldPrice && hasValidStrikeDiscount) parts.push(`was ${oldPrice}`);
    if (discountPct && discountPct > 0) parts.push(`-${discountPct}%`);
    if (dropText && (!discountPct || !/price\s+dropped\s+\d+%\s*$/i.test(dropText))) {
      const shortDrop = dropText.replace(/\s+since\s+added.*$/i, '').trim();
      if (!parts.some((p) => p.toLowerCase() === shortDrop.toLowerCase())) {
        parts.push(shortDrop);
      }
    } else if (dealBadgeText && !parts.includes(dealBadgeText)) {
      parts.push(dealBadgeText);
    }

    return {
      onSale: true,
      currentPrice,
      oldPrice: hasValidStrikeDiscount ? oldPrice : '',
      dropText: dropText || dealBadgeText,
      discountPct,
      summary: parts.join(' · ') || 'Reduced Price'
    };
  }

  function applySaleHighlightToRow(entry) {
    if (!entry || !entry.el) return;
    entry.saleInfo = extractSaleInfoFromContainer(entry.el);
    if (entry.saleInfo && entry.saleInfo.onSale) {
      entry.el.classList.add('mylib-wishlist-item-on-sale');
    } else {
      entry.el.classList.remove('mylib-wishlist-item-on-sale');
    }
  }

  function extractWishlistItemsFromDom() {
    const wishlistName = detectActiveWishlistName();
    const rows = Array.from(
      document.querySelectorAll('li[data-itemid], .g-item-sortable, div[id^="itemMain_"], div[id^="item_"]')
    );

    const newlyFound = [];
    rows.forEach((row) => {
      // Avoid nested duplicate matches inside the same li[data-itemid]
      const ownerLi = row.closest('li[data-itemid]');
      const container = ownerLi || row;
      if (container.dataset.mylibKey) return;

      const itemId =
        container.getAttribute('data-itemid') ||
        (container.id && container.id.replace(/^item(?:Main)?_/, '')) ||
        '';

      const titleAnchor =
        container.querySelector('a[id^="itemName_"]') ||
        container.querySelector('h2 a[href*="/dp/"], h3 a[href*="/dp/"]') ||
        container.querySelector('a[href*="/dp/"]');

      if (!titleAnchor) return;
      const rawTitle = (titleAnchor.getAttribute('title') || titleAnchor.textContent || '').replace(/\s+/g, ' ').trim();
      if (!rawTitle) return;

      const bylineEl =
        container.querySelector('span[id^="item-byline-"]') ||
        container.querySelector('.a-size-base.a-color-secondary');
      const rawAuthor = bylineEl ? bylineEl.textContent.replace(/\s+/g, ' ').trim() : '';

      let asin = extractAsinFromUrl(titleAnchor.getAttribute('href') || '');
      if (!asin) {
        const repoParams = container.getAttribute('data-reposition-action-params') || '';
        const mAsin = repoParams.match(/ASIN:([A-Z0-9]{10})/i) || repoParams.match(/ISBN:([0-9Xx]{10,13})/i);
        if (mAsin) asin = mAsin[1].toUpperCase();
      }

      const key = itemId || asin || `${rawTitle}__${rawAuthor}`;
      container.dataset.mylibKey = key;

      if (!itemStateByKey.has(key)) {
        const detectedIsbn = extractIsbnFromContainer(container, asin);
        const entry = {
          key,
          el: container,
          titleAnchor,
          asin,
          isbn: detectedIsbn,
          title: rawTitle,
          author: rawAuthor,
          wishlist_name: wishlistName,
          saleInfo: extractSaleInfoFromContainer(container),
          libResult: null,
          annasResult: null
        };
        applySaleHighlightToRow(entry);
        itemStateByKey.set(key, entry);
        renderItemPanel(entry);
        newlyFound.push(entry);
      } else {
        const existingEntry = itemStateByKey.get(key);
        if (existingEntry) {
          applySaleHighlightToRow(existingEntry);
        }
      }
    });

    if (newlyFound.length > 0) {
      updateToolbarCounts();
    }
    return newlyFound;
  }

  function ensureTopToolbar() {
    let bar = document.getElementById('mylibrary-wishlist-bar');
    if (bar) {
      const listPill = bar.querySelector('#mylib-active-list-name');
      if (listPill) listPill.textContent = detectActiveWishlistName();
      return bar;
    }

    const host =
      document.getElementById('wl-item-view') ||
      document.getElementById('g-items') ||
      document.querySelector('#right-nav, #centerCol, main') ||
      document.body;

    bar = document.createElement('div');
    bar.id = 'mylibrary-wishlist-bar';
    bar.innerHTML = `
      <div class="mylib-bar-left">
        <span class="mylib-brand-title">📚 MyLibrary + Anna's Archive</span>
        <span id="mylib-active-list-name" class="mylib-list-pill">${escapeHtml(detectActiveWishlistName())}</span>
        <button type="button" class="mylib-stat-chip active-filter" data-filter="all" id="mylib-chip-all">
          Scanned: <strong id="mylib-cnt-total">0</strong>
        </button>
        <button type="button" class="mylib-stat-chip" data-filter="on_sale" id="mylib-chip-sale" title="Click to show ONLY wishlist books currently on sale / reduced in price">
          🏷️ On Sale: <strong id="mylib-cnt-sale" style="color:#fbbf24;">0</strong>
        </button>
        <button type="button" class="mylib-stat-chip" data-filter="in_lib" id="mylib-chip-inlib" title="Click to show ONLY wishlist books already in MyLibrary">
          ✅ In MyLibrary: <strong id="mylib-cnt-inlib" style="color:#4ade80;">0</strong>
        </button>
        <button type="button" class="mylib-stat-chip" data-filter="missing" id="mylib-chip-missing" title="Click to show ONLY wishlist books NOT in MyLibrary">
          ❌ Not in Library: <strong id="mylib-cnt-missing" style="color:#fca5a5;">0</strong>
        </button>
        <button type="button" class="mylib-stat-chip" data-filter="on_annas" id="mylib-chip-annas" title="Click to show wishlist books found on Anna's Archive">
          🔎 On Anna's Archive: <strong id="mylib-cnt-annas" style="color:#38bdf8;">0</strong>
        </button>
      </div>
      <div class="mylib-bar-actions">
        <button type="button" class="mylib-action-btn" id="mylib-btn-rescan" title="Re-check visible wishlist items against MyLibrary">
          🔄 Check Library
        </button>
        <button type="button" class="mylib-action-btn" id="mylib-btn-autoscroll" title="Automatically scroll down to load the entire wishlist and check every book">
          ⏬ Auto-Scroll Entire Wishlist
        </button>
        <button type="button" class="mylib-action-btn mylib-btn-annas" id="mylib-btn-check-annas" title="Check Anna's Archive (annas-archive.pk) for all wishlist books not yet in MyLibrary">
          📚 Check Anna's Archive (Missing)
        </button>
        <button type="button" class="mylib-action-btn" id="mylib-btn-export" title="Download CSV report of wishlist books in MyLibrary & Anna's Archive">
          📥 Export CSV
        </button>
      </div>
    `;

    host.parentNode.insertBefore(bar, host);

    bar.querySelectorAll('.mylib-stat-chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        activeFilter = chip.getAttribute('data-filter') || 'all';
        bar.querySelectorAll('.mylib-stat-chip').forEach((c) => c.classList.remove('active-filter'));
        chip.classList.add('active-filter');
        applyWishlistFilter();
      });
    });

    bar.querySelector('#mylib-btn-rescan').addEventListener('click', () => {
      extractWishlistItemsFromDom();
      checkPendingItemsInMyLibrary(Array.from(itemStateByKey.values()));
    });

    bar.querySelector('#mylib-btn-autoscroll').addEventListener('click', () => {
      toggleAutoScrollWishlist();
    });

    bar.querySelector('#mylib-btn-check-annas').addEventListener('click', () => {
      runAnnasArchiveBatchCheck();
    });

    bar.querySelector('#mylib-btn-export').addEventListener('click', () => {
      exportWishlistCsv();
    });

    return bar;
  }

  function updateToolbarCounts() {
    ensureTopToolbar();
    let total = 0;
    let onSale = 0;
    let inLib = 0;
    let missing = 0;
    let onAnnas = 0;

    itemStateByKey.forEach((entry) => {
      total += 1;
      applySaleHighlightToRow(entry);
      if (entry.saleInfo && entry.saleInfo.onSale) {
        onSale += 1;
      }
      if (entry.libResult) {
        if (isEntryInLibrary(entry)) inLib += 1;
        else missing += 1;
      }
      if (entry.annasResult && entry.annasResult.found) {
        onAnnas += 1;
      }
    });

    const elTotal = document.getElementById('mylib-cnt-total');
    const elSale = document.getElementById('mylib-cnt-sale');
    const elInLib = document.getElementById('mylib-cnt-inlib');
    const elMissing = document.getElementById('mylib-cnt-missing');
    const elAnnas = document.getElementById('mylib-cnt-annas');
    if (elTotal) elTotal.textContent = String(total);
    if (elSale) elSale.textContent = String(onSale);
    if (elInLib) elInLib.textContent = String(inLib);
    if (elMissing) elMissing.textContent = String(missing);
    if (elAnnas) elAnnas.textContent = String(onAnnas);
  }

  function applyWishlistFilter() {
    itemStateByKey.forEach((entry) => {
      if (!entry.el || entry.key === 'product_page') return;
      let visible = true;
      if (activeFilter === 'on_sale') {
        visible = Boolean(entry.saleInfo && entry.saleInfo.onSale);
      } else if (activeFilter === 'in_lib') {
        visible = Boolean(entry.libResult && isEntryInLibrary(entry));
      } else if (activeFilter === 'missing') {
        visible = Boolean(entry.libResult && !isEntryInLibrary(entry));
      } else if (activeFilter === 'on_annas') {
        visible = Boolean(entry.annasResult && entry.annasResult.found);
      }
      entry.el.style.display = visible ? '' : 'none';
    });
  }

  function renderItemPanel(entry) {
    const safeKeyId = `mylib-panel-${entry.key.replace(/[^a-zA-Z0-9_-]/g, '_')}`;
    let panel = document.getElementById(safeKeyId);
    if (!panel) {
      panel = document.createElement('div');
      panel.id = safeKeyId;
      panel.className = 'mylib-item-panel';
      const targetAnchor =
        entry.el.querySelector('[id^="itemInfo_"]') ||
        entry.titleAnchor.parentElement ||
        entry.el;
      targetAnchor.appendChild(panel);
    }

    const lib = entry.libResult;
    const annas = entry.annasResult;
    const primaryIsbn = (lib && lib.primary_isbn) || (lib && Array.isArray(lib.isbns) && lib.isbns[0]) || entry.isbn || '';
    const titleQ = (lib && (lib.annas_title_query || lib.base_title || lib.cleaned_title)) || `${entry.title} ${entry.author}`.trim();
    const searchQ = (lib && lib.search_query) || entry.title || entry.asin;
    const mylibSearchUrl = `${mylibraryBaseUrl}/?q=${encodeURIComponent(searchQ)}`;
    const annasIsbnUrl = primaryIsbn
      ? ((lib && lib.annas_isbn_search_url) || `${annasBaseUrl}/search?q=${encodeURIComponent(primaryIsbn)}`)
      : '';
    const annasTitleUrl = (lib && lib.annas_title_search_url) || `${annasBaseUrl}/search?q=${encodeURIComponent(titleQ)}`;
    const annasSearchUrl = (annas && annas.searchUrl) || annasIsbnUrl || annasTitleUrl;

    applySaleHighlightToRow(entry);
    const sale = entry.saleInfo || { onSale: false };

    panel.classList.remove('mylib-owned', 'mylib-missing', 'mylib-sale');
    if (sale.onSale) {
      panel.classList.add('mylib-sale');
    }

    const saleBadgeHtml = sale.onSale
      ? `<span class="mylib-badge mylib-badge-sale" title="${escapeHtml(sale.dropText || sale.summary || 'Reduced price on Amazon')}">🔥 ON SALE: ${escapeHtml(sale.summary || 'Reduced Price')}</span>`
      : '';

    let libBadgeHtml = `<span class="mylib-badge" style="background:rgba(148,163,184,0.2); color:#cbd5e1;">⏳ Checking MyLibrary...</span>`;
    let detailsHtml = '';

    if (lib) {
      const dismissed = isEntryDismissed(entry);
      const ownedSources = Array.isArray(lib.owned_sources) ? lib.owned_sources : [];
      const ownedBadgeHtml = ownedSources.length > 0
        ? `<span class="mylib-badge" style="background:rgba(16,185,129,0.22); border:1px solid rgba(52,211,153,0.65); color:#6ee7b7;" title="Imported from your ${escapeHtml(ownedSources.join(' & '))} Owned Titles">🏛️ Owned on ${escapeHtml(ownedSources.join(' & '))}</span>`
        : '';

      if (lib.in_library && !dismissed) {
        panel.classList.add('mylib-owned');
        const fmts = (lib.formats && lib.formats.length > 0) ? lib.formats.join(', ') : (lib.item_types || ['BOOK']).join(', ');
        const enrTag = lib.is_enriched ? ' · ✨ Enriched' : '';
        const matchViaLabel = (lib.match_type === 'isbn' || lib.match_type === 'asin')
          ? `Exact ${lib.match_type.toUpperCase()}`
          : (lib.match_type === 'title_author' ? 'Title/Author Match' : 'Title Only Match');
        libBadgeHtml = `
          <a href="${escapeHtml(mylibSearchUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-in-lib" title="Open in MyLibrary (${escapeHtml(matchViaLabel)})">
            ✅ In MyLibrary (${escapeHtml(fmts)}${enrTag} · ${escapeHtml(matchViaLabel)}) ↗
          </a>
          ${ownedBadgeHtml}
          <button type="button" class="mylib-badge mylib-badge-annas-none mylib-btn-dismiss-fp" data-key="${escapeHtml(entry.key)}" title="Mark as False Positive (e.g. different edition or newer book with the same title)">
            ✕ False Positive
          </button>
        `;
        const matchLines = (lib.matches || []).slice(0, 3).map((m) => {
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
        if (ownedSources.length > 0 && !dismissed) {
          panel.classList.add('mylib-owned');
        } else {
          panel.classList.add('mylib-missing');
        }
        libBadgeHtml = `
          ${ownedBadgeHtml}
          <a href="${escapeHtml(mylibSearchUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-not-in-lib" title="Not found in MyLibrary — click to search manually">
            ❌ Not in MyLibrary${dismissed ? ' (Dismissed False Positive)' : ''} ↗
          </a>
          ${dismissed ? `<button type="button" class="mylib-badge mylib-badge-annas-none mylib-btn-undo-fp" data-key="${escapeHtml(entry.key)}" title="Restore MyLibrary match">↩ Undo</button>` : ''}
        `;
        const otherEds = Array.isArray(lib.other_editions) ? lib.other_editions : [];
        if (otherEds.length > 0) {
          const otherLines = otherEds.slice(0, 2).map((oe) => {
            const oeUrl = `${mylibraryBaseUrl}/?q=${encodeURIComponent(oe.isbn || oe.title)}`;
            const reasonStr = oe.note ? ` — <span style="color:#fbbf24;">${escapeHtml(oe.note)}</span>` : '';
            const yrStr = oe.pub_date ? ` (${escapeHtml(oe.pub_date)})` : '';
            return `<div>ℹ️ Different book/edition with same title in Library: <strong>${escapeHtml(oe.title)}</strong>${yrStr}${reasonStr} <a href="${escapeHtml(oeUrl)}" target="_blank" rel="noopener">View</a></div>`;
          });
          detailsHtml = `<div class="mylib-match-details">${otherLines.join('')}</div>`;
        }
      }
    }

    let annasBadgeHtml = '';
    if (annas) {
      if (annas.checking) {
        const checkLabel = primaryIsbn ? `⏳ Checking Anna's (ISBN ${primaryIsbn} & Title)...` : `⏳ Checking Anna's (Title)...`;
        annasBadgeHtml = `<span class="mylib-badge mylib-badge-annas-none">${escapeHtml(checkLabel)}</span>`;
      } else if (annas.ok && annas.found) {
        const fmtStr = (annas.formats && annas.formats.length) ? ` · ${annas.formats.join('/')}` : '';
        const viaStr = annas.matchedBy ? ` [${annas.matchedBy}]` : '';
        const topHref = (annas.topMatch && annas.topMatch.url) ? annas.topMatch.url : annasSearchUrl;
        annasBadgeHtml = `
          <a href="${escapeHtml(annasSearchUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-annas-found" title="Found on Anna's Archive (checked by ${escapeHtml(annas.checkedIsbn ? 'ISBN ' + annas.checkedIsbn + ' & Title' : 'Title')})">
            📚 Anna's Archive: ${annas.count} match${annas.count === 1 ? '' : 'es'}${escapeHtml(viaStr)}${escapeHtml(fmtStr)} ↗
          </a>
          <a href="${escapeHtml(topHref)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-annas-link" title="${escapeHtml((annas.topMatch && annas.topMatch.label) || 'Open top match on Anna\'s Archive')}">
            ⬇ Top Match ${annas.topMatch && annas.topMatch.size ? '(' + escapeHtml(annas.topMatch.size) + ')' : ''} ↗
          </a>
          ${annasIsbnUrl ? `<a href="${escapeHtml(annasIsbnUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-annas-none" title="View ISBN search (${escapeHtml(primaryIsbn)}) on Anna's Archive">🔢 ISBN ↗</a>` : ''}
          <a href="${escapeHtml(annasTitleUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-annas-none" title="View Title search on Anna's Archive">🔤 Title ↗</a>
        `;
      } else if (annas.ok && !annas.found) {
        annasBadgeHtml = `
          <a href="${escapeHtml(annasTitleUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-annas-none" title="0 automatic matches after checking ${escapeHtml(primaryIsbn ? 'ISBN ' + primaryIsbn + ' and Title' : 'Title')} — click to search Anna's Archive directly">
            📚 Anna's Archive: 0 matches (Search Title ↗)
          </a>
          ${annasIsbnUrl ? `<a href="${escapeHtml(annasIsbnUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-annas-none" title="Search ISBN ${escapeHtml(primaryIsbn)} on Anna's Archive">🔢 ISBN ↗</a>` : ''}
        `;
      } else {
        annasBadgeHtml = `
          <a href="${escapeHtml(annasTitleUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-annas-link" title="${escapeHtml(annas.error || 'Open on Anna\'s Archive')}">
            🔎 Open Title on Anna's Archive ↗
          </a>
          ${annasIsbnUrl ? `<a href="${escapeHtml(annasIsbnUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-annas-none" title="Open ISBN ${escapeHtml(primaryIsbn)} on Anna's Archive">🔢 ISBN (${escapeHtml(primaryIsbn)}) ↗</a>` : ''}
        `;
      }
    } else {
      annasBadgeHtml = `
        <button type="button" class="mylib-badge mylib-badge-annas-link mylib-btn-single-annas" data-key="${escapeHtml(entry.key)}" title="Check https://annas-archive.pk/ by ${escapeHtml(primaryIsbn ? 'ISBN (' + primaryIsbn + ') AND Title' : 'Title')}">
          🔍 Check Anna's (${primaryIsbn ? 'ISBN + Title' : 'Title'})
        </button>
        ${annasIsbnUrl ? `<a href="${escapeHtml(annasIsbnUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-annas-none" title="Search ISBN ${escapeHtml(primaryIsbn)} on https://annas-archive.pk/">🔢 ISBN ↗</a>` : ''}
        <a href="${escapeHtml(annasTitleUrl)}" target="_blank" rel="noopener" class="mylib-badge mylib-badge-annas-none" title="Search Title on https://annas-archive.pk/">
          🔤 Title ↗
        </a>
      `;
    }

    panel.innerHTML = `
      <div class="mylib-badges-row">
        ${saleBadgeHtml}
        ${libBadgeHtml}
        ${annasBadgeHtml}
      </div>
      ${detailsHtml}
    `;

    const singleBtn = panel.querySelector('.mylib-btn-single-annas');
    if (singleBtn) {
      singleBtn.addEventListener('click', (e) => {
        e.preventDefault();
        checkSingleItemOnAnnas(entry);
      });
    }

    const dismissBtn = panel.querySelector('.mylib-btn-dismiss-fp');
    if (dismissBtn) {
      dismissBtn.addEventListener('click', (e) => {
        e.preventDefault();
        dismissedKeys.add(entry.asin || entry.key);
        dismissedKeys.add(entry.key);
        saveDismissedKeys();
        renderItemPanel(entry);
        updateToolbarCounts();
        applyWishlistFilter();
      });
    }

    const undoBtn = panel.querySelector('.mylib-btn-undo-fp');
    if (undoBtn) {
      undoBtn.addEventListener('click', (e) => {
        e.preventDefault();
        dismissedKeys.delete(entry.asin || entry.key);
        dismissedKeys.delete(entry.key);
        saveDismissedKeys();
        renderItemPanel(entry);
        updateToolbarCounts();
        applyWishlistFilter();
      });
    }
  }

  /**
   * Fetch /dp/<ASIN> on amazon.com (same-origin) to extract exact Print ISBN-13/10,
   * Publication Year, and full Title/Subtitle when an item only had a B0... Kindle ASIN on the wishlist row.
   */
  async function fetchAmazonProductPageMeta(asin) {
    if (!asin) return null;
    if (dpMetaCache.has(asin)) return dpMetaCache.get(asin);
    try {
      const resp = await fetch(`/dp/${encodeURIComponent(asin)}`, {
        method: 'GET',
        credentials: 'same-origin'
      });
      if (!resp.ok) return null;
      const html = await resp.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');

      let isbn = '';
      const isbn13El =
        doc.querySelector('#rpi-attribute-book_details-isbn13 .rpi-attribute-value') ||
        doc.querySelector('#rpi-attribute-book_details-isbn10 .rpi-attribute-value');
      if (isbn13El && isbn13El.textContent) {
        const cleaned = isbn13El.textContent.replace(/[^0-9Xx]/g, '').toUpperCase();
        if (cleaned.length === 10 || cleaned.length === 13) isbn = cleaned;
      }
      if (!isbn) {
        const bulletsText = (
          (doc.getElementById('detailBullets_feature_div') && doc.getElementById('detailBullets_feature_div').textContent) ||
          (doc.getElementById('productDetails_techSpec_section_1') && doc.getElementById('productDetails_techSpec_section_1').textContent) ||
          ''
        );
        const m13 = bulletsText.match(/ISBN-13\s*[:\u200f\u200e\s]*([0-9\-]{13,17})/i);
        const m10 = bulletsText.match(/ISBN-10\s*[:\u200f\u200e\s]*([0-9Xx\-]{10,13})/i);
        if (m13) isbn = m13[1].replace(/[^0-9]/g, '');
        else if (m10) isbn = m10[1].replace(/[^0-9Xx]/g, '').toUpperCase();
      }
      // Also check format-switcher links (#tmmSwatches / twister) for linked Print edition /dp/<ISBN10>
      if (!isbn) {
        const swatchLinks = Array.from(doc.querySelectorAll('#tmmSwatches a[href*="/dp/"], #twister a[href*="/dp/"]'));
        for (const a of swatchLinks) {
          const candAsin = extractAsinFromUrl(a.getAttribute('href') || '');
          if (candAsin && /^[0-9]{9}[0-9X]$/.test(candAsin)) {
            isbn = candAsin;
            break;
          }
        }
      }

      let pub_year = null;
      const pubDateEl =
        doc.querySelector('#rpi-attribute-book_details-publication_date .rpi-attribute-value') ||
        doc.getElementById('detailBullets_feature_div') ||
        doc.getElementById('bylineInfo');
      if (pubDateEl && pubDateEl.textContent) {
        const ym = pubDateEl.textContent.match(/\b(19[7-9]\d|20[0-2]\d)\b/g);
        if (ym && ym.length) {
          pub_year = Math.max(...ym.map((y) => parseInt(y, 10)));
        }
      }

      let edition = '';
      const edEl = doc.querySelector('#rpi-attribute-book_details-edition .rpi-attribute-value');
      if (edEl && edEl.textContent && edEl.textContent.trim()) {
        const rawEd = edEl.textContent.replace(/\s+/g, ' ').trim();
        edition = /edition|ed\./i.test(rawEd) ? rawEd : `${rawEd} Edition`;
      } else {
        const metaBlockText = [
          doc.getElementById('productSubtitle') && doc.getElementById('productSubtitle').textContent,
          doc.getElementById('bylineInfo') && doc.getElementById('bylineInfo').textContent,
          doc.getElementById('detailBullets_feature_div') && doc.getElementById('detailBullets_feature_div').textContent
        ].filter(Boolean).join(' ');
        const mEd = metaBlockText.match(/\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th))\s+(?:ed\.?|edition)\b/i);
        if (mEd) {
          edition = mEd[0].trim();
        }
      }

      const prodTitleEl = doc.getElementById('productTitle') || doc.getElementById('ebooksProductTitle');
      const prodSubEl = doc.getElementById('productSubtitle');
      let full_title = prodTitleEl ? prodTitleEl.textContent.replace(/\s+/g, ' ').trim() : '';
      if (prodSubEl && prodSubEl.textContent) {
        const subTxt = prodSubEl.textContent.replace(/\s+/g, ' ').trim();
        if (subTxt && !/hardcover|paperback|kindle|audible|spiral|mass market/i.test(subTxt)) {
          full_title = `${full_title}: ${subTxt}`;
        }
      }

      const meta = { isbn, pub_year, edition, full_title };
      dpMetaCache.set(asin, meta);
      return meta;
    } catch (_) {
      return null;
    }
  }

  /**
   * When a wishlist book matched MyLibrary ONLY by title/author (not by ISBN or ASIN),
   * fetch its Amazon /dp/<ASIN> page to get the exact Publication Year, Edition, Subtitle, and Print ISBN,
   * then re-verify with MyLibrary so newer books/editions with the same title as an older book are caught automatically.
   */
  async function verifyTitleOnlyMatchViaProductPage(entry) {
    if (!entry || !entry.asin || entry.dpVerified) return;
    entry.dpVerified = true;
    const meta = await fetchAmazonProductPageMeta(entry.asin);
    if (!meta) return;

    let changed = false;
    if (meta.isbn && !entry.isbn) {
      entry.isbn = meta.isbn;
      changed = true;
    }
    if (meta.pub_year && entry.pub_year !== meta.pub_year) {
      entry.pub_year = meta.pub_year;
      changed = true;
    }
    if (meta.edition && entry.edition !== meta.edition) {
      entry.edition = meta.edition;
      changed = true;
    }
    if (meta.full_title && meta.full_title.length > (entry.title || '').length + 3) {
      entry.title = meta.full_title;
      changed = true;
    }
    if (changed) {
      checkPendingItemsInMyLibrary([entry]);
    }
  }

  function buildAnnasBookPayload(entry) {
    const lib = entry.libResult || {};
    return {
      key: entry.key,
      asin: entry.asin || '',
      isbn: entry.isbn || '',
      primary_isbn: lib.primary_isbn || entry.isbn || '',
      isbns: lib.isbns || (entry.isbn ? [entry.isbn] : []),
      title: lib.cleaned_title || entry.title,
      base_title: lib.base_title || entry.title,
      author: lib.cleaned_author || entry.author,
      annas_isbn_query: lib.annas_isbn_query || '',
      annas_title_query: lib.annas_title_query || '',
      annas_title_only_query: lib.annas_title_only_query || '',
      annas_query: lib.annas_query || ''
    };
  }

  async function checkSingleItemOnAnnas(entry) {
    entry.annasResult = { checking: true };
    renderItemPanel(entry);
    if (!entry.isbn && entry.asin && !entry.dpVerified) {
      await verifyTitleOnlyMatchViaProductPage(entry);
    }
    const payloadBook = buildAnnasBookPayload(entry);
    safeSendMessage({ type: 'CHECK_ANNAS_SINGLE', book: payloadBook }, (res) => {
      entry.annasResult = res || { ok: false, error: 'No response' };
      renderItemPanel(entry);
      updateToolbarCounts();
      applyWishlistFilter();
    });
  }

  async function runAnnasArchiveBatchCheck() {
    if (isCheckingAnnasBatch) return;
    const btn = document.getElementById('mylib-btn-check-annas');
    const targets = Array.from(itemStateByKey.values()).filter(
      (e) => !isEntryInLibrary(e) && (!e.annasResult || !e.annasResult.ok)
    );
    if (targets.length === 0) {
      alert('All missing wishlist books on screen have already been checked against Anna\'s Archive!');
      return;
    }

    isCheckingAnnasBatch = true;
    for (let i = 0; i < targets.length; i++) {
      if (!isExtensionContextValid()) break;
      const entry = targets[i];
      if (btn) {
        btn.textContent = `⏳ Checking Anna's (${i + 1}/${targets.length})...`;
      }
      if (!entry.isbn && entry.asin && !entry.dpVerified) {
        await verifyTitleOnlyMatchViaProductPage(entry);
      }
      await new Promise((resolve) => {
        entry.annasResult = { checking: true };
        renderItemPanel(entry);
        const payloadBook = buildAnnasBookPayload(entry);
        safeSendMessage({ type: 'CHECK_ANNAS_SINGLE', book: payloadBook }, (res) => {
          entry.annasResult = res || { ok: false };
          renderItemPanel(entry);
          updateToolbarCounts();
          resolve();
        });
      });
      await new Promise((r) => setTimeout(r, 950));
    }

    isCheckingAnnasBatch = false;
    if (btn) {
      btn.textContent = `📚 Check Anna's Archive (Missing)`;
    }
  }

  function checkPendingItemsInMyLibrary(entries) {
    if (!entries || entries.length === 0 || !isExtensionContextValid()) return;
    const batchSize = 50;
    for (let i = 0; i < entries.length; i += batchSize) {
      const slice = entries.slice(i, i + batchSize);
      const booksPayload = slice.map((e) => ({
        key: e.key,
        asin: e.asin,
        isbn: e.isbn,
        pub_year: e.pub_year || null,
        edition: e.edition || '',
        title: e.title,
        author: e.author,
        wishlist_name: e.wishlist_name
      }));

      safeSendMessage({ type: 'CHECK_MYLIBRARY_BATCH', books: booksPayload }, (resp) => {
        if (!resp || !resp.ok || !resp.data || !Array.isArray(resp.data.results)) {
          slice.forEach((e) => {
            e.libResult = { in_library: false, error: (resp && resp.error) || 'Offline' };
            renderItemPanel(e);
          });
          updateToolbarCounts();
          return;
        }
        if (resp.mylibraryBaseUrl) mylibraryBaseUrl = resp.mylibraryBaseUrl;
        if (resp.annasBaseUrl) annasBaseUrl = resp.annasBaseUrl;

        resp.data.results.forEach((r) => {
          const entry = itemStateByKey.get(String(r.key));
          if (entry) {
            entry.libResult = r;
            renderItemPanel(entry);
            // If matched only by title/author (not exact ISBN/ASIN) and not yet verified via /dp/<ASIN>,
            // verify the product page's publication year & ISBN in the background to prevent same-title false positives!
            if (r.in_library && (r.match_type === 'title' || r.match_type === 'title_author') && entry.asin && !entry.dpVerified) {
              verifyTitleOnlyMatchViaProductPage(entry);
            }
          }
        });
        updateToolbarCounts();
        applyWishlistFilter();
      });
    }
  }

  function toggleAutoScrollWishlist() {
    const btn = document.getElementById('mylib-btn-autoscroll');
    if (isAutoScrolling) {
      isAutoScrolling = false;
      if (btn) btn.textContent = '⏬ Auto-Scroll Entire Wishlist';
      return;
    }
    isAutoScrolling = true;
    if (btn) btn.textContent = '⏹ Stop Auto-Scroll';

    let unchangedCount = 0;
    let lastCount = itemStateByKey.size;

    const step = () => {
      if (!isAutoScrolling) return;
      window.scrollBy({ top: 900, behavior: 'smooth' });
      const newEntries = extractWishlistItemsFromDom();
      if (newEntries.length > 0) {
        checkPendingItemsInMyLibrary(newEntries);
      }

      const atBottom = (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 150);
      const endOfListMarker = document.querySelector('#endOfListMarker, .wl-end-of-list');
      if (itemStateByKey.size === lastCount && atBottom) {
        unchangedCount += 1;
      } else {
        unchangedCount = 0;
        lastCount = itemStateByKey.size;
      }

      if (unchangedCount >= 4 || (endOfListMarker && atBottom && unchangedCount >= 2)) {
        isAutoScrolling = false;
        if (btn) btn.textContent = '✅ Entire Wishlist Scanned';
        window.scrollTo({ top: 0, behavior: 'smooth' });
        return;
      }
      setTimeout(step, 1100);
    };
    step();
  }

  function exportWishlistCsv() {
    const rows = [
      ['Wishlist', 'Title', 'Author', 'ASIN', 'ISBNs', 'On Sale', 'Current Price', 'Sale Details', 'In MyLibrary', 'MyLibrary Formats', 'On Annas Archive', 'Annas Matches', 'Annas Search URL']
    ];
    itemStateByKey.forEach((e) => {
      const lib = e.libResult || {};
      const annas = e.annasResult || {};
      const sale = e.saleInfo || {};
      rows.push([
        e.wishlist_name || '',
        lib.cleaned_title || e.title || '',
        lib.cleaned_author || e.author || '',
        e.asin || '',
        (lib.isbns || []).join(' '),
        sale.onSale ? 'YES' : 'NO',
        sale.currentPrice || '',
        sale.summary || '',
        lib.in_library ? 'YES' : 'NO',
        (lib.formats || []).join('/'),
        annas.found ? 'YES' : (annas.ok ? 'NO' : 'UNCHECKED'),
        annas.count !== undefined ? String(annas.count) : '',
        lib.annas_search_url || `${annasBaseUrl}/search?q=${encodeURIComponent(e.title || '')}`
      ]);
    });

    const csvContent = rows
      .map((r) => r.map((cell) => `"${String(cell || '').replace(/"/g, '""')}"`).join(','))
      .join('\n');

    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `mylibrary_wishlist_${detectActiveWishlistName().toLowerCase().replace(/[^a-z0-9]+/g, '_')}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function init() {
    if (!isExtensionContextValid()) return;
    await loadDismissedKeys();
    ensureTopToolbar();
    highlightTargetWishlistsInSidebar();
    const initialEntries = extractWishlistItemsFromDom();
    if (initialEntries.length > 0) {
      checkPendingItemsInMyLibrary(initialEntries);
    }

    wishlistObserver = new MutationObserver(() => {
      if (!isExtensionContextValid()) {
        teardownInvalidatedContext();
        return;
      }
      if (pendingScanTimer) clearTimeout(pendingScanTimer);
      pendingScanTimer = setTimeout(() => {
        if (!isExtensionContextValid()) {
          teardownInvalidatedContext();
          return;
        }
        highlightTargetWishlistsInSidebar();
        const added = extractWishlistItemsFromDom();
        if (added.length > 0) {
          checkPendingItemsInMyLibrary(added);
        }
      }, 350);
    });

    if (document.body) {
      wishlistObserver.observe(document.body, { childList: true, subtree: true });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

