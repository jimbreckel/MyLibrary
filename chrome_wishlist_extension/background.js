// Background Service Worker for MyLibrary — Amazon Wishlist & Anna's Archive Checker

const DEFAULT_SETTINGS = {
  mylibraryBaseUrl: 'http://192.168.1.43:8080/mylibrary',
  annasBaseUrl: 'https://annas-archive.pk',
  autoCheckAnnasForMissing: false
};

async function getSettings() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(DEFAULT_SETTINGS, (items) => {
      resolve({
        mylibraryBaseUrl: (items.mylibraryBaseUrl || DEFAULT_SETTINGS.mylibraryBaseUrl).replace(/\/+$/, ''),
        annasBaseUrl: (items.annasBaseUrl || DEFAULT_SETTINGS.annasBaseUrl).replace(/\/+$/, ''),
        autoCheckAnnasForMissing: Boolean(items.autoCheckAnnasForMissing)
      });
    });
  });
}

function normStr(s) {
  return String(s || '')
    .toLowerCase()
    .replace(/\(.*?\)|\[.*?\]/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function isbn10ToIsbn13(isbn10) {
  const clean = String(isbn10 || '').replace(/[^0-9Xx]/g, '').toUpperCase();
  if (clean.length !== 10 || !/^[0-9]{9}[0-9X]$/.test(clean)) return '';
  const prefix = '978' + clean.slice(0, 9);
  let sum = 0;
  for (let i = 0; i < 12; i++) {
    sum += (i % 2 === 0 ? 1 : 3) * parseInt(prefix[i], 10);
  }
  const check = (10 - (sum % 10)) % 10;
  return `${prefix}${check}`;
}

function isbn13ToIsbn10(isbn13) {
  const clean = String(isbn13 || '').replace(/[^0-9]/g, '');
  if (clean.length !== 13 || !clean.startsWith('978')) return '';
  const core = clean.slice(3, 12);
  let sum = 0;
  for (let i = 0; i < 9; i++) {
    sum += (10 - i) * parseInt(core[i], 10);
  }
  const rem = (11 - (sum % 11)) % 11;
  const check = rem === 10 ? 'X' : String(rem);
  return `${core}${check}`;
}

function collectBookIsbns(book) {
  const rawSet = new Set();
  if (Array.isArray(book.isbns)) {
    book.isbns.forEach((x) => {
      const c = String(x || '').replace(/[^0-9Xx]/g, '').toUpperCase();
      if (c.length === 10 || c.length === 13) rawSet.add(c);
    });
  }
  [book.primary_isbn, book.isbn, book.annas_isbn_query].forEach((x) => {
    const c = String(x || '').replace(/[^0-9Xx]/g, '').toUpperCase();
    if (c.length === 10 || c.length === 13) rawSet.add(c);
  });
  const cleanAsin = String(book.asin || '').replace(/[^A-Za-z0-9]/g, '').toUpperCase();
  if (/^[0-9]{9}[0-9X]$/.test(cleanAsin)) {
    rawSet.add(cleanAsin);
  }

  for (const code of Array.from(rawSet)) {
    if (code.length === 10) {
      const i13 = isbn10ToIsbn13(code);
      if (i13) rawSet.add(i13);
    } else if (code.length === 13) {
      const i10 = isbn13ToIsbn10(code);
      if (i10) rawSet.add(i10);
    }
  }

  // Sort 13-digit ISBNs first, then 10-digit ISBNs
  return Array.from(rawSet).sort((a, b) => b.length - a.length || a.localeCompare(b));
}

function cleanTitleParts(rawTitle) {
  let t = String(rawTitle || '').replace(/\s+/g, ' ').trim();
  for (let i = 0; i < 3; i++) {
    const next = t
      .replace(
        /\s*[\(\[](?:kindle[^)\]]*|hardcover[^)\]]*|paperback[^)\]]*|mass\s+market[^)\]]*|spiral[^)\]]*|audio[^)\]]*|audible[^)\]]*|unabridged[^)\]]*|abridged[^)\]]*|print\s+replica[^)\]]*|illustrated[^)\]]*|reprint[^)\]]*|[^)\]]*\b(?:edition|ed\.|book\s+\d+|vol\.?\s*\d+|volume\s+\d+|series)\b[^)\]]*)[\)\]]\s*$/i,
        ''
      )
      .trim();
    if (next === t) break;
    t = next;
  }
  const base = t.split(/\s*[:\-–—]\s+/)[0].trim();
  return {
    fullTitle: t,
    baseTitle: base.length >= 3 ? base : t
  };
}

