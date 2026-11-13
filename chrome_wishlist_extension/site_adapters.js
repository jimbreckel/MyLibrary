// Declarative Book Site Adapters Registry for MyLibrary & Anna's Archive Chrome Extension
//
// How to add a new built-in book site in 10 lines:
// Add an entry to window.MYLIB_BUILTIN_SITE_ADAPTERS below with:
//   - id: unique string ID (e.g. 'my_site')
//   - name: human-readable site name
//   - hostRegex: RegExp matching window.location.hostname
//   - pathRegex: (optional) RegExp matching window.location.pathname
//   - imageAnchorSelectors: array of CSS selectors where the widget should be inserted right after
//   - titleSelectors: array of CSS selectors for the book title
//   - authorSelectors: array of CSS selectors for the book author(s)
//   - getIsbn(doc, url): (optional) function returning ISBN-13 / ISBN-10 string
//   - getTitle(doc, url): (optional) custom title extractor
//   - getAuthor(doc, url): (optional) custom author extractor
//   - getPubYear(doc, url): (optional) function returning 4-digit publication year
//   - getEdition(doc, url): (optional) function returning edition string (e.g. '3rd Edition')
//
// Or add a Custom Site directly in the Extension Popup UI (stored in chrome.storage.sync)!

(function () {
  'use strict';

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

  function normalizeIsbn13(raw) {
    if (!raw) return { raw: '', digits: '' };
    const trimmed = String(raw).trim();
    const digits = trimmed.replace(/[^0-9Xx]/g, '').toUpperCase();
    if (digits.length === 13 && /^97[89][0-9]{10}$/.test(digits)) {
      return { raw: trimmed, digits };
    }
    if (digits.length === 10 && /^[0-9]{9}[0-9X]$/.test(digits)) {
      const conv = isbn10ToIsbn13(digits);
      return { raw: conv || digits, digits: conv || digits, isbn10: digits };
    }
    return { raw: '', digits: '' };
  }

  function cleanExtractedAuthor(raw) {
    if (!raw) return '';
    return String(raw)
      .replace(/<br\b[\s\S]*$/i, '')
      .replace(/<[^>]+>/g, ' ')
      .replace(/^by\s*:?\s+/i, '')
      .replace(/Visit Amazon's\s+(.+?)\s+Page/gi, '$1')
      .replace(/\s*\((?:Author|Editor|Translator|Illustrator|Contributor)s?\)/gi, '')
      .replace(/\s*(?:Foreword|Preface|Introduction|Afterword)\s+by\b[\s\S]*$/i, '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function extractAuthorFromMetaOrJsonLd(doc) {
    const metaStr = allTextBySelectors(doc, [
      'meta[name="citation_author"]',
      'meta[name="dc.creator"]',
      'meta[name="author"]',
      'meta[property="book:author"]'
    ]);
    if (metaStr) return cleanExtractedAuthor(metaStr);

    const ldScripts = Array.from(doc.querySelectorAll('script[type="application/ld+json"]'));
    for (const script of ldScripts) {
      try {
        const parsed = JSON.parse(script.textContent || '{}');
        const items = Array.isArray(parsed) ? parsed : (parsed['@graph'] || [parsed]);
        for (const item of items) {
          if (!item || !item.author) continue;
          const authors = Array.isArray(item.author) ? item.author : [item.author];
          const names = authors
            .map((a) => (typeof a === 'string' ? a : (a && a.name) || ''))
            .map(cleanExtractedAuthor)
            .filter(Boolean);
          if (names.length > 0) return Array.from(new Set(names)).join(', ');
        }
      } catch (_) {}
    }
    return '';
  }

  function extractIsbnFromMetaOrJsonLd(doc) {
    // 1. Check standard academic/publisher meta tags (<meta name="citation_isbn">, <meta property="books:isbn">, etc.)
    const metaSelectors = [
      'meta[name="citation_isbn"]',
      'meta[name="isbn"]',
      'meta[property="books:isbn"]',
      'meta[property="book:isbn"]',
      'meta[property="product:retailer_part_no"]',
      'meta[itemprop="isbn"]'
    ];
    for (const sel of metaSelectors) {
      const els = Array.from(doc.querySelectorAll(sel));
      for (const el of els) {
        const norm = normalizeIsbn13(el.getAttribute('content') || '');
        if (norm.digits) return norm;
      }
    }

    // 2. Check Schema.org JSON-LD scripts for Book / Product isbn or gtin13
    const ldScripts = Array.from(doc.querySelectorAll('script[type="application/ld+json"]'));
    for (const script of ldScripts) {
      try {
        const text = script.textContent || '';
        const m = text.match(/"(?:isbn|gtin13|productID)"\s*:\s*"([0-9Xx\-]{10,17})"/i);
        if (m) {
          const norm = normalizeIsbn13(m[1]);
          if (norm.digits) return norm;
        }
      } catch (_) {}
    }

    return { raw: '', digits: '' };
  }

  function firstTextBySelectors(doc, selectors) {
    if (!Array.isArray(selectors)) return '';
    for (const sel of selectors) {
      if (!sel) continue;
      try {
        const el = doc.querySelector(sel);
        if (el) {
          const contentAttr = el.tagName === 'META' ? el.getAttribute('content') : '';
          const txt = (contentAttr || el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
          if (txt) return txt;
        }
      } catch (_) {}
    }
    return '';
  }

  function allTextBySelectors(doc, selectors) {
    if (!Array.isArray(selectors)) return '';
    for (const sel of selectors) {
      if (!sel) continue;
      try {
        const els = Array.from(doc.querySelectorAll(sel));
        const values = els
          .map((el) => {
            const contentAttr = el.tagName === 'META' ? el.getAttribute('content') : '';
            return (contentAttr || el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
          })
          .filter(Boolean);
        if (values.length > 0) {
          return Array.from(new Set(values)).join(', ');
        }
      } catch (_) {}
    }
    return '';
  }

  function firstElementBySelectors(doc, selectors) {
    if (!Array.isArray(selectors)) return null;
    for (const sel of selectors) {
      if (!sel) continue;
      try {
        const el = doc.querySelector(sel);
        if (el) return el;
      } catch (_) {}
    }
    return null;
  }

  const BUILTIN_SITE_ADAPTERS = [
    // =========================================================================
    // 1. Amazon Book Product Pages (combines UserScript: Amazon Books to Z-Library & Anna's Archive)
    // =========================================================================
    {
      id: 'amazon_book',
      name: 'Amazon Books',
      hostRegex: /(^|\.)amazon\.(com|co\.uk|ca|de|fr|es|it|co\.jp|com\.au|in|com\.br|com\.mx|nl|se|pl)$/i,
      pathRegex: /\/(?:dp|gp\/product)\/[A-Z0-9]{10}/i,
      shouldActivate(doc) {
        const hasImageBlock = Boolean(
          doc.getElementById('imageBlockNew_feature_div') ||
          doc.getElementById('booksImageBlock_feature_div') ||
          doc.getElementById('imageBlock_feature_div')
        );
        const hasTitle = Boolean(doc.getElementById('productTitle') || doc.getElementById('ebooksProductTitle'));
        if (!hasImageBlock && !hasTitle) return false;
        // Verify this is a book/Kindle/Audible product page or has ISBN-13 in details
        const navSubnav = doc.getElementById('nav-subnav');
        const subnavCat = navSubnav ? (navSubnav.getAttribute('data-category') || '') : '';
        if (/books|digital-text|audible/i.test(subnavCat)) return true;
        if (doc.getElementById('booksImageBlock_feature_div') || doc.getElementById('ebooksProductTitle')) return true;
        const bodyText = (doc.getElementById('detailBulletsWrapper_feature_div') || doc.getElementById('detailBullets_feature_div') || doc.body).textContent || '';
        return /ISBN-13|ISBN-10|Print length|Publisher/i.test(bodyText);
      },
      imageAnchorSelectors: [
        '#imageBlockNew_feature_div',
        '#booksImageBlock_feature_div',
        '#imageBlock_feature_div',
        '#leftCol',
        '#bylineInfo'
      ],
      titleSelectors: [
        '#productTitle',
        '#ebooksProductTitle'
      ],
      authorSelectors: [
        '#bylineInfo .author a:not([href*="byline_sr_book"])',
        '#bylineInfo .author .a-link-normal',
        '#bylineInfo a.a-link-normal'
      ],
      getAuthor(doc) {
        const byline = doc.getElementById('bylineInfo');
        if (byline) {
          const authorSpans = Array.from(byline.querySelectorAll('.author'));
          const names = [];
          for (const sp of authorSpans) {
            const a = sp.querySelector('a.a-link-normal:not([href*="byline_sr_book"])') || sp.querySelector('a');
            const txt = cleanExtractedAuthor(a ? a.textContent : sp.textContent);
            if (txt && !/search results|learn about author|follow/i.test(txt)) {
              names.push(txt);
            }
          }
          if (names.length > 0) return Array.from(new Set(names)).join(', ');
        }
        return extractAuthorFromMetaOrJsonLd(doc);
      },
      getAsin(doc, url) {
        const m = String(url.pathname).match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})(?:[/?&#]|$)/i);
        return m ? m[1].toUpperCase() : '';
      },
      getIsbn(doc, url) {
        // 1. UserScript exact method: #detailBulletsWrapper_feature_div ul li span.a-list-item containing ISBN-13
        const detailBullets = doc.getElementById('detailBulletsWrapper_feature_div') || doc.getElementById('detailBullets_feature_div');
        if (detailBullets) {
          const listItems = detailBullets.querySelectorAll('ul li span.a-list-item, li');
          for (let i = 0; i < listItems.length; i++) {
            const txt = listItems[i].textContent || '';
            if (txt.includes('ISBN-13')) {
              const m = txt.match(/(\d{3}-\d{10}|\d{13})/);
              if (m) {
                return { raw: m[0], digits: m[0].replace(/[^0-9]/g, '') };
              }
            }
          }
        }
        // 2. Rich product information carousel (#rpi-attribute-book_details-isbn13)
        const rpi13 = doc.querySelector('#rpi-attribute-book_details-isbn13 .rpi-attribute-value');
        if (rpi13 && rpi13.textContent) {
          const m = rpi13.textContent.match(/(\d{3}-\d{10}|\d{13})/);
          if (m) return { raw: m[0], digits: m[0].replace(/[^0-9]/g, '') };
        }
        // 3. Convert 10-digit print ISBN from /dp/<ISBN10> or format switcher (#tmmSwatches)
        const asinMatch = String(url.pathname).match(/\/(?:dp|gp\/product)\/([0-9]{9}[0-9X])(?:[/?&#]|$)/i);
        if (asinMatch) {
          return normalizeIsbn13(asinMatch[1]);
        }
        const swatchLinks = Array.from(doc.querySelectorAll('#tmmSwatches a[href*="/dp/"], #twister a[href*="/dp/"]'));
        for (const a of swatchLinks) {
          const sm = (a.getAttribute('href') || '').match(/\/dp\/([0-9]{9}[0-9X])(?:[/?&#]|$)/i);
          if (sm) {
            return normalizeIsbn13(sm[1]);
          }
        }
        return extractIsbnFromMetaOrJsonLd(doc);
      },
      getTitle(doc) {
        const prodTitle = doc.getElementById('productTitle') || doc.getElementById('ebooksProductTitle');
        if (!prodTitle) return '';
        let t = prodTitle.textContent.replace(/\s+/g, ' ').trim();
        const subEl = doc.getElementById('productSubtitle');
        if (subEl && subEl.textContent) {
          const sub = subEl.textContent.replace(/\s+/g, ' ').trim();
          if (sub && !/hardcover|paperback|kindle|audible|spiral|mass market/i.test(sub)) {
            t = `${t}: ${sub}`;
          }
        }
        return t;
      },
      getPubYear(doc) {
        const pubEl =
          doc.querySelector('#rpi-attribute-book_details-publication_date .rpi-attribute-value') ||
          doc.getElementById('detailBulletsWrapper_feature_div') ||
          doc.getElementById('detailBullets_feature_div') ||
          doc.getElementById('bylineInfo');
        if (pubEl && pubEl.textContent) {
          const ym = pubEl.textContent.match(/\b(19[7-9]\d|20[0-2]\d)\b/g);
          if (ym && ym.length) return Math.max(...ym.map((y) => parseInt(y, 10)));
        }
        return null;
      },
      getEdition(doc) {
        const edEl = doc.querySelector('#rpi-attribute-book_details-edition .rpi-attribute-value');
        if (edEl && edEl.textContent && edEl.textContent.trim()) {
          const rawEd = edEl.textContent.replace(/\s+/g, ' ').trim();
          return /edition|ed\./i.test(rawEd) ? rawEd : `${rawEd} Edition`;
        }
        const metaText = [
          doc.getElementById('productSubtitle') && doc.getElementById('productSubtitle').textContent,
          doc.getElementById('bylineInfo') && doc.getElementById('bylineInfo').textContent,
          doc.getElementById('detailBulletsWrapper_feature_div') && doc.getElementById('detailBulletsWrapper_feature_div').textContent
        ].filter(Boolean).join(' ');
        const mEd = metaText.match(/\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th))\s+(?:ed\.?|edition)\b/i);
        return mEd ? mEd[0].trim() : '';
      }
    },

    // =========================================================================
    // 2. Barnes & Noble Book Pages (combines UserScript: Barnes and Noble Books to Z-Library & Anna's Archive)
    // =========================================================================
    {
      id: 'barnes_and_noble',
      name: 'Barnes & Noble',
      hostRegex: /(^|\.)barnesandnoble\.com$/i,
      pathRegex: /^\/w\//i,
      imageAnchorSelectors: [
        '#prodImage',
        '.pdp-main-image',
        '.product-image-container',
        '#pdp-header-info'
      ],
      titleSelectors: [
        '#pdp-header-info > h1',
        'h1.pdp-header-title',
        'h1[itemprop="name"]'
      ],
      authorSelectors: [
        '#pdp-header-authors a',
        '.contributors a',
        'span.contributors a'
      ],
      getIsbn(doc, url) {
        // 1. UserScript exact method: url.searchParams.get('ean')
        const ean = url.searchParams.get('ean');
        if (ean) {
          const norm = normalizeIsbn13(ean);
          if (norm.digits) return norm;
        }
        // 2. Check Product Details table (#ProductDetailsTab) for ISBN-13
        const detailsTab = doc.getElementById('ProductDetailsTab') || doc.querySelector('.product-details, #PlainTextdp');
        if (detailsTab && detailsTab.textContent) {
          const m = detailsTab.textContent.match(/ISBN-13\s*:\s*([0-9\-]{13,17})/i);
          if (m) return normalizeIsbn13(m[1]);
        }
        return extractIsbnFromMetaOrJsonLd(doc);
      },
      getPubYear(doc) {
        const detailsTab = doc.getElementById('ProductDetailsTab') || doc.body;
        if (detailsTab && detailsTab.textContent) {
          const m = detailsTab.textContent.match(/Publication\s+date\s*:\s*[^\n]*?\b(19[7-9]\d|20[0-2]\d)\b/i);
          if (m) return parseInt(m[1], 10);
        }
        return null;
      },
      getEdition(doc) {
        const detailsTab = doc.getElementById('ProductDetailsTab') || doc.body;
        if (detailsTab && detailsTab.textContent) {
          const m = detailsTab.textContent.match(/Edition\s+description\s*:\s*([^\n\r]+)/i);
          if (m) return m[1].trim();
        }
        return '';
      }
    },

    // =========================================================================
    // 3. Springer Link Book & Chapter Pages (combines UserScript: Springer Books to Z-Library & Anna's Archive)
    // =========================================================================
    {
      id: 'springer_link',
      name: 'Springer Books',
      hostRegex: /^(?:link\.)?springer\.com$/i,
      pathRegex: /^\/(?:book|referencework|referenceworkentry|chapter)\//i,
      imageAnchorSelectors: [
        '#pdpMainImage',
        '#overview',
        'div[data-test="book-cover"]',
        '.c-book-cover',
        '.app-article-masthead--book__info',
        '.c-article-header',
        'h1.c-article-title',
        'h1'
      ],
      titleSelectors: [
        'meta[name="citation_inbook_title"]',
        'div.app-article-masthead--book__info > h1',
        'h1[data-test="book-title"]',
        '.c-chapter-book-details__title a',
        '.c-article-title',
        'meta[name="citation_title"]',
        'h1'
      ],
      authorSelectors: [
        'meta[name="citation_author"]',
        'meta[name="dc.creator"]',
        '.c-book-authors-list a',
        '.c-article-author-list a'
      ],
      getIsbn(doc, url) {
        // 1. UserScript exact method: last segment of pathname (e.g. /book/10.1007/978-3-031-12345-6 or /chapter/10.1007/978-3-031-12345-6_1)
        const parts = url.pathname.split('/').filter(Boolean);
        const lastPart = parts[parts.length - 1] || '';
        const mPath = lastPart.match(/(97[89][0-9\-]{10,14})/);
        if (mPath) {
          return {
            raw: mPath[1], // preserves hyphenated ISBN-13 from URL just like the UserScript
            digits: mPath[1].replace(/[^0-9]/g, '')
          };
        }
        // 2. Fallback to bibliographic info or citation_isbn meta tag
        const metaIsbn = extractIsbnFromMetaOrJsonLd(doc);
        if (metaIsbn.digits) return metaIsbn;
        const bibText = (doc.querySelector('.c-bibliographic-information') || doc.body).textContent || '';
        const mBib = bibText.match(/(97[89]-[0-9\-]{10,14}|97[89][0-9]{10})/);
        if (mBib) {
          return {
            raw: mBib[1],
            digits: mBib[1].replace(/[^0-9]/g, '')
          };
        }
        return { raw: '', digits: '' };
      },
      getPubYear(doc) {
        const copyrightEl = doc.querySelector('meta[name="citation_publication_date"], meta[name="citation_online_date"], meta[name="dc.date"]');
        if (copyrightEl && copyrightEl.getAttribute('content')) {
          const ym = copyrightEl.getAttribute('content').match(/\b(19[0-9]{2}|20[0-3]\d)\b/);
          if (ym) return parseInt(ym[1], 10);
        }
        const bibText = (doc.querySelector('.c-bibliographic-information, .app-article-masthead--book__info') || doc.body).textContent || '';
        const ym2 = bibText.match(/©\s*(19[0-9]{2}|20[0-3]\d)|\b(19[5-9]\d|20[0-3]\d)\b/);
        if (ym2) return parseInt(ym2[1] || ym2[2], 10);
        return null;
      },
      getEdition(doc) {
        const subText = (doc.querySelector('.app-article-masthead--book__info, .c-bibliographic-information') || doc.body).textContent || '';
        const mEd = subText.match(/\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th))\s+(?:ed\.?|edition)\b/i);
        return mEd ? mEd[0].trim() : '';
      }
    },

    // =========================================================================
    // 4. Built-in Bonus Adapters for Popular Book Sites (Goodreads, O'Reilly, Manning, No Starch, Packt)
    // =========================================================================
    {
      id: 'goodreads',
      name: 'Goodreads',
      hostRegex: /(^|\.)goodreads\.com$/i,
      pathRegex: /^\/book\/show\//i,
      imageAnchorSelectors: ['.BookCover', '.BookPage__leftColumn', 'h1[data-testid="bookTitle"]'],
      titleSelectors: ['h1[data-testid="bookTitle"]', '#bookTitle', 'h1'],
      authorSelectors: ['span.ContributorLink__name', 'a.authorName span'],
      getIsbn(doc) {
        return extractIsbnFromMetaOrJsonLd(doc);
      }
    },
    {
      id: 'oreilly',
      name: "O'Reilly Learning",
      hostRegex: /(^|\.)oreilly\.com$/i,
      pathRegex: /\/library\/view\//i,
      imageAnchorSelectors: ['img.t-cover-image', 'figure', 'h1'],
      titleSelectors: ['h1.t-title', 'h1'],
      authorSelectors: ['.t-authors a', '.author-name'],
      getIsbn(doc, url) {
        const m = url.pathname.match(/\/(97[89][0-9]{10})\/?/);
        if (m) return { raw: m[1], digits: m[1] };
        return extractIsbnFromMetaOrJsonLd(doc);
      }
    },
    {
      id: 'nostarch',
      name: 'No Starch Press',
      hostRegex: /(^|\.)nostarch\.com$/i,
      pathRegex: /^\/[a-z0-9\-]+/i,
      shouldActivate(doc) {
        return Boolean(
          doc.querySelector(
            '.field-name-field-isbn13, .field-name-field-isbn, .node-type-product, .uc-product-node, .main-product-image, .product-image'
          )
        );
      },
      imageAnchorSelectors: [
        '.field-name-field-isbn13',
        '.main-product-image',
        '.product-image',
        '.field-name-field-author',
        'h1.page-header'
      ],
      titleSelectors: ['h1.page-header', '.field-name-entity-title h1', 'h1'],
      authorSelectors: ['.field-name-field-author .field-item', '.field-name-field-author', '.author'],
      getTitle(doc) {
        const h1 = doc.querySelector('h1.page-header, .field-name-entity-title h1, h1');
        if (!h1) return '';
        let title = h1.textContent.replace(/\s+/g, ' ').trim();
        const subEl = doc.querySelector('.field-name-field-subtitle .field-item, .field-name-field-subtitle');
        if (subEl && subEl.textContent) {
          const sub = subEl.textContent.replace(/\s+/g, ' ').trim();
          if (sub && !title.toLowerCase().includes(sub.toLowerCase())) {
            title = `${title}: ${sub}`;
          }
        }
        return title;
      },
      getIsbn(doc) {
        const isbnEl = doc.querySelector('.field-name-field-isbn13, .field-name-field-isbn');
        const text = (isbnEl || doc.body).textContent || '';
        const m = text.match(/(97[89][0-9\-]{10,14})/);
        if (m) return { raw: m[1], digits: m[1].replace(/[^0-9]/g, '') };
        return extractIsbnFromMetaOrJsonLd(doc);
      },
      getPubYear(doc) {
        const relEl = doc.querySelector('.field-name-released-date .field-item, .field-name-released-date');
        if (relEl && relEl.textContent) {
          const ym = relEl.textContent.match(/\b(19[7-9]\d|20[0-3]\d)\b/);
          if (ym) return parseInt(ym[1], 10);
        }
        return null;
      }
    },

    // =========================================================================
    // 5. Pearson & Pearson+ (pearson.com/en-us/pearsonplus/p/..., /subject-catalog/p/...)
    // =========================================================================
    {
      id: 'pearson',
      name: 'Pearson',
      hostRegex: /(^|\.)pearson\.com$/i,
      pathRegex: /\/(?:pearsonplus\/p|subject-catalog\/p|store\/p)\//i,
      imageAnchorSelectors: [
        '[class*="ProductHead_wrapper_left"]',
        'img[class*="ProductImg_img"]',
        '[class*="Viewparts__StyledImageWrapper"]',
        'img[class*="Detailsparts__Image"]',
        'h1[class*="ProductDetails_title"]',
        'h1[class*="Descriptionparts__Name"]',
        'h1'
      ],
      titleSelectors: [
        'h1[class*="ProductDetails_title"]',
        'h1[class*="Descriptionparts__Name"]',
        'h1.analytics-product-title',
        'h1'
      ],
      authorSelectors: [
        '.analytics-product-authors',
        '[class*="ProductDetails_authors_item"]',
        '[class*="Descriptionparts__Author-"]'
      ],
      getTitle(doc) {
        const h1 = firstElementBySelectors(doc, [
          'h1[class*="ProductDetails_title"]',
          'h1[class*="Descriptionparts__Name"]',
          'h1.analytics-product-title',
          'h1'
        ]);
        if (!h1) return '';
        return h1.textContent.replace(/\s*,\s*(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th))\s+edition\s*$/i, '').replace(/\s+/g, ' ').trim();
      },
      getAuthor(doc) {
        const pplusEl = doc.querySelector('.analytics-product-authors, [class*="ProductDetails_authors__"]');
        if (pplusEl && pplusEl.textContent) {
          return cleanExtractedAuthor(pplusEl.textContent);
        }
        const authorNodes = Array.from(doc.querySelectorAll('[class*="Descriptionparts__Author-"]'));
        if (authorNodes.length > 0) {
          const names = authorNodes
            .map((el) => {
              let t = (el.textContent || '').replace(/\s+/g, ' ').trim();
              // Strip trailing university/college affiliation if concatenated
              t = t.replace(/\s+(?:University|College|Institute|State|School)\b.*$/i, '').trim();
              return cleanExtractedAuthor(t);
            })
            .filter(Boolean);
          if (names.length > 0) return Array.from(new Set(names)).join(', ');
        }
        return extractAuthorFromMetaOrJsonLd(doc);
      },
      getIsbn(doc, url) {
        const mUrl = url.pathname.match(/\/(97[89][0-9]{10})(?:[/?#]|$)/);
        if (mUrl) return { raw: mUrl[1], digits: mUrl[1] };
        const metaIsbn = extractIsbnFromMetaOrJsonLd(doc);
        if (metaIsbn.digits) return metaIsbn;
        const bodyTxt = (doc.body && doc.body.textContent) || '';
        const mTxt = bodyTxt.match(/ISBN(?:-13)?\s*[:\s]*?(97[89][0-9\-]{10,14})/i);
        if (mTxt) return normalizeIsbn13(mTxt[1]);
        return { raw: '', digits: '' };
      },
      getEdition(doc) {
        const h1 = doc.querySelector('h1');
        if (h1 && h1.textContent) {
          const m = h1.textContent.match(/\b(\d+(?:st|nd|rd|th)\s+edition)\b/i);
          if (m) return m[1];
        }
        const ldScripts = Array.from(doc.querySelectorAll('script[type="application/ld+json"]'));
        for (const s of ldScripts) {
          const m = (s.textContent || '').match(/"bookEdition"\s*:\s*"([^"]+)"/i);
          if (m) return m[1].trim();
        }
        return '';
      }
    },

    // =========================================================================
    // 6. McGraw-Hill Education (mheducation.com/highered/product/..., /prek-12/product/...)
    // =========================================================================
    {
      id: 'mheducation',
      name: 'McGraw-Hill',
      hostRegex: /(^|\.)mheducation\.com$/i,
      pathRegex: /\/(?:highered|prek-12)\/(?:product|mhebook)\//i,
      imageAnchorSelectors: [
        '.heproductimage',
        '.isbnContainer',
        'img[src*="cover-images/"]',
        'h1'
      ],
      titleSelectors: ['h1'],
      authorSelectors: ['.heauthordetails h3', '.heauthordetails h4'],
      getTitle(doc) {
        const h1 = doc.querySelector('h1');
        if (!h1) return '';
        const raw = h1.textContent.replace(/\s+/g, ' ').trim();
        // McGraw-Hill formats <h1> as "<Title> by <Author1>, <Author2> and <Author3>"
        return raw.split(/\s+by\s+/i)[0].trim();
      },
      getAuthor(doc) {
        const h1 = doc.querySelector('h1');
        if (h1 && /\s+by\s+/i.test(h1.textContent || '')) {
          const afterBy = h1.textContent.replace(/\s+/g, ' ').trim().split(/\s+by\s+/i).slice(1).join(' ');
          const cleaned = afterBy
            .replace(/\s+and\s+/gi, ', ')
            .split(',')
            .map((s) => cleanExtractedAuthor(s))
            .filter(Boolean);
          if (cleaned.length > 0) return Array.from(new Set(cleaned)).join(', ');
        }
        return extractAuthorFromMetaOrJsonLd(doc);
      },
      getIsbn(doc) {
        const isbn13El = doc.querySelector('.isbn13, .isbnContainer');
        if (isbn13El && isbn13El.textContent) {
          const m13 = isbn13El.textContent.match(/(97[89][0-9\-]{10,14})/);
          if (m13) return normalizeIsbn13(m13[1]);
          const m10 = isbn13El.textContent.match(/\b([0-9]{9}[0-9Xx])\b/);
          if (m10) return normalizeIsbn13(m10[1]);
        }
        const coverImg = doc.querySelector('img[src*="cover-images/"]');
        if (coverImg && coverImg.getAttribute('src')) {
          const mImg = coverImg.getAttribute('src').match(/\/(97[89][0-9]{10}|[0-9]{9}[0-9Xx])\.(?:webp|jpg|png)/i);
          if (mImg) return normalizeIsbn13(mImg[1]);
        }
        return extractIsbnFromMetaOrJsonLd(doc);
      },
      getPubYear(doc) {
        const bodyTxt = (doc.querySelector('main') || doc.body).textContent || '';
        const m = bodyTxt.match(/(?:Copyright|Published)\s*:?\s*(19[7-9]\d|20[0-3]\d)/i);
        return m ? parseInt(m[1], 10) : null;
      },
      getEdition(doc) {
        const bodyTxt = (doc.querySelector('main') || doc.body).textContent || '';
        const m = bodyTxt.match(/\b(\d+(?:st|nd|rd|th)\s+Edition)\b/i);
        return m ? m[1] : '';
      }
    },

    // =========================================================================
    // 7. Cengage (cengage.com/c/...)
    // =========================================================================
    {
      id: 'cengage',
      name: 'Cengage',
      hostRegex: /(^|\.)cengage\.(com|uk|ca|com\.au)$/i,
      pathRegex: /^\/c\/|\/product\//i,
      imageAnchorSelectors: [
        '.pdp-header_title-group',
        '.pdp_isbn',
        'h1[cg-data-element="pdp-header-title"]',
        'h1'
      ],
      titleSelectors: [
        'h1[cg-data-element="pdp-header-title"]',
        '.pdp-header_title-group h1',
        'h1'
      ],
      authorSelectors: [
        '[cg-data-element="pdp-header-authors"]',
        '.pdp-header_authors',
        '.pdp-author'
      ],
      getAuthor(doc) {
        const authEl = doc.querySelector('[cg-data-element="pdp-header-authors"], .pdp-header_authors, .pdp-author');
        if (authEl && authEl.textContent) {
          return cleanExtractedAuthor(authEl.textContent);
        }
        // Check header area around h1 for author line
        const h1 = doc.querySelector('h1[cg-data-element="pdp-header-title"], h1');
        if (h1 && h1.parentElement && h1.parentElement.parentElement) {
          const headerTxt = h1.parentElement.parentElement.innerText || '';
          const lines = headerTxt.split('\n').map((l) => l.trim()).filter(Boolean);
          const h1Txt = (h1.textContent || '').trim();
          const h1Idx = lines.findIndex((l) => l === h1Txt || l.includes(h1Txt));
          if (h1Idx >= 0 && lines[h1Idx + 1] && !/edition|isbn|\$|starting|format/i.test(lines[h1Idx + 1])) {
            return cleanExtractedAuthor(lines[h1Idx + 1]);
          }
        }
        return extractAuthorFromMetaOrJsonLd(doc);
      },
      getIsbn(doc, url) {
        const mUrl = url.pathname.match(/\/(97[89][0-9]{10})(?:[/?#]|$)/);
        if (mUrl) return { raw: mUrl[1], digits: mUrl[1] };
        const isbnEl = doc.querySelector('.pdp_isbn-text, .pdp_isbn');
        if (isbnEl && isbnEl.textContent) {
          const m = isbnEl.textContent.match(/(97[89][0-9\-]{10,14})/);
          if (m) return normalizeIsbn13(m[1]);
        }
        return extractIsbnFromMetaOrJsonLd(doc);
      },
      getEdition(doc, url) {
        const mSlug = url.pathname.match(/-(\d+)e-/i);
        if (mSlug) {
          const n = parseInt(mSlug[1], 10);
          const suf = n === 1 ? 'st' : (n === 2 ? 'nd' : (n === 3 ? 'rd' : 'th'));
          return `${n}${suf} Edition`;
        }
        return '';
      }
    },

    // =========================================================================
    // 8. Wiley & Wiley Online Library (onlinelibrary.wiley.com & wiley.com)
    // =========================================================================
    {
      id: 'wiley',
      name: 'Wiley',
      hostRegex: /(^|\.)(onlinelibrary\.wiley\.com|wiley\.com)$/i,
      pathRegex: /\/(?:doi\/book\/|doi\/(?:abs|full|epdf|pdf)\/10\.\d+\/97[89]|en-[a-z]{2}\/.*-p-97[89])/i,
      imageAnchorSelectors: [
        '.cover-image',
        '.product-image',
        '.journal-info-container',
        '.banner-text',
        '.product-biblio__header',
        'h1'
      ],
      titleSelectors: [
        '.banner-text h1',
        'h1.citation__title',
        'h1.product-biblio__title',
        'meta[name="citation_title"]',
        'h1'
      ],
      authorSelectors: [
        '.publication_contrib_author',
        'a.author-name',
        '.product-biblio__author',
        'meta[name="citation_author"]',
        'meta[name="dc.creator"]'
      ],
      getIsbn(doc, url) {
        const mUrl = url.pathname.match(/(?:\/10\.\d+\/|-p-)(97[89][0-9]{10}|[0-9]{9}[0-9Xx])(?:[/?#]|$)/i);
        if (mUrl) return normalizeIsbn13(mUrl[1]);
        const metaIsbn = extractIsbnFromMetaOrJsonLd(doc);
        if (metaIsbn.digits) return metaIsbn;
        const bodyTxt = (doc.querySelector('.info-block, .product-biblio') || doc.body).textContent || '';
        const mTxt = bodyTxt.match(/(?:Print\s+ISBN|Online\s+ISBN|ISBN)\s*:?\s*(97[89][0-9\-]{10,14})/i);
        if (mTxt) return normalizeIsbn13(mTxt[1]);
        return { raw: '', digits: '' };
      },
      getPubYear(doc) {
        const metaDate = doc.querySelector('meta[name="citation_publication_date"], meta[name="dc.date"]');
        if (metaDate && metaDate.getAttribute('content')) {
          const m = metaDate.getAttribute('content').match(/\b(19[7-9]\d|20[0-3]\d)\b/);
          if (m) return parseInt(m[1], 10);
        }
        return null;
      }
    },

    // =========================================================================
    // 9. Manning Publications (manning.com/books/... & livebook.manning.com/book/...)
    // =========================================================================
    {
      id: 'manning',
      name: 'Manning Publications',
      hostRegex: /(^|\.)(manning\.com|livebook\.manning\.com)$/i,
      pathRegex: /^\/(?:books|book)\/[a-z0-9\-]+/i,
      imageAnchorSelectors: [
        '.product-cover-wrapper',
        '.product-title-wrapper',
        'h1.product-title',
        'h1'
      ],
      titleSelectors: [
        'h1.product-title',
        '.product-title-wrapper h1',
        'h1'
      ],
      authorSelectors: [
        '.product-authors',
        '.product-author'
      ],
      getAuthor(doc) {
        const ldAuthor = extractAuthorFromMetaOrJsonLd(doc);
        if (ldAuthor) return ldAuthor;
        const domAuth = firstTextBySelectors(doc, ['.product-authors', '.product-author']);
        if (domAuth) return cleanExtractedAuthor(domAuth);
        const ogTitle = doc.querySelector('meta[property="og:title"], meta[name="apple-mobile-web-app-title"]');
        if (ogTitle && ogTitle.getAttribute('content') && ogTitle.getAttribute('content').includes(' - ')) {
          const parts = ogTitle.getAttribute('content').split(' - ');
          return cleanExtractedAuthor(parts[parts.length - 1]);
        }
        return '';
      },
      getIsbn(doc) {
        const metaIsbn = extractIsbnFromMetaOrJsonLd(doc);
        if (metaIsbn.digits) return metaIsbn;
        const bodyTxt = (doc.body && doc.body.textContent) || '';
        const m = bodyTxt.match(/ISBN\s*:?\s*(97[89][0-9\-]{10,14})/i);
        if (m) return normalizeIsbn13(m[1]);
        return { raw: '', digits: '' };
      },
      getPubYear(doc) {
        const ldScripts = Array.from(doc.querySelectorAll('script[type="application/ld+json"]'));
        for (const s of ldScripts) {
          const m = (s.textContent || '').match(/"releaseDate"\s*:\s*"(19[7-9]\d|20[0-3]\d)/i);
          if (m) return parseInt(m[1], 10);
        }
        return null;
      }
    },

    // =========================================================================
    // 10. Simon & Schuster (simonandschuster.com/books/<Title>/<Author>/<ISBN13>)
    // =========================================================================
    {
      id: 'simon_and_schuster',
      name: 'Simon & Schuster',
      hostRegex: /(^|\.)simonandschuster\.(com|co\.uk|ca|com\.au|co\.in)$/i,
      pathRegex: /^\/books\/[^/]+/i,
      imageAnchorSelectors: [
        '.book-cover',
        '.book-image',
        '.book-jacket',
        '.primary-image',
        'img[src*="d28hgpri8am2if.cloudfront.net"]',
        'img[src*="simonandschuster"]',
        '.book-details',
        '.book-info',
        'h1'
      ],
      titleSelectors: [
        'h1.book-title',
        '.book-details h1',
        'h1[itemprop="name"]',
        'h1'
      ],
      authorSelectors: [
        '.book-author a',
        '.book-authors a',
        '.author-name a',
        'a[href*="/authors/"]',
        '[itemprop="author"]'
      ],
      getTitle(doc, url) {
        // 1. Check JSON-LD Book/Product name
        const ldScripts = Array.from(doc.querySelectorAll('script[type="application/ld+json"]'));
        for (const s of ldScripts) {
          try {
            const parsed = JSON.parse(s.textContent || '{}');
            const items = Array.isArray(parsed) ? parsed : (parsed['@graph'] || [parsed]);
            for (const item of items) {
              if (item && item.name && /book|product/i.test(String(item['@type'] || ''))) {
                return String(item.name).replace(/\s+/g, ' ').trim();
              }
            }
          } catch (_) {}
        }
        // 2. Check H1
        const h1 = firstElementBySelectors(doc, ['h1.book-title', '.book-details h1', 'h1[itemprop="name"]', 'h1']);
        if (h1 && h1.textContent && h1.textContent.trim()) {
          return h1.textContent.replace(/\s+/g, ' ').trim();
        }
        // 3. Check og:title ("The Death of Mrs. Westaway | Book by Ruth Ware | Official Publisher Page | Simon & Schuster")
        const ogTitle = doc.querySelector('meta[property="og:title"]');
        if (ogTitle && ogTitle.getAttribute('content')) {
          const t = ogTitle.getAttribute('content').split(/\s*\|\s*/)[0].trim();
          if (t) return t;
        }
        // 4. Fallback to URL slug (/books/The-Death-of-Mrs-Westaway/Ruth-Ware/9781501156229)
        const parts = (url.pathname || '').split('/').filter(Boolean);
        if (parts[0] && parts[0].toLowerCase() === 'books' && parts[1]) {
          return decodeURIComponent(parts[1]).replace(/-/g, ' ').trim();
        }
        return '';
      },
      getAuthor(doc, url) {
        // 1. JSON-LD or citation/meta tags
        const ldAuthor = extractAuthorFromMetaOrJsonLd(doc);
        if (ldAuthor) return ldAuthor;
        // 2. Check og:title or document.title for "Book by <Author> |"
        const ogTitle = (doc.querySelector('meta[property="og:title"]') && doc.querySelector('meta[property="og:title"]').getAttribute('content')) || doc.title || '';
        const mBy = ogTitle.match(/\bBook\s+by\s+([^|]+?)(?:\s*\||$)/i);
        if (mBy && mBy[1]) {
          return cleanExtractedAuthor(mBy[1]);
        }
        // 3. Check DOM author links
        const domAuth = firstTextBySelectors(doc, [
          '.book-author a',
          '.book-authors a',
          '.author-name a',
          '[itemprop="author"]',
          'main a[href*="/authors/"]'
        ]);
        if (domAuth) return cleanExtractedAuthor(domAuth);
        // 4. Fallback to URL slug (/books/<Title>/<Author>/<ISBN13>)
        const parts = (url.pathname || '').split('/').filter(Boolean);
        if (parts.length >= 4 && parts[0].toLowerCase() === 'books') {
          const authorSlug = parts[2];
          if (authorSlug && !/^97[89]\d{10}$/.test(authorSlug)) {
            return decodeURIComponent(authorSlug).replace(/-/g, ' ').trim();
          }
        }
        return '';
      },
      getIsbn(doc, url) {
        // 1. Extract directly from URL path (/books/<Title>/<Author>/9781501156229)
        const mUrl = (url.pathname || '').match(/\/(97[89][0-9\-]{10,14}|\d{9}[0-9Xx])(?:[/?#]|$)/i);
        if (mUrl) {
          const norm = normalizeIsbn13(mUrl[1]);
          if (norm.digits) return norm;
        }
        // 2. Meta tags / JSON-LD
        const metaIsbn = extractIsbnFromMetaOrJsonLd(doc);
        if (metaIsbn.digits) return metaIsbn;
        // 3. Product details section text
        const bodyTxt = (doc.body && doc.body.textContent) || '';
        const mTxt = bodyTxt.match(/(?:eBook\s+ISBN|ISBN(?:-13)?)\s*[:\s]*?(97[89][0-9\-]{10,14})/i);
        if (mTxt) return normalizeIsbn13(mTxt[1]);
        return { raw: '', digits: '' };
      },
      getPubYear(doc) {
        const ldScripts = Array.from(doc.querySelectorAll('script[type="application/ld+json"]'));
        for (const s of ldScripts) {
          const m = (s.textContent || '').match(/"(?:datePublished|releaseDate)"\s*:\s*"(19[0-9]{2}|20[0-3]\d)/i);
          if (m) return parseInt(m[1], 10);
        }
        const metaDate = doc.querySelector('meta[property="books:release_date"], meta[name="publication_date"]');
        if (metaDate && metaDate.getAttribute('content')) {
          const ym = metaDate.getAttribute('content').match(/\b(19[0-9]{2}|20[0-3]\d)\b/);
          if (ym) return parseInt(ym[1], 10);
        }
        const bodyTxt = (doc.querySelector('.book-details, .product-details, main') || doc.body).textContent || '';
        const mPub = bodyTxt.match(/(?:Published|Publication\s+Date|Release\s+Date|On\s+Sale)\s*:?\s*[^\n\r]{0,30}?\b(19[5-9]\d|20[0-3]\d)\b/i);
        return mPub ? parseInt(mPub[1], 10) : null;
      }
    }
  ];

  /**
   * Convert a user-configured Custom Site rule (from chrome.storage.sync.customSites)
   * into a full runtime adapter object.
   *
   * Custom site schema:
   * {
   *   id: 'custom_1',
   *   name: 'Packt Pub',
   *   hostPattern: 'packtpub.com',
   *   pathPattern: '/product/',
   *   imageSelector: '.product-image, h1',
   *   titleSelector: 'h1',
   *   authorSelector: '.author',
   *   isbnSelector: '.isbn',
   *   isbnUrlParam: 'ean',
   *   isbnFromPathRegex: '(97[89][0-9-]{10,14})'
   * }
   */
  function buildCustomAdapter(cfg) {
    if (!cfg || !cfg.hostPattern) return null;
    const splitSelectors = (str) =>
      String(str || '')
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);

    return {
      id: cfg.id || `custom_${cfg.hostPattern}`,
      name: cfg.name || cfg.hostPattern,
      isCustom: true,
      hostRegex: new RegExp(cfg.hostPattern.replace(/\./g, '\\.').replace(/\*/g, '.*'), 'i'),
      pathRegex: cfg.pathPattern ? new RegExp(cfg.pathPattern, 'i') : null,
      imageAnchorSelectors: splitSelectors(cfg.imageSelector || 'h1'),
      titleSelectors: splitSelectors(cfg.titleSelector || 'h1'),
      authorSelectors: splitSelectors(cfg.authorSelector || ''),
      getIsbn(doc, url) {
        if (cfg.isbnUrlParam) {
          const val = url.searchParams.get(cfg.isbnUrlParam);
          if (val) {
            const norm = normalizeIsbn13(val);
            if (norm.digits) return norm;
          }
        }
        if (cfg.isbnFromPathRegex) {
          try {
            const re = new RegExp(cfg.isbnFromPathRegex);
            const m = url.pathname.match(re);
            if (m) {
              const raw = m[1] || m[0];
              return { raw, digits: raw.replace(/[^0-9Xx]/g, '').toUpperCase() };
            }
          } catch (_) {}
        }
        if (cfg.isbnSelector) {
          const txt = firstTextBySelectors(doc, splitSelectors(cfg.isbnSelector));
          if (txt) {
            const m = txt.match(/(97[89][0-9\-]{10,14}|\b[0-9]{9}[0-9Xx]\b)/);
            if (m) return normalizeIsbn13(m[1]);
          }
        }
        return extractIsbnFromMetaOrJsonLd(doc);
      }
    };
  }

  function findMatchingSiteAdapter(doc, url, customSites = []) {
    const customAdapters = (Array.isArray(customSites) ? customSites : [])
      .map(buildCustomAdapter)
      .filter(Boolean);
    const allAdapters = [...customAdapters, ...BUILTIN_SITE_ADAPTERS];

    for (const adapter of allAdapters) {
      if (adapter.hostRegex && !adapter.hostRegex.test(url.hostname)) continue;
      if (adapter.pathRegex && !adapter.pathRegex.test(url.pathname)) continue;
      if (typeof adapter.shouldActivate === 'function' && !adapter.shouldActivate(doc, url)) continue;
      return adapter;
    }
    return null;
  }

  function extractBookDetailsWithAdapter(adapter, doc, url) {
    const isbnObj = typeof adapter.getIsbn === 'function'
      ? adapter.getIsbn(doc, url)
      : extractIsbnFromMetaOrJsonLd(doc);

    const rawTitle = typeof adapter.getTitle === 'function'
      ? adapter.getTitle(doc, url)
      : firstTextBySelectors(doc, adapter.titleSelectors || ['h1']);

    const rawAuthor = cleanExtractedAuthor(
      (typeof adapter.getAuthor === 'function'
        ? adapter.getAuthor(doc, url)
        : allTextBySelectors(doc, adapter.authorSelectors || [])) ||
      extractAuthorFromMetaOrJsonLd(doc)
    );

    const asin = typeof adapter.getAsin === 'function' ? adapter.getAsin(doc, url) : '';
    const pub_year = typeof adapter.getPubYear === 'function' ? adapter.getPubYear(doc, url) : null;
    const edition = typeof adapter.getEdition === 'function' ? adapter.getEdition(doc, url) : '';
    const anchorEl = firstElementBySelectors(doc, adapter.imageAnchorSelectors || []);

    return {
      adapterId: adapter.id,
      siteName: adapter.name,
      asin: asin || '',
      isbnRaw: (isbnObj && isbnObj.raw) || '',
      isbnDigits: (isbnObj && isbnObj.digits) || '',
      title: rawTitle || '',
      author: rawAuthor || '',
      pub_year: pub_year || null,
      edition: edition || '',
      anchorEl
    };
  }

  /**
   * Extract book cards on multi-result publisher search / catalog pages:
   * - Pearson / Pearson+ Search (pearson.com/en-us/pearsonplus/search.html, /search.html)
   * - McGraw-Hill Course & Catalog Pages (mheducation.com <mhecomm-he-product-cards> shadow DOM)
   * - Cengage Search & Catalog (cengage.com/s?q=...)
   * - Wiley Online Library & Wiley.com Search (onlinelibrary.wiley.com/action/doSearch, wiley.com)
   * - Manning Catalog & Search (manning.com/catalog, manning.com/search)
   */
  function extractSearchCardsFromPage(doc, url) {
    const host = (url && url.hostname) || '';
    const cards = [];

    // 1. Pearson / Pearson+ Search Cards (li.ais-Hits-item .programItem)
    if (/(^|\.)pearson\.com$/i.test(host)) {
      const items = Array.from(doc.querySelectorAll('li.ais-Hits-item .programItem, .programItem'));
      items.forEach((item, idx) => {
        const details = item.querySelector('.programItem__details');
        const titleEl = item.querySelector('.programItem__title, h2, h3');
        const authorEl = item.querySelector('.programItem__author');
        const editionEl = item.querySelector('.programItem__edition');
        const isbnEl = item.querySelector('.programItem__isbn');
        const linkEl = item.querySelector('a[href]');

        const rawIsbn =
          (details && details.getAttribute('data-isbn')) ||
          (isbnEl && isbnEl.textContent) ||
          (linkEl && linkEl.getAttribute('href')) ||
          '';
        const mIsbn = String(rawIsbn).match(/(97[89][0-9]{10})/);
        const isbnObj = mIsbn ? normalizeIsbn13(mIsbn[1]) : { raw: '', digits: '' };

        const title = titleEl ? titleEl.textContent.replace(/\s+/g, ' ').trim() : '';
        if (!title && !isbnObj.digits) return;

        const author = authorEl ? cleanExtractedAuthor(authorEl.textContent) : '';
        const edition = editionEl ? editionEl.textContent.replace(/\s+/g, ' ').trim() : '';
        const pubDateAttr = item.getAttribute('data-publication_date') || '';
        const ym = pubDateAttr.match(/\b(19[7-9]\d|20[0-3]\d)\b/);
        const pub_year = ym ? parseInt(ym[1], 10) : null;

        const targetContainer = item.querySelector('.programItem__buttonWrapper') || item;
        cards.push({
          key: `pearson_card_${isbnObj.digits || idx}_${title.slice(0, 24)}`,
          siteName: 'Pearson',
          isbnRaw: isbnObj.raw,
          isbnDigits: isbnObj.digits,
          title,
          author,
          edition,
          pub_year,
          cardEl: item,
          mountEl: targetContainer
        });
      });
    }

    // 2. McGraw-Hill Course / Discipline / Search Cards (Shadow DOM <mhecomm-he-product-cards>)
    if (/(^|\.)mheducation\.com$/i.test(host)) {
      const cardGroups = Array.from(doc.querySelectorAll('mhecomm-he-product-cards'));
      cardGroups.forEach((group) => {
        if (!group.shadowRoot) return;
        const cardDisplays = Array.from(group.shadowRoot.querySelectorAll('mhecomm-he-product-card-display'));
        cardDisplays.forEach((cardDisp, idx) => {
          const sr = cardDisp.shadowRoot;
          if (!sr) return;
          const imgComp = sr.querySelector('mhecomm-product-card-image-display');
          const titleEl = sr.querySelector('.product-card-title, h3 a, h3');
          const byEl = Array.from(sr.querySelectorAll('p')).find((p) => /by\s*:/i.test(p.textContent || ''));

          const title =
            (titleEl && titleEl.textContent.replace(/\s+/g, ' ').trim()) ||
            (imgComp && imgComp.getAttribute('producttitle')) ||
            '';
          if (!title) return;

          const author = byEl ? cleanExtractedAuthor(byEl.textContent.replace(/^.*?\bby\s*:\s*/i, '')) : '';
          const imgUrl = (imgComp && imgComp.getAttribute('productimageurl')) || '';
          const mIsbn = imgUrl.match(/\/(97[89][0-9]{10}|[0-9]{9}[0-9Xx])\.(?:webp|jpg|png)/i);
          const isbnObj = mIsbn ? normalizeIsbn13(mIsbn[1]) : { raw: '', digits: '' };

          const mountEl = sr.querySelector('.product-card-contents') || sr.querySelector('.product-card');
          if (!mountEl) return;

          cards.push({
            key: `mhe_card_${isbnObj.digits || idx}_${title.slice(0, 24)}`,
            siteName: 'McGraw-Hill',
            isbnRaw: isbnObj.raw,
            isbnDigits: isbnObj.digits,
            title,
            author,
            edition: '',
            pub_year: null,
            cardEl: cardDisp,
            mountEl,
            isShadowDom: true
          });
        });
      });
    }

    // 3. Cengage Search & Catalog Cards (a.results-item)
    if (/(^|\.)cengage\.(com|uk|ca|com\.au)$/i.test(host)) {
      const items = Array.from(doc.querySelectorAll('a.results-item:not(.hide)'));
      items.forEach((item, idx) => {
        const objId = item.getAttribute('cg-object-id') || '';
        const href = item.getAttribute('href') || '';
        if (!objId && (!href || href === '#')) return;

        const titleEl = item.querySelector('[cg-data-element="result-card-title"], .results-item_title, h3');
        const authorEl = item.querySelector('[cg-data-element="result-card-authors"], .results-item_authors');
        const edEl = item.querySelector('[cg-data-element="result-card-edition"], .results-item_meta-item');

        const title = titleEl ? titleEl.textContent.replace(/\s+/g, ' ').trim() : '';
        if (!title) return;

        const author = authorEl ? cleanExtractedAuthor(authorEl.textContent) : '';
        const edText = edEl ? edEl.textContent.replace(/\s+/g, ' ').trim() : '';
        const mEd = edText.match(/\b(\d+(?:st|nd|rd|th)\s+Edition)\b/i);
        const edition = mEd ? mEd[1] : '';
        const mYr = edText.match(/\b(19[7-9]\d|20[0-3]\d)\b/);
        const pub_year = mYr ? parseInt(mYr[1], 10) : null;

        const mIsbn = `${objId} ${href}`.match(/(97[89][0-9]{10})/);
        const isbnObj = mIsbn ? normalizeIsbn13(mIsbn[1]) : { raw: '', digits: '' };

        const mountEl = item.querySelector('.results-item_body') || item;
        cards.push({
          key: `cengage_card_${isbnObj.digits || idx}_${title.slice(0, 24)}`,
          siteName: 'Cengage',
          isbnRaw: isbnObj.raw,
          isbnDigits: isbnObj.digits,
          title,
          author,
          edition,
          pub_year,
          cardEl: item,
          mountEl
        });
      });
    }

    // 4. Wiley Online Library & Wiley.com Search Cards
    if (/(^|\.)(onlinelibrary\.wiley\.com|wiley\.com)$/i.test(host)) {
      const items = Array.from(doc.querySelectorAll('li.search__item, article.product-item, .product-item'));
      items.forEach((item, idx) => {
        const titleEl = item.querySelector('.hlFld-Title a, .publication_title, .product-item__title, h2, h3');
        const authorEl = item.querySelector('.hlFld-ContribAuthor, .meta__authors, .product-item__authors');
        const linkEl = item.querySelector('a[href*="/doi/"], a[href*="-p-97"]');

        const title = titleEl ? titleEl.textContent.replace(/\s+/g, ' ').trim() : '';
        if (!title) return;

        const author = authorEl ? cleanExtractedAuthor(authorEl.textContent) : '';
        const href = (linkEl && linkEl.getAttribute('href')) || item.textContent || '';
        const mIsbn = href.match(/(97[89][0-9]{10})/);
        const isbnObj = mIsbn ? normalizeIsbn13(mIsbn[1]) : { raw: '', digits: '' };

        const mountEl = item.querySelector('.item__body, .product-item__content') || item;
        cards.push({
          key: `wiley_card_${isbnObj.digits || idx}_${title.slice(0, 24)}`,
          siteName: 'Wiley',
          isbnRaw: isbnObj.raw,
          isbnDigits: isbnObj.digits,
          title,
          author,
          edition: '',
          pub_year: null,
          cardEl: item,
          mountEl
        });
      });
    }

    // 5. Manning Catalog & Search Cards (div.product-root & div.book-item)
    if (/(^|\.)manning\.com$/i.test(host)) {
      const items = Array.from(doc.querySelectorAll('div.product-root'));
      items.forEach((item, idx) => {
        const titleEl = item.querySelector('a._title, ._title-container a');
        const coverLinkEl = item.querySelector('a._cover-wrapper, a[href*="/books/"]');
        const imgEl = item.querySelector('._cover-wrapper img[alt], img[alt]');
        const authorEl = item.querySelector('._authors-container');
        const dateEl = item.querySelector('._date-container');

        const textTitle = titleEl
          ? (titleEl.getAttribute('title') || titleEl.textContent || '').replace(/\s+/g, ' ').replace(/(?:\.\.\.|…)\s*$/, '').trim()
          : '';
        const rawAlt = imgEl ? (imgEl.getAttribute('alt') || '').replace(/\s+/g, ' ').trim() : '';
        const altTitle = rawAlt && rawAlt.toLowerCase() !== 'cover' ? rawAlt : '';

        const hrefStr = (titleEl && titleEl.getAttribute('href')) || (coverLinkEl && coverLinkEl.getAttribute('href')) || '';
        const hrefSlugMatch = hrefStr.match(/\/books\/([a-z0-9\-]+)/i);
        const rawSlug = item.getAttribute('data-product-slug') || (hrefSlugMatch ? hrefSlugMatch[1] : '');
        const slugWords = rawSlug ? rawSlug.replace(/-/g, ' ').trim() : '';

        // Prefer img[alt] when a._title text was visually truncated (~42 chars) by Manning's card layout,
        // and if img[alt] is 'cover' (e.g. in Recently Viewed cards), restore the truncated tail from the URL slug!
        let title = (altTitle && altTitle.length > textTitle.length) ? altTitle : (textTitle || altTitle);
        if (slugWords && slugWords.length > title.length) {
          const normTitle = title.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
          const normSlug = slugWords.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
          if (normSlug.startsWith(normTitle) || normTitle.length >= 30) {
            title = slugWords;
          }
        }
        if (!title) return;

        const author = authorEl ? cleanExtractedAuthor(authorEl.getAttribute('title') || authorEl.textContent) : '';
        const ym = (dateEl && dateEl.textContent || '').match(/\b(19[7-9]\d|20[0-3]\d)\b/);
        const pub_year = ym ? parseInt(ym[1], 10) : null;
        const slug = rawSlug || `${idx}`;

        cards.push({
          key: `manning_card_${slug}`,
          siteName: 'Manning',
          isbnRaw: '',
          isbnDigits: '',
          title,
          author,
          edition: '',
          pub_year,
          cardEl: item,
          mountEl: item
        });
      });

      const bookItems = Array.from(doc.querySelectorAll('div.book-item'));
      bookItems.forEach((item, idx) => {
        const linkEl = item.querySelector('a.book-item-image-link[data-name], a[data-name]');
        if (!linkEl) return;
        const title = (linkEl.getAttribute('data-name') || '').replace(/\s+/g, ' ').trim();
        if (!title) return;
        const dataId = linkEl.getAttribute('data-id') || `${idx}`;
        cards.push({
          key: `manning_bookitem_${dataId}`,
          siteName: 'Manning',
          isbnRaw: '',
          isbnDigits: '',
          title,
          author: '',
          edition: linkEl.getAttribute('data-edition') || '',
          pub_year: null,
          cardEl: item,
          mountEl: item
        });
      });
    }

    // 6. Springer Link Search & Series Cards (link.springer.com/search?source=new-es&..., /search, /series/...)
    if (/^(?:link\.)?springer\.com$/i.test(host) && !/^\/(?:book|referencework|referenceworkentry|chapter|article)\//i.test(url.pathname || '')) {
      let items = Array.from(
        doc.querySelectorAll(
          'li[data-test="search-result-item"], [data-test="results-list"] > li, ol#results-list > li, li.app-card-open, div.app-card-open, li.c-card-open, div.c-card-open, article.app-card-open, article.c-card-open'
        )
      );
      if (items.length === 0) {
        items = Array.from(doc.querySelectorAll('main ol > li, main ul > li, ol.u-list-reset > li')).filter((li) =>
          Boolean(li.querySelector('h2 a[href], h3 a[href], a[href*="/book/"], a[href*="/chapter/"], a[href*="/referencework"]'))
        );
      }
      // Deduplicate nested matches (e.g. if an <li> contains a <div class="app-card-open">)
      const uniqueItems = items.filter((el) => !items.some((other) => other !== el && other.contains(el)));

      uniqueItems.forEach((item, idx) => {
        const headingLink = item.querySelector(
          '.app-card-open__heading a[href], .c-card-open__heading a[href], [data-test="search-result-title"] a[href], h2 a[href], h3 a[href], a.title[href]'
        );
        const headingEl =
          headingLink ||
          item.querySelector('.app-card-open__heading, .c-card-open__heading, [data-test="search-result-title"], h2, h3');
        if (!headingEl) return;

        const headingTitle = (headingEl.textContent || '').replace(/\s+/g, ' ').trim();
        const primaryHref = (headingLink && headingLink.getAttribute('href')) || '';

        // Check if this card is a Book Chapter / Reference Work Entry that also links to its parent Book
        const parentBookLink = item.querySelector('a[href*="/book/"], a[href*="/referencework/"]');
        const parentBookHref = (parentBookLink && parentBookLink.getAttribute('href')) || '';
        const parentBookTitle =
          parentBookLink && parentBookLink !== headingLink
            ? (parentBookLink.textContent || '').replace(/\s+/g, ' ').trim()
            : '';

        // Extract ISBN-13 (preserving hyphenated raw form if available) from book/chapter DOI link, cover image URL, or card text
        const coverImg = item.querySelector('img[src*="978"], img[src*="979"], img[data-src*="978"], img[data-src*="979"]');
        const imgSrc = (coverImg && (coverImg.getAttribute('src') || coverImg.getAttribute('data-src'))) || '';
        const candidateIsbnSource = `${parentBookHref} ${primaryHref} ${imgSrc} ${item.innerText || ''}`;
        const mIsbn = candidateIsbnSource.match(/(97[89]-[0-9\-]{10,14}|97[89][0-9]{10})/);
        const isbnRaw = mIsbn ? mIsbn[1].replace(/_.*$/, '') : '';
        const isbnDigits = isbnRaw ? isbnRaw.replace(/[^0-9]/g, '') : '';

        // For chapters with a known parent book title, prefer the parent book title for library lookup
        const isChapterOrEntry = /\/(?:chapter|referenceworkentry)\//i.test(primaryHref);
        const title = (isChapterOrEntry && parentBookTitle) ? parentBookTitle : headingTitle;
        if (!title && !isbnDigits) return;

        // Extract authors/editors cleanly
        let author = '';
        const authorContainer = item.querySelector(
          '.app-card-open__authors, .c-card-open__authors, .c-author-list, [data-test="authors"], [data-test="author-text"], .authors'
        );
        if (authorContainer) {
          const authorNodes = Array.from(authorContainer.querySelectorAll('li, a, span[itemprop="name"]'));
          if (authorNodes.length > 0) {
            const names = authorNodes
              .map((n) => cleanExtractedAuthor((n.textContent || '').replace(/,\s*$/, '')))
              .filter((n) => n && !/^and$/i.test(n) && !/^\+\d+$/i.test(n));
            if (names.length > 0) {
              author = Array.from(new Set(names)).join(', ');
            }
          }
          if (!author) {
            author = cleanExtractedAuthor(authorContainer.textContent);
          }
        }

        // Extract publication year (supports 1900–2039 for classic & modern Springer publications)
        const metaEl = item.querySelector(
          '[data-test="published-on"], [data-test="publication-date"], .app-card-open__meta, .c-card-open__meta, .c-meta, .enumeration'
        );
        const metaTxt = (metaEl && metaEl.textContent) || '';
        const ym = metaTxt.match(/\b(19[0-9]{2}|20[0-3][0-9])\b/);
        const pub_year = ym ? parseInt(ym[1], 10) : null;

        const mountEl =
          item.querySelector('.app-card-open__main, .c-card-open__main, .app-card-open__meta, .c-card-open__meta') ||
          item;

        cards.push({
          key: `springer_card_${isbnDigits || idx}_${(headingTitle || title).slice(0, 28)}`,
          siteName: 'Springer',
          isbnRaw,
          isbnDigits,
          title,
          chapterTitle: (isChapterOrEntry && parentBookTitle && headingTitle !== parentBookTitle) ? headingTitle : '',
          author,
          edition: '',
          pub_year,
          cardEl: item,
          mountEl
        });
      });
    }

    // 7. Simon & Schuster Search, eBook Deals, Author & Series Cards
    if (/(^|\.)simonandschuster\.(com|co\.uk|ca|com\.au|co\.in)$/i.test(host) && !/^\/books\/[^/]+/i.test(url.pathname || '')) {
      const bookLinks = Array.from(doc.querySelectorAll('a[href*="/books/"]'));
      const seenCardEls = new Set();

      bookLinks.forEach((linkEl, idx) => {
        const href = linkEl.getAttribute('href') || '';
        const mHref = href.match(/\/books\/([^/?#]+)(?:\/([^/?#]+))?(?:\/([^/?#]+))?\/(97[89][0-9\-]{10,14}|\d{9}[0-9Xx])(?:[/?#]|$)/i);
        if (!mHref) return;

        const cardEl =
          linkEl.closest('.book-item, .book-card, .product-card, .search-result-item, article, li') ||
          linkEl.parentElement;
        if (!cardEl || seenCardEls.has(cardEl)) return;
        seenCardEls.add(cardEl);

        const slugTitle = decodeURIComponent(mHref[1] || '').replace(/-/g, ' ').trim();
        const slugPart2 = decodeURIComponent(mHref[2] || '').replace(/-/g, ' ').trim();
        const isbnObj = normalizeIsbn13(mHref[4]);

        const titleEl = cardEl.querySelector('.book-title, .title, h2, h3, h4');
        const imgEl = cardEl.querySelector('img[alt]');
        const title =
          (titleEl && titleEl.textContent.replace(/\s+/g, ' ').trim()) ||
          (imgEl && (imgEl.getAttribute('alt') || '').replace(/\s+/g, ' ').trim()) ||
          slugTitle;
        if (!title && !isbnObj.digits) return;

        const authorEl = cardEl.querySelector('.book-author, .author, a[href*="/authors/"]');
        const author =
          (authorEl && cleanExtractedAuthor(authorEl.textContent)) ||
          (slugPart2 && !/^97[89]\d{10}$/.test(slugPart2) ? slugPart2 : '');

        cards.push({
          key: `sands_card_${isbnObj.digits || idx}_${title.slice(0, 24)}`,
          siteName: 'Simon & Schuster',
          isbnRaw: isbnObj.raw,
          isbnDigits: isbnObj.digits,
          title,
          author,
          edition: '',
          pub_year: null,
          cardEl,
          mountEl: cardEl
        });
      });
    }

    return cards;
  }

  window.MyLibSiteAdapters = {
    BUILTIN_SITE_ADAPTERS,
    isbn10ToIsbn13,
    normalizeIsbn13,
    findMatchingSiteAdapter,
    extractBookDetailsWithAdapter,
    extractSearchCardsFromPage
  };
})();

