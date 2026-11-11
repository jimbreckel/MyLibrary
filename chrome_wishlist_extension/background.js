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

  // Un-comment HTML comments so lazy-rendered Anna's Archive result cards are visible to parser
  let uncommented = html.replace(/<!--([\s\S]*?)-->/g, '$1');
  // Truncate at "partial match" / "more results" divider if present so exact results aren't diluted
  const partialMarkerIdx = uncommented.search(/Partial\s+matches|No\s+exact\s+matches/i);
  if (partialMarkerIdx > 0 && queryMode === 'isbn') {
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

    let titleMatched = false;
    if (targetNormTitle && (normCombined.includes(targetNormTitle) || normAnchor.includes(targetNormTitle))) {
      titleMatched = true;
    } else if (targetTitleWords.length >= 2) {
      const matchedWords = targetTitleWords.filter((w) => normCombined.includes(w));
      const required = Math.max(2, Math.ceil(targetTitleWords.length * 0.6));
      if (matchedWords.length >= required) {
        titleMatched = true;
      }
    } else if (targetTitleWords.length === 1 && normCombined.includes(targetTitleWords[0])) {
      titleMatched = true;
    }

    const authorMatched = !surname || normCombined.includes(surname);

    let isRelevant = false;
    if (queryMode === 'isbn') {
      // When querying Anna's Archive directly by a 10/13-digit ISBN, Anna's index filters by ISBN metadata
      // (even when the ISBN isn't printed in the card text). Accept if ISBN appears, title overlaps, or author overlaps.
      isRelevant = hasExplicitIsbn || titleMatched || (Boolean(surname) && normCombined.includes(surname)) || byMd5.size <= 5;
    } else {
      // Title search mode: require title match (and if title is a single common word, also require author surname)
      if (hasExplicitIsbn) {
        isRelevant = true;
      } else if (titleMatched && (targetTitleWords.length >= 2 || authorMatched)) {
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

  // Step 2: Check by Title (Title + Author, with Title-only fallback if 0 matches)
  const primaryTitleQ = titleAuthorQuery || titleOnlyQuery;
  if (primaryTitleQ && (!lastErrorResp || !lastErrorResp.needsTabSession)) {
    let titleRes = await runSingleAnnasQuery(primaryTitleQ, book, annasBaseUrl, 'title');
    if (titleRes.ok) {
      anyQueryOk = true;
      if (!titleRes.found && titleOnlyQuery && titleOnlyQuery.toLowerCase() !== primaryTitleQ.toLowerCase()) {
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
        const resp = await fetch(apiUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ books: msg.books || [] })
        });
        const data = await resp.json();
        sendResponse({
          ok: Boolean(data && data.ok),
          mylibraryBaseUrl: settings.mylibraryBaseUrl,
          annasBaseUrl: settings.annasBaseUrl,
          data
        });
      } catch (err) {
        sendResponse({
          ok: false,
          mylibraryBaseUrl: settings.mylibraryBaseUrl,
          annasBaseUrl: settings.annasBaseUrl,
          error: err ? err.message : 'Could not reach MyLibrary server'
        });
      }
    })();
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

  return false;
});