function cleanAuthorParts(rawAuthor) {
  let a = String(rawAuthor || '')
    .replace(/^\s*by\s+/i, '')
    .replace(/\|.*$/, '')
    .replace(/\s*\([^)]*(?:author|editor|translator|narrator|illustrator|foreword|edition|hardcover|paperback|kindle)[^)]*\)/gi, '')
    .replace(/\s+/g, ' ')
    .trim();
  const firstAuthor = a.split(/\s*(?:,|;|&|\band\b)\s*/i)[0].trim();
  const tokens = firstAuthor
    .split(/[^A-Za-z0-9'-]+/)
    .filter((tok) => tok.length >= 2 && !['jr', 'sr', 'ii', 'iii', 'iv', 'dr', 'md', 'phd'].includes(tok.toLowerCase()));
  const surname = tokens.length ? tokens[tokens.length - 1].toLowerCase() : '';
  return { cleanAuthor: a, surname };
}

/**
 * Parse Anna's Archive /search?q=... HTML (including commented result blocks <!-- <a href="/md5/..."> -->)
 * and verify relevance against the requested book ISBN and/or Title.
 */
function parseAnnasSearchHtml(html, book, annasBaseUrl, query, queryMode = 'title') {
  const searchUrl = `${annasBaseUrl}/search?q=${encodeURIComponent(query)}`;
  if (!html || typeof html !== 'string') {
    return { ok: false, found: false, count: 0, formats: [], matches: [], searchUrl };
  }

  // Truncate at "No files found." / "Show N partial matches" / "Partial matches" divider BEFORE uncommenting
  // so collapsed partial-match blocks are never treated as genuine matches.
  let mainHtml = html;
  const rawPartialIdx = mainHtml.search(/No\s+files\s+found\.|Show\s+\d+\s+partial\s+matches|Partial\s+matches|No\s+exact\s+matches/i);
  if (rawPartialIdx >= 0) {
    mainHtml = mainHtml.slice(0, rawPartialIdx);
  }

  // Un-comment HTML comments in the exact-results region so lazy-rendered Anna's Archive result cards are visible
  let uncommented = mainHtml.replace(/<!--([\s\S]*?)-->/g, '$1');
  const partialMarkerIdx = uncommented.search(/No\s+files\s+found\.|Show\s+\d+\s+partial\s+matches|Partial\s+matches|No\s+exact\s+matches/i);
  if (partialMarkerIdx >= 0) {
    uncommented = uncommented.slice(0, partialMarkerIdx);
  }

  const md5Regex = /<a[^>]+href=["'](\/md5\/([a-f0-9]{32}))["'][^>]*>([\s\S]*?)<\/a>/gi;
  const byMd5 = new Map();

  let match;
  while ((match = md5Regex.exec(uncommented)) !== null) {
    const relHref = match[1];
    const md5 = match[2].toLowerCase();
    const anchorText = match[3]
      .replace(/<script[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style[\s\S]*?<\/style>/gi, ' ')
      .replace(/<[^>]+>/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();

    // Forward snippet from the start of this anchor so we don't bleed into the previous result card
    const startIdx = Math.max(0, match.index - 80);
    const endIdx = Math.min(uncommented.length, match.index + match[0].length + 750);
    const snippet = uncommented
      .slice(startIdx, endIdx)
      .replace(/<script[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style[\s\S]*?<\/style>/gi, ' ')
      .replace(/<[^>]+>/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();

    if (!byMd5.has(md5)) {
      byMd5.set(md5, {
        md5,
        relHref,
        anchorText,
        snippet
      });
    } else {
      const prev = byMd5.get(md5);
      if (anchorText.length > prev.anchorText.length) {
        prev.anchorText = anchorText;
      }
      prev.snippet = `${prev.snippet} ${snippet}`.slice(0, 1600);
    }
  }

  const { fullTitle, baseTitle } = cleanTitleParts(book.base_title || book.title || '');
  const { surname } = cleanAuthorParts(book.author || '');
  const targetNormTitle = normStr(baseTitle || fullTitle);
  const stopWords = new Set(['the', 'and', 'for', 'with', 'from', 'into', 'your', 'that', 'this', 'book', 'edition']);
  const targetTitleWords = targetNormTitle
    .split(' ')
    .filter((w) => w.length >= 3 && !stopWords.has(w));
  const targetIsbns = collectBookIsbns(book);

  const candidates = [];
  byMd5.forEach((item) => {
    const combinedText = `${item.anchorText} ${item.snippet}`.trim();
    if (!combinedText || combinedText.length < 6) return;

    const normCombined = normStr(combinedText);
    const normAnchor = normStr(item.anchorText);
    const upperCombined = combinedText.replace(/[^0-9Xx]/g, '').toUpperCase();

    const hasExplicitIsbn = targetIsbns.some((isbn) => isbn.length >= 10 && upperCombined.includes(isbn));

    const normAnchorWords = new Set(normAnchor.split(' ').filter(Boolean));
    const normCombinedWords = new Set(normCombined.split(' ').filter(Boolean));

    let titleMatched = false;
    if (targetNormTitle && normAnchor.includes(targetNormTitle)) {
      titleMatched = true;
    } else if (targetTitleWords.length >= 2) {
      // Match against the book's actual title anchor (normAnchor), not upload folder paths in snippet
      const matchedWords = targetTitleWords.filter((w) => normAnchorWords.has(w) || normAnchor.includes(w));
      const required = targetTitleWords.length <= 2 ? targetTitleWords.length : Math.ceil(targetTitleWords.length * 0.8);
      if (matchedWords.length >= required) {
        titleMatched = true;
      }
    } else if (targetTitleWords.length === 1 && normAnchorWords.has(targetTitleWords[0])) {
      titleMatched = true;
    }

    const authorMatched = !surname || normCombinedWords.has(surname);

    let isRelevant = false;
    if (queryMode === 'isbn') {
      // When querying Anna's Archive directly by a 10/13-digit ISBN, Anna's index filters by ISBN metadata
      // (even when the ISBN isn't printed in the card text). Accept if ISBN appears, title overlaps, or author overlaps.
      isRelevant = hasExplicitIsbn || titleMatched || (Boolean(surname) && authorMatched);
    } else {
      // Title search mode: ALWAYS require both title match AND author surname match (when author is known)
      if (hasExplicitIsbn) {
        isRelevant = true;
      } else if (titleMatched && authorMatched) {
        isRelevant = true;
      }
    }

    if (!isRelevant) return;

    const fmtMatch = combinedText.match(/\b(epub|pdf|mobi|azw3|djvu|cbr|cbz|fb2)\b/i);
    const sizeMatch = combinedText.match(/\b(\d+(?:\.\d+)?\s*(?:MB|KB|GB))\b/i);
    const fmt = fmtMatch ? fmtMatch[1].toUpperCase() : '';
    const size = sizeMatch ? sizeMatch[1] : '';

    candidates.push({
      md5: item.md5,
      url: `${annasBaseUrl}${item.relHref}`,
      format: fmt,
      size,
      label: item.anchorText || item.snippet.slice(0, 120),
      matchSource: queryMode === 'isbn' ? 'ISBN' : 'Title'
    });
  });

  const formats = Array.from(new Set(candidates.map((c) => c.format).filter(Boolean)));
  return {
    ok: true,
    found: candidates.length > 0,
    count: candidates.length,
    formats,
    topMatch: candidates[0] || null,
    matches: candidates,
    searchUrl
  };
}

async function fetchAnnasViaOpenTab(query) {
  return new Promise((resolve) => {
    chrome.tabs.query(
      {
        url: [
          'https://annas-archive.pk/*',
          'https://*.annas-archive.pk/*',
          'https://annas-archive.gl/*',
          'https://*.annas-archive.gl/*',
          'https://annas-archive.li/*',
          'https://*.annas-archive.li/*',
          'https://annas-archive.se/*',
          'https://*.annas-archive.se/*',
          'https://annas-archive.org/*',
          'https://*.annas-archive.org/*'
        ]
      },
      (tabs) => {
        if (!tabs || tabs.length === 0) {
          resolve(null);
          return;
        }
        chrome.tabs.sendMessage(
          tabs[0].id,
          { type: 'ANNAS_TAB_FETCH_SEARCH', query },
          (resp) => {
            if (chrome.runtime.lastError || !resp) {
              resolve(null);
            } else {
              resolve(resp);
            }
          }
        );
      }
    );
  });
}

async function runSingleAnnasQuery(query, book, annasBaseUrl, queryMode) {
  const searchUrl = `${annasBaseUrl}/search?q=${encodeURIComponent(query)}`;

  // 1. If user has an annas-archive.pk tab open, prefer same-origin tab fetch first
  const tabResp = await fetchAnnasViaOpenTab(query);
  if (tabResp && tabResp.ok && tabResp.html) {
    return parseAnnasSearchHtml(tabResp.html, book, tabResp.origin || annasBaseUrl, query, queryMode);
  }

  // 2. Direct fetch from background service worker with browser cookies
  try {
    const resp = await fetch(searchUrl, {
      method: 'GET',
      credentials: 'include',
      headers: {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
      }
    });
    if (resp.ok) {
      const html = await resp.text();
      return parseAnnasSearchHtml(html, book, annasBaseUrl, query, queryMode);
    }
    return {
      ok: false,
      needsTabSession: resp.status === 403 || resp.status === 503,
      status: resp.status,
      error: `HTTP ${resp.status} — Open ${annasBaseUrl} in a tab once to share session cookies`,
      searchUrl
    };
  } catch (err) {
    return {
      ok: false,
      needsTabSession: true,
      error: err ? err.message : 'Network error',
      searchUrl
    };
  }
}

/**
 * Checks a book on Anna's Archive by:
 * 1) ISBN (if available: ISBN-13 first, then ISBN-10 fallback), AND
 * 2) Title (Title + Author surname first, then Title-only fallback).
 * Combines & deduplicates all matches by MD5 so editions indexed by either ISBN or Title are found.
 */
async function checkBookOnAnnasArchive(book) {
  const settings = await getSettings();
  const annasBaseUrl = settings.annasBaseUrl;

  const isbns = collectBookIsbns(book);
  const primaryIsbn = isbns[0] || '';
  const secondaryIsbn = isbns.find((x) => x !== primaryIsbn) || '';

  const { fullTitle, baseTitle } = cleanTitleParts(book.base_title || book.title || '');
  const { cleanAuthor, surname } = cleanAuthorParts(book.author || '');

  const titleAuthorQuery = (
    book.annas_title_query ||
    [baseTitle || fullTitle, surname || cleanAuthor].filter(Boolean).join(' ')
  ).trim();
  const titleOnlyQuery = (book.annas_title_only_query || baseTitle || fullTitle).trim();

  const isbnSearchUrl = primaryIsbn ? `${annasBaseUrl}/search?q=${encodeURIComponent(primaryIsbn)}` : '';
  const titleSearchUrl = `${annasBaseUrl}/search?q=${encodeURIComponent(titleAuthorQuery || titleOnlyQuery)}`;

  const mergedByMd5 = new Map();
  let isbnMatchesCount = 0;
  let titleMatchesCount = 0;
  let anyQueryOk = false;
  let lastErrorResp = null;

  // Step 1: Check by ISBN if available
  if (primaryIsbn) {
    let isbnRes = await runSingleAnnasQuery(primaryIsbn, book, annasBaseUrl, 'isbn');
    if (isbnRes.ok) {
      anyQueryOk = true;
      if (!isbnRes.found && secondaryIsbn) {
        const secRes = await runSingleAnnasQuery(secondaryIsbn, book, annasBaseUrl, 'isbn');
        if (secRes.ok && secRes.found) {
          isbnRes = secRes;
        }
      }
      (isbnRes.matches || []).forEach((m) => {
        isbnMatchesCount += 1;
        mergedByMd5.set(m.md5, { ...m, matchSource: 'ISBN' });
      });
    } else {
      lastErrorResp = isbnRes;
    }
  }

  // Step 2: Check by Title + Author surname (only fall back to Title-only when no author surname is known)
  const primaryTitleQ = titleAuthorQuery || titleOnlyQuery;
  if (primaryTitleQ && (!lastErrorResp || !lastErrorResp.needsTabSession)) {
    let titleRes = await runSingleAnnasQuery(primaryTitleQ, book, annasBaseUrl, 'title');
    if (titleRes.ok) {
      anyQueryOk = true;
      if (!titleRes.found && !surname && titleOnlyQuery && titleOnlyQuery.toLowerCase() !== primaryTitleQ.toLowerCase()) {
        const fallbackTitleRes = await runSingleAnnasQuery(titleOnlyQuery, book, annasBaseUrl, 'title');
        if (fallbackTitleRes.ok && fallbackTitleRes.found) {
          titleRes = fallbackTitleRes;
        }
      }
      (titleRes.matches || []).forEach((m) => {
        titleMatchesCount += 1;
        if (mergedByMd5.has(m.md5)) {
          const existing = mergedByMd5.get(m.md5);
          existing.matchSource = 'ISBN + Title';
        } else {
          mergedByMd5.set(m.md5, { ...m, matchSource: 'Title' });
        }
      });
    } else if (!anyQueryOk) {
      lastErrorResp = titleRes;
    }
  }

  if (!anyQueryOk && lastErrorResp) {
    return {
      ...lastErrorResp,
      checkedIsbn: primaryIsbn,
      checkedTitle: primaryTitleQ,
      isbnSearchUrl,
      titleSearchUrl,
      searchUrl: isbnSearchUrl || titleSearchUrl
    };
  }

  const combined = Array.from(mergedByMd5.values());
  // Rank ISBN matches first, then EPUB/MOBI/AZW3/PDF formats
  const fmtPriority = { EPUB: 1, AZW3: 2, MOBI: 3, PDF: 4 };
  combined.sort((a, b) => {
    const aIsbn = a.matchSource.includes('ISBN') ? 0 : 1;
    const bIsbn = b.matchSource.includes('ISBN') ? 0 : 1;
    if (aIsbn !== bIsbn) return aIsbn - bIsbn;
    const aFmt = fmtPriority[a.format] || 9;
    const bFmt = fmtPriority[b.format] || 9;
    return aFmt - bFmt;
  });

  const formats = Array.from(new Set(combined.map((c) => c.format).filter(Boolean)));
  const matchedBySummary = [];
  if (isbnMatchesCount > 0) matchedBySummary.push('ISBN');
  if (titleMatchesCount > 0) matchedBySummary.push('Title');

  return {
    ok: true,
    found: combined.length > 0,
    count: combined.length,
    isbnCount: isbnMatchesCount,
    titleCount: titleMatchesCount,
    matchedBy: matchedBySummary.join(' & ') || (primaryIsbn ? 'ISBN + Title' : 'Title'),
    checkedIsbn: primaryIsbn,
    checkedTitle: primaryTitleQ,
    formats,
    topMatch: combined[0] || null,
    matches: combined.slice(0, 6),
    isbnSearchUrl,
    titleSearchUrl,
    searchUrl: (isbnMatchesCount > 0 && isbnSearchUrl) ? isbnSearchUrl : (titleSearchUrl || isbnSearchUrl)
  };
}

const packtSummaryCache = new Map();
const packtAuthorCache = new Map();

function normalizeOwnedTitleKey(title) {
  return String(title || '')
    .toLowerCase()
    .replace(/\s*\[(?:video|ebook|print|book|audio)\]\s*$/i, '')
    .replace(/[:\-–—].*$/, '')
    .replace(/\(.*?\)|\[.*?\]/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function getOwnedStore() {
  return new Promise((resolve) => {
    chrome.storage.local.get({ ownedPublisherTitles: { items: {} } }, (res) => {
      const store = (res && res.ownedPublisherTitles) || { items: {} };
      if (!store.items || typeof store.items !== 'object') store.items = {};
      resolve(store);
    });
  });
}

function saveOwnedStore(store) {
  return new Promise((resolve) => {
    chrome.storage.local.set({ ownedPublisherTitles: store }, () => resolve());
  });
}

async function importOwnedPublisherTitles(source, rawBooks) {
  const store = await getOwnedStore();
  const srcName = String(source || 'Packt').trim();
  const pubSource = /springer/i.test(srcName) ? 'SPRINGER' : 'PACKT';
  let added = 0;
  let updated = 0;

  const enrichedList = await Promise.all(
    (Array.isArray(rawBooks) ? rawBooks : []).map((b) =>
      enrichPacktBookIfNeeded({ ...b, wishlist_name: srcName })
    )
  );

  const serverPayloadItems = [];

  for (const b of enrichedList) {
    if (!b) continue;
    const rawIsbn = String(b.isbn || b.isbnDigits || b.primary_isbn || '').replace(/[^0-9Xx]/g, '').toUpperCase();
    const isbn13 = rawIsbn.length === 10 ? isbn10ToIsbn13(rawIsbn) : (rawIsbn.length === 13 ? rawIsbn : '');
    const isbn10 = isbn13 ? isbn13ToIsbn10(isbn13) : (rawIsbn.length === 10 ? rawIsbn : '');
    const rawTitle = String(b.title || '').replace(/\s+/g, ' ').trim();
    const is_video = Boolean(b.is_video) || /\[video\]/i.test(rawTitle);
    const title = rawTitle.replace(/\s*\[(?:Video|eBook|Print|Book|Audio)\]\s*$/i, '').trim();
    const author = String(b.author || '').replace(/\s+/g, ' ').trim();
    const pub_year = b.pub_year || null;

    const primaryKey = isbn13 || isbn10 || (title ? `title:${normalizeOwnedTitleKey(title)}` : '');
    if (!primaryKey) continue;

    const existing = store.items[primaryKey];
    const sources = new Set(existing && Array.isArray(existing.sources) ? existing.sources : []);
    sources.add(srcName);

    store.items[primaryKey] = {
      key: primaryKey,
      isbn13: isbn13 || (existing && existing.isbn13) || '',
      isbn10: isbn10 || (existing && existing.isbn10) || '',
      title: title || (existing && existing.title) || '',
      normTitle: normalizeOwnedTitleKey(title || (existing && existing.title) || ''),
      author: author || (existing && existing.author) || '',
      pub_year: pub_year || (existing && existing.pub_year) || null,
      is_video,
      sources: Array.from(sources),
      importedAt: new Date().toISOString()
    };

    serverPayloadItems.push({
      source: pubSource,
      isbn: isbn13 || isbn10 || '',
      title: title || (existing && existing.title) || 'Untitled',
      authors: author || (existing && existing.author) || 'Unknown Author',
      is_video,
      cover_image_url: b.cover_image_url || '',
      web_reader_url: b.web_reader_url || ''
    });

    if (existing) updated += 1;
    else added += 1;
  }

  store.lastSyncAt = new Date().toISOString();
  await saveOwnedStore(store);

  // Also push to MyLibrary server (/api/sync/owned/) so it updates the Owned on Packt / Springer shelves in BookDB
  let serverRes = null;
  if (serverPayloadItems.length > 0) {
    try {
      const settings = await getSettings();
      const syncedSources = Array.from(new Set(serverPayloadItems.map((x) => x.source)));
      const resp = await fetch(`${settings.mylibraryBaseUrl}/api/sync/owned/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          items: serverPayloadItems,
          synced_sources: syncedSources,
          is_final_chunk: serverPayloadItems.length >= 20
        })
      });
      if (resp.ok) {
        serverRes = await resp.json();
      }
    } catch (_) { }
  }

  const allItems = Object.values(store.items);
  const packtCount = allItems.filter((it) => Array.isArray(it.sources) && it.sources.some((s) => /packt/i.test(s))).length;
  const springerCount = allItems.filter((it) => Array.isArray(it.sources) && it.sources.some((s) => /springer/i.test(s))).length;

  return {
    ok: true,
    added,
    updated,
    totalCount: allItems.length,
    packtCount: (serverRes && serverRes.packt_owned) || packtCount,
    springerCount: (serverRes && serverRes.springer_owned) || springerCount,
    serverSynced: Boolean(serverRes && serverRes.ok)
  };
}

function matchOwnedSourcesForBook(book, resultObj, store) {
  if (!store || !store.items) return [];
  const itemsMap = store.items;
  const matchedSources = new Set();

  const candIsbns = collectBookIsbns({
    ...(book || {}),
    ...(resultObj || {})
  });

  for (const code of candIsbns) {
    if (itemsMap[code] && Array.isArray(itemsMap[code].sources)) {
      itemsMap[code].sources.forEach((s) => matchedSources.add(s));
    }
  }
  if (matchedSources.size > 0) return Array.from(matchedSources);

  const normBookTitle = normalizeOwnedTitleKey(
    (resultObj && (resultObj.cleaned_title || resultObj.base_title)) || (book && book.title) || ''
  );
  if (!normBookTitle || normBookTitle.length < 4) return [];

  const titleEntry = itemsMap[`title:${normBookTitle}`];
  if (titleEntry && Array.isArray(titleEntry.sources)) {
    titleEntry.sources.forEach((s) => matchedSources.add(s));
  }

  for (const it of Object.values(itemsMap)) {
    if (it && it.normTitle && it.normTitle === normBookTitle && Array.isArray(it.sources)) {
      it.sources.forEach((s) => matchedSources.add(s));
    }
  }
  return Array.from(matchedSources);
}

async function enrichPacktBookIfNeeded(b) {
  if (!b || !/packt/i.test(b.wishlist_name || '')) return b;
  const isbn = String(b.isbn || '').replace(/[^0-9]/g, '');
  if (!/^97[89][0-9]{10}$/.test(isbn)) return b;
  if (b.title && b.author && b.pub_year) return b;

  try {
    let summary = packtSummaryCache.get(isbn);
    if (!summary) {
      const resp = await fetch(`https://static.packt-cdn.com/products/${isbn}/summary`, { method: 'GET' });
      if (resp.ok) {
        summary = await resp.json();
        packtSummaryCache.set(isbn, summary);
      }
    }
    if (!summary) return b;

    const updated = { ...b };
    if (!updated.title && summary.title) {
      updated.title = String(summary.title).replace(/\s*\[(?:Video|eBook|Print|Book|Audio)\]\s*$/i, '').trim();
    }
    if (!updated.pub_year && summary.publicationDate) {
      const ym = String(summary.publicationDate).match(/\b(19[7-9]\d|20[0-3]\d)\b/);
      if (ym) updated.pub_year = parseInt(ym[1], 10);
    }
    if (!updated.author && Array.isArray(summary.authors) && summary.authors.length > 0) {
      const resolvedNames = [];
      for (const rawAuth of summary.authors.slice(0, 3)) {
        const authStr = String(rawAuth || '').trim();
        if (!authStr) continue;
        if (!/^\d+$/.test(authStr)) {
          resolvedNames.push(authStr);
          continue;
        }
        let authorName = packtAuthorCache.get(authStr);
        if (!authorName) {
          const aResp = await fetch(`https://static.packt-cdn.com/authors/${authStr}`, { method: 'GET' });
          if (aResp.ok) {
            const aData = await aResp.json();
            if (aData && aData.author) {
              authorName = String(aData.author).trim();
              packtAuthorCache.set(authStr, authorName);
            }
          }
        }
        if (authorName) resolvedNames.push(authorName);
      }
      if (resolvedNames.length > 0) {
        updated.author = resolvedNames.join(', ');
      }
    }
    return updated;
  } catch (_) {
    return b;
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.type) return false;

  if (msg.type === 'GET_SETTINGS') {
    getSettings().then((settings) => sendResponse({ ok: true, settings }));
    return true;
  }

  if (msg.type === 'CHECK_MYLIBRARY_BATCH') {
    (async () => {
      const settings = await getSettings();
      const apiUrl = `${settings.mylibraryBaseUrl}/api/wishlist/check/`;
      try {
        const inputBooks = Array.isArray(msg.books) ? msg.books : [];
        const [enrichedBooks, ownedStore] = await Promise.all([
          Promise.all(inputBooks.map((b) => enrichPacktBookIfNeeded(b))),
          getOwnedStore()
        ]);
        const resp = await fetch(apiUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ books: enrichedBooks })
        });
        const data = await resp.json();
        if (data && Array.isArray(data.results)) {
          data.results.forEach((r, idx) => {
            const eb = enrichedBooks[idx] || inputBooks[idx] || {};
            const localOwned = matchOwnedSourcesForBook(eb, r, ownedStore);
            const serverOwned = Array.isArray(r.owned_sources) ? r.owned_sources : [];
            r.owned_sources = Array.from(new Set([...serverOwned, ...localOwned]));
          });
        }
        sendResponse({
          ok: Boolean(data && data.ok),
          mylibraryBaseUrl: settings.mylibraryBaseUrl,
          annasBaseUrl: settings.annasBaseUrl,
          enrichedBooks,
          data
        });
      } catch (err) {
        const ownedStore = await getOwnedStore();
        const inputBooks = Array.isArray(msg.books) ? msg.books : [];
        const fallbackResults = inputBooks.map((b, idx) => ({
          key: b.key || String(idx),
          in_library: false,
          error: err ? err.message : 'Could not reach MyLibrary server',
          owned_sources: matchOwnedSourcesForBook(b, {}, ownedStore)
        }));
        sendResponse({
          ok: false,
          mylibraryBaseUrl: settings.mylibraryBaseUrl,
          annasBaseUrl: settings.annasBaseUrl,
          data: { ok: false, results: fallbackResults },
          error: err ? err.message : 'Could not reach MyLibrary server'
        });
      }
    })();
    return true;
  }

  if (msg.type === 'IMPORT_OWNED_TITLES') {
    importOwnedPublisherTitles(msg.source || 'Packt', msg.books || []).then((res) => sendResponse(res));
    return true;
  }

  if (msg.type === 'GET_OWNED_STATS') {
    getOwnedStore().then((store) => {
      const allItems = Object.values(store.items || {});
      const packtCount = allItems.filter((it) => Array.isArray(it.sources) && it.sources.some((s) => /packt/i.test(s))).length;
      const springerCount = allItems.filter((it) => Array.isArray(it.sources) && it.sources.some((s) => /springer/i.test(s))).length;
      sendResponse({
        ok: true,
        totalCount: allItems.length,
        packtCount,
        springerCount,
        lastSyncAt: store.lastSyncAt || null,
        items: allItems
      });
    });
    return true;
  }

  if (msg.type === 'CLEAR_OWNED_TITLES') {
    getOwnedStore().then(async (store) => {
      const targetSource = msg.source ? String(msg.source).toLowerCase() : '';
      if (!targetSource) {
        await saveOwnedStore({ items: {}, lastSyncAt: null });
      } else {
        const nextItems = {};
        for (const [k, it] of Object.entries(store.items || {})) {
          const rem = (it.sources || []).filter((s) => !s.toLowerCase().includes(targetSource));
          if (rem.length > 0) {
            nextItems[k] = { ...it, sources: rem };
          }
        }
        await saveOwnedStore({ items: nextItems, lastSyncAt: store.lastSyncAt });
      }
      sendResponse({ ok: true });
    });
    return true;
  }

  if (msg.type === 'CHECK_ANNAS_SINGLE') {
    checkBookOnAnnasArchive(msg.book || {}).then((res) => sendResponse(res));
    return true;
  }

  if (msg.type === 'OPEN_ANNAS_SESSION_TAB') {
    getSettings().then((settings) => {
      chrome.tabs.create({ url: settings.annasBaseUrl, active: true }, (tab) => {
        sendResponse({ ok: true, tabId: tab ? tab.id : null });
      });
    });
    return true;
  }

  if (msg.type === 'FETCH_MYCD_DEVICES' || msg.type === 'DELIVER_TO_KINDLE_BATCH' || msg.type === 'DEREGISTER_MYCD_DEVICES') {
    (async () => {
      try {
        // Fetch https://www.amazon.com/hz/mycd/digital-console/contentlist/booksAll/dateDsc/ with credentials to extract csrfToken
        let csrfToken = msg.csrfToken || '';
        if (!csrfToken) {
          const pageResp = await fetch('https://www.amazon.com/hz/mycd/digital-console/contentlist/booksAll/dateDsc/', {
            method: 'GET',
            credentials: 'include'
          });
          const html = await pageResp.text();
          const m = html.match(/csrfToken\s*[=:]\s*["']([^"']+)["']/i);
          if (m && m[1]) {
            csrfToken = m[1];
          }
        }
        if (!csrfToken) {
          sendResponse({
            ok: false,
            needSignIn: true,
            error: 'Not signed in to Amazon Content & Devices (csrfToken not found). Please sign in at amazon.com/hz/mycd/digital-console/contentlist/booksAll/dateDsc/.'
          });
          return;
        }

        const fetchLiveKindleDevices = async () => {
          const body1 = new URLSearchParams({
            activity: 'GetDevicesOverview',
            activityInput: JSON.stringify({ surfaceType: 'Desktop' }),
            clientId: 'MYCD_WebService',
            csrfToken
          });
          const r1 = await fetch('https://www.amazon.com/hz/mycd/digital-console/ajax', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
            body: body1.toString(),
            credentials: 'include'
          });
          const j1 = await r1.json();
          const rawDevs = ((j1.GetDevicesOverview || {}).deviceList || []);
          return rawDevs
            .filter((d) => ['KINDLE_APP', 'FIRE_TABLET', 'KINDLE'].includes(d.deviceFamily))
            .map((d) => ({
              deviceName: d.deviceName,
              deviceAccountID: d.deviceAccountID,
              deviceSerialNumber: d.deviceSerialNumber,
              deviceTypeID: d.deviceTypeID,
              deviceFamily: d.deviceFamily,
              deviceTypeString: d.deviceTypeString,
              customerID: d.customerID || '',
              isDefault: d.deviceAccountID === 'A101329625FBGFO0QECXO' || /Kindle for Windows/i.test(d.deviceName || '')
            }));
        };

        if (msg.type === 'FETCH_MYCD_DEVICES') {
          const kindleDevs = await fetchLiveKindleDevices();
          sendResponse({ ok: true, csrfToken, devices: kindleDevs });
          return;
        }

        if (msg.type === 'DEREGISTER_MYCD_DEVICES') {
          const defaultKeepIds = [
            'A101329625FBGFO0QECXO', // JAMES's Kindle for Windows (PC) - _8YLYgU
            'A015285311F3W73S7EA31', // JAMES's 18th Android Device (Tab S11) - _RfOPZk
            'A018607231PVOX7INFXHE', // JAMES's 12th Android Device (Galaxy S25 Ultra) - _GNgiXh
            'A0440102192GKG2EE19T3', // JAMES's 8th Android Device (Tab S8 Ultra) - _vlEewv
            'A02063483UBXG3PKOGGOX', // Jimmy's tablet (Tab A8)
            'A05507311ZARZZO8ORYGN'  // JAMES's Main (Cloud Reader)
          ];
          const defaultKeepNames = [
            "JAMES's Kindle for Windows",
            "Jimmy's Tab S11",
            "Jimmy's Galaxy S25 Ultra",
            "Jimmy's Tab S8 Ultra",
            "Jimmy's Tab A8",
            "JAMES's Kindle for Web",
            "PC",
            "Tab S11",
            "Galaxy S25 Ultra",
            "Tab S8 Ultra",
            "Tab A8",
            "Cloud Reader"
          ];
          const keepIdsSet = new Set(
            Array.isArray(msg.keepDeviceAccountIDs) && msg.keepDeviceAccountIDs.length > 0
              ? msg.keepDeviceAccountIDs
              : defaultKeepIds
          );
          if (msg.keepDeviceAccountID) keepIdsSet.add(String(msg.keepDeviceAccountID).trim());
          const keepNamesSet = new Set(
            (Array.isArray(msg.keepDeviceNames) && msg.keepDeviceNames.length > 0 ? msg.keepDeviceNames : defaultKeepNames)
              .map((n) => String(n || '').trim().toLowerCase())
          );

          const isProtectedDevice = (d) => {
            if (!d) return false;
            if (d.deviceAccountID && keepIdsSet.has(String(d.deviceAccountID).trim())) return true;
            const dn = String(d.deviceName || '').trim().toLowerCase();
            if (dn && keepNamesSet.has(dn)) return true;
            const dtStr = String(d.deviceTypeString || '').trim().toLowerCase();
            if (dtStr.includes('cloud reader')) return true;
            return false;
          };

          const liveDevs = await fetchLiveKindleDevices();
          const targets = Array.isArray(msg.devicesToRemove) && msg.devicesToRemove.length > 0
            ? msg.devicesToRemove
            : liveDevs.filter((d) => !isProtectedDevice(d));

          const deregistered = [];
          const failed = [];
          const keptDevices = liveDevs.filter((d) => isProtectedDevice(d));

          for (const d of targets) {
            if (isProtectedDevice(d)) continue;
            const dsn = String(d.deviceSerialNumber || '').trim();
            const devType = String(d.deviceTypeID || '').trim();
            const acctId = String(d.deviceAccountID || '').trim();
            let success = false;
            let lastResp = null;

            // 1. Try MYCD digital-console/ajax DeregisterDevice activity
            try {
              const actInput = {
                deviceSerialNumber: dsn,
                deviceType: devType,
                deviceTypeID: devType,
                deviceAccountId: acctId,
                deviceAccountID: acctId,
                surfaceType: 'Desktop'
              };
              const b1 = new URLSearchParams({
                activity: 'DeregisterDevice',
                activityInput: JSON.stringify(actInput),
                clientId: 'MYCD_WebService',
                csrfToken
              });
              const r1 = await fetch('https://www.amazon.com/hz/mycd/digital-console/ajax', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
                body: b1.toString(),
                credentials: 'include'
              });
              const j1 = await r1.json();
              lastResp = j1;
              const dd1 = j1.DeregisterDevice || j1;
              if (r1.status === 200 && (dd1.success === true || j1.success === true)) {
                success = true;
              }
            } catch (_) {}

            // 2. Fallback: MYCD /hz/mycd/ajax param.DeregisterDevice or param.Deregister
            if (!success) {
              for (const paramKey of ['DeregisterDevice', 'Deregister']) {
                try {
                  const p2 = {
                    param: {
                      [paramKey]: {
                        deviceSerialNumber: dsn,
                        deviceType: devType,
                        deviceTypeID: devType,
                        deviceAccountId: acctId,
                        deviceAccountID: acctId
                      }
                    }
                  };
                  const b2 = new URLSearchParams({
                    clientId: 'MYCD_WebService',
                    csrfToken,
                    data: JSON.stringify(p2)
                  });
                  const r2 = await fetch('https://www.amazon.com/hz/mycd/ajax', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
                    body: b2.toString(),
                    credentials: 'include'
                  });
                  const j2 = await r2.json();
                  lastResp = j2;
                  const dd2 = j2[paramKey] || j2;
                  if (r2.status === 200 && (dd2.success === true || j2.success === true)) {
                    success = true;
                    break;
                  }
                } catch (_) {}
              }
            }

            if (success) {
              deregistered.push(d);
            } else {
              failed.push({ device: d, resp: lastResp });
            }
          }

          // Also remove delivered book downloads from the kept active devices (without deregistering them!)
          let removedDownloadsCount = 0;
          try {
            const activeAccountIds = new Set(keptDevices.map((d) => String(d.deviceAccountID || '').trim()).filter(Boolean));
            const activeDsns = keptDevices.map((d) => String(d.deviceSerialNumber || '').trim()).filter(Boolean);
            if (activeAccountIds.size > 0) {
              const ownBody = new URLSearchParams({
                activity: 'GetContentOwnershipData',
                activityInput: JSON.stringify({
                  contentType: 'Ebook',
                  contentCategoryReference: 'booksAll',
                  itemStatusList: ['Active'],
                  originTypeList: ['Purchase', 'KOLL', 'Ku', 'Prime', 'Pottermore', 'Sample', 'Comixology'],
                  sortOrder: 'DESCENDING',
                  sortIndex: 'DATE',
                  startIndex: 0,
                  batchSize: 600,
                  surfaceType: 'Desktop'
                }),
                clientId: 'MYCD_WebService',
                csrfToken
              });
              const ownResp = await fetch('https://www.amazon.com/hz/mycd/digital-console/ajax', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
                body: ownBody.toString(),
                credentials: 'include'
              });
              const ownJson = await ownResp.json();
              const itemsList = ((ownJson.GetContentOwnershipData || {}).items || []);
              for (const item of itemsList) {
                const asin = item.asin || (item.targetEntity && item.targetEntity.asin);
                if (!asin) continue;
                const orders = item.orders || [];
                const dlDevices = [];
                for (const ord of orders) {
                  for (const devId of (ord.downloadedDevices || ord.devices || [])) {
                    const idStr = typeof devId === 'string' ? devId : (devId.deviceAccountID || devId.deviceSerialNumber || '');
                    if (activeAccountIds.has(idStr)) dlDevices.push(idStr);
                  }
                }
                if (dlDevices.length > 0) {
                  for (const rmKey of ['RemoveFromDevice', 'DeleteFromDevice']) {
                    try {
                      const rmPayload = {
                        param: {
                          [rmKey]: {
                            asin,
                            category: 'KindleEBook',
                            deviceList: dlDevices,
                            dsnList: activeDsns
                          }
                        }
                      };
                      const rmB = new URLSearchParams({
                        clientId: 'MYCD_WebService',
                        csrfToken,
                        data: JSON.stringify(rmPayload)
                      });
                      const rmR = await fetch('https://www.amazon.com/hz/mycd/ajax', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
                        body: rmB.toString(),
                        credentials: 'include'
                      });
                      const rmJ = await rmR.json();
                      if ((rmJ[rmKey] || rmJ).success === true) {
                        removedDownloadsCount += 1;
                        break;
                      }
                    } catch (_) {}
                  }
                }
              }
            }
          } catch (_) {}

          const remainingDevs = await fetchLiveKindleDevices();
          sendResponse({
            ok: deregistered.length > 0 || targets.length === 0 || removedDownloadsCount > 0,
            csrfToken,
            deregisteredCount: deregistered.length,
            deregistered,
            removedDownloadsCount,
            failedCount: failed.length,
            failed,
            devices: remainingDevs
          });
          return;
        }

        // DELIVER_TO_KINDLE_BATCH
        const items = Array.isArray(msg.items) ? msg.items : [];
        const deviceAccountID = String(msg.deviceAccountID || 'A101329625FBGFO0QECXO').trim();
        const deviceSerialNumber = String(msg.deviceSerialNumber || 'C36839180E3E566C07FCEF7DEFD96D32').trim();

        const asinDetails = {};
        for (const b of items) {
          const asin = String(b.asin || '').trim();
          if (!asin) continue;
          let originType = String(b.originType || b.origin_type || 'Purchase').trim();
          const up = originType.toUpperCase();
          if (up === 'KINDLE_UNLIMITED' || up === 'KU') originType = 'Ku';
          else if (up === 'PRIME_READING' || up === 'PRIME' || up === 'KOLL') originType = 'Prime';
          else if (up === 'COMIXOLOGY' || up === 'COMIXOLOGY_UNLIMITED') originType = 'Comixology';
          else if (up === 'SAMPLE') originType = 'Sample';
          else if (!['Purchase', 'Ku', 'Prime', 'Comixology', 'Sample'].includes(originType)) originType = 'Purchase';

          asinDetails[asin] = {
            category: 'KindleEBook',
            title: String(b.title || 'Untitled'),
            originType,
            eligibleDeviceList: [deviceAccountID]
          };
        }

        const payload = {
          param: {
            DeliverToKindle: {
              asinDetails,
              dsnList: [deviceSerialNumber]
            }
          }
        };
        const body = new URLSearchParams({
          clientId: 'MYCD_WebService',
          csrfToken,
          data: JSON.stringify(payload)
        });
        const r = await fetch('https://www.amazon.com/hz/mycd/ajax', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
          body: body.toString(),
          credentials: 'include'
        });
        const j = await r.json();
        const dtk = (j && j.DeliverToKindle) || {};
        const ok = r.status === 200 && (dtk.success === true || j.success === true);
        sendResponse({
          ok,
          status: r.status,
          csrfToken,
          resultMap: dtk.resultMap || {},
          raw: dtk
        });
      } catch (err) {
        sendResponse({
          ok: false,
          error: err ? err.message : 'Failed to communicate with Amazon MYCD'
        });
      }
    })();
    return true;
  }

  return false;
});
