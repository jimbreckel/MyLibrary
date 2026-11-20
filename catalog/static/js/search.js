const initCatalogSearch = () => {
    const BASE_PREFIX = window.location.pathname.startsWith('/mylibrary') ? '/mylibrary' : '';

    // ----------------------------------------------------
    // Auto-suggest Autocomplete Logic
    // ----------------------------------------------------
    const searchInput = document.getElementById('search-input');
    const suggestionsDropdown = document.getElementById('suggestions-dropdown');
    let debounceTimer;

    if (searchInput && suggestionsDropdown) {
        const triggerSuggestLookup = () => {
            clearTimeout(debounceTimer);
            const rawVal = searchInput.value;
            const query = rawVal.trim();

            if (query.length < 2) {
                suggestionsDropdown.style.display = 'none';
                return;
            }

            debounceTimer = setTimeout(() => {
                fetch(`${BASE_PREFIX}/api/suggest/?q=${encodeURIComponent(rawVal)}`)
                    .then(response => response.json())
                    .then(data => {
                        suggestionsDropdown.innerHTML = '';
                        if (!Array.isArray(data) || data.length === 0) {
                            suggestionsDropdown.style.display = 'none';
                            return;
                        }

                        data.forEach(item => {
                            const div = document.createElement('div');
                            div.className = 'suggestion-item';

                            if (item.kind === 'keyword') {
                                div.style.borderLeft = '3px solid #38bdf8';
                                div.style.background = 'rgba(56, 189, 248, 0.07)';
                                div.innerHTML = `
                                    <div>
                                        <div class="s-title" style="color:#38bdf8; font-family:monospace; font-size:0.86rem;">
                                            <i class="fa-solid fa-terminal" style="font-size:0.75rem; margin-right:0.35rem; opacity:0.85;"></i>${escapeHtml(item.keyword || '')}
                                        </div>
                                        <div class="s-author" style="color:#cbd5e1;">${escapeHtml(item.label || '')}</div>
                                    </div>
                                    <span class="s-type" style="background:rgba(56,189,248,0.2); color:#38bdf8; border:1px solid rgba(56,189,248,0.45);">${escapeHtml(item.category || 'KEYWORD')}</span>
                                `;
                                div.addEventListener('click', (e) => {
                                    e.preventDefault();
                                    e.stopPropagation();
                                    searchInput.value = item.full_query || item.keyword || '';
                                    searchInput.focus();
                                    if ((searchInput.value || '').endsWith(':')) {
                                        triggerSuggestLookup();
                                    } else {
                                        suggestionsDropdown.style.display = 'none';
                                    }
                                });
                            } else {
                                // Color mapping for type badges in suggestions
                                let badgeClass = 'type-book';
                                if (item.type === 'MAGAZINE') badgeClass = 'type-magazine';
                                if (item.type === 'KINDLE') badgeClass = 'type-kindle';
                                if (item.type === 'AUDIBLE') badgeClass = 'type-audible';

                                div.innerHTML = `
                                    <div>
                                        <div class="s-title">${escapeHtml(item.title)}</div>
                                        <div class="s-author">by ${escapeHtml(item.authors)}</div>
                                    </div>
                                    <span class="s-type ${badgeClass}">${item.type}</span>
                                `;

                                div.addEventListener('click', () => {
                                    // Preserve any boolean/status/format/cat/tag/year/sort keyword tokens already in the input
                                    const curTokens = (searchInput.value || '').match(/(-?[A-Za-z_+!]+:"[^"]*"|-?[A-Za-z_+!]+:'[^']*'|\S+)/g) || [];
                                    const keptKeywords = curTokens.filter(tok => {
                                        const low = tok.toLowerCase().replace(/^-/, '');
                                        return (
                                            low.startsWith('is:') ||
                                            low.startsWith('has:') ||
                                            low.startsWith('missing:') ||
                                            low.startsWith('format:') ||
                                            low.startsWith('fmt:') ||
                                            low.startsWith('type:') ||
                                            low.startsWith('cat:') ||
                                            low.startsWith('category:') ||
                                            low.startsWith('folder:') ||
                                            low.startsWith('tag:') ||
                                            low.startsWith('year:') ||
                                            low.startsWith('after:') ||
                                            low.startsWith('before:') ||
                                            low.startsWith('sort:') ||
                                            low === '+enrich' ||
                                            low === '!enrich'
                                        );
                                    });
                                    if (keptKeywords.length > 0) {
                                        searchInput.value = `${keptKeywords.join(' ')} "${item.title}"`;
                                    } else {
                                        searchInput.value = item.title;
                                    }
                                    suggestionsDropdown.style.display = 'none';
                                    searchInput.closest('form').submit();
                                });
                            }

                            suggestionsDropdown.appendChild(div);
                        });

                        suggestionsDropdown.style.display = 'block';
                    })
                    .catch(err => console.error('Error loading suggestions:', err));
            }, 220);
        };

        searchInput.addEventListener('input', triggerSuggestLookup);

        window.insertSearchKeyword = function (tokenStr, isPrefix) {
            if (!searchInput) return;
            const cur = (searchInput.value || '').trim();
            if (!isPrefix) {
                // Toggle complete keyword token (like `is:enriched`, `format:epub`, `sort:newest`)
                const escaped = tokenStr.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                const rx = new RegExp(`(?:^|\\s)${escaped}(?=\\s|$)`, 'i');
                if (rx.test(cur)) {
                    searchInput.value = cur.replace(rx, ' ').replace(/\s+/g, ' ').trim();
                } else {
                    searchInput.value = cur ? `${cur} ${tokenStr} ` : `${tokenStr} `;
                }
                searchInput.focus();
                triggerSuggestLookup();
                return;
            }
            // Prefix operator (like `author:`, `title:`, `cat:`, `tag:`, `year:`, `-`)
            searchInput.value = cur ? `${cur} ${tokenStr}` : tokenStr;
            searchInput.focus();
            triggerSuggestLookup();
        };

        window.removeSearchKeywordToken = function (rawToken) {
            if (!searchInput) return;
            const cur = searchInput.value || '';
            const idx = cur.indexOf(rawToken);
            if (idx >= 0) {
                const updated = (cur.slice(0, idx) + ' ' + cur.slice(idx + rawToken.length)).replace(/\s+/g, ' ').trim();
                searchInput.value = updated;
                const form = searchInput.closest('form');
                if (form) form.submit();
            }
        };

        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!searchInput.contains(e.target) && !suggestionsDropdown.contains(e.target)) {
                suggestionsDropdown.style.display = 'none';
            }
        });
    }

    // ----------------------------------------------------
    // Details Modal Logic
    // ----------------------------------------------------
    const modalOverlay = document.getElementById('modal-overlay');
    const modalClose = document.getElementById('modal-close');
    const detailsButtons = document.querySelectorAll('.btn-details');

    window.openDetailsModal = function (btn) {
        if (!btn) return;
        const overlay = modalOverlay || document.getElementById('modal-overlay');
        if (!overlay) return;

        // Parse details from data attributes
        const itemId = btn.getAttribute('data-item-id') || '';
        const isEnriched = btn.getAttribute('data-is-enriched') === 'true';
        const isInteresting = btn.getAttribute('data-is-interesting') === 'true';
        const isBadMetadata = btn.getAttribute('data-is-bad-metadata') === 'true';
        const hasEnrichable = btn.getAttribute('data-has-enrichable') === 'true';
        const title = btn.getAttribute('data-title') || '';
        const authors = btn.getAttribute('data-authors') || '';
        const desc = btn.getAttribute('data-desc') || 'No description available for this item.';
        const type = btn.getAttribute('data-type') || 'BOOK';
        const cover = btn.getAttribute('data-cover') || '';
        const publisher = btn.getAttribute('data-publisher') || '';
        const pubDate = btn.getAttribute('data-pubdate') || '';
        const fileDate = btn.getAttribute('data-filedate') || '';
        const isbn = btn.getAttribute('data-isbn') || '';
        const asin = btn.getAttribute('data-asin') || '';
        const amazonBadge = btn.getAttribute('data-amazon-badge') || '';
        const amazonPlus = btn.getAttribute('data-amazon-plus') === 'true';
        const amazonType = btn.getAttribute('data-amazon-type') || '';
        const amazonPct = parseInt(btn.getAttribute('data-amazon-pct') || '0', 10);
        const amazonDate = btn.getAttribute('data-amazon-date') || '';
        const amazonUrl = btn.getAttribute('data-amazon-url') || '';
        let filesData = [];
        try {
            filesData = JSON.parse(btn.getAttribute('data-files') || '[]');
            if (!Array.isArray(filesData)) filesData = [];
        } catch (e) {
            filesData = [];
        }

        // Populate modal content
        window._activeModalItem = {
            itemId,
            title,
            authors,
            publisher,
            pubDate,
            isbn,
            desc: desc === 'No description available for this item.' ? '' : desc,
            cover,
            isEnriched,
            isInteresting,
            isBadMetadata,
            firstFileId: (filesData[0] && filesData[0].id) ? filesData[0].id : '',
            firstFilePath: (filesData[0] && filesData[0].file_path) ? filesData[0].file_path : ''
        };
        document.getElementById('modal-title').textContent = title;
        document.getElementById('modal-authors').innerHTML = renderAuthorLinksHtml(authors);
        document.getElementById('modal-desc').innerHTML = desc.replace(/\n/g, '<br>');

        // Reset Web Cover Finder drawer and Peek Inside drawer
        const finderDrawer = document.getElementById('modal-cover-finder');
        if (finderDrawer) finderDrawer.style.display = 'none';
        const peekDrawer = document.getElementById('modal-book-peek-drawer');
        if (peekDrawer) {
            peekDrawer.style.display = 'none';
            peekDrawer.dataset.loadedFor = '';
        }
        const cleanAuth = (authors && !['unknown', 'unknown author', '..'].includes(authors.toLowerCase().trim())) ? authors : '';
        let cleanedModalTitle = (title || '').replace(/\.(?:epub|pdf|mobi|azw3?|djvu|cbz|cbr|fb2|txt|rtf|doc|docx|lit)$/i, '');
        if ((cleanedModalTitle.includes('.') && !cleanedModalTitle.includes(' ')) || cleanedModalTitle.includes('_')) {
            cleanedModalTitle = cleanedModalTitle.replace(/[._]+/g, ' ').replace(/\s+/g, ' ').trim();
        }
        const shortSearchTitle = (cleanedModalTitle && cleanedModalTitle.length > 35 && cleanedModalTitle.includes(':') && cleanedModalTitle.split(':')[0].trim().length >= 6)
            ? cleanedModalTitle.split(':')[0].trim()
            : cleanedModalTitle;
        const covInput = document.getElementById('cover-search-input');
        if (covInput) covInput.value = [shortSearchTitle, cleanAuth].filter(Boolean).join(' ').trim();
        const covBtn = document.getElementById('btn-run-cover-search');
        if (covBtn) covBtn.disabled = false;
        if (window._activeCoverSearchAbort) {
            try { window._activeCoverSearchAbort.abort(); } catch (_) {}
            window._activeCoverSearchAbort = null;
        }
        const covRes = document.getElementById('cover-finder-results');
        if (covRes) covRes.innerHTML = '';
        const covSt = document.getElementById('cover-finder-status');
        if (covSt) covSt.textContent = '';

        // Populate Enrichment Error / Diagnostic Banner in View Details when applicable
        const cardEl = itemId ? document.getElementById(`card-${itemId}`) : btn.closest('.media-card');
        const cardEnrBtn = cardEl ? cardEl.querySelector('.btn-enrich-card') : null;
        const fileDiag = filesData.find(f => f && (f.enrich_error || f.enrich_status));
        const errStatus = btn.getAttribute('data-enrich-status') || (fileDiag ? fileDiag.enrich_status : '') || (cardEnrBtn && cardEnrBtn._reviewData ? cardEnrBtn._reviewData.status : '') || '';
        const errMsg = btn.getAttribute('data-enrich-error') || (fileDiag ? fileDiag.enrich_error : '') || (cardEnrBtn && cardEnrBtn._reviewData ? cardEnrBtn._reviewData.message : '') || '';
        const errUpper = (errStatus || '').toUpperCase();
        const isNoMatchItem = !isEnriched && (errUpper === 'NOT_FOUND' || errUpper === 'NO_ISBN');
        const hasNoDescItem = !window._activeModalItem.desc || !window._activeModalItem.desc.trim();
        const modalAiDescBtn = document.getElementById('modal-btn-ai-desc');
        const modalAiDescSt = document.getElementById('modal-ai-desc-status');
        if (modalAiDescSt) {
            modalAiDescSt.style.display = 'none';
            modalAiDescSt.textContent = '';
        }
        if (modalAiDescBtn) {
            modalAiDescBtn.disabled = false;
            modalAiDescBtn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> AI Generate Description from Content`;
            modalAiDescBtn.style.display = 'inline-flex';
        }
        const modalAiMetaBtn = document.getElementById('modal-btn-ai-title-author');
        const modalAiMetaSt = document.getElementById('modal-ai-title-author-status');
        if (modalAiMetaSt) {
            modalAiMetaSt.style.display = 'none';
            modalAiMetaSt.textContent = '';
        }
        if (modalAiMetaBtn) {
            modalAiMetaBtn.disabled = false;
            modalAiMetaBtn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> AI Generate Title &amp; Author`;
            modalAiMetaBtn.style.display = 'inline-flex';
        }
        const quickEditDrawer = document.getElementById('modal-quick-edit-drawer');
        if (quickEditDrawer) {
            quickEditDrawer.style.display = 'none';
        }
        const qInTitle = document.getElementById('modal-quick-input-title');
        const qInAuth = document.getElementById('modal-quick-input-author');
        const qInPub = document.getElementById('modal-quick-input-publisher');
        if (qInTitle) qInTitle.value = title || '';
        if (qInAuth) qInAuth.value = (authors && authors !== 'Unknown Author') ? authors : '';
        if (qInPub) qInPub.value = publisher || '';
        const errBanner = document.getElementById('modal-enrich-error-banner');
        if (errBanner) {
            if (!isEnriched && (errMsg || errStatus)) {
                const isRev = errStatus === 'NEEDS_REVIEW';
                errBanner.style.display = 'block';
                errBanner.style.background = isRev ? 'rgba(251,191,36,0.14)' : 'rgba(239,68,68,0.14)';
                errBanner.style.borderColor = isRev ? 'rgba(251,191,36,0.48)' : 'rgba(239,68,68,0.48)';
                errBanner.style.color = isRev ? '#fde68a' : '#fecaca';
                const aiDescBannerBtnHtml = (isNoMatchItem && hasNoDescItem)
                    ? `<button type="button" id="modal-err-ai-desc-btn" style="background:rgba(168,85,247,0.26); border:1px solid rgba(168,85,247,0.62); color:#f3e8ff; border-radius:6px; padding:0.32rem 0.65rem; font-size:0.75rem; font-weight:700; cursor:pointer; white-space:nowrap;" title="No Match &amp; No Description — Generate a synopsis from this book's actual content with Gemini AI"><i class="fa-solid fa-wand-magic-sparkles"></i> AI Describe from Content</button>`
                    : '';
                errBanner.innerHTML = `
                    <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:0.75rem; flex-wrap:wrap;">
                        <div style="flex:1; min-width:220px;">
                            <div style="font-weight:700; font-size:0.78rem; text-transform:uppercase; margin-bottom:0.25rem; display:flex; align-items:center; gap:0.4rem;">
                                <i class="fa-solid ${isRev ? 'fa-clipboard-question' : 'fa-triangle-exclamation'}"></i>
                                <span>Enrichment ${escapeHtml(errStatus || 'FAILED')} Details</span>
                            </div>
                            <div style="font-family:monospace; font-size:0.78rem; color:#f8fafc; word-break:break-word; line-height:1.4;">${escapeHtml(errMsg || ('Enrichment status: ' + errStatus))}</div>
                        </div>
                        <div style="display:flex; gap:0.45rem; flex-wrap:wrap; align-items:center;">
                            ${aiDescBannerBtnHtml}
                            <button type="button" id="modal-err-keep-btn" style="background:rgba(16,185,129,0.22); border:1px solid rgba(16,185,129,0.55); color:#6ee7b7; border-radius:6px; padding:0.32rem 0.65rem; font-size:0.75rem; font-weight:700; cursor:pointer; white-space:nowrap;" title="Current metadata already looks right — mark as Enriched/Approved as-is">
                                <i class="fa-solid fa-check"></i> Keep As-Is
                            </button>
                            <button type="button" id="modal-err-open-review-btn" style="background:rgba(251,191,36,0.22); border:1px solid rgba(251,191,36,0.55); color:#fde68a; border-radius:6px; padding:0.32rem 0.65rem; font-size:0.75rem; font-weight:700; cursor:pointer; white-space:nowrap;">
                                <i class="fa-solid fa-pen-to-square"></i> Review / Manual Edit
                            </button>
                            <button type="button" id="modal-err-force-retry-btn" style="background:rgba(168,85,247,0.24); border:1px solid rgba(168,85,247,0.55); color:#e9d5ff; border-radius:6px; padding:0.32rem 0.65rem; font-size:0.75rem; font-weight:700; cursor:pointer; white-space:nowrap;">
                                <i class="fa-solid fa-rotate-right"></i> Force Retry
                            </button>
                        </div>
                    </div>
                `;
                const errAiDescBtn = document.getElementById('modal-err-ai-desc-btn');
                if (errAiDescBtn) {
                    errAiDescBtn.onclick = () => {
                        window.generateAiDescriptionForItem({ itemId, mode: 'modal', btn: errAiDescBtn });
                    };
                }
                const errKeepBtn = document.getElementById('modal-err-keep-btn');
                if (errKeepBtn) {
                    errKeepBtn.onclick = () => {
                        const firstFile = filesData[0] || {};
                        window.keepCurrentMetadataAsIs({ itemId, filePath: firstFile.file_path || '', btn: errKeepBtn });
                    };
                }
                const revBtn = document.getElementById('modal-err-open-review-btn');
                if (revBtn) {
                    revBtn.onclick = () => {
                        const firstFile = filesData[0] || {};
                        const revPayload = (cardEnrBtn && cardEnrBtn._reviewData) || btn._enrichResultData || {
                            status: errStatus || 'FAILED',
                            message: errMsg,
                            item_id: itemId,
                            file_id: firstFile.id || '',
                            file_path: firstFile.file_path || '',
                            file_name: firstFile.file_name || title,
                            orig_title: title || '',
                            orig_authors: authors || '',
                            orig_publisher: publisher || '',
                            orig_pub_date: pubDate || '',
                            orig_isbn: isbn || '',
                            orig_description: desc === 'No description available for this item.' ? '' : desc,
                        };
                        openEnrichReviewModal(revPayload, cardEnrBtn, true, itemId, firstFile.id || '');
                    };
                }
                const retryBtn = document.getElementById('modal-err-force-retry-btn');
                if (retryBtn) {
                    retryBtn.onclick = () => {
                        const topEnrBtn = document.getElementById('modal-top-enrich-btn') || retryBtn;
                        triggerOnDemandEnrich({ itemId, force: false, reEnrich: true, btn: topEnrBtn, updateModal: true });
                    };
                }
            } else {
                errBanner.style.display = 'none';
                errBanner.innerHTML = '';
            }
        }

        // Cover Image
        const coverImg = document.getElementById('modal-cover');
        const coverFallback = document.getElementById('modal-cover-fallback');
        if (cover) {
            coverImg.src = cover;
            coverImg.style.display = 'block';
            if (coverFallback) coverFallback.style.display = 'none';
        } else {
            coverImg.style.display = 'none';
            if (coverFallback) coverFallback.style.display = 'block';
        }

        // Badges
        const badgeContainer = document.getElementById('modal-badges-container');
        badgeContainer.innerHTML = '';

        // Add Item Type Badge
        const typeBadge = document.createElement('span');
        typeBadge.className = `card-type-badge ${getTypeBadgeClass(type)}`;
        typeBadge.textContent = type;
        typeBadge.style.position = 'static';
        badgeContainer.appendChild(typeBadge);

        // Add Publisher / PubDate / Identifiers as general badges
        if (publisher) {
            badgeContainer.appendChild(createBadge(`Publisher: ${publisher}`));
        }
        if (pubDate) {
            badgeContainer.appendChild(createBadge(`Published: ${pubDate}`));
        }
        if (fileDate) {
            badgeContainer.appendChild(createBadge(`File Date: ${fileDate}`));
        }
        if (isbn) {
            badgeContainer.appendChild(createBadge(`ISBN: ${isbn}`));
        }
        if (asin) {
            badgeContainer.appendChild(createBadge(`ASIN: ${asin}`));
        }

        if (itemId) {
            const interestingModalBtn = document.createElement('button');
            interestingModalBtn.type = 'button';
            interestingModalBtn.id = 'modal-top-interesting-btn';
            interestingModalBtn.style.cssText = isInteresting
                ? 'display:inline-flex; align-items:center; gap:0.3rem; padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(250,204,21,0.22); border:1px solid rgba(250,204,21,0.65); color:#fde047;'
                : 'display:inline-flex; align-items:center; gap:0.3rem; padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(148,163,184,0.14); border:1px solid rgba(148,163,184,0.38); color:#cbd5e1;';
            interestingModalBtn.innerHTML = `<i class="fa-${isInteresting ? 'solid' : 'regular'} fa-star"></i> Interesting`;
            interestingModalBtn.title = isInteresting
                ? 'Marked as Interesting — click to unmark (also resets automatically when downloaded)'
                : 'Mark as Interesting so it shows up in the Interesting Books category until downloaded';
            interestingModalBtn.addEventListener('click', () => {
                window.toggleBookInteresting(itemId, interestingModalBtn);
            });
            badgeContainer.appendChild(interestingModalBtn);

            const badMetaModalBtn = document.createElement('button');
            badMetaModalBtn.type = 'button';
            badMetaModalBtn.id = 'modal-top-bad-meta-btn';
            badMetaModalBtn.style.cssText = isBadMetadata
                ? 'display:inline-flex; align-items:center; gap:0.3rem; padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(244,63,94,0.24); border:1px solid rgba(244,63,94,0.65); color:#fda4af;'
                : 'display:inline-flex; align-items:center; gap:0.3rem; padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(148,163,184,0.14); border:1px solid rgba(148,163,184,0.38); color:#cbd5e1;';
            badMetaModalBtn.innerHTML = `<i class="fa-${isBadMetadata ? 'solid' : 'regular'} fa-flag"></i> Bad Metadata`;
            badMetaModalBtn.title = isBadMetadata
                ? 'Flagged as Bad Metadata — click to unflag (also clears automatically when enriched or approved)'
                : 'Flag this book as having Bad Metadata so it appears in the Bad Metadata shelf';
            badMetaModalBtn.addEventListener('click', () => {
                window.toggleBookBadMetadata(itemId, badMetaModalBtn);
            });
            badgeContainer.appendChild(badMetaModalBtn);
        }

        if (filesData.length > 0) {
            const peekModalBtn = document.createElement('button');
            peekModalBtn.type = 'button';
            peekModalBtn.id = 'modal-top-peek-btn';
            peekModalBtn.style.cssText = 'display:inline-flex; align-items:center; gap:0.3rem; padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(56,189,248,0.18); border:1px solid rgba(56,189,248,0.5); color:#38bdf8;';
            peekModalBtn.innerHTML = '<i class="fa-solid fa-book-open-reader"></i> Peek Inside';
            peekModalBtn.title = 'Peek inside this book file (pages, cover images, front-matter text, and detected ISBNs)';
            peekModalBtn.addEventListener('click', () => {
                const firstFile = filesData[0] || {};
                window.toggleModalBookPeek({
                    itemId,
                    fileId: firstFile.id || '',
                    filePath: firstFile.file_path || ''
                });
            });
            badgeContainer.appendChild(peekModalBtn);
        }

        if (hasEnrichable && itemId) {
            const enrichModalBtn = document.createElement('button');
            enrichModalBtn.type = 'button';
            enrichModalBtn.id = 'modal-top-enrich-btn';
            enrichModalBtn.style.cssText = isEnriched
                ? 'padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(74,222,128,0.16); border:1px solid rgba(74,222,128,0.45); color:#4ade80;'
                : 'padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(168,85,247,0.22); border:1px solid rgba(168,85,247,0.55); color:#c084fc;';
            enrichModalBtn.innerHTML = isEnriched
                ? '<i class="fa-solid fa-check"></i> Enriched — Re-Enrich (epubmeta)'
                : '<i class="fa-solid fa-wand-magic-sparkles"></i> Enrich Metadata Now (epubmeta)';
            enrichModalBtn.addEventListener('click', () => {
                const currentlyEnr = window._activeModalItem ? Boolean(window._activeModalItem.isEnriched) : isEnriched;
                triggerOnDemandEnrich({ itemId, force: false, reEnrich: currentlyEnr, btn: enrichModalBtn, updateModal: true });
            });
            badgeContainer.appendChild(enrichModalBtn);

            const keepModalBtn = document.createElement('button');
            keepModalBtn.type = 'button';
            keepModalBtn.id = 'modal-top-keep-btn';
            keepModalBtn.style.cssText = (!isEnriched ? 'display:inline-flex;' : 'display:none;') +
                ' align-items:center; gap:0.3rem; padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(16,185,129,0.2); border:1px solid rgba(16,185,129,0.55); color:#34d399;';
            keepModalBtn.innerHTML = '<i class="fa-solid fa-check"></i> Keep As-Is';
            keepModalBtn.title = 'This book already looks right — mark it as Enriched/Approved without overwriting its metadata';
            keepModalBtn.addEventListener('click', () => {
                const firstFile = filesData[0] || {};
                window.keepCurrentMetadataAsIs({ itemId, filePath: firstFile.file_path || '', btn: keepModalBtn });
            });
            badgeContainer.appendChild(keepModalBtn);

            const showRevert = Boolean(
                isEnriched || errMsg || errStatus ||
                filesData.some(f => f.is_enriched || f.enrich_status || f.enrich_error || (Array.isArray(f.tags) && f.tags.length > 0))
            );
            const revertModalBtn = document.createElement('button');
            revertModalBtn.type = 'button';
            revertModalBtn.id = 'modal-top-revert-btn';
            revertModalBtn.style.cssText = (showRevert ? 'display:inline-flex;' : 'display:none;') +
                ' align-items:center; gap:0.3rem; padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(244,63,94,0.18); border:1px solid rgba(244,63,94,0.5); color:#fb7185;';
            revertModalBtn.innerHTML = '<i class="fa-solid fa-rotate-left"></i> Revert to Original';
            revertModalBtn.title = 'Revert this book back to its original pre-enrichment metadata and clear enriched tags/synopsis';
            revertModalBtn.addEventListener('click', () => {
                window.revertItemEnrichment({ itemId, btn: revertModalBtn, updateModal: true });
            });
            badgeContainer.appendChild(revertModalBtn);
        }

        // Collect and render enriched Tags / Subjects across all files on this item
        const allTagsSet = new Set();
        filesData.forEach(f => {
            if (Array.isArray(f.tags)) {
                f.tags.forEach(t => {
                    if (t && typeof t === 'string') allTagsSet.add(t.trim());
                });
            }
        });
        const allTagsArr = Array.from(allTagsSet);
        const renderReadOnlyTagsHtml = (tagsList) => {
            const clean = Array.isArray(tagsList) ? tagsList.filter(Boolean) : [];
            if (!clean.length) {
                return `<span style="font-size:0.78rem; color:#64748b; font-style:italic;">No tags recorded for this item.</span>`;
            }
            return clean.map(t =>
                `<span style="background:rgba(56,189,248,0.12); border:1px solid rgba(56,189,248,0.32); color:#cbd5e1; border-radius:999px; padding:0.18rem 0.6rem; font-size:0.74rem; font-weight:600; display:inline-flex; align-items:center; gap:0.3rem; user-select:text;"><i class="fa-solid fa-tag" style="font-size:0.65rem; color:#38bdf8;"></i> ${escapeHtml(t)}</span>`
            ).join('');
        };
        window._renderReadOnlyTagsHtml = renderReadOnlyTagsHtml;
        const bottomTagsEl = document.getElementById('modal-bottom-tags-list');
        if (bottomTagsEl) {
            bottomTagsEl.innerHTML = renderReadOnlyTagsHtml(allTagsArr);
        }

        // Add prominent Amazon / Kindle+ / Audible Ownership & Bookmark banner when opened
        if (amazonBadge) {
            const amzBanner = document.createElement('div');
            const isAudible = amazonType === 'AUDIBLE';
            const borderColor = isAudible ? 'rgba(192,132,252,0.55)' : (amazonPlus ? 'rgba(45,212,191,0.55)' : 'rgba(251,146,60,0.6)');
            const bgColor = isAudible ? 'rgba(192,132,252,0.14)' : (amazonPlus ? 'rgba(45,212,191,0.14)' : 'rgba(251,146,60,0.15)');
            const textColor = isAudible ? '#c084fc' : (amazonPlus ? '#2dd4bf' : '#fb923c');
            const iconHtml = isAudible ? '<i class="fa-solid fa-headphones"></i>' : (amazonPlus ? '<i class="fa-solid fa-bolt"></i>' : '<i class="fa-brands fa-amazon"></i>');
            const appNote = isAudible ? 'Listen in Audible App' : 'Prefer Reading in Amazon Kindle App';

            let bmText = '';
            if (amazonPct > 0 || amazonDate) {
                const parts = [];
                if (amazonPct > 0) parts.push(`${amazonPct}% Read`);
                if (amazonDate) parts.push(`Bookmarked: ${escapeHtml(amazonDate)}`);
                bmText = `<div style="margin-top:0.25rem; font-size:0.78rem; color:#fde68a;"><i class="fa-solid fa-bookmark"></i> Amazon Bookmark: ${parts.join(' · ')}</div>`;
            }

            let linkHtml = '';
            if (amazonUrl) {
                linkHtml = `<a href="${escapeHtml(amazonUrl)}" target="_blank" rel="noopener" style="margin-left:auto; background:${borderColor}; color:#0f172a; font-weight:700; font-size:0.74rem; padding:0.25rem 0.6rem; border-radius:6px; text-decoration:none;">Open on Amazon &rarr;</a>`;
            }

            amzBanner.style.cssText = `width:100%; margin-top:0.45rem; padding:0.55rem 0.75rem; border-radius:8px; background:${bgColor}; border:1px solid ${borderColor}; color:${textColor}; font-size:0.84rem; font-weight:600; display:flex; flex-direction:column;`;
            amzBanner.innerHTML = `
                <div style="display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">
                    <span>${iconHtml} ${escapeHtml(amazonBadge)} &mdash; <span style="color:#f8fafc; font-weight:500;">${appNote}</span></span>
                    ${linkHtml}
                </div>
                ${bmText}
            `;
            badgeContainer.appendChild(amzBanner);
        }

        // Files
        const filesList = document.getElementById('modal-files-list');
        filesList.innerHTML = '';

        if (filesData.length === 0) {
            filesList.innerHTML = '<li class="text-secondary" style="font-size:0.9rem;">No physical/digital files registered for this catalog item.</li>';
        } else {
            if (filesData.length > 1) {
                const splitBar = document.createElement('li');
                splitBar.style.cssText = 'list-style:none; margin-bottom:0.55rem; padding:0.45rem 0.65rem; border-radius:8px; background:rgba(245,158,11,0.12); border:1px solid rgba(245,158,11,0.38); display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:0.5rem; font-size:0.78rem; color:#fde68a;';
                splitBar.innerHTML = `
                    <span><i class="fa-solid fa-code-branch" style="color:#fbbf24; margin-right:0.35rem;"></i><strong>${filesData.length} files attached</strong> — If these are different books grouped together, split them into separate catalog entries:</span>
                    <button type="button" class="btn-split-all-files" data-item-id="${itemId}" style="padding:0.25rem 0.65rem; font-size:0.75rem; font-weight:700; border-radius:6px; background:rgba(245,158,11,0.22); border:1px solid rgba(245,158,11,0.55); color:#fde047; cursor:pointer; display:inline-flex; align-items:center; gap:0.3rem;">
                        <i class="fa-solid fa-arrows-split-up-and-left"></i> Split into Separate Books
                    </button>
                `;
                filesList.appendChild(splitBar);
            }
            filesData.forEach(file => {
                const li = document.createElement('li');
                li.className = 'modal-file-item';

                const sizeMB = ((file.file_size || 0) / (1024 * 1024)).toFixed(2);

                let badges = '';
                const fileEnriched = Boolean(file.is_enriched || isEnriched);
                if (fileEnriched) {
                    badges += ` <span class="modal-badge file-enriched-badge" style="background:rgba(74,222,128,0.16); border-color:rgba(74,222,128,0.45); color:#4ade80; padding:0.15rem 0.45rem; font-size:0.7rem; margin-left:0.5rem;"><i class="fa-solid fa-check" style="margin-right:0.2rem;"></i>Enriched</span>`;
                }
                if (file.has_drm) {
                    if (file.drm_lock_state === 'unlocked') {
                        badges += ` <span class="modal-badge" style="background:rgba(56,189,248,0.14); border-color:rgba(56,189,248,0.35); color:#38bdf8; padding:0.15rem 0.45rem; font-size:0.7rem; margin-left:0.5rem;" title="Has publisher permission restrictions or font obfuscation, but opens and reads normally"><i class="fa-solid fa-lock-open" style="margin-right:0.2rem;"></i>Permissions Only (Openable)</span>`;
                    } else {
                        badges += ` <span class="modal-badge" style="background:rgba(251,191,36,0.15); border-color:rgba(251,191,36,0.3); color:#fbbf24; padding:0.15rem 0.45rem; font-size:0.7rem; margin-left:0.5rem;" title="Locked by password or DRM — cannot be opened"><i class="fa-solid fa-lock" style="margin-right:0.2rem;"></i>DRM / Password Locked</span>`;
                    }
                }
                if (file.is_invalid) {
                    badges += ` <span class="modal-badge" style="background:rgba(239,68,68,0.15); border-color:rgba(239,68,68,0.3); color:#fca5a5; padding:0.15rem 0.4rem; font-size:0.7rem; margin-left:0.5rem;"><i class="fa-solid fa-triangle-exclamation" style="margin-right:0.2rem;"></i>Corrupted</span>`;
                }

                let libraryInfo = '';
                let calibreButton = '';
                if (file.library_name) {
                    libraryInfo = ` | Library: <strong>${escapeHtml(file.library_name)}</strong>`;
                } else {
                    calibreButton = `
                        <button class="btn-add-calibre" data-file-id="${file.id}" style="padding:0.25rem 0.6rem; font-size:0.75rem; border-radius:4px; background:rgba(59,130,246,0.15); border:1px solid rgba(59,130,246,0.3); color:#60a5fa; cursor:pointer; display:inline-flex; align-items:center; gap:0.25rem; transition: all 0.2s;">
                            <i class="fa-solid fa-folder-plus"></i> Add to Calibre
                        </button>
                    `;
                }
                const fmtUpper = (file.file_format || '').toUpperCase().replace(/^\./, '');
                const canEnrichFile = ['EPUB', 'PDF', 'MOBI', 'AZW', 'AZW3', 'PRC', 'PDB', 'FB2', 'CBZ', 'CBR', 'DJVU', 'TXT', 'RTF', 'DOC', 'DOCX', 'LIT'].includes(fmtUpper);
                const enrichFileBtn = canEnrichFile ? (
                    fileEnriched ? `
                        <button type="button" class="btn-enrich-file" data-file-id="${file.id || ''}" data-item-id="${itemId}" data-force="true" style="padding:0.25rem 0.6rem; font-size:0.75rem; border-radius:4px; background:rgba(74,222,128,0.16); border:1px solid rgba(74,222,128,0.45); color:#4ade80; cursor:pointer; display:inline-flex; align-items:center; gap:0.25rem;" title="Already enriched by epubmeta — click to re-enrich">
                            <i class="fa-solid fa-check"></i> Enriched (Re-run)
                        </button>
                    ` : `
                        <button type="button" class="btn-enrich-file" data-file-id="${file.id || ''}" data-item-id="${itemId}" style="padding:0.25rem 0.6rem; font-size:0.75rem; border-radius:4px; background:rgba(168,85,247,0.16); border:1px solid rgba(168,85,247,0.45); color:#c084fc; cursor:pointer; display:inline-flex; align-items:center; gap:0.25rem;">
                            <i class="fa-solid fa-wand-magic-sparkles"></i> Enrich File
                        </button>
                        <button type="button" class="btn-keep-file" data-file-path="${escapeHtml(file.file_path || '')}" data-item-id="${itemId}" style="padding:0.25rem 0.6rem; font-size:0.75rem; border-radius:4px; background:rgba(16,185,129,0.16); border:1px solid rgba(16,185,129,0.48); color:#34d399; cursor:pointer; display:inline-flex; align-items:center; gap:0.25rem;" title="Keep existing metadata as-is and mark this book as Enriched">
                            <i class="fa-solid fa-check"></i> Keep As-Is
                        </button>
                    `
                ) : '';
                const fileTagsList = Array.isArray(file.tags) ? file.tags.filter(Boolean) : [];
                const showFileRevert = Boolean(fileEnriched || file.enrich_error || file.enrich_status || fileTagsList.length > 0);
                const revertFileBtn = canEnrichFile ? `
                    <button type="button" class="btn-revert-file" data-file-id="${file.id || ''}" data-item-id="${itemId}" style="padding:0.25rem 0.6rem; font-size:0.75rem; border-radius:4px; background:rgba(244,63,94,0.16); border:1px solid rgba(244,63,94,0.45); color:#fb7185; cursor:pointer; display:${showFileRevert ? 'inline-flex' : 'none'}; align-items:center; gap:0.25rem;" title="Revert this file and item back to original pre-enrichment metadata">
                        <i class="fa-solid fa-rotate-left"></i> Revert
                    </button>
                ` : '';
                const downloadHref = file.id ? `${BASE_PREFIX}/opds/download/${file.id}/` : '#';
                const downloadFileLink = file.id ? `
                    <a href="${downloadHref}" class="btn-download-file" data-item-id="${itemId}" style="padding:0.25rem 0.6rem; font-size:0.75rem; border-radius:4px; background:rgba(16,185,129,0.15); border:1px solid rgba(16,185,129,0.35); color:#34d399; text-decoration:none; display:inline-flex; align-items:center; gap:0.25rem;" title="Download book (automatically enriches on the fly if not already enriched and resets Interesting flag)">
                        <i class="fa-solid fa-download"></i> Download
                    </a>
                ` : '';
                const peekFileBtn = (file.file_path || file.id) ? `
                    <button type="button" class="btn-peek-file" data-file-id="${file.id || ''}" data-file-path="${escapeHtml(file.file_path || '')}" data-item-id="${itemId}" style="padding:0.25rem 0.6rem; font-size:0.75rem; border-radius:4px; background:rgba(56,189,248,0.16); border:1px solid rgba(56,189,248,0.45); color:#38bdf8; cursor:pointer; display:inline-flex; align-items:center; gap:0.25rem;" title="Peek inside this specific book file (pages, cover images, front-matter text & ISBNs)">
                        <i class="fa-solid fa-book-open-reader"></i> Peek Inside
                    </button>
                ` : '';
                const fileTagsHtml = fileTagsList.length > 0 ? `
                    <div class="file-tags-row" style="display:flex; flex-wrap:wrap; align-items:center; gap:0.3rem; margin-top:0.35rem; font-size:0.73rem; color:#94a3b8;">
                        <span><i class="fa-solid fa-tags" style="color:#38bdf8;"></i> File Tags:</span>
                        ${fileTagsList.map(t => `<span style="background:rgba(56,189,248,0.12); border:1px solid rgba(56,189,248,0.32); color:#cbd5e1; border-radius:999px; padding:0.08rem 0.45rem; font-size:0.69rem; user-select:text;">${escapeHtml(t)}</span>`).join('')}
                    </div>
                ` : `<div class="file-tags-row" style="display:none;"></div>`;

                const fileErrHtml = (file.enrich_error || file.enrich_status) ? `
                    <div class="file-err-box" style="margin-top:0.35rem; padding:0.35rem 0.55rem; border-radius:6px; background:rgba(239,68,68,0.13); border:1px solid rgba(239,68,68,0.38); color:#fca5a5; font-size:0.74rem; font-family:monospace; word-break:break-word;">
                        <strong style="color:#f87171;">[${escapeHtml(file.enrich_status || 'ERROR')}]</strong> ${escapeHtml(file.enrich_error || 'Enrichment issue recorded')}
                    </div>
                ` : '';

                const splitSingleBtn = (filesData.length > 1 && file.id) ? `
                    <button type="button" class="btn-split-single-file" data-file-id="${file.id}" data-item-id="${itemId}" style="padding:0.25rem 0.6rem; font-size:0.75rem; border-radius:4px; background:rgba(245,158,11,0.16); border:1px solid rgba(245,158,11,0.45); color:#fde047; cursor:pointer; display:inline-flex; align-items:center; gap:0.25rem;" title="Split this file out into its own separate book entry">
                        <i class="fa-solid fa-arrows-split-up-and-left"></i> Split Out
                    </button>
                ` : '';

                li.innerHTML = `
                    <div class="modal-file-header">
                        <span class="modal-file-name">📂 ${escapeHtml(file.file_name)}${badges}</span>
                        <span class="file-format-tag">${escapeHtml(file.file_format)}</span>
                    </div>
                    <div class="text-secondary" style="font-size:0.8rem; margin-bottom:0.3rem;">
                        <span>Size: <strong>${sizeMB} MB</strong></span>${libraryInfo}${file.file_date ? ` | <span>File Date: <strong>${escapeHtml(file.file_date)}</strong></span>` : ''} | 
                        <span style="font-family:monospace; font-size:0.75rem;">SHA256: ${(file.file_hash || '').substring(0, 16)}...</span>
                    </div>
                    <div class="modal-file-path" title="Double click to copy path" onclick="navigator.clipboard.writeText('${escapeJsString(file.file_path)}')">
                        ${escapeHtml(file.file_path)}
                    </div>
                    ${fileErrHtml}
                    ${fileTagsHtml}
                    <div style="display:flex; flex-wrap:wrap; gap:0.45rem; margin-top:0.45rem;">
                        ${downloadFileLink}
                        ${peekFileBtn}
                        ${enrichFileBtn}
                        ${revertFileBtn}
                        ${splitSingleBtn}
                        ${calibreButton}
                    </div>
                `;
                filesList.appendChild(li);
            });
        }

        // Open modal with glass transition
        overlay.style.display = 'flex';
        overlay.offsetHeight;
        overlay.classList.add('active');
    };

    if (modalOverlay && modalClose) {
        detailsButtons.forEach(btn => {
            if (!btn.hasAttribute('onclick')) {
                btn.addEventListener('click', () => window.openDetailsModal(btn));
            }
        });

        // Event delegation for Add to Calibre buttons
        const filesList = document.getElementById('modal-files-list');
        if (filesList) {
            filesList.addEventListener('click', (e) => {
                const btn = e.target.closest('.btn-add-calibre');
                if (btn) {
                    const fileId = btn.getAttribute('data-file-id');

                    // Show loading state on button
                    btn.disabled = true;
                    btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Adding...`;

                    fetch(`${BASE_PREFIX}/api/calibre/add/`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': getCookie('csrftoken')
                        },
                        body: JSON.stringify({ file_location_id: fileId })
                    })
                        .then(response => response.json())
                        .then(data => {
                            if (data.status === 'success') {
                                btn.innerHTML = `<i class="fa-solid fa-check"></i> Added!`;
                                btn.style.background = 'rgba(16,185,129,0.15)';
                                btn.style.borderColor = 'rgba(16,185,129,0.3)';
                                btn.style.color = '#34d399';
                                btn.title = data.message || 'Added to Calibre';
                            } else {
                                btn.disabled = false;
                                btn.innerHTML = `<i class="fa-solid fa-circle-exclamation"></i> Error`;
                                alert(data.error || 'Failed to add to Calibre');
                            }
                        })
                        .catch(err => {
                            btn.disabled = false;
                            btn.innerHTML = `<i class="fa-solid fa-circle-exclamation"></i> Failed`;
                            console.error('Error adding to Calibre:', err);
                            alert('Network/server error while adding to Calibre.');
                        });
                }

                const enrichBtn = e.target.closest('.btn-enrich-file');
                if (enrichBtn) {
                    const fileId = enrichBtn.getAttribute('data-file-id');
                    const itemId = enrichBtn.getAttribute('data-item-id');
                    const isAlreadyEnr = enrichBtn.getAttribute('data-force') === 'true';
                    triggerOnDemandEnrich({ fileId, itemId, force: false, reEnrich: isAlreadyEnr, btn: enrichBtn, updateModal: true });
                }

                const revertBtn = e.target.closest('.btn-revert-file');
                if (revertBtn) {
                    const fileId = revertBtn.getAttribute('data-file-id');
                    const itemId = revertBtn.getAttribute('data-item-id');
                    window.revertItemEnrichment({ itemId, fileId, btn: revertBtn, updateModal: true });
                }

                const keepBtn = e.target.closest('.btn-keep-file');
                if (keepBtn) {
                    const filePath = keepBtn.getAttribute('data-file-path');
                    const itemId = keepBtn.getAttribute('data-item-id');
                    window.keepCurrentMetadataAsIs({ itemId, filePath, btn: keepBtn });
                }

                const peekBtn = e.target.closest('.btn-peek-file');
                if (peekBtn) {
                    const fileId = peekBtn.getAttribute('data-file-id') || '';
                    const filePath = peekBtn.getAttribute('data-file-path') || '';
                    const itemId = peekBtn.getAttribute('data-item-id') || '';
                    window.toggleModalBookPeek({ itemId, fileId, filePath, forceShow: true });
                }

                const splitAllBtn = e.target.closest('.btn-split-all-files');
                if (splitAllBtn) {
                    const itemId = splitAllBtn.getAttribute('data-item-id') || '';
                    const origHtml = splitAllBtn.innerHTML;
                    splitAllBtn.disabled = true;
                    splitAllBtn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Splitting Books...`;
                    fetch(`${BASE_PREFIX}/api/split-item/`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': getCookie('csrftoken')
                        },
                        body: JSON.stringify({ item_id: itemId, force_split_all: true })
                    })
                        .then(r => r.json())
                        .then(res => {
                            if (res.ok) {
                                splitAllBtn.innerHTML = `<i class="fa-solid fa-check"></i> ${escapeHtml(res.message || 'Split Complete!')}`;
                                setTimeout(() => window.location.reload(), 650);
                            } else {
                                splitAllBtn.disabled = false;
                                splitAllBtn.innerHTML = origHtml;
                                alert(res.error || res.message || 'Could not split files.');
                            }
                        })
                        .catch(err => {
                            splitAllBtn.disabled = false;
                            splitAllBtn.innerHTML = origHtml;
                            alert('Network error splitting files: ' + err.message);
                        });
                }

                const splitSingleBtn = e.target.closest('.btn-split-single-file');
                if (splitSingleBtn) {
                    const fileId = splitSingleBtn.getAttribute('data-file-id') || '';
                    const itemId = splitSingleBtn.getAttribute('data-item-id') || '';
                    const origHtml = splitSingleBtn.innerHTML;
                    splitSingleBtn.disabled = true;
                    splitSingleBtn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Splitting...`;
                    fetch(`${BASE_PREFIX}/api/split-item/`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': getCookie('csrftoken')
                        },
                        body: JSON.stringify({ item_id: itemId, file_id: fileId })
                    })
                        .then(r => r.json())
                        .then(res => {
                            if (res.ok) {
                                const li = splitSingleBtn.closest('.modal-file-item');
                                if (li) li.remove();
                            } else {
                                splitSingleBtn.disabled = false;
                                splitSingleBtn.innerHTML = origHtml;
                                alert(res.error || 'Could not split file.');
                            }
                        })
                        .catch(err => {
                            splitSingleBtn.disabled = false;
                            splitSingleBtn.innerHTML = origHtml;
                            alert('Network error splitting file: ' + err.message);
                        });
                }

                const downloadLink = e.target.closest('.btn-download-file');
                if (downloadLink) {
                    const dlItemId = downloadLink.getAttribute('data-item-id') || (window._activeModalItem && window._activeModalItem.itemId) || '';
                    if (dlItemId) {
                        const cardEl = document.getElementById(`card-${dlItemId}`);
                        const detBtn = cardEl ? cardEl.querySelector('.btn-details') : null;
                        const wasInteresting = (detBtn && detBtn.getAttribute('data-is-interesting') === 'true') ||
                            (window._activeModalItem && window._activeModalItem.itemId === dlItemId && window._activeModalItem.isInteresting);
                        if (wasInteresting && typeof window.applyInterestingStateToDom === 'function') {
                            window.applyInterestingStateToDom(dlItemId, false, null, -1);
                        }
                    }
                }
            });
        }

        window.applyInterestingStateToDom = function (itemId, isInteresting, exactCount = null, deltaCount = 0) {
            if (!itemId) return;
            const cardEl = document.getElementById(`card-${itemId}`);
            if (cardEl) {
                const detBtn = cardEl.querySelector('.btn-details');
                if (detBtn) {
                    detBtn.setAttribute('data-is-interesting', isInteresting ? 'true' : 'false');
                }
            }
            const cardBtns = new Set();
            if (cardEl) {
                const cb = cardEl.querySelector('.btn-interesting-card');
                if (cb) cardBtns.add(cb);
            }
            document.querySelectorAll(`.btn-interesting-card[data-item-id="${itemId}"]`).forEach(cb => cardBtns.add(cb));
            cardBtns.forEach(cardBtn => {
                cardBtn.setAttribute('data-interesting', isInteresting ? 'true' : 'false');
                cardBtn.style.setProperty('background', isInteresting ? 'rgba(250,204,21,0.26)' : 'rgba(148,163,184,0.12)', 'important');
                cardBtn.style.setProperty('border-color', isInteresting ? '#facc15' : 'rgba(148,163,184,0.32)', 'important');
                cardBtn.style.setProperty('color', isInteresting ? '#fde047' : '#cbd5e1', 'important');
                cardBtn.style.setProperty('box-shadow', isInteresting ? '0 0 10px rgba(250,204,21,0.28)' : 'none', 'important');
                cardBtn.title = isInteresting
                    ? 'Marked as Interesting — click to unmark (also resets automatically when downloaded)'
                    : 'Mark as Interesting so it shows up in the Interesting Books category';
                cardBtn.innerHTML = `<i class="fa-${isInteresting ? 'solid' : 'regular'} fa-star" style="margin-right:0.25rem;${isInteresting ? ' color:#facc15;' : ''}"></i>Interesting`;
            });

            if (window._activeModalItem && String(window._activeModalItem.itemId) === String(itemId)) {
                window._activeModalItem.isInteresting = Boolean(isInteresting);
            }
            const modalBtn = document.getElementById('modal-top-interesting-btn');
            if (modalBtn && (!window._activeModalItem || String(window._activeModalItem.itemId) === String(itemId))) {
                modalBtn.setAttribute('data-interesting', isInteresting ? 'true' : 'false');
                modalBtn.style.cssText = isInteresting
                    ? 'display:inline-flex; align-items:center; gap:0.3rem; padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(250,204,21,0.26); border:1px solid #facc15; color:#fde047; box-shadow:0 0 10px rgba(250,204,21,0.28);'
                    : 'display:inline-flex; align-items:center; gap:0.3rem; padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(148,163,184,0.14); border:1px solid rgba(148,163,184,0.38); color:#cbd5e1;';
                modalBtn.innerHTML = `<i class="fa-${isInteresting ? 'solid' : 'regular'} fa-star" style="${isInteresting ? 'color:#facc15;' : ''}"></i> Interesting`;
                modalBtn.title = isInteresting
                    ? 'Marked as Interesting — click to unmark (also resets automatically when downloaded)'
                    : 'Mark as Interesting so it shows up in the Interesting Books category until downloaded';
            }

            const countEl = document.getElementById('sidebar-interesting-count');
            if (countEl) {
                if (exactCount !== null && exactCount !== undefined && !isNaN(Number(exactCount))) {
                    countEl.textContent = Number(exactCount).toLocaleString();
                } else if (deltaCount !== 0) {
                    const cur = parseInt((countEl.textContent || '0').replace(/,/g, ''), 10) || 0;
                    const next = Math.max(0, cur + deltaCount);
                    countEl.textContent = next.toLocaleString();
                }
            }

            const params = new URLSearchParams(window.location.search);
            if (params.get('shelf') === 'interesting' && cardEl) {
                cardEl.style.opacity = isInteresting ? '1' : '0.45';
            }
        };

        window.toggleBookInteresting = function (itemId, btn, explicitState = null) {
            if (!itemId) return;
            const origDisabled = btn ? btn.disabled : false;
            const origHtml = btn ? btn.innerHTML : '';
            if (btn) {
                btn.disabled = true;
                btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin" style="margin-right:0.25rem;"></i>Interesting`;
            }

            const payload = { item_id: itemId };
            if (explicitState !== null && explicitState !== undefined) {
                payload.is_interesting = Boolean(explicitState);
            }

            fetch(`${BASE_PREFIX}/api/interesting/`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCookie('csrftoken') || ''
                },
                body: JSON.stringify(payload)
            })
                .then(async r => {
                    const text = await r.text();
                    let data = null;
                    try { data = JSON.parse(text); } catch (_) { }
                    if (!r.ok && !data) {
                        throw new Error(`HTTP ${r.status}`);
                    }
                    return data;
                })
                .then(res => {
                    if (btn) btn.disabled = origDisabled;
                    if (!res || !(res.ok || res.status === 'success')) {
                        if (btn && origHtml) btn.innerHTML = origHtml;
                        alert((res && (res.error || res.detail)) || 'Failed to update Interesting status.');
                        return;
                    }
                    window.applyInterestingStateToDom(itemId, Boolean(res.is_interesting), res.interesting_count);
                })
                .catch(err => {
                    if (btn) {
                        btn.disabled = origDisabled;
                        if (origHtml) btn.innerHTML = origHtml;
                    }
                    alert('Network error updating Interesting status: ' + err.message);
                });
        };

        window.applyBadMetadataStateToDom = function (itemId, isBad, exactCount = null, deltaCount = 0) {
            if (!itemId) return;
            const cardEl = document.getElementById(`card-${itemId}`);
            if (cardEl) {
                const detBtn = cardEl.querySelector('.btn-details');
                if (detBtn) {
                    detBtn.setAttribute('data-is-bad-metadata', isBad ? 'true' : 'false');
                }
            }
            const cardBtns = new Set();
            if (cardEl) {
                const cb = cardEl.querySelector('.btn-bad-meta-card');
                if (cb) cardBtns.add(cb);
            }
            document.querySelectorAll(`.btn-bad-meta-card[data-item-id="${itemId}"]`).forEach(cb => cardBtns.add(cb));
            cardBtns.forEach(cardBtn => {
                cardBtn.setAttribute('data-bad-metadata', isBad ? 'true' : 'false');
                cardBtn.style.setProperty('background', isBad ? 'rgba(244,63,94,0.24)' : 'rgba(148,163,184,0.12)', 'important');
                cardBtn.style.setProperty('border-color', isBad ? 'rgba(244,63,94,0.65)' : 'rgba(148,163,184,0.32)', 'important');
                cardBtn.style.setProperty('color', isBad ? '#fda4af' : '#cbd5e1', 'important');
                cardBtn.title = isBad
                    ? 'Flagged as Bad Metadata — click to unflag (also clears automatically when enriched/approved)'
                    : 'Flag this book as having Bad Metadata (adds it to the Bad Metadata shelf)';
                cardBtn.innerHTML = `<i class="fa-${isBad ? 'solid' : 'regular'} fa-flag"></i>`;
            });

            if (window._activeModalItem && String(window._activeModalItem.itemId) === String(itemId)) {
                window._activeModalItem.isBadMetadata = Boolean(isBad);
            }
            const modalBtn = document.getElementById('modal-top-bad-meta-btn');
            if (modalBtn && (!window._activeModalItem || String(window._activeModalItem.itemId) === String(itemId))) {
                modalBtn.style.cssText = isBad
                    ? 'display:inline-flex; align-items:center; gap:0.3rem; padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(244,63,94,0.24); border:1px solid rgba(244,63,94,0.65); color:#fda4af;'
                    : 'display:inline-flex; align-items:center; gap:0.3rem; padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(148,163,184,0.14); border:1px solid rgba(148,163,184,0.38); color:#cbd5e1;';
                modalBtn.innerHTML = `<i class="fa-${isBad ? 'solid' : 'regular'} fa-flag"></i> Bad Metadata`;
                modalBtn.title = isBad
                    ? 'Flagged as Bad Metadata — click to unflag (also clears automatically when enriched or approved)'
                    : 'Flag this book as having Bad Metadata so it appears in the Bad Metadata shelf';
            }

            const countEl = document.getElementById('sidebar-bad-meta-count');
            if (countEl) {
                if (exactCount !== null && exactCount !== undefined && !isNaN(Number(exactCount))) {
                    countEl.textContent = Number(exactCount).toLocaleString();
                } else if (deltaCount !== 0) {
                    const cur = parseInt((countEl.textContent || '0').replace(/,/g, ''), 10) || 0;
                    const next = Math.max(0, cur + deltaCount);
                    countEl.textContent = next.toLocaleString();
                }
            }

            const params = new URLSearchParams(window.location.search);
            if (params.get('shelf') === 'bad_metadata' && cardEl) {
                cardEl.style.opacity = isBad ? '1' : '0.45';
            }
        };

        window.toggleBookBadMetadata = function (itemId, btn, explicitState = null) {
            if (!itemId) return;
            const origDisabled = btn ? btn.disabled : false;
            if (btn) btn.disabled = true;

            const payload = { item_id: itemId };
            if (explicitState !== null && explicitState !== undefined) {
                payload.is_bad_metadata = Boolean(explicitState);
            }

            fetch(`${BASE_PREFIX}/api/bad-metadata/`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCookie('csrftoken') || ''
                },
                body: JSON.stringify(payload)
            })
                .then(async r => {
                    const text = await r.text();
                    let data = null;
                    try { data = JSON.parse(text); } catch (_) { }
                    if (!r.ok && !data) {
                        throw new Error(`HTTP ${r.status}`);
                    }
                    return data;
                })
                .then(res => {
                    if (btn) btn.disabled = origDisabled;
                    if (!res || !(res.ok || res.status === 'success')) {
                        alert((res && (res.error || res.detail)) || 'Failed to update Bad Metadata status.');
                        return;
                    }
                    window.applyBadMetadataStateToDom(itemId, Boolean(res.is_bad_metadata), res.bad_metadata_count);
                })
                .catch(err => {
                    if (btn) btn.disabled = origDisabled;
                    alert('Network error updating Bad Metadata status: ' + err.message);
                });
        };

        let modalPeekPages = [];
        let _lastPreviewSelectedText = '';

        document.addEventListener('selectionchange', () => {
            try {
                const selObj = window.getSelection ? window.getSelection() : null;
                if (!selObj || selObj.isCollapsed) return;
                const txt = (selObj.toString() || '').trim();
                if (!txt) return;
                const anchor = selObj.anchorNode ? (selObj.anchorNode.nodeType === 1 ? selObj.anchorNode : selObj.anchorNode.parentElement) : null;
                if (anchor && anchor.closest && (anchor.closest('#er-book-preview-drawer') || anchor.closest('#modal-book-peek-drawer'))) {
                    _lastPreviewSelectedText = txt;
                }
            } catch (_) { }
        });

        window.toggleModalBookPeek = function ({ itemId = '', fileId = '', filePath = '', forceShow = false } = {}) {
            const drawer = document.getElementById('modal-book-peek-drawer');
            if (!drawer) return;
            const active = window._activeModalItem || {};
            const targetItemId = itemId || active.itemId || '';
            const targetFilePath = filePath || active.firstFilePath || '';
            const key = `${targetFilePath}|${targetItemId}|${fileId}`;

            if (!forceShow && drawer.style.display === 'block' && drawer.dataset.loadedFor === key) {
                drawer.style.display = 'none';
                return;
            }
            drawer.style.display = 'block';
            drawer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            if (drawer.dataset.loadedFor === key) return;

            drawer._peekCtx = { itemId: targetItemId, fileId, filePath: targetFilePath };
            const textBody = document.getElementById('modal-peek-text-body');
            const imgStrip = document.getElementById('modal-peek-images-strip');
            const tabsEl = document.getElementById('modal-peek-section-tabs');
            const isbnsBar = document.getElementById('modal-peek-isbns-bar');
            const metaInfo = document.getElementById('modal-peek-meta-info');
            const rawLink = document.getElementById('modal-peek-raw-link');

            if (textBody) textBody.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Reading pages, cover images, and front matter from book file on NAS...`;
            if (imgStrip) { imgStrip.style.display = 'none'; imgStrip.innerHTML = ''; }
            if (tabsEl) tabsEl.innerHTML = '';
            if (isbnsBar) { isbnsBar.style.display = 'none'; isbnsBar.innerHTML = ''; }
            if (metaInfo) metaInfo.textContent = '';
            if (rawLink) rawLink.style.display = 'none';
            const copySt = document.getElementById('modal-peek-copy-status');
            if (copySt) { copySt.style.display = 'none'; copySt.innerHTML = ''; }

            const params = new URLSearchParams();
            if (targetFilePath) params.set('file_path', targetFilePath);
            if (fileId) params.set('file_id', fileId);
            if (targetItemId) params.set('item_id', targetItemId);

            const fetchPeekWithRetry = (attempt = 1) => fetch(`${BASE_PREFIX}/api/book-preview/?${params.toString()}`)
                .then(async r => {
                    const text = await r.text();
                    try {
                        return JSON.parse(text);
                    } catch (e) {
                        if (attempt < 2) {
                            await new Promise(res => setTimeout(res, 1500));
                            return fetchPeekWithRetry(attempt + 1);
                        }
                        throw new Error(`Server returned HTTP ${r.status} (please click Peek Inside again)`);
                    }
                });

            fetchPeekWithRetry()
                .then(res => {
                    if (!res.ok) {
                        if (textBody) textBody.textContent = res.error || 'Could not load book preview.';
                        return;
                    }
                    drawer.dataset.loadedFor = key;
                    if (metaInfo) {
                        metaInfo.textContent = `${res.file_name || ''} (${res.file_ext} • ${res.file_size_mb} MB • ${res.total_sections || 0} pages/sections)`;
                    }
                    if (rawLink && res.raw_stream_url) {
                        rawLink.href = res.raw_stream_url;
                        rawLink.style.display = 'inline-flex';
                    }

                    if (isbnsBar && Array.isArray(res.detected_isbns) && res.detected_isbns.length > 0) {
                        isbnsBar.style.display = 'flex';
                        isbnsBar.innerHTML = `<span style="color:#94a3b8;"><i class="fa-solid fa-barcode" style="color:#4ade80;"></i> ISBNs found inside book:</span>` +
                            res.detected_isbns.map(isbn =>
                                `<button type="button" onclick="window.enrichModalFromPeekIsbn('${escapeJsString(isbn)}', this)" style="background:rgba(74,222,128,0.16); border:1px solid rgba(74,222,128,0.45); color:#4ade80; border-radius:999px; padding:0.12rem 0.55rem; font-family:monospace; font-size:0.73rem; font-weight:700; cursor:pointer;" title="Click to enrich this book using ISBN ${escapeHtml(isbn)}"><i class="fa-solid fa-wand-magic-sparkles"></i> Enrich with ${escapeHtml(isbn)}</button>`
                            ).join('');
                    }

                    if (imgStrip && Array.isArray(res.images) && res.images.length > 0) {
                        imgStrip.style.display = 'flex';
                        imgStrip.innerHTML = res.images.map((img, i) => `
                            <div style="flex:0 0 auto; width:132px; background:rgba(15,23,42,0.9); border:1px solid rgba(255,255,255,0.14); border-radius:8px; padding:0.35rem; display:flex; flex-direction:column; align-items:center; gap:0.25rem;">
                                <img src="${img.data_uri}" alt="${escapeHtml(img.label)}" onclick="window.open().document.write('<body style=\\'margin:0;background:#0f172a;display:flex;justify-content:center;align-items:center;min-height:100vh;\\'><img src=\\'${img.data_uri}\\' style=\\'max-width:95vw;max-height:95vh;\\'></body>')" style="width:100%; height:160px; object-fit:contain; border-radius:5px; cursor:zoom-in; background:#020617;" title="Click to view full size: ${escapeHtml(img.label)}">
                                <span style="font-size:0.66rem; color:#94a3b8; text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; width:100%;">${escapeHtml(img.label)}</span>
                                <div style="display:flex; flex-direction:column; gap:0.22rem; width:100%;">
                                    <button type="button" onclick="window.useModalPeekCover(${i}, this)" style="width:100%; background:rgba(56,189,248,0.15); border:1px solid rgba(56,189,248,0.38); color:#38bdf8; border-radius:4px; font-size:0.65rem; font-weight:600; padding:0.15rem 0.3rem; cursor:pointer;">Use as Cover</button>
                                    <button type="button" onclick="window.modalPeekAiReevaluate(${i}, this)" style="width:100%; background:rgba(168,85,247,0.2); border:1px solid rgba(168,85,247,0.48); color:#e9d5ff; border-radius:4px; font-size:0.65rem; font-weight:700; padding:0.16rem 0.3rem; cursor:pointer;" title="Have Gemini AI Vision inspect this page image to identify the book and re-evaluate enrichment"><i class="fa-solid fa-wand-magic-sparkles"></i> AI Re-Evaluate</button>
                                </div>
                            </div>
                        `).join('');
                        drawer._previewImages = res.images;
                    }

                    modalPeekPages = Array.isArray(res.pages) ? res.pages : [];
                    if (modalPeekPages.length === 0) {
                        if (textBody) textBody.textContent = 'No extractable text found in the front matter (scanned image PDF — inspect the page images above or click Open Full File).';
                    } else {
                        if (tabsEl) {
                            tabsEl.innerHTML = modalPeekPages.map((p, idx) => `
                                <button type="button" class="modal-peek-tab-btn" data-idx="${idx}" onclick="window.selectModalPeekSection(${idx})" style="background:${idx === 0 ? 'rgba(56,189,248,0.28)' : 'rgba(30,41,59,0.85)'}; border:1px solid ${idx === 0 ? 'rgba(56,189,248,0.6)' : 'rgba(255,255,255,0.12)'}; color:${idx === 0 ? '#f8fafc' : '#cbd5e1'}; border-radius:6px; padding:0.22rem 0.55rem; font-size:0.72rem; font-weight:600; cursor:pointer;">
                                    ${escapeHtml(p.label)}
                                </button>
                            `).join('');
                        }
                        window.selectModalPeekSection(0);
                    }
                })
                .catch(err => {
                    if (textBody) textBody.textContent = 'Network error loading book preview: ' + err.message;
                });
        };

        window.selectModalPeekSection = function (idx) {
            const textBody = document.getElementById('modal-peek-text-body');
            if (!textBody || !modalPeekPages[idx]) return;
            textBody.textContent = modalPeekPages[idx].text || '';
            textBody.scrollTop = 0;
            document.querySelectorAll('.modal-peek-tab-btn').forEach(btn => {
                const active = Number(btn.getAttribute('data-idx')) === idx;
                btn.style.background = active ? 'rgba(56,189,248,0.28)' : 'rgba(30,41,59,0.85)';
                btn.style.borderColor = active ? 'rgba(56,189,248,0.6)' : 'rgba(255,255,255,0.12)';
                btn.style.color = active ? '#f8fafc' : '#cbd5e1';
            });
        };

        window.modalPeekUseSelectedText = function (targetField, btnEl = null) {
            const liveSel = (window.getSelection ? window.getSelection().toString() : '').trim();
            const sel = liveSel || (_lastPreviewSelectedText || '').trim();
            if (!sel) {
                alert('First highlight any text inside the book preview box below, then click this button to copy it into the field.');
                return;
            }
            const clean = sel.replace(/\s+/g, ' ').trim();
            const finalVal = targetField === 'desc' ? sel.trim() : clean;
            const active = window._activeModalItem || {};
            if (!active._peekStagedMeta) active._peekStagedMeta = {};
            active._peekStagedMeta[targetField] = finalVal;

            const labels = { title: 'Title', author: 'Author', publisher: 'Publisher', desc: 'Synopsis' };
            const fieldLabel = labels[targetField] || targetField;

            if (btnEl) {
                const origHtml = btnEl.dataset.origHtml || btnEl.innerHTML;
                btnEl.dataset.origHtml = origHtml;
                btnEl.innerHTML = `<i class="fa-solid fa-check"></i> ${fieldLabel} Set!`;
                btnEl.style.background = 'rgba(16,185,129,0.28)';
                btnEl.style.borderColor = '#4ade80';
                btnEl.style.color = '#4ade80';
                setTimeout(() => {
                    btnEl.innerHTML = origHtml;
                    btnEl.style.background = 'rgba(56,189,248,0.16)';
                    btnEl.style.borderColor = 'rgba(56,189,248,0.4)';
                    btnEl.style.color = '#38bdf8';
                }, 1500);
            }

            const statusBar = document.getElementById('modal-peek-copy-status');
            if (statusBar) {
                const shortPreview = finalVal.length > 90 ? finalVal.slice(0, 90) + '…' : finalVal;
                statusBar.style.display = 'flex';
                statusBar.innerHTML = `
                    <div style="min-width:0; flex:1;">
                        <strong style="color:#4ade80;"><i class="fa-solid fa-check"></i> ${escapeHtml(fieldLabel)} staged:</strong>
                        <span style="color:#f8fafc; font-weight:600; margin-left:0.25rem;">"${escapeHtml(shortPreview)}"</span>
                    </div>
                    <div style="display:flex; gap:0.4rem; align-items:center; flex-shrink:0;">
                        <button type="button" onclick="window.saveModalPeekStagedMetadata(this)" style="background:rgba(74,222,128,0.24); border:1px solid rgba(74,222,128,0.6); color:#4ade80; border-radius:6px; padding:0.24rem 0.65rem; font-size:0.74rem; font-weight:700; cursor:pointer;">
                            <i class="fa-solid fa-floppy-disk"></i> Save Metadata Now
                        </button>
                    </div>
                `;
            }
        };

        window.saveModalPeekStagedMetadata = function (btnEl = null) {
            const active = window._activeModalItem || {};
            const staged = active._peekStagedMeta || {};
            const drawer = document.getElementById('modal-book-peek-drawer');
            const ctx = (drawer && drawer._peekCtx) || {};
            const targetItemId = ctx.itemId || active.itemId || '';
            const targetFilePath = ctx.filePath || active.firstFilePath || '';
            const selectedMetadata = {
                title: staged.title !== undefined ? staged.title : (active.title || ''),
                author: staged.author !== undefined ? staged.author : (active.authors || ''),
                publisher: staged.publisher !== undefined ? staged.publisher : (active.publisher || ''),
                pub_date: active.pubDate || '',
                isbn: active.isbn || '',
                description: staged.desc !== undefined ? staged.desc : (active.desc || ''),
                cover: active.cover || '',
            };
            const origHtml = btnEl ? btnEl.innerHTML : '';
            if (btnEl) {
                btnEl.disabled = true;
                btnEl.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Saving...`;
            }
            fetch(`${BASE_PREFIX}/api/status/items/`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCookie('csrftoken')
                },
                body: JSON.stringify({
                    action: 'approve',
                    item_id: targetItemId,
                    file_path: targetFilePath,
                    selected_metadata: selectedMetadata,
                })
            })
                .then(r => r.json())
                .then(res => {
                    if (btnEl) {
                        btnEl.disabled = false;
                        btnEl.innerHTML = res && res.ok ? `<i class="fa-solid fa-check"></i> Saved!` : origHtml;
                    }
                    if (res && res.ok) {
                        active.title = selectedMetadata.title;
                        active.authors = selectedMetadata.author;
                        active.publisher = selectedMetadata.publisher;
                        active.desc = selectedMetadata.description;
                        active._peekStagedMeta = {};
                        transitionCardEnrichBucket(targetItemId, 'enriched');
                        applyCardMetadataUpdate(targetItemId, {
                            item_id: res.item_id || targetItemId,
                            title: selectedMetadata.title,
                            authors: selectedMetadata.author,
                            publisher: selectedMetadata.publisher,
                            pub_date: selectedMetadata.pub_date,
                            isbn: selectedMetadata.isbn,
                            description: selectedMetadata.description,
                            cover_url: res.cover_url || selectedMetadata.cover,
                            merged_item_ids: res.merged_item_ids || [],
                        }, true);
                    } else {
                        alert((res && res.error) || 'Could not save metadata.');
                    }
                })
                .catch(err => {
                    if (btnEl) {
                        btnEl.disabled = false;
                        btnEl.innerHTML = origHtml;
                    }
                    alert('Network error saving metadata: ' + err.message);
                });
        };

        window.useModalPeekCover = function (imgIdx, btnEl) {
            const drawer = document.getElementById('modal-book-peek-drawer');
            const imgs = drawer && Array.isArray(drawer._previewImages) ? drawer._previewImages : [];
            const active = window._activeModalItem || {};
            if (!imgs[imgIdx] || !active.itemId) return;
            const origText = btnEl ? btnEl.textContent : 'Use as Cover';
            if (btnEl) {
                btnEl.disabled = true;
                btnEl.textContent = 'Applying...';
            }
            fetch(`${BASE_PREFIX}/api/cover-apply/`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCookie('csrftoken')
                },
                body: JSON.stringify({
                    item_id: active.itemId,
                    file_id: active.firstFileId || '',
                    file_path: active.firstFilePath || '',
                    isbn: active.isbn || '',
                    title: active.title || '',
                    authors: active.authors || '',
                    cover_url: imgs[imgIdx].data_uri,
                    selected_candidate: {
                        source: 'Peek Inside Page',
                        title: imgs[imgIdx].label || `Page ${imgIdx + 1}`,
                        candidate_index: imgIdx,
                    },
                })
            })
                .then(r => r.json())
                .then(res => {
                    if (btnEl) {
                        btnEl.disabled = false;
                        btnEl.textContent = res.ok ? '✓ Cover Applied' : origText;
                    }
                    if (res.ok) {
                        const resolvedId = res.item_id || active.itemId;
                        if (window._activeModalItem && res.item_id) {
                            window._activeModalItem.itemId = res.item_id;
                        }
                        applyCardMetadataUpdate(resolvedId, { cover_url: res.cover_url || imgs[imgIdx].data_uri }, true);
                    }
                })
                .catch(() => {
                    if (btnEl) {
                        btnEl.disabled = false;
                        btnEl.textContent = origText;
                    }
                });
        };

        window.enrichModalFromPeekIsbn = function (isbn, btnEl) {
            const drawer = document.getElementById('modal-book-peek-drawer');
            const ctx = (drawer && drawer._peekCtx) || {};
            const active = window._activeModalItem || {};
            const targetItemId = ctx.itemId || active.itemId || '';
            const origHtml = btnEl ? btnEl.innerHTML : '';
            if (btnEl) {
                btnEl.disabled = true;
                btnEl.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Enriching with ${escapeHtml(isbn)}...`;
            }
            fetch(`${BASE_PREFIX}/api/enrich/`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCookie('csrftoken')
                },
                body: JSON.stringify({
                    item_id: targetItemId,
                    file_id: ctx.fileId || '',
                    file_path: ctx.filePath || active.firstFilePath || '',
                    re_enrich: true,
                    hint_isbn: isbn
                })
            })
                .then(r => r.json())
                .then(res => {
                    if (btnEl) {
                        btnEl.disabled = false;
                        btnEl.innerHTML = origHtml;
                    }
                    if (res.ok) {
                        transitionCardEnrichBucket(targetItemId || res.item_id, 'enriched');
                        applyCardMetadataUpdate(targetItemId, res, true);
                    } else {
                        transitionCardEnrichBucket(targetItemId || res.item_id, 'needs_review');
                        const cardEl = targetItemId ? document.getElementById(`card-${targetItemId}`) : null;
                        const cardEnrBtn = cardEl ? cardEl.querySelector('.btn-enrich-card') : null;
                        openEnrichReviewModal(res, cardEnrBtn, true, targetItemId, ctx.fileId || '');
                    }
                })
                .catch(() => {
                    if (btnEl) {
                        btnEl.disabled = false;
                        btnEl.innerHTML = origHtml;
                    }
                });
        };

        window.modalPeekAiReevaluate = function (imgIdx = null, triggerBtn = null) {
            const drawer = document.getElementById('modal-book-peek-drawer');
            const ctx = (drawer && drawer._peekCtx) || {};
            const active = window._activeModalItem || {};
            const targetItemId = ctx.itemId || active.itemId || '';
            const imgs = drawer && Array.isArray(drawer._previewImages) ? drawer._previewImages : [];
            const selectedUris = [];
            if (imgIdx !== null && imgIdx !== undefined && imgs[imgIdx] && imgs[imgIdx].data_uri) {
                selectedUris.push(imgs[imgIdx].data_uri);
            } else {
                imgs.slice(0, 3).forEach(im => {
                    if (im && im.data_uri) selectedUris.push(im.data_uri);
                });
            }
            const origHtml = triggerBtn ? triggerBtn.innerHTML : '';
            if (triggerBtn) {
                triggerBtn.disabled = true;
                triggerBtn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> AI Analyzing...`;
            }
            fetch(`${BASE_PREFIX}/api/enrich/`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCookie('csrftoken')
                },
                body: JSON.stringify({
                    item_id: targetItemId,
                    file_id: ctx.fileId || '',
                    file_path: ctx.filePath || active.firstFilePath || '',
                    re_enrich: true,
                    force_ai_vision: true,
                    ai_image_data_uris: selectedUris
                })
            })
                .then(r => r.json())
                .then(res => {
                    if (triggerBtn) {
                        triggerBtn.disabled = false;
                        triggerBtn.innerHTML = origHtml;
                    }
                    if (res.ok) {
                        transitionCardEnrichBucket(targetItemId || res.item_id, 'enriched');
                        applyCardMetadataUpdate(targetItemId, res, true);
                    } else {
                        transitionCardEnrichBucket(targetItemId || res.item_id, 'needs_review');
                        const cardEl = targetItemId ? document.getElementById(`card-${targetItemId}`) : null;
                        const cardEnrBtn = cardEl ? cardEl.querySelector('.btn-enrich-card') : null;
                        openEnrichReviewModal(res, cardEnrBtn, true, targetItemId, ctx.fileId || '');
                    }
                })
                .catch(() => {
                    if (triggerBtn) {
                        triggerBtn.disabled = false;
                        triggerBtn.innerHTML = origHtml;
                    }
                });
        };

        window.keepCurrentMetadataAsIs = function ({ itemId, filePath, btn }) {
            const active = window._activeModalItem || {};
            const targetItemId = itemId || active.itemId || '';
            const targetFilePath = filePath || active.firstFilePath || '';
            const origHtml = btn ? btn.innerHTML : '';
            if (btn) {
                btn.disabled = true;
                btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Keeping...`;
            }
            const selectedMetadata = {
                title: active.title || '',
                author: active.authors || '',
                publisher: active.publisher || '',
                pub_date: active.pubDate || '',
                isbn: active.isbn || '',
                description: active.desc || '',
                cover: active.cover || '',
            };
            fetch(`${BASE_PREFIX}/api/status/items/`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCookie('csrftoken')
                },
                body: JSON.stringify({
                    action: 'approve',
                    item_id: targetItemId,
                    file_path: targetFilePath,
                    selected_metadata: selectedMetadata,
                })
            })
                .then(r => r.json())
                .then(res => {
                    if (res && res.ok) {
                        if (window._activeModalItem) {
                            window._activeModalItem.isEnriched = true;
                        }
                        const topKeepBtn = document.getElementById('modal-top-keep-btn');
                        if (topKeepBtn) topKeepBtn.style.display = 'none';
                        const topRevertBtn = document.getElementById('modal-top-revert-btn');
                        if (topRevertBtn) topRevertBtn.style.display = 'inline-flex';
                        document.querySelectorAll('#modal-files-list .btn-keep-file').forEach(b => {
                            b.style.display = 'none';
                        });
                        const cardEl = targetItemId ? document.getElementById(`card-${targetItemId}`) : null;
                        const cardEnrBtn = cardEl ? cardEl.querySelector('.btn-enrich-card') : null;
                        const hadReviewDiag = Boolean(cardEnrBtn && (cardEnrBtn._reviewData || cardEnrBtn.getAttribute('data-enrich-status')));
                        if (cardEnrBtn) {
                            cardEnrBtn._reviewData = null;
                            cardEnrBtn.setAttribute('data-force', 'true');
                            cardEnrBtn.setAttribute('data-enrich-status', 'SUCCESS');
                            cardEnrBtn.innerHTML = `<i class="fa-solid fa-check"></i> Enriched!`;
                            cardEnrBtn.style.background = 'rgba(74,222,128,0.18)';
                            cardEnrBtn.style.borderColor = 'rgba(74,222,128,0.5)';
                            cardEnrBtn.style.color = '#4ade80';
                        }
                        transitionCardEnrichBucket(targetItemId, 'enriched', hadReviewDiag ? 'needs_review' : 'needs_enriching');
                        applyCardMetadataUpdate(targetItemId, {
                            title: res.title || selectedMetadata.title,
                            authors: res.authors || selectedMetadata.author,
                            publisher: res.publisher || selectedMetadata.publisher,
                            pub_date: selectedMetadata.pub_date,
                            isbn: res.isbn || selectedMetadata.isbn,
                            description: res.description || selectedMetadata.description,
                            cover_url: res.cover_url || selectedMetadata.cover,
                            merged_item_ids: res.merged_item_ids || [],
                        }, true);
                    } else {
                        if (btn) {
                            btn.disabled = false;
                            btn.innerHTML = origHtml;
                        }
                        alert((res && res.error) || 'Could not mark item as kept/enriched.');
                    }
                })
                .catch(err => {
                    if (btn) {
                        btn.disabled = false;
                        btn.innerHTML = origHtml;
                    }
                    alert('Network error keeping metadata: ' + err.message);
                });
        };

        // Event listeners for card-level Enrich Now and Move to toMag buttons
        window.enrichCardNow = function (btn) {
            if (!btn) return;
            if (!btn._reviewData) {
                const rawRevJson = btn.getAttribute('data-review-json') || '';
                if (rawRevJson) {
                    try {
                        btn._reviewData = JSON.parse(rawRevJson);
                    } catch (e) { }
                }
            }
            const itemId = btn.getAttribute('data-item-id');
            const isAlreadyEnr = btn.getAttribute('data-force') === 'true';
            triggerOnDemandEnrich({ itemId, force: false, reEnrich: isAlreadyEnr, btn, updateModal: false });
        };

        document.querySelectorAll('.btn-enrich-card').forEach(btn => {
            const rawRevJson = btn.getAttribute('data-review-json') || '';
            if (rawRevJson) {
                try {
                    btn._reviewData = JSON.parse(rawRevJson);
                } catch (e) { }
            }
            if (!btn.hasAttribute('onclick')) {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    window.enrichCardNow(btn);
                });
            }
        });

        window.reEnrichCardNow = function (reBtn) {
            if (!reBtn) return;
            const itemId = reBtn.getAttribute('data-item-id');
            const cardEl = itemId ? document.getElementById(`card-${itemId}`) : reBtn.closest('.media-card');
            const mainBtn = cardEl ? cardEl.querySelector('.btn-enrich-card') : null;
            triggerOnDemandEnrich({
                itemId,
                force: false,
                reEnrich: true,
                btn: mainBtn || reBtn,
                reBtn,
                updateModal: false
            });
        };

        document.querySelectorAll('.btn-reenrich-card').forEach(reBtn => {
            if (!reBtn.hasAttribute('onclick')) {
                reBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    window.reEnrichCardNow(reBtn);
                });
            }
        });

        window.moveToMagNow = function (btn, targetBucket) {
            if (!btn) return;
            const itemId = btn.getAttribute('data-item-id');
            triggerMoveToMags({ itemId, btn, targetBucket: targetBucket || '' });
        };

        document.querySelectorAll('.btn-move-tomag').forEach(btn => {
            if (!btn.hasAttribute('onclick')) {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    window.moveToMagNow(btn);
                });
            }
        });

        window.moveToBookNow = function (btn) {
            if (!btn) return;
            const itemId = btn.getAttribute('data-item-id');
            triggerMoveToBooks({ itemId, btn });
        };

        document.querySelectorAll('.btn-move-tobook').forEach(btn => {
            if (!btn.hasAttribute('onclick')) {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    window.moveToBookNow(btn);
                });
            }
        });

        // Close modal handlers
        modalClose.addEventListener('click', closeModal);
        modalOverlay.addEventListener('click', (e) => {
            if (e.target === modalOverlay) closeModal();
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                closeTypePillDropdown();
                if (modalOverlay.classList.contains('active')) closeModal();
            }
        });
        document.addEventListener('click', (e) => {
            const menu = document.getElementById('type-pill-dropdown-menu');
            if (menu && menu.style.display !== 'none' && !menu.contains(e.target) && !e.target.closest('.card-type-badge')) {
                closeTypePillDropdown();
            }
        });
    }

    function closeModal() {
        modalOverlay.classList.remove('active');
        setTimeout(() => {
            modalOverlay.style.display = 'none';
        }, 300); // matches CSS transition duration
    }

    function getAvailableTypePillOptions() {
        return window.VX_UNLOCKED ? ['BOOK', 'MAGAZINE', 'ARCHIVE-X'] : ['BOOK', 'MAGAZINE'];
    }

    function closeTypePillDropdown() {
        const menu = document.getElementById('type-pill-dropdown-menu');
        if (menu) {
            menu.style.display = 'none';
            menu._anchorBadge = null;
        }
    }

    function updateTypePillAfterMove(typeBadge, newType, isUndoneToOrigin, fileId, customLabel, customTitle) {
        if (!typeBadge) return;
        const normType = String(newType || 'BOOK').toUpperCase();
        const opts = getAvailableTypePillOptions();
        const hasDropdown = opts.length > 2;
        const canUndo = !isUndoneToOrigin;

        typeBadge.disabled = false;
        typeBadge.setAttribute('data-current-type', normType);
        typeBadge.setAttribute('data-can-undo', canUndo ? 'true' : 'false');
        if (fileId !== undefined) {
            typeBadge.setAttribute('data-file-id', fileId || '');
        }

        if (normType === 'BOOK') {
            typeBadge.className = 'card-type-badge type-book';
            typeBadge.style.background = '';
        } else if (normType === 'ARCHIVE-X') {
            typeBadge.className = 'card-type-badge type-magazine';
            typeBadge.style.background = 'rgba(244,63,94,0.88)';
        } else {
            typeBadge.className = 'card-type-badge type-magazine';
            typeBadge.style.background = canUndo ? 'rgba(56,189,248,0.88)' : '';
        }

        const labelText = customLabel || normType;
        if (canUndo) {
            typeBadge.innerHTML = `${labelText} <i class="fa-solid fa-rotate-left pill-undo-icon" style="font-size:0.55rem; opacity:0.95; margin-left:2px;" title="Undo move (restore directly to origin)"></i>${hasDropdown ? '<i class="fa-solid fa-caret-down" style="font-size:0.58rem; opacity:0.85; margin-left:3px;"></i>' : ''}`;
        } else {
            typeBadge.innerHTML = `${labelText} ${hasDropdown ? '<i class="fa-solid fa-caret-down" style="font-size:0.58rem; opacity:0.85; margin-left:3px;"></i>' : '<i class="fa-solid fa-repeat" style="font-size:0.55rem; opacity:0.8; margin-left:2px;"></i>'}`;
        }

        if (customTitle) {
            typeBadge.title = customTitle;
        }
        typeBadge.onclick = function (e) {
            if (e) e.stopPropagation();
            window.onTypePillClick(typeBadge, e);
        };
    }

    function openTypePillDropdown(badgeEl) {
        if (!badgeEl) return;
        let menu = document.getElementById('type-pill-dropdown-menu');
        if (menu && menu.style.display !== 'none' && menu._anchorBadge === badgeEl) {
            closeTypePillDropdown();
            return;
        }
        if (!menu) {
            menu = document.createElement('div');
            menu.id = 'type-pill-dropdown-menu';
            menu.style.cssText = [
                'position:fixed',
                'z-index:10050',
                'min-width:175px',
                'background:rgba(15,23,42,0.97)',
                'border:1px solid rgba(56,189,248,0.45)',
                'border-radius:10px',
                'padding:0.32rem',
                'box-shadow:0 14px 34px rgba(0,0,0,0.75)',
                'backdrop-filter:blur(8px)',
                'display:none',
                'flex-direction:column',
                'gap:0.18rem',
            ].join(';');
            document.body.appendChild(menu);
        }

        const itemId = badgeEl.getAttribute('data-item-id') || '';
        const fileId = badgeEl.getAttribute('data-file-id') || '';
        const curType = (badgeEl.getAttribute('data-current-type') || (badgeEl.classList.contains('type-book') ? 'BOOK' : 'MAGAZINE')).toUpperCase();
        const canUndo = badgeEl.getAttribute('data-can-undo') === 'true';
        const opts = getAvailableTypePillOptions();

        menu.innerHTML = '';

        function makeOptionRow({ html, title, accentColor, onClick }) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.title = title || '';
            btn.style.cssText = [
                'display:flex',
                'align-items:center',
                'gap:0.45rem',
                'width:100%',
                'text-align:left',
                'padding:0.4rem 0.58rem',
                'border-radius:7px',
                'border:none',
                'background:transparent',
                `color:${accentColor || '#f1f5f9'}`,
                'font-size:0.75rem',
                'font-weight:700',
                'cursor:pointer',
                'transition:background 0.15s',
            ].join(';');
            btn.innerHTML = html;
            btn.addEventListener('mouseenter', () => {
                btn.style.background = 'rgba(56,189,248,0.16)';
            });
            btn.addEventListener('mouseleave', () => {
                btn.style.background = 'transparent';
            });
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                closeTypePillDropdown();
                onClick();
            });
            return btn;
        }

        if (canUndo) {
            menu.appendChild(makeOptionRow({
                html: `<i class="fa-solid fa-rotate-left" style="color:#fde047; width:14px; text-align:center;"></i> <span>Undo Move (Restore Origin)</span>`,
                title: 'Move physical file directly back to the exact folder it came from',
                accentColor: '#fde047',
                onClick: () => {
                    window.undoMoveToMag(itemId, fileId, [], badgeEl, false);
                },
            }));
            const sep = document.createElement('div');
            sep.style.cssText = 'height:1px; background:rgba(148,163,184,0.22); margin:0.15rem 0.25rem;';
            menu.appendChild(sep);
        }

        opts.forEach(opt => {
            if (opt === curType) return;
            if (opt === 'BOOK') {
                menu.appendChild(makeOptionRow({
                    html: `<i class="fa-solid fa-book" style="color:#c084fc; width:14px; text-align:center;"></i> <span>Move to Books</span>`,
                    title: 'Move to Books (/books/<Category>/<Shard>/ if enriched, else /toBooks)',
                    accentColor: '#e9d5ff',
                    onClick: () => {
                        triggerMoveToBooks({ itemId, fileId, btn: badgeEl });
                    },
                }));
            } else if (opt === 'MAGAZINE') {
                menu.appendChild(makeOptionRow({
                    html: `<i class="fa-solid fa-newspaper" style="color:#38bdf8; width:14px; text-align:center;"></i> <span>Move to Magazines</span>`,
                    title: 'Move to Periodicals (/periodicals/<Series>/<Year>/ if enriched, else /toMags)',
                    accentColor: '#bae6fd',
                    onClick: () => {
                        triggerMoveToMags({ itemId, fileId, btn: badgeEl, targetBucket: 'Periodicals' });
                    },
                }));
            } else if (opt === 'ARCHIVE-X') {
                menu.appendChild(makeOptionRow({
                    html: `<i class="fa-solid fa-box-archive" style="color:#fb7185; width:14px; text-align:center;"></i> <span>Move to Archive-X</span>`,
                    title: 'Move to Archive-X',
                    accentColor: '#fda4af',
                    onClick: () => {
                        triggerMoveToMags({ itemId, fileId, btn: badgeEl, targetBucket: 'Archive-X' });
                    },
                }));
            }
        });

        menu._anchorBadge = badgeEl;
        menu.style.display = 'flex';
        const rect = badgeEl.getBoundingClientRect();
        const menuWidth = Math.max(menu.offsetWidth || 180, 180);
        const leftPos = Math.max(8, Math.min(window.innerWidth - menuWidth - 8, rect.right - menuWidth));
        const topPos = Math.min(window.innerHeight - 160, rect.bottom + 5);
        menu.style.left = `${leftPos}px`;
        menu.style.top = `${topPos}px`;
    }

    window.onTypePillClick = function (badgeEl, event) {
        if (!badgeEl) return;
        const itemId = badgeEl.getAttribute('data-item-id') || '';
        const fileId = badgeEl.getAttribute('data-file-id') || '';
        const curType = (badgeEl.getAttribute('data-current-type') || (badgeEl.classList.contains('type-book') ? 'BOOK' : 'MAGAZINE')).toUpperCase();
        const canUndo = badgeEl.getAttribute('data-can-undo') === 'true';
        const opts = getAvailableTypePillOptions();

        // When only 2 options exist (BOOK <-> MAGAZINE), act as a direct 2-way toggle & 1-click Undo
        if (opts.length <= 2) {
            closeTypePillDropdown();
            if (canUndo) {
                window.undoMoveToMag(itemId, fileId, [], badgeEl, false);
                return;
            }
            if (curType === 'BOOK') {
                triggerMoveToMags({ itemId, fileId, btn: badgeEl });
            } else {
                triggerMoveToBooks({ itemId, fileId, btn: badgeEl });
            }
            return;
        }

        // When 3+ options exist (e.g. Archive-X unlocked):
        // Clicking directly on the undo icon performs immediate 1-click Undo; otherwise open dropdown
        if (canUndo && event && event.target && event.target.closest && event.target.closest('.pill-undo-icon')) {
            closeTypePillDropdown();
            window.undoMoveToMag(itemId, fileId, [], badgeEl, false);
            return;
        }
        openTypePillDropdown(badgeEl);
    };

    function triggerMoveToMags({ itemId, fileId, btn, targetBucket }) {
        if (!btn) return;
        const origHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Moving...`;

        const payload = {};
        if (fileId) payload.file_id = fileId;
        else if (itemId) payload.item_id = itemId;
        if (targetBucket) payload.target_bucket = targetBucket;

        fetch(`${BASE_PREFIX}/api/move-to-mag/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify(payload)
        })
            .then(resp => resp.json())
            .then(data => {
                if (data.ok) {
                    const targetId = data.item_id || itemId;
                    const card = targetId ? document.getElementById(`card-${targetId}`) : null;
                    if (data.moved_to_periodicals_direct && card) {
                        card.style.opacity = '1';
                        const topFld = data.top_folder || (targetBucket === 'Archive-X' ? 'Archive-X' : 'Periodicals');
                        card.setAttribute('data-top-folder', topFld);
                        const newPillType = topFld === 'Archive-X' ? 'ARCHIVE-X' : 'MAGAZINE';
                        const typeBadge = card.querySelector('.card-type-badge');
                        if (typeBadge) {
                            updateTypePillAfterMove(
                                typeBadge,
                                newPillType,
                                Boolean(data.undone_to_origin),
                                data.file_id || fileId || '',
                                newPillType,
                                data.undone_to_origin
                                    ? `Undone — restored to original ${topFld} folder`
                                    : `Moved to ${topFld} — Click to Undo and move directly back to original folder`
                            );
                        }
                        const fldLbl = card.querySelector('.card-folder-label');
                        if (fldLbl) {
                            const ser = data.periodical_series || '';
                            fldLbl.textContent = ser ? `${topFld} › ${ser}` : topFld;
                        }
                        const descEl = card.querySelector('.card-description');
                        if (descEl && (data.description || data.message)) {
                            descEl.textContent = data.description || data.message;
                        }
                        applyCardMetadataUpdate(targetId, {
                            title: data.title,
                            authors: data.authors,
                            publisher: data.publisher,
                            pub_date: data.pub_date,
                            description: data.description || data.message,
                            cover_url: data.cover_url,
                        }, false);
                        return;
                    }
                    const enrBtn = card ? card.querySelector('.btn-enrich-card') : null;
                    const detBtn = card ? card.querySelector('.btn-details') : null;
                    const wasEnriched = Boolean(
                        (enrBtn && enrBtn.getAttribute('data-force') === 'true')
                        || (detBtn && detBtn.getAttribute('data-is-enriched') === 'true')
                    );
                    const wasInReview = Boolean(
                        !wasEnriched && (
                            (enrBtn && (enrBtn._reviewData || enrBtn.hasAttribute('data-review-json')))
                            || (detBtn && detBtn.getAttribute('data-enrich-status'))
                            || window.location.search.includes('needs_review')
                            || window.location.search.includes('in_review')
                        )
                    );
                    adjustLiveEnrichCounters({
                        enrichedDelta: wasEnriched ? -1 : 0,
                        needsReviewDelta: wasInReview ? -1 : 0,
                        needsEnrichingDelta: (!wasEnriched && !wasInReview) ? -1 : 0,
                    });
                    adjustTomagsCounter(1);
                    setCardMovedToMagsState(targetId, data.file_id || fileId, data.moved_files || [], data.message, data);
                } else {
                    btn.disabled = false;
                    btn.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> Move Failed`;
                    btn.title = data.message || data.error || 'Move to toMags failed';
                    alert(data.message || data.error || 'Could not move file to toMags.');
                }
            })
            .catch(err => {
                btn.disabled = false;
                btn.innerHTML = origHtml;
                console.error('Error moving to toMags:', err);
                alert('Network or server error moving file to toMags.');
            });
    }

    function triggerMoveToBooks({ itemId, fileId, btn }) {
        if (!btn) return;
        const origHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Moving...`;

        const payload = {};
        if (fileId) payload.file_id = fileId;
        else if (itemId) payload.item_id = itemId;

        fetch(`${BASE_PREFIX}/api/move-to-book/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify(payload)
        })
            .then(resp => resp.json())
            .then(data => {
                if (data.ok) {
                    const targetId = data.item_id || itemId;
                    const card = targetId ? document.getElementById(`card-${targetId}`) : null;
                    if (card) {
                        card.style.opacity = '1';
                        card.setAttribute('data-top-folder', data.top_folder || 'Misc');
                        const typeBadge = card.querySelector('.card-type-badge');
                        if (typeBadge) {
                            const lbl = data.staged_in_tobooks ? 'BOOK → toBooks' : 'BOOK';
                            const ttl = data.undone_to_origin
                                ? `Undone — restored to /books/${data.top_folder || 'Books'}/`
                                : (data.staged_in_tobooks
                                    ? 'Moved to /toBooks — Click to Undo and move directly back to origin'
                                    : `Moved to /books/${data.top_folder || 'Books'}/ — Click to Undo and move directly back to origin`);
                            updateTypePillAfterMove(
                                typeBadge,
                                'BOOK',
                                Boolean(data.undone_to_origin),
                                data.file_id || fileId || '',
                                lbl,
                                ttl
                            );
                        }
                        const fldLbl = card.querySelector('.card-folder-label');
                        if (fldLbl) {
                            fldLbl.textContent = data.top_folder || 'Books';
                        }
                        const enrBtn = card.querySelector('.btn-enrich-card');
                        if (enrBtn && data.undone_to_origin) {
                            enrBtn.disabled = false;
                            enrBtn.onclick = function (e) {
                                if (e) e.stopPropagation();
                                if (window.enrichCardNow) window.enrichCardNow(enrBtn);
                            };
                            const isEnr = enrBtn.getAttribute('data-force') === 'true';
                            enrBtn.innerHTML = isEnr
                                ? `<i class="fa-solid fa-wand-magic-sparkles"></i> Re-Enrich`
                                : `<i class="fa-solid fa-wand-magic-sparkles"></i> Enrich Now`;
                            enrBtn.style.background = isEnr ? 'rgba(74,222,128,0.14)' : 'rgba(168,85,247,0.18)';
                            enrBtn.style.borderColor = isEnr ? 'rgba(74,222,128,0.4)' : 'rgba(168,85,247,0.45)';
                            enrBtn.style.color = isEnr ? '#4ade80' : '#c084fc';
                        }
                        const descEl = card.querySelector('.card-description');
                        if (descEl && data.message) {
                            descEl.textContent = data.message;
                        }
                        applyCardMetadataUpdate(targetId, {
                            title: data.title,
                            authors: data.authors,
                            publisher: data.publisher,
                            pub_date: data.pub_date,
                            isbn: data.isbn,
                            description: data.description || data.message,
                            cover_url: data.cover_url,
                        }, false);
                    }
                } else {
                    btn.disabled = false;
                    btn.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> Move Failed`;
                    btn.title = data.message || data.error || 'Move to Books failed';
                    alert(data.message || data.error || 'Could not move file to Books.');
                }
            })
            .catch(err => {
                btn.disabled = false;
                btn.innerHTML = origHtml;
                console.error('Error moving to Books:', err);
                alert('Network or server error moving file to Books.');
            });
    }

    function adjustTomagsCounter(delta) {
        if (!delta) return;
        const el = document.getElementById('sidebar-tomags-count');
        if (!el) return;
        const cur = parseInt((el.textContent || '0').replace(/[^0-9-]/g, ''), 10) || 0;
        el.textContent = Math.max(0, cur + delta).toLocaleString();
    }

    function setCardMovedToMagsState(targetId, fileId, movedFiles, message, data = null) {
        const card = targetId ? document.getElementById(`card-${targetId}`) : null;
        if (!card) return;
        if (data && (data.title || data.authors || data.cover_url || data.pub_date || data.description)) {
            applyCardMetadataUpdate(targetId, {
                title: data.title,
                authors: data.authors,
                publisher: data.publisher,
                pub_date: data.pub_date,
                description: data.description || message,
                cover_url: data.cover_url,
            }, false);
        }
        card.style.opacity = '0.96';
        const topFld = (data && data.top_folder) ? data.top_folder : 'Periodicals';
        card.setAttribute('data-top-folder', topFld);
        const fldLbl = card.querySelector('.card-folder-label');
        if (fldLbl && topFld) {
            const ser = (data && data.periodical_series) ? data.periodical_series : '';
            fldLbl.textContent = ser ? `${topFld} › ${ser}` : topFld;
        }
        const typeBadge = card.querySelector('.card-type-badge');
        if (typeBadge) {
            const newPillType = topFld === 'Archive-X' ? 'ARCHIVE-X' : 'MAGAZINE';
            updateTypePillAfterMove(
                typeBadge,
                newPillType,
                false,
                fileId || '',
                newPillType,
                `Enriched & moved to ${topFld} — Click to Undo and move directly back to original folder`
            );
        }
        const descEl = card.querySelector('.card-description');
        if (descEl && ((data && data.description) || message)) {
            descEl.textContent = (data && data.description) || message;
        }
        const reBtn = card.querySelector('.btn-reenrich-card');
        if (reBtn) reBtn.style.display = 'none';

        const enrBtn = card.querySelector('.btn-enrich-card');
        if (enrBtn) {
            enrBtn.disabled = false;
            enrBtn._movedFiles = movedFiles || [];
            enrBtn._movedFileId = fileId || '';
            enrBtn.innerHTML = `<i class="fa-solid fa-rotate-left"></i> Undo &amp; Enrich`;
            enrBtn.style.background = 'rgba(250,204,21,0.2)';
            enrBtn.style.borderColor = 'rgba(250,204,21,0.55)';
            enrBtn.style.color = '#fde047';
            enrBtn.title = 'Restore file(s) from Magazines back to original /books/ folder and run epubmeta Book Enrichment';
            enrBtn.onclick = function (e) {
                if (e) e.stopPropagation();
                window.undoMoveToMag(targetId, enrBtn._movedFileId, enrBtn._movedFiles, enrBtn, true);
            };
        }
    }

    window.undoMoveToMag = function (itemId, fileId, movedFiles, btnEl, enrichAfter) {
        if (!btnEl) return;
        const origHtml = btnEl.innerHTML;
        btnEl.disabled = true;
        btnEl.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> ${enrichAfter ? 'Restoring & Enriching...' : 'Undoing...'}`;

        fetch(`${BASE_PREFIX}/api/undo-to-mag/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({
                item_id: itemId || '',
                file_id: fileId || '',
                moved_files: Array.isArray(movedFiles) ? movedFiles : [],
                enrich: Boolean(enrichAfter)
            })
        })
            .then(resp => resp.json())
            .then(data => {
                if (!data.ok && !data.restored_from_tomags) {
                    btnEl.disabled = false;
                    btnEl.innerHTML = origHtml;
                    alert(data.message || data.error || 'Could not restore file(s) to origin.');
                    return;
                }

                adjustTomagsCounter(-1);
                const targetId = data.item_id || itemId;
                const card = targetId ? document.getElementById(`card-${targetId}`) : null;
                if (card) {
                    card.style.opacity = '1';
                    const topFld = data.top_folder || 'Books';
                    card.setAttribute('data-top-folder', topFld);
                    const restoredType = topFld === 'Archive-X' ? 'ARCHIVE-X' : (data.item_type || 'BOOK').toUpperCase();
                    const typeBadge = card.querySelector('.card-type-badge');
                    if (typeBadge) {
                        updateTypePillAfterMove(
                            typeBadge,
                            restoredType,
                            true,
                            '',
                            restoredType,
                            `Undone — restored directly back to ${topFld}`
                        );
                    }
                    const fldLbl = card.querySelector('.card-folder-label');
                    if (fldLbl && topFld) {
                        const ser = data.periodical_series || '';
                        fldLbl.textContent = ser ? `${topFld} › ${ser}` : topFld;
                    }
                    const descEl = card.querySelector('.card-description');
                    if (descEl && data.message) {
                        descEl.textContent = data.summary || data.description || data.message;
                    }
                    if (data.title || data.authors || data.cover_url) {
                        applyCardMetadataUpdate(targetId, {
                            title: data.title,
                            authors: data.authors,
                            cover_url: data.cover_url,
                        }, false);
                    }
                    const enrBtn = card.querySelector('.btn-enrich-card');
                    if (enrBtn) {
                        enrBtn.disabled = false;
                        enrBtn.onclick = function (e) {
                            if (e) e.stopPropagation();
                            if (window.enrichCardNow) window.enrichCardNow(enrBtn);
                        };
                        if (enrichAfter && data.ok) {
                            adjustLiveEnrichCounters({ enrichedDelta: 1 });
                            enrBtn.innerHTML = `<i class="fa-solid fa-check"></i> Enriched!`;
                            enrBtn.style.background = 'rgba(74,222,128,0.18)';
                            enrBtn.style.borderColor = 'rgba(74,222,128,0.5)';
                            enrBtn.style.color = '#4ade80';
                            enrBtn.setAttribute('data-force', 'true');
                            applyCardMetadataUpdate(targetId, data, false);
                        } else if (enrichAfter && !data.ok) {
                            enrBtn._reviewData = data;
                            enrBtn.innerHTML = `<i class="fa-solid fa-clipboard-question"></i> Review Match`;
                            enrBtn.style.background = 'rgba(251,191,36,0.2)';
                            enrBtn.style.borderColor = 'rgba(251,191,36,0.55)';
                            enrBtn.style.color = '#fbbf24';
                            openEnrichReviewModal(data, enrBtn, false, targetId, fileId);
                        } else {
                            adjustLiveEnrichCounters({ needsEnrichingDelta: 1 });
                            const isEnr = enrBtn.getAttribute('data-force') === 'true';
                            enrBtn.innerHTML = isEnr
                                ? `<i class="fa-solid fa-wand-magic-sparkles"></i> Re-Enrich`
                                : `<i class="fa-solid fa-wand-magic-sparkles"></i> Enrich Now`;
                            enrBtn.style.background = isEnr ? 'rgba(74,222,128,0.14)' : 'rgba(168,85,247,0.18)';
                            enrBtn.style.borderColor = isEnr ? 'rgba(74,222,128,0.4)' : 'rgba(168,85,247,0.45)';
                            enrBtn.style.color = isEnr ? '#4ade80' : '#c084fc';
                        }
                    }
                }
            })
            .catch(err => {
                btnEl.disabled = false;
                btnEl.innerHTML = origHtml;
                console.error('Error restoring from toMags:', err);
                alert('Network error restoring file(s) to origin.');
            });
    };

    let activeReviewCtx = null;

    const AUTHOR_SUFFIXES_SET = new Set([
        'jr', 'sr', 'ii', 'iii', 'iv', 'v', 'phd', 'md', 'ed', 'eds',
        'inc', 'ltd', 'llc', 'plc', 'corp', 'co', 'press', 'staff', 'team'
    ]);
    const SURNAME_PARTICLES_SET = new Set([
        'van', 'von', 'de', 'del', 'della', 'da', 'di', 'le', 'la', 'du',
        'st', 'st.', 'san', 'santa', 'mac', 'mc', 'bin', 'ibn', 'al', 'el'
    ]);
    const ORG_KEYWORDS_SET = new Set([
        'united', 'nations', 'national', 'council', 'committee', 'institute',
        'association', 'society', 'university', 'department', 'ministry',
        'commission', 'foundation', 'corporation', 'board', 'office', 'center',
        'centre', 'group', 'press', 'publishing', 'publications', 'organization',
        'organisation', 'agency', 'bureau', 'library', 'academy', 'college'
    ]);

    function isAuthorSuffix(tok) {
        return AUTHOR_SUFFIXES_SET.has(String(tok || '').toLowerCase().replace(/[^a-z0-9]/g, ''));
    }

    function isOrgSegment(s) {
        return String(s || '').split(/\s+/).some(w => ORG_KEYWORDS_SET.has(w.toLowerCase().replace(/[^a-z0-9]/g, '')));
    }

    function isLikelySurnameToken(s) {
        const clean = String(s || '').trim();
        if (!clean || isOrgSegment(clean)) return false;
        if (!clean.includes(' ')) return true;
        const firstWord = clean.split(/\s+/)[0].toLowerCase().replace(/\.$/, '');
        return SURNAME_PARTICLES_SET.has(firstWord);
    }

    function isLikelyGivenName(s) {
        const clean = String(s || '').trim();
        if (!clean || isAuthorSuffix(clean) || isOrgSegment(clean)) return false;
        const words = clean.split(/\s+/);
        if (words.length === 1) return true;
        if (words.length === 2 && (words[1].replace(/\.$/, '').length === 1 || words[0].replace(/\.$/, '').length === 1)) return true;
        return false;
    }

    function parseAndNormalizeAuthors(authorsStr) {
        const raw = (authorsStr || '').trim();
        if (!raw) return ['Unknown Author'];
        const segments = [];
        raw.split(';').forEach(semi => {
            const s = semi.trim();
            if (!s) return;
            if (isOrgSegment(s)) {
                segments.push(s);
                return;
            }
            s.split(/\s+(?:&|and)\s+/i).forEach(sub => {
                if (sub && sub.trim()) segments.push(sub.trim());
            });
        });

        const result = [];
        segments.forEach(seg => {
            if (isOrgSegment(seg)) {
                result.push(seg);
                return;
            }
            const cParts = seg.split(',').map(p => p.trim()).filter(Boolean);
            if (!cParts.length) return;
            if (cParts.length === 1) {
                result.push(cParts[0]);
                return;
            }
            if (cParts.length === 2) {
                const [p0, p1] = cParts;
                if (isAuthorSuffix(p1)) {
                    result.push(`${p0}, ${p1}`);
                } else if (isLikelySurnameToken(p0) && !isOrgSegment(p1) && p1.split(/\s+/).length <= 3) {
                    result.push(`${p1} ${p0}`);
                } else if (isLikelyGivenName(p1) && !isOrgSegment(p0)) {
                    result.push(`${p1} ${p0}`);
                } else {
                    result.push(p0, p1);
                }
                return;
            }
            let i = 0;
            const n = cParts.length;
            while (i < n) {
                const cur = cParts[i];
                const nxt = i + 1 < n ? cParts[i + 1] : null;
                const nxt2 = i + 2 < n ? cParts[i + 2] : null;
                if (nxt && isAuthorSuffix(nxt)) {
                    result.push(`${cur}, ${nxt}`);
                    i += 2;
                    continue;
                }
                if (nxt && isLikelySurnameToken(cur) && !isOrgSegment(nxt) && nxt.split(/\s+/).length <= 3) {
                    if (nxt2 && isAuthorSuffix(nxt2)) {
                        result.push(`${nxt} ${cur}, ${nxt2}`);
                        i += 3;
                    } else {
                        result.push(`${nxt} ${cur}`);
                        i += 2;
                    }
                    continue;
                }
                result.push(cur);
                i += 1;
            }
        });

        const dedup = [];
        const seen = new Set();
        result.forEach(a => {
            const clean = a.replace(/\s+/g, ' ').replace(/^[\s,;]+|[\s,;]+$/g, '');
            if (clean && !seen.has(clean.toLowerCase())) {
                seen.add(clean.toLowerCase());
                dedup.push(clean);
            }
        });
        return dedup.length ? dedup : [raw];
    }

    function renderAuthorLinksHtml(authorsStr) {
        const list = parseAndNormalizeAuthors(authorsStr);
        return 'by ' + list.map(a =>
            `<a href="?author=${encodeURIComponent(a)}&sort=pubdate_desc" class="author-filter-link" title="Filter by author: ${escapeHtml(a)} (Sorted by Newest First)">${escapeHtml(a)}</a>`
        ).join(', ');
    }

    function applyCardMetadataUpdate(targetId, data, updateModal) {
        let card = targetId ? document.getElementById(`card-${targetId}`) : null;
        if (!card && data && data.item_id) {
            card = document.getElementById(`card-${data.item_id}`);
        }
        if (!card && window._activeModalItem && window._activeModalItem.firstFileId) {
            const wantFid = String(window._activeModalItem.firstFileId);
            const allDetBtns = document.querySelectorAll('.media-card .btn-details[data-files]');
            for (const b of allDetBtns) {
                if ((b.getAttribute('data-files') || '').includes(wantFid)) {
                    card = b.closest('.media-card');
                    break;
                }
            }
        }
        if (card) {
            const detCheck = card.querySelector('.btn-details');
            if (detCheck && detCheck.getAttribute('data-is-bad-metadata') === 'true' && typeof window.applyBadMetadataStateToDom === 'function') {
                window.applyBadMetadataStateToDom(targetId, false, null, -1);
            }
            const titleEl = card.querySelector('.card-title');
            const authorEl = card.querySelector('.card-author');
            const descEl = card.querySelector('.card-description');
            const detailsBtn = card.querySelector('.btn-details');
            if (data.title && titleEl) {
                titleEl.textContent = data.title;
                titleEl.title = data.title;
            }
            if (data.authors && authorEl) {
                authorEl.innerHTML = renderAuthorLinksHtml(data.authors);
                authorEl.title = `by ${data.authors}`;
            }
            if (data.pub_date) {
                const pubDateWrap = card.querySelector('.card-pubdate');
                if (pubDateWrap) {
                    let pill = pubDateWrap.querySelector('.pubdate-pill');
                    if (!pill) {
                        pill = document.createElement('span');
                        pill.className = 'pubdate-pill has-date';
                        pubDateWrap.insertBefore(pill, pubDateWrap.firstChild);
                    } else {
                        pill.className = 'pubdate-pill has-date';
                    }
                    pill.title = `Published Date: ${data.pub_date}`;
                    pill.innerHTML = `<i class="fa-regular fa-calendar"></i> ${escapeHtml(data.pub_date)}`;
                }
            }
            if (data.description && descEl) {
                descEl.textContent = data.description;
            }
            if (data.cover_url) {
                const headerImg = card.querySelector('.card-header-image');
                let imgEl = headerImg ? headerImg.querySelector('.cover-image') : null;
                const fallbackEl = headerImg ? headerImg.querySelector('.card-icon-fallback') : null;
                if (imgEl) {
                    imgEl.src = data.cover_url;
                } else if (headerImg) {
                    if (fallbackEl) fallbackEl.style.display = 'none';
                    imgEl = document.createElement('img');
                    imgEl.className = 'cover-image';
                    imgEl.src = data.cover_url;
                    headerImg.insertBefore(imgEl, headerImg.firstChild);
                }
            }
            if (detailsBtn) {
                if (data.title) detailsBtn.setAttribute('data-title', data.title);
                if (data.authors) detailsBtn.setAttribute('data-authors', data.authors);
                if (data.description) detailsBtn.setAttribute('data-desc', data.description);
                if (data.isbn) detailsBtn.setAttribute('data-isbn', data.isbn);
                if (data.publisher) detailsBtn.setAttribute('data-publisher', data.publisher);
                if (data.pub_date) detailsBtn.setAttribute('data-pubdate', data.pub_date);
                if (data.cover_url) detailsBtn.setAttribute('data-cover', data.cover_url);
                detailsBtn.setAttribute('data-is-enriched', 'true');
                try {
                    const fArr = JSON.parse(detailsBtn.getAttribute('data-files') || '[]');
                    fArr.forEach(f => {
                        if (!data.file_id || String(f.id) === String(data.file_id) || ['EPUB', 'PDF'].includes((f.file_format || '').toUpperCase())) {
                            f.is_enriched = true;
                            if (Array.isArray(data.tags) && data.tags.length > 0) {
                                f.tags = data.tags;
                            }
                        }
                    });
                    detailsBtn.setAttribute('data-files', JSON.stringify(fArr));
                } catch (e) { }
            }
            if (data.remaining_files_count !== undefined && Number(data.remaining_files_count) <= 2) {
                const curList = card.querySelector('.card-files-list');
                if (curList) {
                    Array.from(curList.querySelectorAll('li.text-secondary')).forEach(li => li.remove());
                    const fileItems = Array.from(curList.querySelectorAll('.file-link-item'));
                    while (fileItems.length > Number(data.remaining_files_count)) {
                        const rem = fileItems.pop();
                        if (rem) rem.remove();
                    }
                }
            }
            const cardEnrBtn = card.querySelector('.btn-enrich-card');
            if (cardEnrBtn) {
                cardEnrBtn.innerHTML = `<i class="fa-solid fa-check"></i> Enriched!`;
                cardEnrBtn.style.background = 'rgba(74,222,128,0.18)';
                cardEnrBtn.style.borderColor = 'rgba(74,222,128,0.5)';
                cardEnrBtn.style.color = '#4ade80';
                cardEnrBtn.setAttribute('data-force', 'true');
            }
            const cardReEnrBtn = card.querySelector('.btn-reenrich-card');
            if (cardReEnrBtn) {
                cardReEnrBtn.style.display = 'none';
            }
            // If backend merged duplicate CatalogItems into this card, remove the duplicate card(s) from the live DOM and combine their files
            const mergedIds = Array.isArray(data.merged_item_ids) ? data.merged_item_ids.map(String) : [];
            if (data.item_id && String(data.item_id) !== String(targetId)) {
                card.id = `card-${data.item_id}`;
                card.querySelectorAll('[data-item-id]').forEach(el => el.setAttribute('data-item-id', data.item_id));
            }
            if (window._activeModalItem && mergedIds.includes(String(window._activeModalItem.itemId))) {
                window._activeModalItem.itemId = data.item_id || targetId;
            }
            mergedIds.forEach(dupId => {
                if (!dupId || String(dupId) === String(targetId)) return;
                const dupCard = document.getElementById(`card-${dupId}`);
                const dupBucket = dupCard ? (dupCard.getAttribute('data-enrich-bucket') || 'needs_enriching') : 'needs_enriching';
                if (dupBucket === 'needs_review') {
                    adjustLiveEnrichCounters({ needsReviewDelta: -1 });
                } else if (dupBucket === 'enriched') {
                    adjustLiveEnrichCounters({ enrichedDelta: -1 });
                } else if (dupBucket === 'needs_enriching') {
                    adjustLiveEnrichCounters({ needsEnrichingDelta: -1 });
                }
                if (!dupCard) return;
                try {
                    const dupDetBtn = dupCard.querySelector('.btn-details');
                    if (dupDetBtn && detailsBtn) {
                        const curFiles = JSON.parse(detailsBtn.getAttribute('data-files') || '[]');
                        const dupFiles = JSON.parse(dupDetBtn.getAttribute('data-files') || '[]');
                        const seenPaths = new Set(curFiles.map(f => f.path || f.id));
                        dupFiles.forEach(df => {
                            const key = df.path || df.id;
                            if (key && !seenPaths.has(key)) {
                                curFiles.push(df);
                                seenPaths.add(key);
                            }
                        });
                        detailsBtn.setAttribute('data-files', JSON.stringify(curFiles));
                    }
                    const curList = card.querySelector('.card-files-list');
                    const dupList = dupCard.querySelector('.card-files-list');
                    if (curList && dupList) {
                        Array.from(dupList.querySelectorAll('.file-link-item')).forEach(li => {
                            if (curList.querySelectorAll('.file-link-item').length < 2) {
                                curList.appendChild(li.cloneNode(true));
                            }
                        });
                    }
                } catch (e) { }
                dupCard.remove();
            });
        }
        if (updateModal) {
            if (window._activeModalItem) {
                if (data.item_id) window._activeModalItem.itemId = data.item_id;
                if (data.title) window._activeModalItem.title = data.title;
                if (data.authors) window._activeModalItem.authors = data.authors;
                if (data.isbn) window._activeModalItem.isbn = data.isbn;
                window._activeModalItem.isEnriched = true;
                const covInput = document.getElementById('cover-search-input');
                if (covInput) {
                    const cleanAuth = (window._activeModalItem.authors && !['unknown', 'unknown author', '..'].includes(window._activeModalItem.authors.toLowerCase().trim()))
                        ? window._activeModalItem.authors : '';
                    covInput.value = [window._activeModalItem.title, cleanAuth].filter(Boolean).join(' ').trim();
                }
            }
            if (data.title) document.getElementById('modal-title').textContent = data.title;
            if (data.authors) document.getElementById('modal-authors').innerHTML = renderAuthorLinksHtml(data.authors);
            if (data.description) document.getElementById('modal-desc').innerHTML = escapeHtml(data.description).replace(/\n/g, '<br>');
            if (data.cover_url) {
                const coverImg = document.getElementById('modal-cover');
                const coverFallback = document.getElementById('modal-cover-fallback');
                if (coverImg) {
                    coverImg.src = data.cover_url;
                    coverImg.style.display = 'block';
                }
                if (coverFallback) coverFallback.style.display = 'none';
            }
            const topEnrBtn = document.getElementById('modal-top-enrich-btn');
            if (topEnrBtn) {
                topEnrBtn.style.cssText = 'padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(74,222,128,0.16); border:1px solid rgba(74,222,128,0.45); color:#4ade80;';
                topEnrBtn.innerHTML = '<i class="fa-solid fa-check"></i> Enriched — Re-Enrich (epubmeta)';
            }
            const topRevBtn = document.getElementById('modal-top-revert-btn');
            if (topRevBtn) {
                topRevBtn.style.display = 'inline-flex';
            }
            if (Array.isArray(data.tags) && data.tags.length > 0) {
                const bottomTagsEl = document.getElementById('modal-bottom-tags-list');
                if (bottomTagsEl && typeof window._renderReadOnlyTagsHtml === 'function') {
                    bottomTagsEl.innerHTML = window._renderReadOnlyTagsHtml(data.tags);
                }
            }
            // Update file rows inside #modal-files-list to reflect Enriched status and tags
            document.querySelectorAll('#modal-files-list .modal-file-item').forEach(li => {
                const fnSpan = li.querySelector('.modal-file-name');
                if (fnSpan && !fnSpan.querySelector('.file-enriched-badge')) {
                    fnSpan.insertAdjacentHTML('beforeend', ` <span class="modal-badge file-enriched-badge" style="background:rgba(74,222,128,0.16); border-color:rgba(74,222,128,0.45); color:#4ade80; padding:0.15rem 0.45rem; font-size:0.7rem; margin-left:0.5rem;"><i class="fa-solid fa-check" style="margin-right:0.2rem;"></i>Enriched</span>`);
                }
                const fBtn = li.querySelector('.btn-enrich-file');
                if (fBtn) {
                    fBtn.style.background = 'rgba(74,222,128,0.16)';
                    fBtn.style.borderColor = 'rgba(74,222,128,0.45)';
                    fBtn.style.color = '#4ade80';
                    fBtn.innerHTML = '<i class="fa-solid fa-check"></i> Enriched (Re-run)';
                    fBtn.setAttribute('data-force', 'true');
                }
                const rBtn = li.querySelector('.btn-revert-file');
                if (rBtn) {
                    rBtn.style.display = 'inline-flex';
                }
                if (Array.isArray(data.tags) && data.tags.length > 0) {
                    const fTagsRow = li.querySelector('.file-tags-row');
                    if (fTagsRow) {
                        fTagsRow.style.cssText = 'display:flex; flex-wrap:wrap; align-items:center; gap:0.3rem; margin-top:0.35rem; font-size:0.73rem; color:#94a3b8;';
                        fTagsRow.innerHTML = `<span><i class="fa-solid fa-tags" style="color:#38bdf8;"></i> File Tags:</span>` +
                            data.tags.map(t => `<span style="background:rgba(56,189,248,0.12); border:1px solid rgba(56,189,248,0.32); color:#cbd5e1; border-radius:999px; padding:0.08rem 0.45rem; font-size:0.69rem; user-select:text;">${escapeHtml(t)}</span>`).join('');
                    }
                }
            });
        }
    }

    window.toggleCoverFinder = function () {
        const drawer = document.getElementById('modal-cover-finder');
        if (!drawer) return;
        if (drawer.style.display === 'none' || !drawer.style.display) {
            drawer.style.display = 'block';
            const resGrid = document.getElementById('cover-finder-results');
            if (resGrid && resGrid.children.length === 0) {
                window.runWebCoverSearch();
            }
        } else {
            drawer.style.display = 'none';
        }
    };

    window.runWebCoverSearch = function (isRetry = false) {
        const ctx = window._activeModalItem || {};
        const covInput = document.getElementById('cover-search-input');
        const statusEl = document.getElementById('cover-finder-status');
        const resultsEl = document.getElementById('cover-finder-results');
        const searchBtn = document.getElementById('btn-run-cover-search');
        if (!resultsEl) return;

        if (window._activeCoverSearchAbort) {
            try { window._activeCoverSearchAbort.abort(); } catch (_) {}
            window._activeCoverSearchAbort = null;
        }
        const ctrl = new AbortController();
        window._activeCoverSearchAbort = ctrl;
        const timeoutId = setTimeout(() => {
            try { ctrl.abort(); } catch (_) {}
        }, 16000);

        const q = (covInput ? covInput.value : '').trim();
        if (statusEl) statusEl.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Searching Web Images, Goodreads, Apple Books, Google Books &amp; Open Library for covers...`;
        resultsEl.innerHTML = '';
        if (searchBtn) searchBtn.disabled = true;

        fetch(`${BASE_PREFIX}/api/cover-search/`, {
            method: 'POST',
            signal: ctrl.signal,
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({
                query: q,
                title: ctx.title || '',
                authors: ctx.authors || '',
                isbn: ctx.isbn || '',
                item_id: ctx.itemId || '',
            })
        })
            .then(r => {
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                return r.json();
            })
            .then(data => {
                clearTimeout(timeoutId);
                if (searchBtn) searchBtn.disabled = false;
                const covers = data.covers || [];
                window._lastCoverSearchCandidates = covers;
                if (covers.length === 0) {
                    if (statusEl) statusEl.textContent = 'No covers found for that query. Try editing the title or author above and click Search Web.';
                    return;
                }
                if (statusEl) statusEl.textContent = `Found ${covers.length} cover(s) across Web Images & Book Catalogs — click any cover below to apply it immediately:`;
                resultsEl.innerHTML = '';
                covers.forEach((c, idx) => {
                    const isOrig = c.source === 'Original Cover';
                    const isGen = Boolean(c.is_generated || (c.source && c.source.startsWith('🎨')));
                    const isColorMatch = Boolean(c.same_cover_in_color);
                    const card = document.createElement('div');
                    card.className = 'cover-cand-card';
                    card._candData = Object.assign({}, c, { candidate_index: idx });
                    if (isColorMatch) {
                        card.style.borderColor = '#f59e0b';
                        card.style.boxShadow = '0 0 0 2px rgba(245, 158, 11, 0.55)';
                    } else if (isOrig) {
                        card.style.borderColor = '#4ade80';
                        card.style.boxShadow = '0 0 0 1px rgba(74, 222, 128, 0.45)';
                    } else if (isGen) {
                        card.style.borderColor = '#c084fc';
                        card.style.boxShadow = '0 0 0 1px rgba(192, 132, 252, 0.45)';
                    }
                    const badgeColor = isColorMatch ? '#fbbf24' : (isOrig ? '#4ade80' : (isGen ? '#e9d5ff' : '#38bdf8'));
                    card.title = `${c.title} (${c.source}) — Click to apply this cover (or click 🔍 to zoom)`;
                    card.innerHTML = `
                    <button type="button" class="cover-cand-zoom-btn" title="Zoom in &amp; inspect this cover full-screen"><i class="fa-solid fa-magnifying-glass-plus"></i></button>
                    <img src="${escapeHtml(c.cover_url)}" data-fallback="${escapeHtml(c.fallback_url || '')}" alt="Cover candidate" loading="lazy" onerror="if(this.dataset.fallback && this.src !== this.dataset.fallback){this.src=this.dataset.fallback;}else{this.closest('.cover-cand-card').style.display='none';}">
                    <div class="cover-cand-meta">
                        <div style="font-weight:700; color:${badgeColor}; font-size:0.64rem; text-transform:uppercase;">${isOrig && !isColorMatch ? '★ ' : ''}${escapeHtml(c.source)}</div>
                        <div style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-weight:600;" title="${escapeHtml(c.title)}">${escapeHtml(c.title)}</div>
                        <div style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#94a3b8;">${escapeHtml(c.authors)}</div>
                    </div>
                `;
                    const zBtn = card.querySelector('.cover-cand-zoom-btn');
                    if (zBtn) {
                        zBtn.addEventListener('click', ev => {
                            ev.stopPropagation();
                            const imgEl = card.querySelector('img');
                            const activeUrl = (imgEl && imgEl.src) ? imgEl.src : c.cover_url;
                            window.openCoverZoomLightbox(activeUrl, `${c.title || 'Book Cover'} (${c.source})`, () => {
                                window.applyWebCover(activeUrl, card, c.fallback_url || '');
                            });
                        });
                    }
                    card.addEventListener('click', () => window.applyWebCover(c.cover_url, card, c.fallback_url || ''));
                    resultsEl.appendChild(card);
                });
            })
            .catch(err => {
                clearTimeout(timeoutId);
                if (searchBtn) searchBtn.disabled = false;
                if (err && err.name === 'AbortError') {
                    if (!isRetry) {
                        window.runWebCoverSearch(true);
                        return;
                    }
                    if (statusEl) statusEl.innerHTML = `Cover search timed out. <button type="button" onclick="window.runWebCoverSearch()" style="margin-left:6px; background:rgba(56,189,248,0.2); border:1px solid rgba(56,189,248,0.5); color:#38bdf8; border-radius:5px; padding:2px 8px; cursor:pointer; font-size:0.75rem;">Retry Search</button>`;
                    return;
                }
                if (statusEl) statusEl.textContent = 'Error searching covers: ' + err.message;
            });
    };

    window.applyWebCover = function (coverUrl, cardEl, fallbackUrl = '') {
        const ctx = window._activeModalItem || {};
        if (!ctx.itemId) return;
        const statusEl = document.getElementById('cover-finder-status');
        if (statusEl) statusEl.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Downloading and saving selected cover...`;
        document.querySelectorAll('#cover-finder-results .cover-cand-card').forEach(el => {
            el.style.borderColor = 'rgba(255,255,255,0.12)';
            el.style.boxShadow = 'none';
        });
        if (cardEl) {
            cardEl.style.borderColor = '#4ade80';
            cardEl.style.boxShadow = '0 0 0 2px rgba(74, 222, 128, 0.5)';
        }

        const covInput = document.getElementById('cover-search-input');
        const searchQuery = covInput ? covInput.value.trim() : '';

        fetch(`${BASE_PREFIX}/api/cover-apply/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({
                item_id: ctx.itemId,
                file_id: ctx.firstFileId || '',
                file_path: ctx.firstFilePath || '',
                isbn: ctx.isbn || '',
                title: ctx.title || '',
                authors: ctx.authors || '',
                query: searchQuery,
                cover_url: coverUrl,
                fallback_url: fallbackUrl || '',
                selected_candidate: (cardEl && cardEl._candData) || null,
                all_candidates: window._lastCoverSearchCandidates || [],
            })
        })
            .then(async r => {
                const txt = await r.text();
                try {
                    return JSON.parse(txt);
                } catch (_) {
                    throw new Error(`Server returned HTTP ${r.status}`);
                }
            })
            .then(res => {
                if (res.ok) {
                    const finalCover = res.cover_url || coverUrl;
                    const origItemId = ctx.itemId;
                    const resolvedId = res.item_id || origItemId;
                    const updPayload = { item_id: resolvedId, cover_url: finalCover };
                    if (res.title && (!ctx.title || /\(\d+\)$|\.pdf$|\.epub$/i.test(ctx.title))) {
                        updPayload.title = res.title;
                    }
                    if (res.authors && res.authors !== 'Unknown Author' && (!ctx.authors || ctx.authors === 'Unknown Author')) {
                        updPayload.authors = res.authors;
                    }
                    if (window._activeModalItem) {
                        window._activeModalItem.cover = finalCover;
                        if (res.item_id) window._activeModalItem.itemId = res.item_id;
                        if (updPayload.title) window._activeModalItem.title = updPayload.title;
                        if (updPayload.authors) window._activeModalItem.authors = updPayload.authors;
                    }
                    applyCardMetadataUpdate(origItemId, updPayload, true);
                    if (resolvedId && origItemId && resolvedId !== origItemId) {
                        applyCardMetadataUpdate(resolvedId, updPayload, false);
                    }
                    const cardDom = document.getElementById(`card-${resolvedId}`) || document.getElementById(`card-${origItemId}`);
                    if (cardDom) {
                        const issBadge = cardDom.querySelector('.card-cover-issue-badge');
                        if (issBadge) issBadge.remove();
                        const covActRow = cardDom.querySelector('.card-cover-actions-row');
                        if (covActRow) covActRow.remove();
                    }
                    if (res.missing_cover_count !== undefined) {
                        const cntEl = document.getElementById('sidebar-missing-cover-count');
                        if (cntEl) cntEl.textContent = Number(res.missing_cover_count).toLocaleString();
                    }
                    if (statusEl) statusEl.innerHTML = `<span style="color:#4ade80; font-weight:600;"><i class="fa-solid fa-check"></i> Cover updated and saved to BookDB!</span>`;
                } else {
                    if (statusEl) statusEl.innerHTML = `<span style="color:#f87171;">Could not apply cover: ${escapeHtml(res.error || 'Unknown error')}</span>`;
                }
            })
            .catch(err => {
                if (statusEl) statusEl.textContent = 'Network error saving cover: ' + err.message;
            });
    };

    window.generateBookCover = function (mode = 'modal') {
        const isReview = mode === 'review';
        const statusEl = document.getElementById(isReview ? 'er-cover-finder-status' : 'cover-finder-status');
        const resultsEl = document.getElementById(isReview ? 'er-cover-finder-results' : 'cover-finder-results');
        const genBtn = document.getElementById(isReview ? 'er-btn-generate-cover' : 'btn-generate-cover');
        const inputEl = document.getElementById(isReview ? 'er-cover-search-input' : 'cover-search-input');

        let payload = { action: 'generate' };
        if (isReview) {
            if (!activeReviewCtx || !activeReviewCtx.data) return;
            const d = activeReviewCtx.data;
            payload.item_id = activeReviewCtx.itemId || d.item_id || '';
            payload.file_id = activeReviewCtx.fileId || d.file_id || '';
            payload.file_path = d.file_path || '';
            payload.isbn = d.candidate_isbn || d.orig_isbn || '';
            payload.title = d.candidate_title || d.orig_title || '';
            payload.authors = d.candidate_authors || d.orig_authors || '';
            payload.publisher = d.candidate_publisher || d.orig_publisher || '';
            payload.description = d.candidate_description || d.orig_description || '';
            payload.query = inputEl ? inputEl.value.trim() : '';
        } else {
            const ctx = window._activeModalItem || {};
            const fallbackTitle = (document.getElementById('modal-title')?.textContent || '').trim();
            const fallbackAuthors = (document.getElementById('modal-authors')?.textContent || '').trim();
            payload.item_id = ctx.itemId || '';
            payload.file_id = ctx.firstFileId || '';
            payload.file_path = ctx.firstFilePath || '';
            payload.isbn = ctx.isbn || '';
            payload.title = ctx.title || fallbackTitle || '';
            payload.authors = ctx.authors || fallbackAuthors || '';
            payload.publisher = ctx.publisher || '';
            payload.description = ctx.description || '';
            payload.query = inputEl ? inputEl.value.trim() : '';
        }

        if (genBtn) genBtn.disabled = true;
        if (statusEl) {
            statusEl.innerHTML = `<span style="color:#e9d5ff;"><i class="fa-solid fa-wand-magic-sparkles fa-spin"></i> Designing &amp; rendering new custom book cover from description, opening pages &amp; tags...</span>`;
        }

        fetch(`${BASE_PREFIX}/api/cover-generate/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify(payload)
        })
            .then(async r => {
                const txt = await r.text();
                try {
                    return JSON.parse(txt);
                } catch (_) {
                    throw new Error(`Server returned HTTP ${r.status}`);
                }
            })
            .then(res => {
                if (genBtn) genBtn.disabled = false;
                if (!res.ok) {
                    if (statusEl) {
                        statusEl.innerHTML = `<span style="color:#f87171;">Could not generate cover: ${escapeHtml(res.error || 'Unknown error')}</span>`;
                    }
                    return;
                }
                const genCoverUrl = res.cover_url;
                const resolvedId = res.item_id || payload.item_id;
                const styleLabel = res.style_label || `🎨 Generated #${res.gen_count || 1}`;
                const cardTitle = res.title || payload.title || 'Generated Cover';
                const cardAuthors = res.authors || payload.authors || '';

                if (resultsEl && genCoverUrl) {
                    resultsEl.querySelectorAll('.cover-cand-card').forEach(el => {
                        if (el.dataset.isOrig === '1') {
                            el.style.borderColor = '#4ade80';
                            el.style.boxShadow = '0 0 0 1px rgba(74, 222, 128, 0.45)';
                        } else if (el.dataset.isGen === '1') {
                            el.style.borderColor = '#c084fc';
                            el.style.boxShadow = '0 0 0 1px rgba(192, 132, 252, 0.35)';
                        } else {
                            el.style.borderColor = 'rgba(255,255,255,0.12)';
                            el.style.boxShadow = 'none';
                        }
                    });

                    const card = document.createElement('div');
                    card.className = 'cover-cand-card';
                    card.dataset.isGen = '1';
                    card.style.borderColor = '#4ade80';
                    card.style.boxShadow = '0 0 0 2px rgba(74, 222, 128, 0.55)';
                    card.title = `${cardTitle} (${styleLabel}) — Saved in BookDB. Click to apply this cover (or click 🔍 to zoom)`;
                    card.innerHTML = `
                        <button type="button" class="cover-cand-zoom-btn" title="Zoom in &amp; inspect this cover full-screen"><i class="fa-solid fa-magnifying-glass-plus"></i></button>
                        <img src="${escapeHtml(genCoverUrl)}" alt="Generated cover" loading="lazy">
                        <div class="cover-cand-meta">
                            <div style="font-weight:700; color:#e9d5ff; font-size:0.64rem; text-transform:uppercase;">${escapeHtml(styleLabel)}</div>
                            <div style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-weight:600;" title="${escapeHtml(cardTitle)}">${escapeHtml(cardTitle)}</div>
                            <div style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#94a3b8;">${escapeHtml(cardAuthors)}</div>
                        </div>
                    `;
                    const zBtn = card.querySelector('.cover-cand-zoom-btn');
                    if (zBtn) {
                        zBtn.addEventListener('click', ev => {
                            ev.stopPropagation();
                            window.openCoverZoomLightbox(genCoverUrl, `${cardTitle} (${styleLabel})`, () => {
                                if (isReview) {
                                    window.erSelectWebCover(genCoverUrl, '', styleLabel, card);
                                } else {
                                    window.applyWebCover(genCoverUrl, card, '');
                                }
                            });
                        });
                    }
                    if (isReview) {
                        card.addEventListener('click', () => {
                            window.erSelectWebCover(genCoverUrl, '', styleLabel, card);
                        });
                    } else {
                        card.addEventListener('click', () => {
                            window.applyWebCover(genCoverUrl, card, '');
                        });
                    }

                    // Insert right after Original Cover if present, or at the very beginning
                    const firstCard = resultsEl.firstElementChild;
                    const firstMeta = firstCard ? firstCard.querySelector('.cover-cand-meta div') : null;
                    if (firstCard && firstMeta && firstMeta.textContent.includes('Original Cover')) {
                        resultsEl.insertBefore(card, firstCard.nextSibling);
                    } else {
                        resultsEl.insertBefore(card, firstCard);
                    }
                }

                if (isReview) {
                    if (activeReviewCtx && activeReviewCtx.data) {
                        const d = activeReviewCtx.data;
                        if (res.item_id) {
                            activeReviewCtx.itemId = res.item_id;
                            d.item_id = res.item_id;
                        }
                        d.candidate_cover = genCoverUrl;
                        d.UseDataUriCover = genCoverUrl;
                        const candCoverRow = document.getElementById('er-field-cover-cand');
                        if (candCoverRow) {
                            const leftWrap = candCoverRow.querySelector('div');
                            if (leftWrap) {
                                leftWrap.innerHTML = `
                                    <img src="${escapeHtml(genCoverUrl)}" alt="Cover" style="width:42px; height:60px; object-fit:cover; border-radius:4px; background:#020617; flex-shrink:0;">
                                    <div>
                                        <span style="font-size:0.68rem; color:#94a3b8; text-transform:uppercase; display:block;">Select Cover (Candidate)</span>
                                        <span id="er-cover-lbl-cand" style="font-size:0.76rem; color:#e9d5ff; font-weight:600;">${escapeHtml(styleLabel)}</span>
                                    </div>
                                `;
                            }
                        }
                        window.erPickField('cover', 'cand', true);
                    }
                    if (resolvedId) {
                        applyCardMetadataUpdate(resolvedId, { cover_url: genCoverUrl }, true);
                    }
                } else {
                    const ctx = window._activeModalItem || {};
                    const origItemId = ctx.itemId || payload.item_id;
                    const updPayload = { item_id: resolvedId, cover_url: genCoverUrl };
                    if (res.metadata_updated || (res.title && (!ctx.title || /\(\d+\)$|\.pdf$|\.epub$/i.test(ctx.title)))) {
                        if (res.title) updPayload.title = res.title;
                    }
                    if (res.metadata_updated || (res.authors && res.authors !== 'Unknown Author' && (!ctx.authors || ctx.authors === 'Unknown Author'))) {
                        if (res.authors) updPayload.authors = res.authors;
                    }
                    if (res.metadata_updated && res.description) {
                        updPayload.description = res.description;
                    }
                    if (window._activeModalItem) {
                        window._activeModalItem.cover = genCoverUrl;
                        if (res.item_id) window._activeModalItem.itemId = res.item_id;
                        if (updPayload.title) window._activeModalItem.title = updPayload.title;
                        if (updPayload.authors) window._activeModalItem.authors = updPayload.authors;
                    }
                    applyCardMetadataUpdate(origItemId || resolvedId, updPayload, true);
                    if (resolvedId && origItemId && resolvedId !== origItemId) {
                        applyCardMetadataUpdate(resolvedId, updPayload, false);
                    }
                    const cardDom = document.getElementById(`card-${resolvedId}`) || document.getElementById(`card-${origItemId}`);
                    if (cardDom) {
                        const issBadge = cardDom.querySelector('.card-cover-issue-badge');
                        if (issBadge) issBadge.remove();
                        const covActRow = cardDom.querySelector('.card-cover-actions-row');
                        if (covActRow) covActRow.remove();
                    }
                }

                if (res.missing_cover_count !== undefined) {
                    const cntEl = document.getElementById('sidebar-missing-cover-count');
                    if (cntEl) cntEl.textContent = Number(res.missing_cover_count).toLocaleString();
                }

                if (statusEl) {
                    statusEl.innerHTML = `<span style="color:#4ade80; font-weight:600;"><i class="fa-solid fa-check"></i> Generated &amp; saved <strong style="color:#e9d5ff;">${escapeHtml(styleLabel)}</strong> to BookDB!</span> <span style="color:#94a3b8;">(Click <b>🎨 Generate Cover</b> again to add another variation, or <b>↩ Original Cover</b> to revert)</span>`;
                }
            })
            .catch(err => {
                if (genBtn) genBtn.disabled = false;
                if (statusEl) statusEl.textContent = 'Network error generating cover: ' + err.message;
            });
    };

    function updateItemDescriptionInDom(targetId, newDesc) {
        if (!newDesc) return;
        const card = targetId ? document.getElementById(`card-${targetId}`) : null;
        if (card) {
            const descEl = card.querySelector('.card-description');
            if (descEl) descEl.textContent = newDesc;
            const detBtn = card.querySelector('.btn-details');
            if (detBtn) {
                detBtn.setAttribute('data-desc', newDesc);
                if (detBtn._enrichResultData) {
                    detBtn._enrichResultData.candidate_description = newDesc;
                }
            }
            const enrBtn = card.querySelector('.btn-enrich-card');
            if (enrBtn && enrBtn._reviewData) {
                enrBtn._reviewData.candidate_description = newDesc;
            }
            const aiRow = card.querySelector('.card-ai-desc-row');
            if (aiRow) {
                const aiCardBtn = aiRow.querySelector('.btn-ai-desc-card');
                if (aiCardBtn) {
                    aiCardBtn.disabled = false;
                    aiCardBtn.innerHTML = `<i class="fa-solid fa-check"></i> AI Description Saved`;
                    aiCardBtn.style.background = 'rgba(74,222,128,0.18)';
                    aiCardBtn.style.borderColor = 'rgba(74,222,128,0.5)';
                    aiCardBtn.style.color = '#4ade80';
                }
            }
        }
        if (window._activeModalItem && (!targetId || String(window._activeModalItem.itemId) === String(targetId))) {
            window._activeModalItem.desc = newDesc;
            const modalDescEl = document.getElementById('modal-desc');
            if (modalDescEl) {
                modalDescEl.innerHTML = escapeHtml(newDesc).replace(/\n/g, '<br>');
            }
            const errAiBtn = document.getElementById('modal-err-ai-desc-btn');
            if (errAiBtn) errAiBtn.style.display = 'none';
            const modalAiBtn = document.getElementById('modal-btn-ai-desc');
            if (modalAiBtn) {
                modalAiBtn.disabled = false;
                modalAiBtn.innerHTML = `<i class="fa-solid fa-arrows-rotate"></i> Regenerate AI Description`;
            }
            const modalAiSt = document.getElementById('modal-ai-desc-status');
            if (modalAiSt) {
                modalAiSt.style.display = 'inline';
                modalAiSt.innerHTML = `<i class="fa-solid fa-check"></i> Generated from content &amp; saved`;
            }
        }
    }

    window.generateAiDescriptionForItem = function ({ itemId = '', fileId = '', filePath = '', mode = 'modal', btn = null } = {}) {
        const isReview = mode === 'review';
        const isCard = mode === 'card';
        const revData = (isReview && activeReviewCtx && activeReviewCtx.data) ? activeReviewCtx.data : null;
        const activeModal = (!isReview && !isCard && window._activeModalItem) ? window._activeModalItem : {};

        let targetItemId = itemId || (revData ? (revData.item_id || activeReviewCtx.itemId || '') : (activeModal.itemId || ''));
        let targetFileId = fileId || (revData ? (revData.file_id || activeReviewCtx.fileId || '') : (activeModal.firstFileId || ''));
        let targetFilePath = filePath || (revData ? (revData.file_path || '') : (activeModal.firstFilePath || '')) || (isReview ? (document.getElementById('er-file-path')?.textContent || '').trim() : '');
        let titleHint = '';
        let authorsHint = '';
        let pubHint = '';
        let isbnHint = '';

        if (isReview) {
            titleHint = (document.getElementById('er-input-title')?.value || (revData && (revData.orig_title || revData.candidate_title)) || '').trim();
            authorsHint = (document.getElementById('er-input-author')?.value || (revData && (revData.orig_authors || revData.candidate_authors)) || '').trim();
            pubHint = (document.getElementById('er-input-publisher')?.value || (revData && (revData.orig_publisher || revData.candidate_publisher)) || '').trim();
            isbnHint = (document.getElementById('er-input-isbn')?.value || (revData && (revData.orig_isbn || revData.candidate_isbn)) || '').trim();
        } else if (mode === 'card' && targetItemId) {
            const cardEl = document.getElementById(`card-${targetItemId}`);
            const detBtn = cardEl ? cardEl.querySelector('.btn-details') : null;
            if (detBtn) {
                titleHint = detBtn.getAttribute('data-title') || '';
                authorsHint = detBtn.getAttribute('data-authors') || '';
                pubHint = detBtn.getAttribute('data-publisher') || '';
                isbnHint = detBtn.getAttribute('data-isbn') || '';
                try {
                    const fArr = JSON.parse(detBtn.getAttribute('data-files') || '[]');
                    if (fArr.length > 0) {
                        if (!targetFileId && fArr[0].id) targetFileId = fArr[0].id;
                        if (!targetFilePath && fArr[0].file_path) targetFilePath = fArr[0].file_path;
                    }
                } catch (e) { }
            }
        } else {
            titleHint = activeModal.title || '';
            authorsHint = activeModal.authors || '';
            pubHint = activeModal.publisher || '';
            isbnHint = activeModal.isbn || '';
        }

        const origBtnHtml = btn ? btn.innerHTML : '';
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Reading Book &amp; Generating...`;
        }
        const statusSpan = document.getElementById(isReview ? 'er-ai-desc-status' : 'modal-ai-desc-status');
        if (statusSpan && mode !== 'card') {
            statusSpan.style.display = 'inline';
            statusSpan.style.color = '#d8b4fe';
            statusSpan.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Analyzing book content with Gemini AI...`;
        }

        fetch(`${BASE_PREFIX}/api/ai-description/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({
                item_id: targetItemId,
                file_id: targetFileId,
                file_path: targetFilePath,
                title: titleHint,
                authors: authorsHint,
                publisher: pubHint,
                isbn: isbnHint,
                apply_immediately: true,
            })
        })
            .then(async r => {
                const txt = await r.text();
                try {
                    return JSON.parse(txt);
                } catch (_) {
                    throw new Error(`Server returned HTTP ${r.status}`);
                }
            })
            .then(res => {
                if (!res || !res.ok || !res.description) {
                    if (btn) {
                        btn.disabled = false;
                        btn.innerHTML = origBtnHtml;
                    }
                    const errMsg = (res && res.error) || 'Could not generate AI description from book content.';
                    if (statusSpan && mode !== 'card') {
                        statusSpan.style.display = 'inline';
                        statusSpan.style.color = '#fca5a5';
                        statusSpan.textContent = errMsg;
                    } else {
                        alert(errMsg);
                    }
                    return;
                }

                const newDesc = res.description;
                const resolvedId = res.item_id || targetItemId;
                updateItemDescriptionInDom(resolvedId, newDesc);
                if (res.title_updated || res.authors_updated || res.publisher_updated) {
                    updateItemTitleAuthorInDom(resolvedId, res);
                }

                if (isReview) {
                    if (btn) {
                        btn.disabled = false;
                        btn.innerHTML = `<i class="fa-solid fa-arrows-rotate"></i> Regenerate AI Description`;
                    }
                    const noMatchHint = document.getElementById('er-no-match-desc-hint');
                    if (noMatchHint) noMatchHint.style.display = 'none';
                    if (revData) {
                        revData.candidate_description = newDesc;
                        const candDescBox = document.querySelector('#er-field-desc-cand > span');
                        if (candDescBox) {
                            candDescBox.style.color = '#f8fafc';
                            candDescBox.textContent = newDesc.length > 160 ? (newDesc.slice(0, 160) + '…') : newDesc;
                        }
                        window.erPickField('desc', 'cand', true);
                        if (res.suggested_title && res.title_updated) {
                            revData.candidate_title = res.suggested_title;
                            const candTBox = document.querySelector('#er-field-title-cand > span');
                            if (candTBox) { candTBox.style.color = '#f8fafc'; candTBox.textContent = res.suggested_title; }
                            window.erPickField('title', 'cand', true);
                        }
                        if (res.suggested_authors && res.authors_updated) {
                            revData.candidate_authors = res.suggested_authors;
                            const candABox = document.querySelector('#er-field-author-cand > span');
                            if (candABox) { candABox.style.color = '#f8fafc'; candABox.textContent = res.suggested_authors; }
                            window.erPickField('author', 'cand', true);
                        }
                    }
                    const descInput = document.getElementById('er-input-desc');
                    if (descInput) descInput.value = newDesc;
                    if (res.title_updated && res.title) {
                        const tIn = document.getElementById('er-input-title');
                        if (tIn) tIn.value = res.title;
                    }
                    if (res.authors_updated && res.authors) {
                        const aIn = document.getElementById('er-input-author');
                        if (aIn) aIn.value = res.authors;
                    }
                    if (statusSpan) {
                        statusSpan.style.display = 'inline';
                        statusSpan.style.color = '#4ade80';
                        statusSpan.innerHTML = `<i class="fa-solid fa-check"></i> Generated from content &amp; saved to BookDB (click <b>Approve Selected Fields</b> to mark Enriched)`;
                    }
                } else if (mode === 'modal' && btn && btn.id !== 'modal-btn-ai-desc') {
                    btn.style.display = 'none';
                }
            })
            .catch(err => {
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = origBtnHtml;
                }
                if (statusSpan && mode !== 'card') {
                    statusSpan.style.display = 'inline';
                    statusSpan.style.color = '#fca5a5';
                    statusSpan.textContent = 'Network error: ' + err.message;
                } else {
                    alert('Network error generating AI description: ' + err.message);
                }
            });
    };

    function updateItemTitleAuthorInDom(targetId, data) {
        if (!data) return;
        const newTitle = (data.title || data.suggested_title || '').trim();
        const newAuthors = (data.authors || data.suggested_authors || '').trim();
        const newPublisher = (data.publisher || data.suggested_publisher || '').trim();
        const newPubDate = (data.pub_date || data.suggested_year || '').trim();
        const newIsbn = (data.isbn || data.suggested_isbn || '').trim();

        const card = targetId ? document.getElementById(`card-${targetId}`) : null;
        if (card) {
            const detCheck = card.querySelector('.btn-details');
            if (detCheck && detCheck.getAttribute('data-is-bad-metadata') === 'true' && typeof window.applyBadMetadataStateToDom === 'function') {
                window.applyBadMetadataStateToDom(targetId, false, null, -1);
            }
            const titleEl = card.querySelector('.card-title');
            const authorEl = card.querySelector('.card-author');
            const detailsBtn = card.querySelector('.btn-details');
            if (newTitle && titleEl) {
                titleEl.textContent = newTitle;
                titleEl.title = newTitle;
            }
            if (newAuthors && authorEl) {
                authorEl.innerHTML = renderAuthorLinksHtml(newAuthors);
                authorEl.title = `by ${newAuthors}`;
            }
            if (detailsBtn) {
                if (newTitle) detailsBtn.setAttribute('data-title', newTitle);
                if (newAuthors) detailsBtn.setAttribute('data-authors', newAuthors);
                if (newPublisher) detailsBtn.setAttribute('data-publisher', newPublisher);
                if (newPubDate) detailsBtn.setAttribute('data-pubdate', newPubDate);
                if (newIsbn) detailsBtn.setAttribute('data-isbn', newIsbn);
                if (detailsBtn._enrichResultData) {
                    if (newTitle) detailsBtn._enrichResultData.candidate_title = newTitle;
                    if (newAuthors) detailsBtn._enrichResultData.candidate_authors = newAuthors;
                    if (newPublisher) detailsBtn._enrichResultData.candidate_publisher = newPublisher;
                }
            }
            const mergedIds = Array.isArray(data.merged_item_ids) ? data.merged_item_ids.map(String) : [];
            mergedIds.forEach(dupId => {
                if (!dupId || String(dupId) === String(targetId)) return;
                const dupCard = document.getElementById(`card-${dupId}`);
                if (dupCard) dupCard.remove();
            });
        }

        if (window._activeModalItem && (!targetId || String(window._activeModalItem.itemId) === String(targetId))) {
            if (data.item_id) window._activeModalItem.itemId = data.item_id;
            if (newTitle) window._activeModalItem.title = newTitle;
            if (newAuthors) window._activeModalItem.authors = newAuthors;
            if (newPublisher) window._activeModalItem.publisher = newPublisher;
            if (newPubDate && !window._activeModalItem.pubDate) window._activeModalItem.pubDate = newPubDate;
            if (newIsbn && !window._activeModalItem.isbn) window._activeModalItem.isbn = newIsbn;

            const modalTitleEl = document.getElementById('modal-title');
            if (modalTitleEl && newTitle) modalTitleEl.textContent = newTitle;
            const modalAuthEl = document.getElementById('modal-authors');
            if (modalAuthEl && newAuthors) modalAuthEl.innerHTML = renderAuthorLinksHtml(newAuthors);

            const qInTitle = document.getElementById('modal-quick-input-title');
            const qInAuth = document.getElementById('modal-quick-input-author');
            const qInPub = document.getElementById('modal-quick-input-publisher');
            if (qInTitle && newTitle) qInTitle.value = newTitle;
            if (qInAuth && newAuthors) qInAuth.value = (newAuthors !== 'Unknown Author') ? newAuthors : '';
            if (qInPub && newPublisher) qInPub.value = newPublisher;

            // Keep cover search query synced with the newly resolved clean title & author
            const covInput = document.getElementById('cover-search-input');
            if (covInput) {
                const cleanAuth = (window._activeModalItem.authors && !['unknown', 'unknown author', '..'].includes(window._activeModalItem.authors.toLowerCase().trim()))
                    ? window._activeModalItem.authors : '';
                const shortT = (window._activeModalItem.title && window._activeModalItem.title.length > 35 && window._activeModalItem.title.includes(':') && window._activeModalItem.title.split(':')[0].trim().length >= 6)
                    ? window._activeModalItem.title.split(':')[0].trim()
                    : (window._activeModalItem.title || '');
                covInput.value = [shortT, cleanAuth].filter(Boolean).join(' ').trim();
            }

            // Update or insert Publisher badge in #modal-badges-container if present
            if (newPublisher) {
                const badgeContainer = document.getElementById('modal-badges-container');
                if (badgeContainer) {
                    const existingPub = Array.from(badgeContainer.querySelectorAll('.modal-badge')).find(b => (b.textContent || '').startsWith('Publisher:'));
                    if (existingPub) {
                        existingPub.textContent = `Publisher: ${newPublisher}`;
                    } else {
                        const newBadge = createBadge(`Publisher: ${newPublisher}`);
                        const firstBadge = badgeContainer.firstElementChild;
                        if (firstBadge && firstBadge.nextSibling) {
                            badgeContainer.insertBefore(newBadge, firstBadge.nextSibling);
                        } else {
                            badgeContainer.appendChild(newBadge);
                        }
                    }
                }
            }
        }
    }

    window.generateAiTitleAuthorForItem = function ({ itemId = '', fileId = '', filePath = '', mode = 'modal', btn = null } = {}) {
        const isReview = mode === 'review';
        const revData = (isReview && activeReviewCtx && activeReviewCtx.data) ? activeReviewCtx.data : null;
        const activeModal = (!isReview && window._activeModalItem) ? window._activeModalItem : {};

        const targetItemId = itemId || (revData ? (revData.item_id || activeReviewCtx.itemId || '') : (activeModal.itemId || ''));
        const targetFileId = fileId || (revData ? (revData.file_id || activeReviewCtx.fileId || '') : (activeModal.firstFileId || ''));
        const targetFilePath = filePath || (revData ? (revData.file_path || '') : (activeModal.firstFilePath || '')) || (isReview ? (document.getElementById('er-file-path')?.textContent || '').trim() : '');
        const titleHint = isReview
            ? (document.getElementById('er-input-title')?.value || (revData && (revData.orig_title || revData.candidate_title)) || '').trim()
            : (activeModal.title || '');
        const authorsHint = isReview
            ? (document.getElementById('er-input-author')?.value || (revData && (revData.orig_authors || revData.candidate_authors)) || '').trim()
            : (activeModal.authors || '');
        const pubHint = isReview
            ? (document.getElementById('er-input-publisher')?.value || (revData && (revData.orig_publisher || revData.candidate_publisher)) || '').trim()
            : (activeModal.publisher || '');
        const isbnHint = isReview
            ? (document.getElementById('er-input-isbn')?.value || (revData && (revData.orig_isbn || revData.candidate_isbn)) || '').trim()
            : (activeModal.isbn || '');

        const origBtnHtml = btn ? btn.innerHTML : '';
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Reading Cover &amp; Content...`;
        }
        const statusSpan = document.getElementById(isReview ? 'er-ai-title-author-status' : 'modal-ai-title-author-status');
        if (statusSpan) {
            statusSpan.style.display = 'inline';
            statusSpan.style.color = '#d8b4fe';
            statusSpan.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Identifying Title &amp; Author with Gemini AI...`;
        }

        fetch(`${BASE_PREFIX}/api/ai-title-author/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({
                item_id: targetItemId,
                file_id: targetFileId,
                file_path: targetFilePath,
                title: titleHint,
                authors: authorsHint,
                publisher: pubHint,
                isbn: isbnHint,
                apply_immediately: true,
                mode: 'title_author',
            })
        })
            .then(async r => {
                const txt = await r.text();
                try {
                    return JSON.parse(txt);
                } catch (_) {
                    throw new Error(`Server returned HTTP ${r.status}`);
                }
            })
            .then(res => {
                if (!res || !res.ok || (!res.suggested_title && !res.suggested_authors && !res.title)) {
                    if (btn) {
                        btn.disabled = false;
                        btn.innerHTML = origBtnHtml;
                    }
                    const errMsg = (res && res.error) || 'Could not extract Title & Author from book content.';
                    if (statusSpan) {
                        statusSpan.style.display = 'inline';
                        statusSpan.style.color = '#fca5a5';
                        statusSpan.textContent = errMsg;
                    } else {
                        alert(errMsg);
                    }
                    return;
                }

                const resolvedId = res.item_id || targetItemId;
                updateItemTitleAuthorInDom(resolvedId, res);
                if (res.description_updated && res.description) {
                    updateItemDescriptionInDom(resolvedId, res.description);
                }

                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = `<i class="fa-solid fa-arrows-rotate"></i> Regenerate Title &amp; Author`;
                }

                if (isReview) {
                    const finalT = res.suggested_title || res.title || '';
                    const finalA = res.suggested_authors || res.authors || '';
                    const finalP = res.suggested_publisher || res.publisher || '';
                    if (revData) {
                        if (finalT) {
                            revData.candidate_title = finalT;
                            const ct = document.querySelector('#er-field-title-cand > span');
                            if (ct) { ct.style.color = '#f8fafc'; ct.textContent = finalT; }
                            window.erPickField('title', 'cand', true);
                        }
                        if (finalA) {
                            revData.candidate_authors = finalA;
                            const ca = document.querySelector('#er-field-author-cand > span');
                            if (ca) { ca.style.color = '#f8fafc'; ca.textContent = finalA; }
                            window.erPickField('author', 'cand', true);
                        }
                        if (finalP) {
                            revData.candidate_publisher = finalP;
                            const cp = document.querySelector('#er-field-publisher-cand > span');
                            if (cp) { cp.style.color = '#f8fafc'; cp.textContent = finalP; }
                            window.erPickField('publisher', 'cand', true);
                        }
                    }
                    if (finalT && document.getElementById('er-input-title')) document.getElementById('er-input-title').value = finalT;
                    if (finalA && document.getElementById('er-input-author')) document.getElementById('er-input-author').value = finalA;
                    if (finalP && document.getElementById('er-input-publisher')) document.getElementById('er-input-publisher').value = finalP;
                    if (res.suggested_year && document.getElementById('er-input-pubdate') && !document.getElementById('er-input-pubdate').value.trim()) {
                        document.getElementById('er-input-pubdate').value = res.suggested_year;
                    }
                    if (res.suggested_isbn && document.getElementById('er-input-isbn') && !document.getElementById('er-input-isbn').value.trim()) {
                        document.getElementById('er-input-isbn').value = res.suggested_isbn;
                    }
                    const erCovIn = document.getElementById('er-cover-search-input');
                    if (erCovIn && finalT) {
                        erCovIn.value = [finalT, (finalA && finalA !== 'Unknown Author') ? finalA : ''].filter(Boolean).join(' ').trim();
                    }
                    if (statusSpan) {
                        statusSpan.style.display = 'inline';
                        statusSpan.style.color = '#4ade80';
                        statusSpan.innerHTML = `<i class="fa-solid fa-check"></i> Title &amp; Author extracted &amp; saved!`;
                    }
                } else {
                    if (statusSpan) {
                        statusSpan.style.display = 'inline';
                        statusSpan.style.color = '#4ade80';
                        statusSpan.innerHTML = `<i class="fa-solid fa-check"></i> Generated Title &amp; Author &amp; saved`;
                    }
                }
            })
            .catch(err => {
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = origBtnHtml;
                }
                if (statusSpan) {
                    statusSpan.style.display = 'inline';
                    statusSpan.style.color = '#fca5a5';
                    statusSpan.textContent = 'Network error: ' + err.message;
                } else {
                    alert('Network error generating Title & Author: ' + err.message);
                }
            });
    };

    window.toggleModalQuickEditMeta = function () {
        const drawer = document.getElementById('modal-quick-edit-drawer');
        if (!drawer) return;
        const active = window._activeModalItem || {};
        if (drawer.style.display === 'none' || !drawer.style.display) {
            const qInTitle = document.getElementById('modal-quick-input-title');
            const qInAuth = document.getElementById('modal-quick-input-author');
            const qInPub = document.getElementById('modal-quick-input-publisher');
            if (qInTitle) qInTitle.value = active.title || '';
            if (qInAuth) qInAuth.value = (active.authors && active.authors !== 'Unknown Author') ? active.authors : '';
            if (qInPub) qInPub.value = active.publisher || '';
            drawer.style.display = 'block';
            if (qInTitle) qInTitle.focus();
        } else {
            drawer.style.display = 'none';
        }
    };

    window.saveModalQuickEditMeta = function (btnEl = null) {
        const active = window._activeModalItem || {};
        const newTitle = (document.getElementById('modal-quick-input-title')?.value || '').trim();
        const newAuth = (document.getElementById('modal-quick-input-author')?.value || '').trim() || 'Unknown Author';
        const newPub = (document.getElementById('modal-quick-input-publisher')?.value || '').trim();
        if (!newTitle) {
            alert('Please enter a Title before saving.');
            return;
        }
        const origHtml = btnEl ? btnEl.innerHTML : '';
        if (btnEl) {
            btnEl.disabled = true;
            btnEl.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Saving...`;
        }
        const selectedMetadata = {
            title: newTitle,
            author: newAuth,
            publisher: newPub,
            pub_date: active.pubDate || '',
            isbn: active.isbn || '',
            description: active.desc || '',
            cover: active.cover || '',
        };
        fetch(`${BASE_PREFIX}/api/status/items/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({
                action: 'approve',
                item_id: active.itemId || '',
                file_path: active.firstFilePath || '',
                selected_metadata: selectedMetadata,
            })
        })
            .then(r => r.json())
            .then(res => {
                if (btnEl) {
                    btnEl.disabled = false;
                    btnEl.innerHTML = origHtml;
                }
                if (res && res.ok) {
                    const resolvedId = res.item_id || active.itemId || '';
                    transitionCardEnrichBucket(resolvedId, 'enriched');
                    applyCardMetadataUpdate(resolvedId, {
                        item_id: resolvedId,
                        title: newTitle,
                        authors: newAuth,
                        publisher: newPub,
                        pub_date: selectedMetadata.pub_date,
                        isbn: selectedMetadata.isbn,
                        description: selectedMetadata.description,
                        cover_url: res.cover_url || selectedMetadata.cover,
                        merged_item_ids: res.merged_item_ids || [],
                    }, true);
                    updateItemTitleAuthorInDom(resolvedId, {
                        item_id: resolvedId,
                        title: newTitle,
                        authors: newAuth,
                        publisher: newPub,
                        merged_item_ids: res.merged_item_ids || [],
                    });
                    const drawer = document.getElementById('modal-quick-edit-drawer');
                    if (drawer) drawer.style.display = 'none';
                    const stEl = document.getElementById('modal-ai-title-author-status');
                    if (stEl) {
                        stEl.style.display = 'inline';
                        stEl.style.color = '#4ade80';
                        stEl.innerHTML = `<i class="fa-solid fa-check"></i> Saved Title &amp; Author`;
                    }
                } else {
                    alert((res && res.error) || 'Could not save metadata.');
                }
            })
            .catch(err => {
                if (btnEl) {
                    btnEl.disabled = false;
                    btnEl.innerHTML = origHtml;
                }
                alert('Network error saving metadata: ' + err.message);
            });
    };

    window.revertBookCoverToOriginal = function (mode = 'modal') {
        const isReview = mode === 'review';
        const statusEl = document.getElementById(isReview ? 'er-cover-finder-status' : 'cover-finder-status');
        const resultsEl = document.getElementById(isReview ? 'er-cover-finder-results' : 'cover-finder-results');
        const revBtn = document.getElementById(isReview ? 'er-btn-revert-original-cover' : 'btn-revert-original-cover');

        let payload = { action: 'revert_original' };
        if (isReview) {
            if (!activeReviewCtx || !activeReviewCtx.data) return;
            const d = activeReviewCtx.data;
            payload.item_id = activeReviewCtx.itemId || d.item_id || '';
            payload.file_id = activeReviewCtx.fileId || d.file_id || '';
            payload.file_path = d.file_path || '';
            payload.isbn = d.candidate_isbn || d.orig_isbn || '';
            payload.title = d.candidate_title || d.orig_title || '';
            payload.authors = d.candidate_authors || d.orig_authors || '';
        } else {
            const ctx = window._activeModalItem || {};
            const fallbackTitle = (document.getElementById('modal-title')?.textContent || '').trim();
            const fallbackAuthors = (document.getElementById('modal-authors')?.textContent || '').trim();
            payload.item_id = ctx.itemId || '';
            payload.file_id = ctx.firstFileId || '';
            payload.file_path = ctx.firstFilePath || '';
            payload.isbn = ctx.isbn || '';
            payload.title = ctx.title || fallbackTitle || '';
            payload.authors = ctx.authors || fallbackAuthors || '';
        }

        if (revBtn) revBtn.disabled = true;
        if (statusEl) {
            statusEl.innerHTML = `<i class="fa-solid fa-rotate-left fa-spin"></i> Reverting active cover back to the book's original cover...`;
        }

        fetch(`${BASE_PREFIX}/api/cover-generate/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify(payload)
        })
            .then(async r => {
                const txt = await r.text();
                try {
                    return JSON.parse(txt);
                } catch (_) {
                    throw new Error(`Server returned HTTP ${r.status}`);
                }
            })
            .then(res => {
                if (revBtn) revBtn.disabled = false;
                if (!res.ok) {
                    if (statusEl) {
                        statusEl.innerHTML = `<span style="color:#f87171;">Could not revert cover: ${escapeHtml(res.error || 'Unknown error')}</span>`;
                    }
                    return;
                }
                const origCoverUrl = res.cover_url || '';
                const resolvedId = res.item_id || payload.item_id;

                if (resultsEl) {
                    resultsEl.querySelectorAll('.cover-cand-card').forEach(el => {
                        const metaDiv = el.querySelector('.cover-cand-meta div');
                        const isOrigCard = metaDiv && metaDiv.textContent.includes('Original Cover');
                        if (isOrigCard) {
                            el.style.borderColor = '#4ade80';
                            el.style.boxShadow = '0 0 0 2px rgba(74, 222, 128, 0.55)';
                        } else if (el.dataset.isGen === '1' || (metaDiv && metaDiv.textContent.includes('🎨'))) {
                            el.style.borderColor = '#c084fc';
                            el.style.boxShadow = '0 0 0 1px rgba(192, 132, 252, 0.35)';
                        } else {
                            el.style.borderColor = 'rgba(255,255,255,0.12)';
                            el.style.boxShadow = 'none';
                        }
                    });
                }

                if (isReview) {
                    if (activeReviewCtx && activeReviewCtx.data) {
                        const d = activeReviewCtx.data;
                        d.orig_cover = origCoverUrl || d.orig_cover || '';
                        d.UseDataUriCover = '';
                        window.erPickField('cover', 'orig', true);
                    }
                    if (resolvedId && origCoverUrl) {
                        applyCardMetadataUpdate(resolvedId, { cover_url: origCoverUrl }, true);
                    }
                } else {
                    const ctx = window._activeModalItem || {};
                    const origItemId = ctx.itemId || payload.item_id;
                    if (window._activeModalItem) {
                        window._activeModalItem.cover = origCoverUrl;
                        if (res.item_id) window._activeModalItem.itemId = res.item_id;
                    }
                    applyCardMetadataUpdate(origItemId || resolvedId, { item_id: resolvedId, cover_url: origCoverUrl }, true);
                    if (resolvedId && origItemId && resolvedId !== origItemId) {
                        applyCardMetadataUpdate(resolvedId, { cover_url: origCoverUrl }, false);
                    }
                }

                if (res.missing_cover_count !== undefined) {
                    const cntEl = document.getElementById('sidebar-missing-cover-count');
                    if (cntEl) cntEl.textContent = Number(res.missing_cover_count).toLocaleString();
                }

                if (statusEl) {
                    statusEl.innerHTML = `<span style="color:#4ade80; font-weight:600;"><i class="fa-solid fa-rotate-left"></i> Reverted to the original cover!</span> <span style="color:#94a3b8;">(Your generated covers remain saved in BookDB below and can be re-selected anytime)</span>`;
                }
            })
            .catch(err => {
                if (revBtn) revBtn.disabled = false;
                if (statusEl) statusEl.textContent = 'Network error reverting cover: ' + err.message;
            });
    };

    // Interactive Cover Zoom Lightbox state & handlers
    let _coverZoomScale = 1;
    let _coverZoomX = 0;
    let _coverZoomY = 0;
    let _coverZoomDragging = false;
    let _coverZoomStartX = 0;
    let _coverZoomStartY = 0;
    let _coverZoomListenersBound = false;

    function _applyCoverZoomTransform() {
        const img = document.getElementById('cover-zoom-img');
        const lbl = document.getElementById('cover-zoom-level');
        const stage = document.getElementById('cover-zoom-stage');
        if (img) {
            img.style.transform = `translate(${_coverZoomX}px, ${_coverZoomY}px) scale(${_coverZoomScale})`;
        }
        if (lbl) {
            lbl.textContent = `${Math.round(_coverZoomScale * 100)}%`;
        }
        if (stage) {
            stage.style.cursor = _coverZoomDragging ? 'grabbing' : (_coverZoomScale > 1.02 ? 'grab' : 'zoom-in');
        }
    }

    function _bindCoverZoomListenersOnce() {
        if (_coverZoomListenersBound) return;
        const lb = document.getElementById('cover-zoom-lightbox');
        const stage = document.getElementById('cover-zoom-stage');
        const img = document.getElementById('cover-zoom-img');
        if (!lb || !stage || !img) return;
        _coverZoomListenersBound = true;

        stage.addEventListener('wheel', ev => {
            ev.preventDefault();
            const delta = ev.deltaY < 0 ? 0.25 : -0.25;
            window.adjustCoverZoom(delta);
        }, { passive: false });

        stage.addEventListener('mousedown', ev => {
            if (ev.button !== 0) return;
            if (ev.target === stage && _coverZoomScale <= 1.02) {
                window.closeCoverZoomLightbox();
                return;
            }
            _coverZoomDragging = true;
            _coverZoomStartX = ev.clientX - _coverZoomX;
            _coverZoomStartY = ev.clientY - _coverZoomY;
            img.style.transition = 'none';
            _applyCoverZoomTransform();
        });

        window.addEventListener('mousemove', ev => {
            if (!_coverZoomDragging) return;
            _coverZoomX = ev.clientX - _coverZoomStartX;
            _coverZoomY = ev.clientY - _coverZoomStartY;
            _applyCoverZoomTransform();
        });

        window.addEventListener('mouseup', () => {
            if (!_coverZoomDragging) return;
            _coverZoomDragging = false;
            if (img) img.style.transition = 'transform 0.08s ease-out';
            _applyCoverZoomTransform();
        });

        img.addEventListener('dblclick', ev => {
            ev.stopPropagation();
            if (_coverZoomScale > 1.3) {
                window.resetCoverZoom(1);
            } else {
                window.resetCoverZoom(2.25);
            }
        });

        document.addEventListener('keydown', ev => {
            if (ev.key === 'Escape' && lb.style.display !== 'none' && lb.style.display !== '') {
                ev.stopPropagation();
                window.closeCoverZoomLightbox();
            }
        });
    }

    window.openCoverZoomLightbox = function (src, titleText = '', onApplyCallback = null) {
        if (!src || src === window.location.href) return;
        const lb = document.getElementById('cover-zoom-lightbox');
        const img = document.getElementById('cover-zoom-img');
        const titleEl = document.getElementById('cover-zoom-title');
        const applyBtn = document.getElementById('cover-zoom-apply-btn');
        if (!lb || !img) return;

        _bindCoverZoomListenersOnce();
        img.src = src;
        if (titleEl) {
            titleEl.textContent = titleText || 'Book Cover Preview';
        }
        if (applyBtn) {
            if (typeof onApplyCallback === 'function') {
                applyBtn.style.display = 'inline-flex';
                applyBtn.onclick = () => {
                    onApplyCallback();
                    window.closeCoverZoomLightbox();
                };
            } else {
                applyBtn.style.display = 'none';
                applyBtn.onclick = null;
            }
        }
        _coverZoomScale = 1;
        _coverZoomX = 0;
        _coverZoomY = 0;
        _applyCoverZoomTransform();
        lb.style.display = 'flex';
    };

    window.closeCoverZoomLightbox = function () {
        const lb = document.getElementById('cover-zoom-lightbox');
        if (lb) lb.style.display = 'none';
        _coverZoomDragging = false;
    };

    window.adjustCoverZoom = function (delta) {
        _coverZoomScale = Math.max(0.5, Math.min(5.0, Number((_coverZoomScale + delta).toFixed(2))));
        if (_coverZoomScale <= 1.0) {
            _coverZoomX = 0;
            _coverZoomY = 0;
        }
        _applyCoverZoomTransform();
    };

    window.resetCoverZoom = function (targetScale = 1) {
        _coverZoomScale = targetScale;
        _coverZoomX = 0;
        _coverZoomY = 0;
        _applyCoverZoomTransform();
    };

    // Delegated click listener so clicking ANY book cover (on a shelf card, in Book Details, or in Review Modal) pops up the zoomed cover
    document.addEventListener('click', ev => {
        const cardImg = ev.target.closest('.card-header-image img.cover-image');
        if (cardImg && cardImg.src) {
            ev.stopPropagation();
            const card = cardImg.closest('.media-card');
            const titleTxt = (card?.querySelector('.card-title')?.textContent || cardImg.alt || 'Book Cover').trim();
            window.openCoverZoomLightbox(cardImg.src, titleTxt);
            return;
        }
        const erImg = ev.target.closest('#er-field-cover-orig img, #er-field-cover-cand img');
        if (erImg && erImg.src) {
            ev.stopPropagation();
            const curT = (document.getElementById('er-input-title')?.value || 'Book Cover').trim();
            window.openCoverZoomLightbox(erImg.src, curT);
        }
    });

    window.openCardFindCover = function (itemId) {
        const card = document.getElementById(`card-${itemId}`);
        if (!card) return;
        const detBtn = card.querySelector('.btn-details');
        if (!detBtn) return;
        if (window.openDetailsModal) {
            window.openDetailsModal(detBtn);
            setTimeout(() => {
                const drawer = document.getElementById('modal-cover-finder');
                if (drawer && (drawer.style.display === 'none' || !drawer.style.display)) {
                    window.toggleCoverFinder();
                } else if (drawer) {
                    window.runWebCoverSearch();
                }
            }, 80);
        }
    };

    window.dismissNeedsCover = function (itemId, btnEl) {
        if (!itemId) return;
        if (btnEl) {
            btnEl.disabled = true;
            btnEl.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
        }
        fetch(`${BASE_PREFIX}/api/needs-cover/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({
                item_id: itemId,
                needs_cover: false,
                reason: 'Kept current cover by user'
            })
        })
            .then(async r => {
                const txt = await r.text();
                try {
                    return JSON.parse(txt);
                } catch (_) {
                    throw new Error(`Server returned HTTP ${r.status}`);
                }
            })
            .then(res => {
                if (res.ok) {
                    if (res.missing_cover_count !== undefined) {
                        const cntEl = document.getElementById('sidebar-missing-cover-count');
                        if (cntEl) cntEl.textContent = Number(res.missing_cover_count).toLocaleString();
                    }
                    const card = document.getElementById(`card-${itemId}`);
                    const params = new URLSearchParams(window.location.search);
                    const onMissingShelf = (params.get('shelf') === 'missing_cover' || params.get('enrich_status') === 'missing_cover');
                    if (card) {
                        if (onMissingShelf) {
                            card.style.transition = 'opacity 0.25s, transform 0.25s';
                            card.style.opacity = '0';
                            card.style.transform = 'scale(0.95)';
                            setTimeout(() => card.remove(), 260);
                        } else {
                            const issBadge = card.querySelector('.card-cover-issue-badge');
                            if (issBadge) issBadge.remove();
                            const covActRow = card.querySelector('.card-cover-actions-row');
                            if (covActRow) covActRow.remove();
                        }
                    }
                } else if (btnEl) {
                    btnEl.disabled = false;
                    btnEl.innerHTML = '<i class="fa-solid fa-check"></i> Keep Cover';
                }
            })
            .catch(() => {
                if (btnEl) {
                    btnEl.disabled = false;
                    btnEl.innerHTML = '<i class="fa-solid fa-check"></i> Keep Cover';
                }
            });
    };

    window.erFocusManualEdit = function () {
        const box = document.getElementById('er-custom-edit-box');
        const titleInput = document.getElementById('er-input-title');
        if (box) {
            box.scrollIntoView({ behavior: 'smooth', block: 'center' });
            box.style.borderColor = '#4ade80';
            box.style.boxShadow = '0 0 0 3px rgba(74, 222, 128, 0.35)';
            setTimeout(() => {
                box.style.borderColor = 'rgba(74,222,128,0.35)';
                box.style.boxShadow = 'none';
            }, 1400);
        }
        if (titleInput) {
            setTimeout(() => {
                titleInput.focus();
                titleInput.select();
            }, 150);
        }
    };

    function openEnrichReviewModal(data, btn, updateModal, itemId, fileId) {
        const modal = document.getElementById('enrich-review-modal');
        if (!modal) return;
        activeReviewCtx = { data, btn, updateModal, itemId: data.item_id || itemId, fileId: data.file_id || fileId };

        const stBadge = document.getElementById('er-status-badge');
        if (stBadge) stBadge.textContent = data.status || 'NEEDS_REVIEW';
        const confBadge = document.getElementById('er-conf-badge');
        if (confBadge) {
            confBadge.innerHTML = data.confidence !== undefined && data.confidence !== null
                ? `<i class="fa-solid fa-pen-to-square"></i> Match Confidence: ${data.confidence}% (Custom Edit)`
                : `<i class="fa-solid fa-pen-to-square"></i> Manual Review / Custom Edit`;
        }
        const fnEl = document.getElementById('er-file-name');
        if (fnEl) fnEl.textContent = data.file_name || data.orig_title || 'Book File';
        const fpEl = document.getElementById('er-file-path');
        if (fpEl) fpEl.textContent = data.file_path || '';
        const diagEl = document.getElementById('er-diag-msg');
        if (diagEl) diagEl.textContent = data.message || 'Review original vs candidate metadata or enter custom fields below.';

        const renderClickableFields = (side, meta) => {
            const coverHtml = `
                <div id="er-field-cover-${side}" data-pick-key="cover" data-pick-side="${side}" onclick="window.erPickField('cover', '${side}', true)" style="cursor:pointer; user-select:none; background:rgba(15,23,42,0.65); border:2px solid rgba(255,255,255,0.08); border-radius:6px; padding:0.4rem 0.55rem; display:flex; align-items:center; justify-content:space-between; gap:0.65rem; transition:all 0.15s;" title="Click to use ${side === 'orig' ? 'Original Cover' : 'Candidate Cover'}">
                    <div style="display:flex; align-items:center; gap:0.65rem; min-width:0;">
                        ${meta.cover
                            ? `<img src="${escapeHtml(meta.cover)}" alt="Cover" style="width:42px; height:60px; object-fit:cover; border-radius:4px; background:#020617; flex-shrink:0;">`
                            : `<div style="width:42px; height:60px; border-radius:4px; background:#020617; border:1px dashed rgba(255,255,255,0.15); display:flex; align-items:center; justify-content:center; color:#64748b; font-size:0.65rem; flex-shrink:0;">None</div>`}
                        <div>
                            <span style="font-size:0.68rem; color:#94a3b8; text-transform:uppercase; display:block;">Select Cover (${side === 'orig' ? '1st Option: Original' : 'Candidate'})</span>
                            <span id="er-cover-lbl-${side}" style="font-size:0.76rem; color:${meta.cover ? '#f8fafc' : '#64748b'}; font-weight:600;">${meta.cover ? (side === 'orig' ? 'Original Book Cover' : 'Enriched Web Cover') : 'No Cover Available'}</span>
                        </div>
                    </div>
                    <span id="er-badge-cover-${side}" style="display:none; background:rgba(74,222,128,0.2); border:1px solid rgba(74,222,128,0.55); color:#4ade80; border-radius:999px; padding:0.1rem 0.45rem; font-size:0.65rem; font-weight:700; white-space:nowrap; flex-shrink:0;"><i class="fa-solid fa-check"></i> Selected</span>
                </div>
            `;
            const rows = [
                { key: 'title', label: 'Title', val: meta.title || '' },
                { key: 'author', label: 'Author', val: meta.authors || '' },
                { key: 'publisher', label: 'Publisher', val: meta.publisher || '' },
                { key: 'pubdate', label: 'Published Date', val: meta.pubdate || '' },
                { key: 'isbn', label: 'ISBN', val: meta.isbn || '' },
                { key: 'desc', label: 'Description', val: meta.description || '' },
            ];
            return coverHtml + rows.map(r => `
                <div id="er-field-${r.key}-${side}" data-pick-key="${r.key}" data-pick-side="${side}" onclick="window.erPickField('${r.key}', '${side}', true)" style="cursor:pointer; user-select:none; background:rgba(15,23,42,0.65); border:2px solid rgba(255,255,255,0.08); border-radius:6px; padding:0.35rem 0.55rem; transition:all 0.15s;" title="Click to use this ${r.label} in Selected Metadata">
                    <div style="display:flex; justify-content:space-between; align-items:center; gap:0.4rem; margin-bottom:0.1rem;">
                        <span style="font-size:0.68rem; color:#94a3b8; text-transform:uppercase; display:block;">${r.label}</span>
                        <span id="er-badge-${r.key}-${side}" style="display:none; background:rgba(74,222,128,0.2); border:1px solid rgba(74,222,128,0.55); color:#4ade80; border-radius:999px; padding:0.06rem 0.42rem; font-size:0.63rem; font-weight:700; white-space:nowrap;"><i class="fa-solid fa-check"></i> Selected</span>
                    </div>
                    <span style="color:${r.val ? '#f8fafc' : '#64748b'}; font-weight:${r.key === 'title' ? '600' : '400'}; display:block; word-break:break-word;">${r.val ? escapeHtml(r.val.length > 160 ? r.val.slice(0, 160) + '…' : r.val) : '—'}</span>
                </div>
            `).join('');
        };

        const bindPickDelegation = (boxEl) => {
            if (!boxEl || boxEl._hasPickDelegation) return;
            boxEl._hasPickDelegation = true;
            boxEl.addEventListener('click', (ev) => {
                const targetBox = ev.target.closest('[data-pick-key][data-pick-side]');
                if (targetBox) {
                    const k = targetBox.getAttribute('data-pick-key');
                    const s = targetBox.getAttribute('data-pick-side');
                    if (k && s) window.erPickField(k, s, true);
                }
            });
        };

        const origBox = document.getElementById('er-orig-box');
        if (origBox) {
            origBox.innerHTML = renderClickableFields('orig', {
                cover: data.orig_cover,
                title: data.orig_title,
                authors: data.orig_authors,
                publisher: data.orig_publisher,
                pubdate: data.orig_pub_date,
                isbn: data.orig_isbn,
                description: data.orig_description,
            });
            bindPickDelegation(origBox);
        }
        const candBox = document.getElementById('er-cand-box');
        if (candBox) {
            candBox.innerHTML = renderClickableFields('cand', {
                cover: data.candidate_cover,
                title: data.candidate_title,
                authors: data.candidate_authors,
                publisher: data.candidate_publisher,
                pubdate: data.candidate_pub_date,
                isbn: data.candidate_isbn,
                description: data.candidate_description,
            });
            bindPickDelegation(candBox);
        }

        // Smart default selection per field:
        // - Cover: Original Cover is ALWAYS 1st option unless missing
        // - When confidence < 50% (candidate is a low-confidence mismatch) and Original Metadata has a title,
        //   prefer Original Metadata for all fields where Original is present so a wrong book never overwrites good original metadata.
        const isLowConfMismatch = (data.confidence !== null && data.confidence !== undefined && Number(data.confidence) < 50 && Boolean(data.orig_title));
        window.erPickField('cover', data.orig_cover ? 'orig' : (data.candidate_cover ? 'cand' : 'orig'), false);
        if (isLowConfMismatch) {
            window.erPickField('title', data.orig_title ? 'orig' : (data.candidate_title ? 'cand' : 'orig'), false);
            window.erPickField('author', data.orig_authors ? 'orig' : (data.candidate_authors ? 'cand' : 'orig'), false);
            window.erPickField('publisher', data.orig_publisher ? 'orig' : (data.candidate_publisher ? 'cand' : 'orig'), false);
            window.erPickField('pubdate', data.orig_pub_date ? 'orig' : (data.candidate_pub_date ? 'cand' : 'orig'), false);
            window.erPickField('isbn', data.orig_isbn ? 'orig' : 'orig', false);
            window.erPickField('desc', data.orig_description ? 'orig' : (data.candidate_description ? 'cand' : 'orig'), false);
        } else {
            window.erPickField('title', data.candidate_title ? 'cand' : 'orig', false);
            window.erPickField('author', data.candidate_authors ? 'cand' : 'orig', false);
            window.erPickField('publisher', data.candidate_publisher ? 'cand' : 'orig', false);
            window.erPickField('pubdate', data.candidate_pub_date ? 'cand' : 'orig', false);
            window.erPickField('isbn', data.candidate_isbn ? 'cand' : 'orig', false);
            window.erPickField('desc', data.candidate_description ? 'cand' : 'orig', false);
        }

        const forceBtn = document.getElementById('er-btn-force');
        if (forceBtn) {
            forceBtn.style.display = data.candidate_title ? 'inline-block' : 'none';
        }
        const stUpperRev = String(data.status || '').toUpperCase();
        const isRevNoMatch = (stUpperRev === 'NOT_FOUND' || stUpperRev === 'NO_ISBN');
        const hasAnyRevDesc = Boolean((data.orig_description || data.candidate_description || '').trim());
        const erAiDescBtn = document.getElementById('er-btn-ai-desc');
        const erNoMatchDescHint = document.getElementById('er-no-match-desc-hint');
        const erAiDescSt = document.getElementById('er-ai-desc-status');
        if (erAiDescSt) {
            erAiDescSt.style.display = 'none';
            erAiDescSt.textContent = '';
        }
        if (erNoMatchDescHint) {
            erNoMatchDescHint.style.display = (isRevNoMatch && !hasAnyRevDesc) ? 'inline-block' : 'none';
        }
        if (erAiDescBtn) {
            erAiDescBtn.disabled = false;
            erAiDescBtn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> AI Generate Description from Content`;
            erAiDescBtn.style.display = 'inline-flex';
        }
        const erAiTitleBtn = document.getElementById('er-btn-ai-title-author');
        const erAiTitleSt = document.getElementById('er-ai-title-author-status');
        if (erAiTitleSt) {
            erAiTitleSt.style.display = 'none';
            erAiTitleSt.textContent = '';
        }
        if (erAiTitleBtn) {
            erAiTitleBtn.disabled = false;
            erAiTitleBtn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> AI Generate Title &amp; Author`;
            erAiTitleBtn.style.display = 'inline-flex';
        }
        const erTagsBox = document.getElementById('er-readonly-tags-box');
        if (erTagsBox) {
            const combinedTags = Array.from(new Set([
                ...(Array.isArray(data.candidate_tags) ? data.candidate_tags : []),
                ...(Array.isArray(data.orig_tags) ? data.orig_tags : []),
            ].filter(Boolean)));
            if (typeof window._renderReadOnlyTagsHtml === 'function') {
                erTagsBox.innerHTML = window._renderReadOnlyTagsHtml(combinedTags);
            } else {
                erTagsBox.textContent = combinedTags.length ? combinedTags.join(', ') : 'No tags recorded for this item.';
            }
        }

        // Reset Inside Book Preview drawer & Cover Finder drawer for this item
        const prevDrawer = document.getElementById('er-book-preview-drawer');
        if (prevDrawer) {
            prevDrawer.style.display = 'none';
            prevDrawer.dataset.loadedFor = '';
        }
        const erCopySt = document.getElementById('er-preview-copy-status');
        if (erCopySt) {
            erCopySt.style.display = 'none';
            erCopySt.innerHTML = '';
        }
        const erCovDrawer = document.getElementById('er-cover-finder-drawer');
        if (erCovDrawer) {
            erCovDrawer.style.display = 'none';
        }
        const erCovInput = document.getElementById('er-cover-search-input');
        if (erCovInput) {
            const prefTitle = (document.getElementById('er-input-title')?.value || data.candidate_title || data.orig_title || '').trim();
            const prefAuth = (document.getElementById('er-input-author')?.value || data.candidate_authors || data.orig_authors || '').trim();
            const cleanPrefAuth = (prefAuth && !['unknown', 'unknown author', '..'].includes(prefAuth.toLowerCase())) ? prefAuth : '';
            erCovInput.value = [prefTitle, cleanPrefAuth].filter(Boolean).join(' ').trim();
        }
        const erCovRes = document.getElementById('er-cover-finder-results');
        if (erCovRes) erCovRes.innerHTML = '';
        const erCovSt = document.getElementById('er-cover-finder-status');
        if (erCovSt) erCovSt.textContent = '';

        const rawLink = document.getElementById('er-raw-stream-link');
        if (rawLink) {
            if (data.file_path) {
                rawLink.href = `${BASE_PREFIX}/api/book-preview/raw/?file_path=${encodeURIComponent(data.file_path)}`;
                rawLink.style.display = 'inline-flex';
            } else {
                rawLink.style.display = 'none';
            }
        }

        modal.style.display = 'flex';
    }

    window.toggleReviewCoverFinder = function () {
        const drawer = document.getElementById('er-cover-finder-drawer');
        if (!drawer) return;
        if (drawer.style.display === 'none' || !drawer.style.display) {
            drawer.style.display = 'block';
            const erCovInput = document.getElementById('er-cover-search-input');
            if (erCovInput) {
                const curT = (document.getElementById('er-input-title')?.value || '').trim();
                const curA = (document.getElementById('er-input-author')?.value || '').trim();
                const cleanA = (curA && !['unknown', 'unknown author', '..'].includes(curA.toLowerCase())) ? curA : '';
                if (curT) {
                    erCovInput.value = [curT, cleanA].filter(Boolean).join(' ').trim();
                }
            }
            const resGrid = document.getElementById('er-cover-finder-results');
            if (resGrid && resGrid.children.length === 0) {
                window.erRunCoverSearch();
            }
        } else {
            drawer.style.display = 'none';
        }
    };

    window.erRunCoverSearch = function (isRetry = false) {
        if (!activeReviewCtx) return;
        const { data, itemId } = activeReviewCtx;
        const covInput = document.getElementById('er-cover-search-input');
        const statusEl = document.getElementById('er-cover-finder-status');
        const resultsEl = document.getElementById('er-cover-finder-results');
        const searchBtn = document.getElementById('er-btn-run-cover-search');
        if (!resultsEl) return;

        if (window._activeErCoverSearchAbort) {
            try { window._activeErCoverSearchAbort.abort(); } catch (_) {}
            window._activeErCoverSearchAbort = null;
        }
        const ctrl = new AbortController();
        window._activeErCoverSearchAbort = ctrl;
        const timeoutId = setTimeout(() => {
            try { ctrl.abort(); } catch (_) {}
        }, 16000);

        const curTitle = (document.getElementById('er-input-title')?.value || data?.candidate_title || data?.orig_title || '').trim();
        const curAuth = (document.getElementById('er-input-author')?.value || data?.candidate_authors || data?.orig_authors || '').trim();
        const curIsbn = (document.getElementById('er-input-isbn')?.value || data?.candidate_isbn || data?.orig_isbn || '').trim();
        const q = (covInput ? covInput.value : '').trim() || [curTitle, curAuth].filter(Boolean).join(' ').trim();

        if (statusEl) statusEl.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Searching Web Images, Goodreads, Apple Books, Google Books &amp; Open Library for covers...`;
        resultsEl.innerHTML = '';
        if (searchBtn) searchBtn.disabled = true;

        fetch(`${BASE_PREFIX}/api/cover-search/`, {
            method: 'POST',
            signal: ctrl.signal,
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({
                query: q,
                title: curTitle,
                authors: curAuth,
                isbn: curIsbn,
                item_id: itemId || data?.item_id || '',
            })
        })
            .then(r => {
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                return r.json();
            })
            .then(resData => {
                clearTimeout(timeoutId);
                if (searchBtn) searchBtn.disabled = false;
                const covers = resData.covers || [];
                window._lastErCoverSearchCandidates = covers;
                if (covers.length === 0) {
                    if (statusEl) statusEl.textContent = 'No covers found for that query. Try editing the search box above and click Search Covers.';
                    return;
                }
                if (statusEl) statusEl.textContent = `Found ${covers.length} cover(s) — click any cover below to select & apply it:`;
                resultsEl.innerHTML = '';
                covers.forEach((c, idx) => {
                    const isOrig = c.source === 'Original Cover';
                    const isGen = Boolean(c.is_generated || (c.source && c.source.startsWith('🎨')));
                    const card = document.createElement('div');
                    card.className = 'cover-cand-card';
                    card._candData = Object.assign({}, c, { candidate_index: idx });
                    if (isOrig) {
                        card.style.borderColor = '#4ade80';
                        card.style.boxShadow = '0 0 0 1px rgba(74, 222, 128, 0.45)';
                    } else if (isGen) {
                        card.style.borderColor = '#c084fc';
                        card.style.boxShadow = '0 0 0 1px rgba(192, 132, 252, 0.45)';
                    }
                    const badgeColor = isOrig ? '#4ade80' : (isGen ? '#e9d5ff' : '#38bdf8');
                    card.title = `${c.title} (${c.source}) — Click to use this cover (or click 🔍 to zoom)`;
                    card.innerHTML = `
                    <button type="button" class="cover-cand-zoom-btn" title="Zoom in &amp; inspect this cover full-screen"><i class="fa-solid fa-magnifying-glass-plus"></i></button>
                    <img src="${escapeHtml(c.cover_url)}" data-fallback="${escapeHtml(c.fallback_url || '')}" alt="Cover candidate" loading="lazy" onerror="if(this.dataset.fallback && this.src !== this.dataset.fallback){this.src=this.dataset.fallback;}else{this.closest('.cover-cand-card').style.display='none';}">
                    <div class="cover-cand-meta">
                        <div style="font-weight:700; color:${badgeColor}; font-size:0.64rem; text-transform:uppercase;">${isOrig ? '★ ' : ''}${escapeHtml(c.source)}</div>
                        <div style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-weight:600;" title="${escapeHtml(c.title)}">${escapeHtml(c.title)}</div>
                        <div style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#94a3b8;">${escapeHtml(c.authors)}</div>
                    </div>
                `;
                    const zBtn = card.querySelector('.cover-cand-zoom-btn');
                    if (zBtn) {
                        zBtn.addEventListener('click', ev => {
                            ev.stopPropagation();
                            const imgEl = card.querySelector('img');
                            const activeUrl = (imgEl && imgEl.src) ? imgEl.src : c.cover_url;
                            window.openCoverZoomLightbox(activeUrl, `${c.title || 'Book Cover'} (${c.source})`, () => {
                                window.erSelectWebCover(activeUrl, c.fallback_url || '', c.source, card);
                            });
                        });
                    }
                    card.addEventListener('click', () => {
                        const imgEl = card.querySelector('img');
                        const activeUrl = (imgEl && imgEl.src) ? imgEl.src : c.cover_url;
                        window.erSelectWebCover(activeUrl, c.fallback_url || '', c.source, card);
                    });
                    resultsEl.appendChild(card);
                });
            })
            .catch(err => {
                clearTimeout(timeoutId);
                if (searchBtn) searchBtn.disabled = false;
                if (err && err.name === 'AbortError') {
                    if (!isRetry) {
                        window.erRunCoverSearch(true);
                        return;
                    }
                    if (statusEl) statusEl.innerHTML = `Cover search timed out. <button type="button" onclick="window.erRunCoverSearch()" style="margin-left:6px; background:rgba(56,189,248,0.2); border:1px solid rgba(56,189,248,0.5); color:#38bdf8; border-radius:5px; padding:2px 8px; cursor:pointer; font-size:0.75rem;">Retry Search</button>`;
                    return;
                }
                if (statusEl) statusEl.textContent = 'Error searching covers: ' + err.message;
            });
    };

    window.erSelectWebCover = function (coverUrl, fallbackUrl, sourceName, cardEl) {
        if (!activeReviewCtx || !activeReviewCtx.data) return;
        const d = activeReviewCtx.data;
        const statusEl = document.getElementById('er-cover-finder-status');
        d.candidate_cover = coverUrl;
        d.UseDataUriCover = coverUrl;

        document.querySelectorAll('#er-cover-finder-results .cover-cand-card').forEach(el => {
            el.style.borderColor = 'rgba(255,255,255,0.12)';
            el.style.boxShadow = 'none';
        });
        if (cardEl) {
            cardEl.style.borderColor = '#4ade80';
            cardEl.style.boxShadow = '0 0 0 2px rgba(74, 222, 128, 0.5)';
        }

        // Update Candidate Cover preview box in Review Modal
        const candCoverRow = document.getElementById('er-field-cover-cand');
        if (candCoverRow) {
            const leftWrap = candCoverRow.querySelector('div');
            if (leftWrap) {
                leftWrap.innerHTML = `
                    <img src="${escapeHtml(coverUrl)}" alt="Cover" style="width:42px; height:60px; object-fit:cover; border-radius:4px; background:#020617; flex-shrink:0;">
                    <div>
                        <span style="font-size:0.68rem; color:#94a3b8; text-transform:uppercase; display:block;">Select Cover (Candidate)</span>
                        <span id="er-cover-lbl-cand" style="font-size:0.76rem; color:#4ade80; font-weight:600;">${escapeHtml(sourceName || 'Web Cover')}</span>
                    </div>
                `;
            }
        }
        window.erPickField('cover', 'cand', true);

        const itemId = activeReviewCtx.itemId || d.item_id;
        const fileId = activeReviewCtx.fileId || d.file_id || '';
        const erInput = document.getElementById('er-cover-search-input');
        if (itemId || fileId) {
            if (statusEl) statusEl.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Saving selected cover to BookDB...`;
            fetch(`${BASE_PREFIX}/api/cover-apply/`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCookie('csrftoken')
                },
                body: JSON.stringify({
                    item_id: itemId || '',
                    file_id: fileId,
                    file_path: d.file_path || '',
                    isbn: d.candidate_isbn || d.orig_isbn || '',
                    title: d.candidate_title || d.orig_title || '',
                    authors: d.candidate_authors || d.orig_authors || '',
                    query: erInput ? erInput.value.trim() : '',
                    cover_url: coverUrl,
                    fallback_url: fallbackUrl || '',
                    selected_candidate: (cardEl && cardEl._candData) || { source: sourceName || 'Web Cover', cover_url: coverUrl },
                    all_candidates: window._lastErCoverSearchCandidates || [],
                })
            })
                .then(r => r.json())
                .then(res => {
                    if (res.ok) {
                        const savedCover = res.cover_url || coverUrl;
                        const resolvedId = res.item_id || itemId;
                        if (res.item_id) {
                            activeReviewCtx.itemId = res.item_id;
                            d.item_id = res.item_id;
                        }
                        d.candidate_cover = savedCover;
                        d.UseDataUriCover = savedCover;
                        applyCardMetadataUpdate(resolvedId, { cover_url: savedCover }, true);
                        if (statusEl) statusEl.innerHTML = `<span style="color:#4ade80; font-weight:600;"><i class="fa-solid fa-check"></i> Cover selected and saved to BookDB!</span>`;
                    } else if (statusEl) {
                        statusEl.innerHTML = `<span style="color:#4ade80; font-weight:600;"><i class="fa-solid fa-check"></i> Cover selected for Approve Selected Fields!</span>`;
                    }
                })
                .catch(() => {
                    if (statusEl) statusEl.innerHTML = `<span style="color:#4ade80; font-weight:600;"><i class="fa-solid fa-check"></i> Cover selected for Approve Selected Fields!</span>`;
                });
        } else if (statusEl) {
            statusEl.innerHTML = `<span style="color:#4ade80; font-weight:600;"><i class="fa-solid fa-check"></i> Cover selected for Approve Selected Fields!</span>`;
        }
    };

    let currentPreviewPages = [];

    window.toggleBookPreviewInReview = function () {
        const drawer = document.getElementById('er-book-preview-drawer');
        if (!drawer || !activeReviewCtx) return;
        if (drawer.style.display === 'block') {
            drawer.style.display = 'none';
            return;
        }
        drawer.style.display = 'block';
        const { data, itemId, fileId } = activeReviewCtx;
        const key = `${data?.file_path || ''}|${itemId || ''}|${fileId || ''}`;
        if (drawer.dataset.loadedFor === key) return;

        const textBody = document.getElementById('er-preview-text-body');
        const imgStrip = document.getElementById('er-preview-images-strip');
        const tabsEl = document.getElementById('er-preview-section-tabs');
        const isbnsBar = document.getElementById('er-preview-isbns-bar');
        const metaInfo = document.getElementById('er-preview-meta-info');

        if (textBody) textBody.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Reading pages and cover images from book file on NAS...`;
        if (imgStrip) { imgStrip.style.display = 'none'; imgStrip.innerHTML = ''; }
        if (tabsEl) tabsEl.innerHTML = '';
        if (isbnsBar) { isbnsBar.style.display = 'none'; isbnsBar.innerHTML = ''; }
        if (metaInfo) metaInfo.textContent = '';

        const params = new URLSearchParams();
        if (data && data.file_path) params.set('file_path', data.file_path);
        if (fileId || (data && data.file_id)) params.set('file_id', fileId || data.file_id);
        if (itemId || (data && data.item_id)) params.set('item_id', itemId || data.item_id);

        const fetchReviewPeekWithRetry = (attempt = 1) => fetch(`${BASE_PREFIX}/api/book-preview/?${params.toString()}`)
            .then(async r => {
                const text = await r.text();
                try {
                    return JSON.parse(text);
                } catch (e) {
                    if (attempt < 2) {
                        await new Promise(res => setTimeout(res, 1500));
                        return fetchReviewPeekWithRetry(attempt + 1);
                    }
                    throw new Error(`Server returned HTTP ${r.status} (please click Peek Inside again)`);
                }
            });

        fetchReviewPeekWithRetry()
            .then(res => {
                if (!res.ok) {
                    if (textBody) textBody.textContent = res.error || 'Could not load book preview.';
                    return;
                }
                drawer.dataset.loadedFor = key;
                if (metaInfo) {
                    metaInfo.textContent = `${res.file_ext} • ${res.file_size_mb} MB • ${res.total_sections || 0} total pages/sections`;
                }
                const rawLink = document.getElementById('er-raw-stream-link');
                if (rawLink && res.raw_stream_url) {
                    rawLink.href = res.raw_stream_url;
                    rawLink.style.display = 'inline-flex';
                }

                // Detected ISBNs
                if (isbnsBar && Array.isArray(res.detected_isbns) && res.detected_isbns.length > 0) {
                    isbnsBar.style.display = 'flex';
                    isbnsBar.innerHTML = `<span style="color:#94a3b8;"><i class="fa-solid fa-barcode" style="color:#4ade80;"></i> ISBNs found inside book:</span>` +
                        res.detected_isbns.map(isbn =>
                            `<button type="button" onclick="document.getElementById('er-input-isbn').value='${escapeJsString(isbn)}'" style="background:rgba(74,222,128,0.16); border:1px solid rgba(74,222,128,0.45); color:#4ade80; border-radius:999px; padding:0.12rem 0.55rem; font-family:monospace; font-size:0.73rem; font-weight:700; cursor:pointer;" title="Click to copy ${escapeHtml(isbn)} into ISBN field">+ Use ${escapeHtml(isbn)}</button>`
                        ).join('');
                }

                // Images / Rendered Pages
                if (imgStrip && Array.isArray(res.images) && res.images.length > 0) {
                    imgStrip.style.display = 'flex';
                    imgStrip.innerHTML = res.images.map((img, i) => `
                        <div style="flex:0 0 auto; width:132px; background:rgba(15,23,42,0.9); border:1px solid rgba(255,255,255,0.14); border-radius:8px; padding:0.35rem; display:flex; flex-direction:column; align-items:center; gap:0.25rem;">
                            <img src="${img.data_uri}" alt="${escapeHtml(img.label)}" onclick="window.open().document.write('<body style=\\'margin:0;background:#0f172a;display:flex;justify-content:center;align-items:center;min-height:100vh;\\'><img src=\\'${img.data_uri}\\' style=\\'max-width:95vw;max-height:95vh;\\'></body>')" style="width:100%; height:160px; object-fit:contain; border-radius:5px; cursor:zoom-in; background:#020617;" title="Click to view full size: ${escapeHtml(img.label)}">
                            <span style="font-size:0.66rem; color:#94a3b8; text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; width:100%;">${escapeHtml(img.label)}</span>
                            <div style="display:flex; flex-direction:column; gap:0.22rem; width:100%;">
                                <button type="button" onclick="window.erUsePreviewCover(${i})" style="width:100%; background:rgba(56,189,248,0.15); border:1px solid rgba(56,189,248,0.38); color:#38bdf8; border-radius:4px; font-size:0.65rem; font-weight:600; padding:0.15rem 0.3rem; cursor:pointer;">Use as Cover</button>
                                <button type="button" onclick="window.erAiReevaluateFromPreview(${i}, this)" style="width:100%; background:rgba(168,85,247,0.2); border:1px solid rgba(168,85,247,0.48); color:#e9d5ff; border-radius:4px; font-size:0.65rem; font-weight:700; padding:0.16rem 0.3rem; cursor:pointer;" title="Have Gemini AI Vision inspect this page image to identify the book and re-evaluate enrichment"><i class="fa-solid fa-wand-magic-sparkles"></i> AI Re-Evaluate</button>
                            </div>
                        </div>
                    `).join('');
                    drawer._previewImages = res.images;
                }

                // Pages / Spine text sections
                currentPreviewPages = Array.isArray(res.pages) ? res.pages : [];
                if (currentPreviewPages.length === 0) {
                    if (textBody) textBody.textContent = 'No extractable text found in the front matter (scanned image PDF — inspect the page images above or click Open Full File).';
                } else {
                    if (tabsEl) {
                        tabsEl.innerHTML = currentPreviewPages.map((p, idx) => `
                            <button type="button" class="er-prev-tab-btn" data-idx="${idx}" onclick="window.erSelectPreviewSection(${idx})" style="background:${idx === 0 ? 'rgba(56,189,248,0.28)' : 'rgba(30,41,59,0.85)'}; border:1px solid ${idx === 0 ? 'rgba(56,189,248,0.6)' : 'rgba(255,255,255,0.12)'}; color:${idx === 0 ? '#f8fafc' : '#cbd5e1'}; border-radius:6px; padding:0.22rem 0.55rem; font-size:0.72rem; font-weight:600; cursor:pointer;">
                                ${escapeHtml(p.label)}
                            </button>
                        `).join('');
                    }
                    window.erSelectPreviewSection(0);
                }
            })
            .catch(err => {
                if (textBody) textBody.textContent = 'Network error loading book preview: ' + err.message;
            });
    };

    window.erSelectPreviewSection = function (idx) {
        const textBody = document.getElementById('er-preview-text-body');
        if (!textBody || !currentPreviewPages[idx]) return;
        textBody.textContent = currentPreviewPages[idx].text || '';
        textBody.scrollTop = 0;
        document.querySelectorAll('.er-prev-tab-btn').forEach(btn => {
            const active = Number(btn.getAttribute('data-idx')) === idx;
            btn.style.background = active ? 'rgba(56,189,248,0.28)' : 'rgba(30,41,59,0.85)';
            btn.style.borderColor = active ? 'rgba(56,189,248,0.6)' : 'rgba(255,255,255,0.12)';
            btn.style.color = active ? '#f8fafc' : '#cbd5e1';
        });
    };

    window.erUseSelectedPreviewText = function (targetField, btnEl = null) {
        const liveSel = (window.getSelection ? window.getSelection().toString() : '').trim();
        const sel = liveSel || (_lastPreviewSelectedText || '').trim();
        if (!sel) {
            alert('First highlight any text inside the book preview box below, then click this button to copy it into the field.');
            return;
        }
        const clean = sel.replace(/\s+/g, ' ').trim();
        const finalVal = targetField === 'desc' ? sel.trim() : clean;

        // 1. Update the Selected Metadata to Save input box (#er-input-...)
        const el = document.getElementById(`er-input-${targetField}`);
        if (el) {
            el.value = finalVal;
            el.style.borderColor = '#4ade80';
            el.style.boxShadow = '0 0 0 2px rgba(74, 222, 128, 0.45)';
            setTimeout(() => {
                el.style.borderColor = 'rgba(255,255,255,0.16)';
                el.style.boxShadow = '';
            }, 1400);
        }

        // 2. Also update activeReviewCtx.data AND the Candidate Match card (#er-field-<key>-cand) right below the preview box
        if (activeReviewCtx && activeReviewCtx.data) {
            const d = activeReviewCtx.data;
            if (d._initialCandidateTitle === undefined) d._initialCandidateTitle = d.candidate_title || '';
            if (d._initialCandidateAuthors === undefined) d._initialCandidateAuthors = d.candidate_authors || '';
            if (targetField === 'title') {
                d.candidate_title = finalVal;
                d._userCustomTitle = true;
            } else if (targetField === 'author') {
                d.candidate_authors = finalVal;
                d._userCustomAuthor = true;
            } else if (targetField === 'publisher') {
                d.candidate_publisher = finalVal;
            } else if (targetField === 'desc') {
                d.candidate_description = finalVal;
            }
        }
        const candBox = document.getElementById(`er-field-${targetField}-cand`);
        if (candBox) {
            const valSpans = candBox.querySelectorAll('span');
            const textSpan = valSpans.length > 0 ? valSpans[valSpans.length - 1] : null;
            if (textSpan) {
                textSpan.textContent = finalVal.length > 160 ? finalVal.slice(0, 160) + '…' : finalVal;
                textSpan.style.color = '#4ade80';
                textSpan.style.fontWeight = '600';
            }
        }
        window.erHighlightFieldSide(targetField, 'cand');

        // 3. Keep Cover Finder query in sync when Title or Author is updated from preview
        if (targetField === 'title' || targetField === 'author') {
            const erCovInput = document.getElementById('er-cover-search-input');
            if (erCovInput) {
                const curT = (document.getElementById('er-input-title')?.value || '').trim();
                const curA = (document.getElementById('er-input-author')?.value || '').trim();
                const cleanA = (curA && !['unknown', 'unknown author', '..'].includes(curA.toLowerCase())) ? curA : '';
                if (curT) erCovInput.value = [curT, cleanA].filter(Boolean).join(' ').trim();
            }
        }

        // 4. Visual button feedback + inline confirmation banner right inside Inside Book Preview
        const labels = { title: 'Title', author: 'Author', publisher: 'Publisher', desc: 'Synopsis' };
        const fieldLabel = labels[targetField] || targetField;
        if (btnEl) {
            const origHtml = btnEl.dataset.origHtml || btnEl.innerHTML;
            btnEl.dataset.origHtml = origHtml;
            btnEl.innerHTML = `<i class="fa-solid fa-check"></i> ${fieldLabel} Set!`;
            btnEl.style.background = 'rgba(16,185,129,0.28)';
            btnEl.style.borderColor = '#4ade80';
            btnEl.style.color = '#4ade80';
            setTimeout(() => {
                btnEl.innerHTML = origHtml;
                btnEl.style.background = 'rgba(56,189,248,0.16)';
                btnEl.style.borderColor = 'rgba(56,189,248,0.4)';
                btnEl.style.color = '#38bdf8';
            }, 1500);
        }

        const statusBar = document.getElementById('er-preview-copy-status');
        if (statusBar) {
            const shortVal = finalVal.length > 85 ? finalVal.slice(0, 85) + '…' : finalVal;
            statusBar.style.display = 'flex';
            statusBar.innerHTML = `
                <div style="min-width:0; flex:1;">
                    <strong style="color:#4ade80;"><i class="fa-solid fa-check"></i> ${escapeHtml(fieldLabel)} set to:</strong>
                    <span style="color:#f8fafc; font-weight:600; margin-left:0.25rem;">"${escapeHtml(shortVal)}"</span>
                </div>
                <div style="display:flex; flex-wrap:wrap; gap:0.4rem; align-items:center; flex-shrink:0;">
                    <button type="button" onclick="window.erApproveSelected()" style="background:rgba(74,222,128,0.24); border:1px solid rgba(74,222,128,0.6); color:#4ade80; border-radius:6px; padding:0.22rem 0.6rem; font-size:0.73rem; font-weight:700; cursor:pointer;" title="Save the selected fields immediately">
                        <i class="fa-solid fa-check"></i> Approve &amp; Save Now
                    </button>
                    <button type="button" onclick="window.erReEnrichFromModal()" style="background:rgba(56,189,248,0.22); border:1px solid rgba(56,189,248,0.55); color:#38bdf8; border-radius:6px; padding:0.22rem 0.6rem; font-size:0.73rem; font-weight:700; cursor:pointer;" title="Re-run epubmeta lookup using this Title/Author hint">
                        <i class="fa-solid fa-arrows-rotate"></i> Re-Enrich with Hint
                    </button>
                    <button type="button" onclick="window.erFocusManualEdit()" style="background:rgba(148,163,184,0.2); border:1px solid rgba(148,163,184,0.45); color:#e2e8f0; border-radius:6px; padding:0.22rem 0.55rem; font-size:0.72rem; font-weight:600; cursor:pointer;" title="Scroll down to Selected Metadata to Save">
                        <i class="fa-solid fa-arrow-down"></i> Edit Below
                    </button>
                </div>
            `;
        }
    };

    window.erUsePreviewCover = function (imgIdx) {
        const drawer = document.getElementById('er-book-preview-drawer');
        const imgs = drawer && drawer._previewImages ? drawer._previewImages : [];
        if (!imgs[imgIdx] || !activeReviewCtx || !activeReviewCtx.data) return;
        const chosenUri = imgs[imgIdx].data_uri;
        activeReviewCtx.data.candidate_cover = chosenUri;
        activeReviewCtx.data.UseDataUriCover = chosenUri;
        const candCoverRow = document.getElementById('er-field-cover-cand');
        if (candCoverRow) {
            const leftWrap = candCoverRow.querySelector('div');
            if (leftWrap) {
                leftWrap.innerHTML = `
                    <img src="${escapeHtml(chosenUri)}" alt="Cover" style="width:42px; height:60px; object-fit:cover; border-radius:4px; background:#020617; flex-shrink:0;">
                    <div>
                        <span style="font-size:0.68rem; color:#94a3b8; text-transform:uppercase; display:block;">Select Cover (Candidate)</span>
                        <span id="er-cover-lbl-cand" style="font-size:0.76rem; color:#4ade80; font-weight:600;">${escapeHtml(imgs[imgIdx].label || 'Preview Page Cover')}</span>
                    </div>
                `;
            }
        }
        window.erPickField('cover', 'cand', true);
        const statusBar = document.getElementById('er-preview-copy-status');
        if (statusBar) {
            statusBar.style.display = 'flex';
            statusBar.innerHTML = `
                <div style="min-width:0; flex:1;">
                    <strong style="color:#4ade80;"><i class="fa-solid fa-check"></i> Cover set to:</strong>
                    <span style="color:#f8fafc; font-weight:600; margin-left:0.25rem;">${escapeHtml(imgs[imgIdx].label || 'Preview Page')}</span>
                </div>
                <div style="display:flex; gap:0.4rem; align-items:center; flex-shrink:0;">
                    <button type="button" onclick="window.erApproveSelected()" style="background:rgba(74,222,128,0.24); border:1px solid rgba(74,222,128,0.6); color:#4ade80; border-radius:6px; padding:0.22rem 0.6rem; font-size:0.73rem; font-weight:700; cursor:pointer;">
                        <i class="fa-solid fa-check"></i> Approve &amp; Save Now
                    </button>
                </div>
            `;
        }
    };

    window.erAiReevaluateFromPreview = function (imgIdx = null, triggerBtn = null) {
        if (!activeReviewCtx) return;
        const { data, btn, updateModal, itemId, fileId } = activeReviewCtx;
        const drawer = document.getElementById('er-book-preview-drawer');
        const imgs = drawer && Array.isArray(drawer._previewImages) ? drawer._previewImages : [];
        const selectedUris = [];
        if (imgIdx !== null && imgIdx !== undefined && imgs[imgIdx] && imgs[imgIdx].data_uri) {
            selectedUris.push(imgs[imgIdx].data_uri);
        } else {
            imgs.slice(0, 3).forEach(im => {
                if (im && im.data_uri) selectedUris.push(im.data_uri);
            });
        }
        if (selectedUris.length === 0 && data && data.orig_cover && String(data.orig_cover).startsWith('data:image')) {
            selectedUris.push(data.orig_cover);
        }

        const origBtnHtml = triggerBtn ? triggerBtn.innerHTML : '';
        if (triggerBtn) {
            triggerBtn.disabled = true;
            triggerBtn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> AI Analyzing...`;
        }
        const diagEl = document.getElementById('er-diag-msg');
        if (diagEl) {
            diagEl.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Gemini AI Vision is inspecting the previewed book image(s) to extract Title, Author &amp; ISBN and re-evaluate enrichment...`;
        }

        const payload = {
            re_enrich: true,
            force: false,
            force_ai_vision: true,
            review_only: true,
            ai_image_data_uris: selectedUris,
        };
        if (fileId) payload.file_id = fileId;
        if (itemId) payload.item_id = itemId;
        if (data && data.file_path) payload.file_path = data.file_path;

        fetch(`${BASE_PREFIX}/api/enrich/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify(payload)
        })
            .then(async r => {
                const txt = await r.text();
                try {
                    return JSON.parse(txt);
                } catch (_) {
                    throw new Error(`Server returned HTTP ${r.status}`);
                }
            })
            .then(res => {
                if (triggerBtn) {
                    triggerBtn.disabled = false;
                    triggerBtn.innerHTML = origBtnHtml;
                }
                if (res.ok && res.moved_to_tomags) {
                    transitionCardEnrichBucket(res.item_id || itemId, 'tomags', 'needs_review');
                    adjustTomagsCounter(1);
                    setCardMovedToMagsState(res.item_id || itemId, res.file_id || fileId, res.moved_files || [], res.message, res);
                    window.closeEnrichReviewModal();
                    return;
                }
                if (res.ok) {
                    const targetId = itemId || res.requested_item_id || res.item_id;
                    transitionCardEnrichBucket(targetId, 'enriched', 'needs_review');
                    if (btn) {
                        btn.disabled = false;
                        btn._reviewData = null;
                        btn.innerHTML = `<i class="fa-solid fa-check"></i> Enriched!`;
                        btn.style.background = 'rgba(74,222,128,0.18)';
                        btn.style.borderColor = 'rgba(74,222,128,0.5)';
                        btn.style.color = '#4ade80';
                        btn.setAttribute('data-force', 'true');
                    }
                    applyCardMetadataUpdate(targetId, res, updateModal);
                    window.closeEnrichReviewModal();
                } else {
                    const mergedReviewData = Object.assign({}, data || {}, res);
                    if (btn) btn._reviewData = mergedReviewData;
                    openEnrichReviewModal(mergedReviewData, btn, updateModal, itemId, fileId);
                    if (res.candidate_title || res.candidate_authors) {
                        window.erUseAll('cand');
                    }
                }
            })
            .catch(err => {
                if (triggerBtn) {
                    triggerBtn.disabled = false;
                    triggerBtn.innerHTML = origBtnHtml;
                }
                if (diagEl) {
                    diagEl.textContent = 'Network error during AI Vision re-evaluation: ' + err.message;
                }
            });
    };

    window.closeEnrichReviewModal = function () {
        const modal = document.getElementById('enrich-review-modal');
        if (modal) modal.style.display = 'none';
    };

    window.erHighlightFieldSide = function (key, side) {
        ['orig', 'cand'].forEach(s => {
            const box = document.getElementById(`er-field-${key}-${s}`);
            const badge = document.getElementById(`er-badge-${key}-${s}`);
            const active = (s === side);
            if (box) {
                box.style.borderColor = active ? '#4ade80' : 'rgba(255,255,255,0.08)';
                box.style.background = active ? 'rgba(16,185,129,0.16)' : 'rgba(15,23,42,0.65)';
                box.style.boxShadow = active ? '0 0 0 1px rgba(74,222,128,0.28)' : 'none';
            }
            if (badge) {
                badge.style.display = active ? 'inline-flex' : 'none';
            }
        });
    };

    window.erPickField = function (key, side, fromUserClick = false) {
        if (!activeReviewCtx || !activeReviewCtx.data) return;
        const d = activeReviewCtx.data;
        if (key === 'cover') {
            const chosen = side === 'orig' ? (d.orig_cover || '') : (d.candidate_cover || '');
            d.UseDataUriCover = chosen;
            window.erHighlightFieldSide('cover', side);
            return;
        }
        const map = {
            title: side === 'orig' ? (d.orig_title || '') : (d.candidate_title || ''),
            author: side === 'orig' ? (d.orig_authors || '') : (d.candidate_authors || ''),
            publisher: side === 'orig' ? (d.orig_publisher || '') : (d.candidate_publisher || ''),
            pubdate: side === 'orig' ? (d.orig_pub_date || '') : (d.candidate_pub_date || ''),
            isbn: side === 'orig' ? (d.orig_isbn || '') : (d.candidate_isbn || ''),
            desc: side === 'orig' ? (d.orig_description || '') : (d.candidate_description || ''),
        };
        const inputEl = document.getElementById(`er-input-${key}`);
        if (inputEl) {
            inputEl.value = map[key] || '';
            if (fromUserClick) {
                inputEl.style.borderColor = '#4ade80';
                inputEl.style.boxShadow = '0 0 0 2px rgba(74,222,128,0.35)';
                setTimeout(() => {
                    inputEl.style.borderColor = 'rgba(255,255,255,0.16)';
                    inputEl.style.boxShadow = '';
                }, 700);
            }
        }
        window.erHighlightFieldSide(key, side);
    };

    window.erSelectAllSide = function (side) {
        if (!activeReviewCtx || !activeReviewCtx.data) return;
        const d = activeReviewCtx.data;
        if (side === 'cand') {
            // Keep Original Cover unless Original Cover is missing and Candidate has a cover
            window.erPickField('cover', (d.candidate_cover && !d.orig_cover) ? 'cand' : (d.orig_cover ? 'orig' : 'cand'), true);
            ['title', 'author', 'publisher', 'pubdate', 'isbn', 'desc'].forEach(k => {
                const candMap = {
                    title: d.candidate_title,
                    author: d.candidate_authors,
                    publisher: d.candidate_publisher,
                    pubdate: d.candidate_pub_date,
                    isbn: d.candidate_isbn,
                    desc: d.candidate_description,
                };
                window.erPickField(k, candMap[k] ? 'cand' : 'orig', true);
            });
        } else {
            ['cover', 'title', 'author', 'publisher', 'pubdate', 'isbn', 'desc'].forEach(k => window.erPickField(k, 'orig', true));
        }
    };
    window.erUseAll = window.erSelectAllSide;

    function adjustLiveEnrichCounters({ enrichedDelta = 0, needsEnrichingDelta = 0, needsReviewDelta = 0, noMatchDelta = 0 }) {
        const parseCount = (str) => {
            const digits = String(str || '').replace(/[^0-9]/g, '');
            return digits ? parseInt(digits, 10) : null;
        };
        const fmtCount = (n) => Math.max(0, n).toLocaleString('en-US');

        const sbEnriched = document.getElementById('sidebar-enriched-count');
        if (sbEnriched && enrichedDelta !== 0) {
            const cur = parseCount(sbEnriched.textContent);
            if (cur !== null) sbEnriched.textContent = fmtCount(cur + enrichedDelta);
        }
        const hdrEnriched = document.getElementById('header-enriched-count');
        if (hdrEnriched && enrichedDelta !== 0) {
            const cur = parseCount(hdrEnriched.textContent);
            if (cur !== null) hdrEnriched.textContent = fmtCount(cur + enrichedDelta);
        }
        const sbNeeds = document.getElementById('sidebar-needs-enriching-count');
        if (sbNeeds && needsEnrichingDelta !== 0) {
            const cur = parseCount(sbNeeds.textContent);
            if (cur !== null) sbNeeds.textContent = fmtCount(cur + needsEnrichingDelta);
        }
        const sbReview = document.getElementById('sidebar-needs-review-count');
        if (sbReview && needsReviewDelta !== 0) {
            const cur = parseCount(sbReview.textContent);
            if (cur !== null) sbReview.textContent = fmtCount(cur + needsReviewDelta);
        }
        const sbNoMatch = document.getElementById('sidebar-no-match-count');
        if (sbNoMatch && noMatchDelta !== 0) {
            const cur = parseCount(sbNoMatch.textContent);
            if (cur !== null) sbNoMatch.textContent = fmtCount(cur + noMatchDelta);
        }

        // Also update the "(of N)" total in the top results-count bar when viewing that shelf
        const qs = window.location.search || '';
        const isNoMatchShelf = qs.includes('no_match') || qs.includes('no_isbn') || qs.includes('not_found');
        const isReviewShelf = (qs.includes('needs_review') || qs.includes('in_review')) && !isNoMatchShelf;
        const isEnrichedShelf = qs.includes('shelf=enriched') || qs.includes('enrich_status=enriched');
        const isNeedsEnrichShelf = qs.includes('needs_enriching');
        const activeShelfDelta = isNoMatchShelf ? noMatchDelta : (isReviewShelf ? needsReviewDelta : (isEnrichedShelf ? enrichedDelta : (isNeedsEnrichShelf ? needsEnrichingDelta : 0)));
        if (activeShelfDelta !== 0) {
            const resCountEl = document.querySelector('.results-count');
            if (resCountEl) {
                const strongEls = resCountEl.querySelectorAll('strong');
                if (strongEls.length >= 2) {
                    const totalEl = strongEls[1];
                    const curTot = parseCount(totalEl.textContent);
                    if (curTot !== null) totalEl.textContent = fmtCount(curTot + activeShelfDelta);
                }
            }
        }

        const topSel = document.getElementById('top-enrich-status-select');
        if (topSel) {
            Array.from(topSel.options).forEach(opt => {
                if (opt.value === 'enriched' && enrichedDelta !== 0) {
                    const m = opt.textContent.match(/\(([0-9,]+)\)/);
                    if (m) {
                        const nxt = fmtCount(parseInt(m[1].replace(/,/g, ''), 10) + enrichedDelta);
                        opt.textContent = opt.textContent.replace(/\([0-9,]+\)/, `(${nxt})`);
                    }
                } else if (opt.value === 'needs_enriching' && needsEnrichingDelta !== 0) {
                    const m = opt.textContent.match(/\(([0-9,]+)\)/);
                    if (m) {
                        const nxt = fmtCount(parseInt(m[1].replace(/,/g, ''), 10) + needsEnrichingDelta);
                        opt.textContent = opt.textContent.replace(/\([0-9,]+\)/, `(${nxt})`);
                    }
                } else if (opt.value === 'needs_review' && needsReviewDelta !== 0) {
                    const m = opt.textContent.match(/\(([0-9,]+)\)/);
                    if (m) {
                        const nxt = fmtCount(parseInt(m[1].replace(/,/g, ''), 10) + needsReviewDelta);
                        opt.textContent = opt.textContent.replace(/\([0-9,]+\)/, `(${nxt})`);
                    }
                } else if (opt.value === 'no_match' && noMatchDelta !== 0) {
                    const m = opt.textContent.match(/\(([0-9,]+)\)/);
                    if (m) {
                        const nxt = fmtCount(parseInt(m[1].replace(/,/g, ''), 10) + noMatchDelta);
                        opt.textContent = opt.textContent.replace(/\([0-9,]+\)/, `(${nxt})`);
                    }
                }
            });
        }
    }

    const _itemEnrichBucketMap = {};

    function getCardEnrichBucket(itemId, fallbackBucket = 'needs_enriching') {
        const key = String(itemId || '');
        const card = key ? document.getElementById(`card-${key}`) : null;
        if (card) {
            const attr = card.getAttribute('data-enrich-bucket');
            if (attr) return attr;
            const detBtn = card.querySelector('.btn-details');
            if (detBtn && detBtn.getAttribute('data-is-enriched') === 'true') return 'enriched';
            const qs = window.location.search || '';
            if (qs.includes('needs_enriching')) return 'needs_enriching';
            if (qs.includes('no_match') || qs.includes('no_isbn') || qs.includes('not_found')) return 'no_match';
            if (qs.includes('needs_review') || qs.includes('in_review')) return 'needs_review';
            const st = detBtn ? (detBtn.getAttribute('data-enrich-status') || '').toUpperCase() : '';
            if (st === 'NEEDS_REVIEW') return 'needs_review';
            if (st) return 'no_match';
            return 'needs_enriching';
        }
        if (key && _itemEnrichBucketMap[key]) return _itemEnrichBucketMap[key];
        const qs = window.location.search || '';
        if (qs.includes('needs_enriching')) return 'needs_enriching';
        if (qs.includes('no_match') || qs.includes('no_isbn') || qs.includes('not_found')) return 'no_match';
        if (qs.includes('needs_review') || qs.includes('in_review')) return 'needs_review';
        return fallbackBucket || 'needs_enriching';
    }

    function transitionCardEnrichBucket(itemId, newBucket, fallbackOldBucket = 'needs_enriching') {
        const key = String(itemId || '');
        const oldBucket = getCardEnrichBucket(key, fallbackOldBucket);
        const card = key ? document.getElementById(`card-${key}`) : null;
        if (card) {
            card.setAttribute('data-enrich-bucket', newBucket);
        }
        if (key) {
            _itemEnrichBucketMap[key] = newBucket;
        }
        if (oldBucket === newBucket) return;
        adjustLiveEnrichCounters({
            enrichedDelta: (newBucket === 'enriched' ? 1 : 0) - (oldBucket === 'enriched' ? 1 : 0),
            needsEnrichingDelta: (newBucket === 'needs_enriching' ? 1 : 0) - (oldBucket === 'needs_enriching' ? 1 : 0),
            needsReviewDelta: (newBucket === 'needs_review' ? 1 : 0) - (oldBucket === 'needs_review' ? 1 : 0),
            noMatchDelta: (newBucket === 'no_match' ? 1 : 0) - (oldBucket === 'no_match' ? 1 : 0),
        });
    }

    window.erApproveSelected = function () {
        if (!activeReviewCtx) return;
        const { data, btn, updateModal, itemId } = activeReviewCtx;
        const approveBtn = document.getElementById('er-btn-approve');
        if (approveBtn) {
            approveBtn.disabled = true;
            approveBtn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Saving...`;
        }
        // Prefer the Original Cover first unless missing; fall back to candidate cover
        const rawCover = data.orig_cover || data.candidate_cover || '';
        const selectedMetadata = {
            title: (document.getElementById('er-input-title')?.value || '').trim(),
            author: (document.getElementById('er-input-author')?.value || '').trim(),
            publisher: (document.getElementById('er-input-publisher')?.value || '').trim(),
            pub_date: (document.getElementById('er-input-pubdate')?.value || '').trim(),
            isbn: (document.getElementById('er-input-isbn')?.value || '').trim(),
            description: (document.getElementById('er-input-desc')?.value || '').trim(),
            cover: data.UseDataUriCover !== undefined ? data.UseDataUriCover : rawCover,
        };
        const doPostApprove = (attempt = 1) => fetch(`${BASE_PREFIX}/api/status/items/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({
                action: 'approve',
                file_path: data.file_path,
                item_id: itemId,
                selected_metadata: selectedMetadata,
            })
        }).then(async r => {
            const text = await r.text();
            try {
                return JSON.parse(text);
            } catch (e) {
                if (attempt < 2) {
                    await new Promise(res => setTimeout(res, 1500));
                    return doPostApprove(attempt + 1);
                }
                throw new Error(`Server returned HTTP ${r.status} (please click Approve Selected Fields again)`);
            }
        });
        doPostApprove()
            .then(res => {
                if (approveBtn) {
                    approveBtn.disabled = false;
                    approveBtn.innerHTML = `<i class="fa-solid fa-check"></i> Approve Selected Fields`;
                }
                if (res.ok) {
                    if (btn) {
                        btn._reviewData = null;
                        btn.innerHTML = `<i class="fa-solid fa-check"></i> Enriched!`;
                        btn.style.background = 'rgba(74,222,128,0.18)';
                        btn.style.borderColor = 'rgba(74,222,128,0.5)';
                        btn.style.color = '#4ade80';
                    }
                    transitionCardEnrichBucket(itemId, 'enriched', 'needs_review');
                    applyCardMetadataUpdate(itemId, {
                        item_id: res.item_id || itemId,
                        title: selectedMetadata.title,
                        authors: selectedMetadata.author,
                        publisher: selectedMetadata.publisher,
                        pub_date: selectedMetadata.pub_date,
                        isbn: selectedMetadata.isbn,
                        description: selectedMetadata.description,
                        cover_url: res.cover_url || selectedMetadata.cover || rawCover,
                        merged_item_ids: res.merged_item_ids || [],
                    }, updateModal || Boolean(window._activeModalItem && String(window._activeModalItem.itemId) === String(itemId)));
                    window.closeEnrichReviewModal();
                } else {
                    alert(res.error || 'Could not save approved metadata.');
                }
            })
            .catch(err => {
                if (approveBtn) {
                    approveBtn.disabled = false;
                    approveBtn.innerHTML = `<i class="fa-solid fa-check"></i> Approve Selected Fields`;
                }
                alert('Network error saving metadata: ' + err.message);
            });
    };

    window.erReEnrichFromModal = function () {
        if (!activeReviewCtx) return;
        const { data, btn, updateModal, itemId, fileId } = activeReviewCtx;
        const reBtn = document.getElementById('er-btn-reenrich');
        const diagEl = document.getElementById('er-diag-msg');
        if (reBtn) {
            reBtn.disabled = true;
            reBtn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Re-Enriching...`;
        }
        if (diagEl) {
            diagEl.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Running fresh epubmeta lookup using current Title / Author / ISBN hints...`;
        }

        const rawTitle = (document.getElementById('er-input-title')?.value || '').trim();
        const rawAuthors = (document.getElementById('er-input-author')?.value || '').trim();
        const rawIsbn = (document.getElementById('er-input-isbn')?.value || '').trim();

        // Only send hint_title / hint_authors / hint_isbn if the user customized them away from the rejected candidate
        // (and away from internal 000_... PDF codes), so Re-Enrich doesn't just re-query the rejected candidate!
        const candTitle = (data && (data._initialCandidateTitle !== undefined ? data._initialCandidateTitle : data.candidate_title) ? String(data._initialCandidateTitle !== undefined ? data._initialCandidateTitle : data.candidate_title) : '').trim();
        const candAuthors = (data && (data._initialCandidateAuthors !== undefined ? data._initialCandidateAuthors : data.candidate_authors) ? String(data._initialCandidateAuthors !== undefined ? data._initialCandidateAuthors : data.candidate_authors) : '').trim();
        const candIsbn = (data && data.candidate_isbn ? String(data.candidate_isbn) : '').trim();
        const isInternalCode = /^\d{3}_[A-Za-z0-9_]+$/.test(rawTitle);

        const hintTitle = (rawTitle && (Boolean(data && data._userCustomTitle) || rawTitle !== candTitle) && !isInternalCode) ? rawTitle : '';
        const hintAuthors = (rawAuthors && (Boolean(data && data._userCustomAuthor) || rawAuthors !== candAuthors) && rawAuthors.toLowerCase() !== 'asura01') ? rawAuthors : '';
        const hintIsbn = (rawIsbn && rawIsbn !== candIsbn) ? rawIsbn : '';

        const payload = {
            re_enrich: true,
            force: false,
            hint_title: hintTitle,
            hint_authors: hintAuthors,
            hint_isbn: hintIsbn,
        };
        if (fileId) payload.file_id = fileId;
        if (itemId) payload.item_id = itemId;
        if (data && data.file_path) payload.file_path = data.file_path;

        fetch(`${BASE_PREFIX}/api/enrich/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify(payload)
        })
            .then(async r => {
                const txt = await r.text();
                try {
                    return JSON.parse(txt);
                } catch (_) {
                    throw new Error(`Server returned HTTP ${r.status}`);
                }
            })
            .then(res => {
                if (reBtn) {
                    reBtn.disabled = false;
                    reBtn.innerHTML = `<i class="fa-solid fa-arrows-rotate"></i> Re-Enrich`;
                }
                if (res.ok && res.moved_to_tomags) {
                    transitionCardEnrichBucket(res.item_id || itemId, 'tomags', 'needs_review');
                    adjustTomagsCounter(1);
                    setCardMovedToMagsState(res.item_id || itemId, res.file_id || fileId, res.moved_files || [], res.message, res);
                    window.closeEnrichReviewModal();
                    return;
                }
                if (res.ok) {
                    const targetId = itemId || res.requested_item_id || res.item_id;
                    transitionCardEnrichBucket(targetId, 'enriched', 'needs_review');
                    if (btn) {
                        btn.disabled = false;
                        btn._reviewData = null;
                        btn.innerHTML = `<i class="fa-solid fa-check"></i> Enriched!`;
                        btn.style.background = 'rgba(74,222,128,0.18)';
                        btn.style.borderColor = 'rgba(74,222,128,0.5)';
                        btn.style.color = '#4ade80';
                        btn.setAttribute('data-force', 'true');
                    }
                    applyCardMetadataUpdate(targetId, res, updateModal);
                    window.closeEnrichReviewModal();
                } else {
                    const mergedReviewData = Object.assign({}, data || {}, res);
                    if (btn) btn._reviewData = mergedReviewData;
                    openEnrichReviewModal(mergedReviewData, btn, updateModal, itemId, fileId);
                    if (res.candidate_title || res.candidate_authors) {
                        window.erUseAll('cand');
                    }
                }
            })
            .catch(err => {
                if (reBtn) {
                    reBtn.disabled = false;
                    reBtn.innerHTML = `<i class="fa-solid fa-arrows-rotate"></i> Re-Enrich`;
                }
                if (diagEl) {
                    diagEl.textContent = 'Network error during Re-Enrich: ' + err.message;
                }
            });
    };

    window.erForceWriteCandidate = function () {
        if (!activeReviewCtx) return;
        const { btn, updateModal, itemId, fileId } = activeReviewCtx;
        if (btn) {
            btn._wasInReview = true;
            btn._reviewData = null;
        }
        window.closeEnrichReviewModal();
        triggerOnDemandEnrich({ itemId, fileId, force: true, btn, updateModal });
    };

    window.erKeepOriginalIgnore = function () {
        if (!activeReviewCtx) return;
        const { data, btn } = activeReviewCtx;
        fetch(`${BASE_PREFIX}/api/status/items/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({
                action: 'ignore',
                file_path: data.file_path,
            })
        })
            .then(async r => {
                const txt = await r.text();
                try {
                    return JSON.parse(txt);
                } catch (_) {
                    throw new Error(`Server returned HTTP ${r.status}`);
                }
            })
            .then(res => {
                if (res.ok && btn) {
                    btn._reviewData = null;
                    btn.innerHTML = `<i class="fa-solid fa-shield-halved"></i> Original Kept`;
                    btn.style.background = 'rgba(148,163,184,0.2)';
                    btn.style.borderColor = 'rgba(148,163,184,0.45)';
                    btn.style.color = '#cbd5e1';
                    adjustLiveEnrichCounters({ needsReviewDelta: -1 });
                }
                window.closeEnrichReviewModal();
            });
    };

    window.erMoveToMag = function () {
        if (!activeReviewCtx) return;
        const { btn, itemId, fileId } = activeReviewCtx;
        window.closeEnrichReviewModal();
        triggerMoveToMags({ itemId, fileId, btn });
    };

    window.erRevertToOriginal = function () {
        if (!activeReviewCtx) return;
        const { data, btn, updateModal, itemId, fileId } = activeReviewCtx;
        const revBtn = document.getElementById('er-btn-revert');
        window.revertItemEnrichment({
            itemId,
            fileId,
            filePath: data ? data.file_path : '',
            btn: revBtn || btn,
            cardBtn: btn,
            updateModal: Boolean(updateModal),
            closeReviewModal: true,
        });
    };

    window.revertItemEnrichment = function ({ itemId, fileId, filePath = '', btn = null, cardBtn = null, updateModal = true, closeReviewModal = false }) {
        const origHtml = btn ? btn.innerHTML : '';
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Reverting...`;
        }

        const payload = {};
        if (itemId) payload.item_id = itemId;
        if (fileId) payload.file_id = fileId;
        if (filePath) payload.file_path = filePath;

        fetch(`${BASE_PREFIX}/api/revert-enrich/`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify(payload)
        })
            .then(r => r.json())
            .then(data => {
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = origHtml || `<i class="fa-solid fa-rotate-left"></i> Revert to Original`;
                }
                if (!data.ok) {
                    alert(data.error || data.message || 'Could not revert item metadata.');
                    return;
                }
                const targetId = data.item_id || itemId;
                transitionCardEnrichBucket(targetId, 'needs_enriching', 'enriched');
                const card = targetId ? document.getElementById(`card-${targetId}`) : null;
                if (card) {
                    const titleEl = card.querySelector('.card-title');
                    const authorEl = card.querySelector('.card-author');
                    const descEl = card.querySelector('.card-description');
                    const detailsBtn = card.querySelector('.btn-details');
                    if (data.title && titleEl) {
                        titleEl.textContent = data.title;
                        titleEl.title = data.title;
                    }
                    if (data.authors && authorEl) {
                        authorEl.innerHTML = renderAuthorLinksHtml(data.authors);
                        authorEl.title = `by ${data.authors}`;
                    }
                    if (descEl) {
                        descEl.textContent = data.description || 'No description available.';
                    }
                    if (data.cover_url) {
                        const headerImg = card.querySelector('.card-header-image');
                        const imgEl = headerImg ? headerImg.querySelector('.cover-image') : null;
                        if (imgEl) imgEl.src = data.cover_url;
                    }
                    if (detailsBtn) {
                        detailsBtn.setAttribute('data-title', data.title || '');
                        detailsBtn.setAttribute('data-authors', data.authors || '');
                        detailsBtn.setAttribute('data-desc', data.description || '');
                        detailsBtn.setAttribute('data-isbn', data.isbn || '');
                        detailsBtn.setAttribute('data-publisher', data.publisher || '');
                        detailsBtn.setAttribute('data-pubdate', data.pub_date || '');
                        if (data.cover_url) detailsBtn.setAttribute('data-cover', data.cover_url);
                        detailsBtn.setAttribute('data-is-enriched', 'false');
                        detailsBtn.setAttribute('data-enrich-status', '');
                        detailsBtn.setAttribute('data-enrich-error', '');
                        detailsBtn._enrichResultData = null;
                        try {
                            const fArr = JSON.parse(detailsBtn.getAttribute('data-files') || '[]');
                            fArr.forEach(f => {
                                f.is_enriched = false;
                                f.enrich_status = '';
                                f.enrich_error = '';
                                f.tags = [];
                            });
                            detailsBtn.setAttribute('data-files', JSON.stringify(fArr));
                        } catch (e) { }
                    }
                    const cEnrBtn = cardBtn || card.querySelector('.btn-enrich-card');
                    if (cEnrBtn) {
                        cEnrBtn.disabled = false;
                        cEnrBtn._reviewData = null;
                        cEnrBtn.removeAttribute('data-review-json');
                        cEnrBtn.setAttribute('data-force', 'false');
                        cEnrBtn.style.background = 'rgba(168,85,247,0.16)';
                        cEnrBtn.style.borderColor = 'rgba(168,85,247,0.45)';
                        cEnrBtn.style.color = '#c084fc';
                        cEnrBtn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> Enrich Now`;
                        cEnrBtn.title = 'Run epubmeta enrichment on this book right now';
                    }
                    const cReBtn = card.querySelector('.btn-reenrich-card');
                    if (cReBtn) {
                        cReBtn.style.display = 'none';
                    }
                }
                if (updateModal) {
                    if (window._activeModalItem) {
                        window._activeModalItem.title = data.title || '';
                        window._activeModalItem.authors = data.authors || '';
                        window._activeModalItem.isbn = data.isbn || '';
                        window._activeModalItem.isEnriched = false;
                    }
                    if (data.title) document.getElementById('modal-title').textContent = data.title;
                    if (data.authors) document.getElementById('modal-authors').innerHTML = renderAuthorLinksHtml(data.authors);
                    const modalDesc = document.getElementById('modal-desc');
                    if (modalDesc) {
                        modalDesc.innerHTML = data.description
                            ? escapeHtml(data.description).replace(/\n/g, '<br>')
                            : 'No description available for this item.';
                    }
                    if (data.cover_url) {
                        const coverImg = document.getElementById('modal-cover');
                        if (coverImg) {
                            coverImg.src = data.cover_url;
                            coverImg.style.display = 'block';
                        }
                    }
                    const bottomTagsEl = document.getElementById('modal-bottom-tags-list');
                    if (bottomTagsEl && typeof window._renderReadOnlyTagsHtml === 'function') {
                        bottomTagsEl.innerHTML = window._renderReadOnlyTagsHtml([]);
                    }
                    const errBanner = document.getElementById('modal-enrich-error-banner');
                    if (errBanner) {
                        errBanner.style.display = 'none';
                        errBanner.innerHTML = '';
                    }
                    const topEnrBtn = document.getElementById('modal-top-enrich-btn');
                    if (topEnrBtn) {
                        topEnrBtn.style.cssText = 'padding:0.25rem 0.65rem; border-radius:6px; font-size:0.78rem; font-weight:700; cursor:pointer; background:rgba(168,85,247,0.22); border:1px solid rgba(168,85,247,0.55); color:#c084fc;';
                        topEnrBtn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i> Enrich Metadata Now (epubmeta)';
                    }
                    const topRevBtn = document.getElementById('modal-top-revert-btn');
                    if (topRevBtn) {
                        topRevBtn.style.display = 'none';
                    }
                    document.querySelectorAll('#modal-files-list .modal-file-item').forEach(li => {
                        const enrBadge = li.querySelector('.file-enriched-badge');
                        if (enrBadge) enrBadge.remove();
                        const fTagsRow = li.querySelector('.file-tags-row');
                        if (fTagsRow) {
                            fTagsRow.style.display = 'none';
                            fTagsRow.innerHTML = '';
                        }
                        const fErrBox = li.querySelector('.file-err-box');
                        if (fErrBox) fErrBox.remove();
                        const fBtn = li.querySelector('.btn-enrich-file');
                        if (fBtn) {
                            fBtn.style.background = 'rgba(168,85,247,0.16)';
                            fBtn.style.borderColor = 'rgba(168,85,247,0.45)';
                            fBtn.style.color = '#c084fc';
                            fBtn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i> Enrich File';
                            fBtn.removeAttribute('data-force');
                        }
                        const rBtn = li.querySelector('.btn-revert-file');
                        if (rBtn) rBtn.style.display = 'none';
                    });
                }
                if (closeReviewModal) {
                    window.closeEnrichReviewModal();
                }
            })
            .catch(err => {
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = origHtml || `<i class="fa-solid fa-rotate-left"></i> Revert to Original`;
                }
                console.error('Error reverting enrichment:', err);
                alert('Network error while reverting metadata: ' + err.message);
            });
    };

    const MAX_CONCURRENT_ENRICH = 1;
    let activeEnrichCount = 0;
    const enrichQueue = [];
    const activeEnrichControllers = new Set();
    let batchPageTotal = 0;
    let batchPageDone = 0;

    function abortAllEnrichRequests() {
        enrichQueue.length = 0;
        activeEnrichControllers.forEach(ctrl => {
            try { ctrl.abort(); } catch (e) { }
        });
        activeEnrichControllers.clear();
        activeEnrichCount = 0;
    }

    window.addEventListener('beforeunload', () => {
        abortAllEnrichRequests();
    });

    function getUnenrichedCardButtons() {
        return Array.from(document.querySelectorAll('.btn-enrich-card')).filter(btn => {
            if (btn.disabled) return false;
            if (btn.getAttribute('data-force') === 'true') return false;
            if (btn._reviewData) return false;
            if (btn.dataset.batchTouched === 'true') return false;
            return true;
        });
    }

    function getReviewCardButtons() {
        return Array.from(document.querySelectorAll('.btn-enrich-card')).filter(btn => {
            if (btn.disabled) return false;
            if (!btn._reviewData) return false;
            if (btn.dataset.batchTouched === 'true') return false;
            return true;
        });
    }

    function updateEnrichPageButtons() {
        const pageBtns = document.querySelectorAll('.btn-enrich-page');
        if (!pageBtns.length) return;
        const remainingCards = getUnenrichedCardButtons();
        const reviewCards = getReviewCardButtons();
        const inFlight = activeEnrichCount + enrichQueue.length;

        pageBtns.forEach(b => {
            if (batchPageTotal > 0 && inFlight > 0) {
                b.style.display = 'inline-flex';
                b.disabled = true;
                b.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Enriching Page (${batchPageDone}/${batchPageTotal})...`;
            } else if (batchPageTotal > 0 && inFlight === 0 && remainingCards.length === 0 && reviewCards.length === 0) {
                b.style.display = 'inline-flex';
                b.disabled = true;
                b.style.background = 'rgba(74,222,128,0.18)';
                b.style.borderColor = 'rgba(74,222,128,0.5)';
                b.style.color = '#4ade80';
                b.innerHTML = `<i class="fa-solid fa-check-double"></i> Page Enriched (${batchPageDone}/${batchPageTotal})`;
            } else if (remainingCards.length > 0) {
                b.style.display = 'inline-flex';
                b.disabled = false;
                b.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> Enrich Page (${remainingCards.length})`;
            } else if (reviewCards.length > 0) {
                b.style.display = 'inline-flex';
                b.disabled = false;
                b.innerHTML = `<i class="fa-solid fa-arrows-rotate"></i> Re-Enrich Page (${reviewCards.length})`;
            } else {
                b.style.display = 'none';
            }
        });
    }

    window.enrichAllOnPage = function() {
        const unenrichedTargets = getUnenrichedCardButtons();
        if (unenrichedTargets.length > 0) {
            batchPageTotal = unenrichedTargets.length;
            batchPageDone = 0;
            unenrichedTargets.forEach(btn => {
                const itemId = btn.getAttribute('data-item-id');
                btn.dataset.batchTouched = 'true';
                triggerOnDemandEnrich({ itemId, force: false, reEnrich: false, btn, updateModal: false, silentReview: true });
            });
            updateEnrichPageButtons();
            return;
        }
        const reviewTargets = getReviewCardButtons();
        if (!reviewTargets.length) return;
        batchPageTotal = reviewTargets.length;
        batchPageDone = 0;
        reviewTargets.forEach(btn => {
            const itemId = btn.getAttribute('data-item-id');
            const cardEl = itemId ? document.getElementById(`card-${itemId}`) : btn.closest('.media-card');
            const reBtn = cardEl ? cardEl.querySelector('.btn-reenrich-card') : null;
            btn.dataset.batchTouched = 'true';
            triggerOnDemandEnrich({ itemId, force: false, reEnrich: true, btn, reBtn, updateModal: false, silentReview: true });
        });
        updateEnrichPageButtons();
    };

    function pumpEnrichQueue() {
        while (activeEnrichCount < MAX_CONCURRENT_ENRICH && enrichQueue.length > 0) {
            const nextTask = enrichQueue.shift();
            activeEnrichCount++;
            updateEnrichPageButtons();
            executeOnDemandEnrich(nextTask).finally(() => {
                activeEnrichCount = Math.max(0, activeEnrichCount - 1);
                if (batchPageTotal > 0) {
                    batchPageDone = Math.min(batchPageTotal, batchPageDone + 1);
                }
                pumpEnrichQueue();
                updateEnrichPageButtons();
            });
        }
    }

    function triggerOnDemandEnrich({ itemId, fileId, force = false, reEnrich = false, btn, reBtn = null, updateModal = false, silentReview = false }) {
        if (!btn) return;
        if (btn._reviewData && !force && !reEnrich) {
            openEnrichReviewModal(btn._reviewData, btn, updateModal, itemId, fileId);
            return;
        }
        btn.disabled = true;
        btn.dataset.batchTouched = 'true';
        if (reBtn) {
            reBtn.disabled = true;
            reBtn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i>`;
        }
        if (activeEnrichCount >= MAX_CONCURRENT_ENRICH) {
            btn.innerHTML = `<i class="fa-solid fa-clock"></i> Queued...`;
        } else {
            btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> ${reEnrich ? 'Re-Enriching...' : 'Enriching...'}`;
        }
        enrichQueue.push({ itemId, fileId, force, reEnrich, btn, reBtn, updateModal, silentReview });
        pumpEnrichQueue();
        updateEnrichPageButtons();
    }

    function executeOnDemandEnrich({ itemId, fileId, force = false, reEnrich = false, btn, reBtn = null, updateModal = false, silentReview = false }) {
        const cardElBefore = itemId ? document.getElementById(`card-${itemId}`) : (btn ? btn.closest('.media-card') : null);
        const detBtnBefore = cardElBefore ? cardElBefore.querySelector('.btn-details') : null;
        const wasInReview = Boolean(
            reEnrich
            || (btn && (btn._wasInReview || btn._reviewData || btn.hasAttribute('data-review-json')))
            || (detBtnBefore && detBtnBefore.getAttribute('data-enrich-status'))
            || window.location.search.includes('needs_review')
            || window.location.search.includes('in_review')
        );
        btn.disabled = true;
        btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> ${reEnrich ? 'Re-Enriching...' : 'Enriching...'}`;
        if (reBtn) {
            reBtn.disabled = true;
            reBtn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i>`;
        }

        const optAiDescNoMatch = document.getElementById('opt-ai-desc-no-match');
        const payload = {
            force: Boolean(force),
            re_enrich: Boolean(reEnrich),
            ai_describe_no_match: Boolean(optAiDescNoMatch && optAiDescNoMatch.checked),
        };
        if (fileId) payload.file_id = fileId;
        else if (itemId) {
            payload.item_id = itemId;
            const cardEl = document.getElementById(`card-${itemId}`);
            const detBtn = cardEl ? cardEl.querySelector('.btn-details') : null;
            if (detBtn) {
                payload.title = detBtn.getAttribute('data-title') || '';
                payload.authors = detBtn.getAttribute('data-authors') || '';
                payload.isbn = detBtn.getAttribute('data-isbn') || '';
                try {
                    const fArr = JSON.parse(detBtn.getAttribute('data-files') || '[]');
                    if (fArr.length > 0) {
                        if (fArr[0].id) payload.fallback_file_id = fArr[0].id;
                        if (fArr[0].file_path) payload.file_path = fArr[0].file_path;
                    }
                } catch (e) { }
            }
        }

        const abortCtrl = new AbortController();
        activeEnrichControllers.add(abortCtrl);

        const doFetchEnrich = (attempt = 1) => fetch(`${BASE_PREFIX}/api/enrich/`, {
            method: 'POST',
            signal: abortCtrl.signal,
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify(payload)
        })
            .then(async resp => {
                const rawText = await resp.text();
                try {
                    return JSON.parse(rawText);
                } catch (parseErr) {
                    if (attempt < 2 && (resp.status >= 500 || resp.status === 0)) {
                        await new Promise(res => setTimeout(res, 1500));
                        return doFetchEnrich(attempt + 1);
                    }
                    throw new Error(`Server returned HTTP ${resp.status}`);
                }
            })
            .catch(async fetchErr => {
                if (fetchErr && fetchErr.name === 'AbortError') throw fetchErr;
                if (attempt < 2) {
                    await new Promise(res => setTimeout(res, 1500));
                    return doFetchEnrich(attempt + 1);
                }
                throw fetchErr;
            });

        return doFetchEnrich()
            .then(data => {
                activeEnrichControllers.delete(abortCtrl);
                if (reBtn) {
                    reBtn.disabled = false;
                    reBtn.innerHTML = `<i class="fa-solid fa-arrows-rotate"></i> Re-Enrich`;
                }
                if (data.ok && data.moved_to_tomags) {
                    const targetId = data.item_id || itemId;
                    transitionCardEnrichBucket(targetId, 'tomags', wasInReview ? 'needs_review' : 'needs_enriching');
                    adjustTomagsCounter(1);
                    setCardMovedToMagsState(targetId, data.file_id || '', data.moved_files || [], data.message, data);
                    return;
                }
                if (data.ok) {
                    const targetId = itemId || data.requested_item_id || data.item_id;
                    transitionCardEnrichBucket(targetId, 'enriched', wasInReview ? 'needs_review' : 'needs_enriching');
                    btn.disabled = false;
                    btn._wasInReview = false;
                    btn._reviewData = null;
                    btn.removeAttribute('data-review-json');
                    btn.innerHTML = `<i class="fa-solid fa-check"></i> Enriched!`;
                    btn.style.background = 'rgba(74,222,128,0.18)';
                    btn.style.borderColor = 'rgba(74,222,128,0.5)';
                    btn.style.color = '#4ade80';
                    btn.setAttribute('data-force', 'true');
                    if (reBtn) reBtn.style.display = 'none';

                    const cardEl = targetId ? document.getElementById(`card-${targetId}`) : null;
                    const detBtn = cardEl ? cardEl.querySelector('.btn-details') : null;
                    if (detBtn) {
                        detBtn.setAttribute('data-enrich-status', '');
                        detBtn.setAttribute('data-enrich-error', '');
                        detBtn._enrichResultData = null;
                    }
                    const errBanner = document.getElementById('modal-enrich-error-banner');
                    if (errBanner && window._activeModalItem && window._activeModalItem.itemId === targetId) {
                        errBanner.style.display = 'none';
                        errBanner.innerHTML = '';
                    }
                    applyCardMetadataUpdate(targetId, data, updateModal);
                } else if (data.status === 'NEEDS_REVIEW' && !force) {
                    const targetId = itemId || data.item_id;
                    transitionCardEnrichBucket(targetId, 'needs_review', wasInReview ? 'needs_review' : 'needs_enriching');
                    btn.disabled = false;
                    btn._reviewData = data;
                    btn.innerHTML = `<i class="fa-solid fa-clipboard-question"></i> In Review (${data.confidence || 0}%)`;
                    btn.style.background = 'rgba(251,191,36,0.18)';
                    btn.style.borderColor = 'rgba(251,191,36,0.5)';
                    btn.style.color = '#fbbf24';
                    btn.title = `${data.message || 'Low confidence match'} — Click to open Review & Mix-and-Match options`;
                    const cardEl = targetId ? document.getElementById(`card-${targetId}`) : null;
                    const detBtn = cardEl ? cardEl.querySelector('.btn-details') : null;
                    if (detBtn) {
                        detBtn.setAttribute('data-enrich-status', 'NEEDS_REVIEW');
                        detBtn.setAttribute('data-enrich-error', data.message || '');
                        detBtn._enrichResultData = data;
                    }
                    if (!silentReview && enrichQueue.length === 0 && activeEnrichCount <= 1) {
                        openEnrichReviewModal(data, btn, updateModal, itemId, fileId);
                    }
                } else {
                    const targetId = itemId || data.item_id;
                    const st = (data.status || 'FAILED').toUpperCase();
                    transitionCardEnrichBucket(targetId, (st === 'NEEDS_REVIEW' ? 'needs_review' : 'no_match'), wasInReview ? 'needs_review' : 'needs_enriching');
                    btn.disabled = false;
                    btn._reviewData = data;
                    let label = `<i class="fa-solid fa-triangle-exclamation"></i> Failed`;
                    let bg = 'rgba(239,68,68,0.18)';
                    let border = 'rgba(239,68,68,0.5)';
                    let color = '#f87171';
                    if (st === 'NOT_FOUND' || st === 'NO_ISBN') {
                        label = `<i class="fa-solid fa-magnifying-glass-minus"></i> No Match (Review)`;
                        bg = 'rgba(245,158,11,0.18)';
                        border = 'rgba(245,158,11,0.5)';
                        color = '#fbbf24';
                    } else if (st === 'HAS_DRM') {
                        label = `<i class="fa-solid fa-lock"></i> DRM Locked`;
                        bg = 'rgba(245,158,11,0.18)';
                        border = 'rgba(245,158,11,0.5)';
                        color = '#fbbf24';
                    } else if (st === 'QUARANTINED') {
                        label = `<i class="fa-solid fa-biohazard"></i> Quarantined`;
                    }
                    btn.innerHTML = label;
                    btn.style.background = bg;
                    btn.style.borderColor = border;
                    btn.style.color = color;
                    const errMsg = data.message || data.error || `Enrichment status: ${st}`;
                    btn.title = `${errMsg} — Click to inspect details & manual review options`;
                    const cardEl = targetId ? document.getElementById(`card-${targetId}`) : null;
                    const detBtn = cardEl ? cardEl.querySelector('.btn-details') : null;
                    if (detBtn) {
                        detBtn.setAttribute('data-enrich-status', st);
                        detBtn.setAttribute('data-enrich-error', errMsg);
                        detBtn._enrichResultData = data;
                    }
                    if (data.title_updated || data.authors_updated) {
                        updateItemTitleAuthorInDom(targetId, data);
                    }
                    if (data.ai_generated_description) {
                        updateItemDescriptionInDom(targetId, data.ai_generated_description);
                    } else if ((st === 'NOT_FOUND' || st === 'NO_ISBN') && cardEl) {
                        const curDescAttr = (detBtn ? (detBtn.getAttribute('data-desc') || '') : '').trim();
                        const lacksDesc = !curDescAttr || curDescAttr === 'No description available for this item.';
                        if (lacksDesc) {
                            let aiRow = cardEl.querySelector('.card-ai-desc-row');
                            if (!aiRow) {
                                const row2 = cardEl.querySelector('.card-row2-actions');
                                if (row2 && row2.parentElement) {
                                    aiRow = document.createElement('div');
                                    aiRow.className = 'card-ai-desc-row';
                                    aiRow.setAttribute('data-item-id', targetId);
                                    aiRow.style.cssText = 'display:flex; gap:0.38rem; align-items:center; width:100%;';
                                    aiRow.innerHTML = `<button type="button" class="btn-ai-desc-card" data-item-id="${escapeHtml(targetId)}" onclick="event.stopPropagation(); if(window.generateAiDescriptionForItem) window.generateAiDescriptionForItem({ itemId: '${escapeJsString(targetId)}', mode: 'card', btn: this });" title="No Match &amp; No Description — Use Gemini AI to generate a synopsis based on this book's actual content" style="flex:1; display:inline-flex; align-items:center; justify-content:center; gap:0.32rem; padding:0.4rem 0.55rem; border-radius:8px; font-size:0.74rem; font-weight:700; cursor:pointer; white-space:nowrap; background:rgba(168,85,247,0.2); border:1px solid rgba(168,85,247,0.52); color:#e9d5ff; transition:all 0.2s;"><i class="fa-solid fa-wand-magic-sparkles"></i> AI Generate Description</button>`;
                                    row2.insertAdjacentElement('afterend', aiRow);
                                }
                            } else {
                                aiRow.style.display = 'flex';
                            }
                        }
                    }
                    const errBanner = document.getElementById('modal-enrich-error-banner');
                    if (errBanner && window._activeModalItem && window._activeModalItem.itemId === targetId) {
                        const curModalDesc = (window._activeModalItem.desc || '').trim();
                        const isNoMatch = (st === 'NOT_FOUND' || st === 'NO_ISBN');
                        const showModalAiBtn = isNoMatch && !curModalDesc;
                        const modalAiBtn = document.getElementById('modal-btn-ai-desc');
                        if (modalAiBtn) modalAiBtn.style.display = showModalAiBtn ? 'inline-flex' : 'none';
                        errBanner.style.display = 'block';
                        errBanner.style.background = isNoMatch ? 'rgba(245,158,11,0.14)' : 'rgba(239,68,68,0.14)';
                        errBanner.style.borderColor = isNoMatch ? 'rgba(245,158,11,0.48)' : 'rgba(239,68,68,0.48)';
                        errBanner.style.color = isNoMatch ? '#fde68a' : '#fecaca';
                        errBanner.innerHTML = `
                            <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:0.75rem; flex-wrap:wrap;">
                                <div style="flex:1; min-width:220px;">
                                    <div style="font-weight:700; font-size:0.78rem; text-transform:uppercase; margin-bottom:0.25rem;">
                                        <i class="fa-solid ${isNoMatch ? 'fa-magnifying-glass-minus' : 'fa-triangle-exclamation'}"></i> ${isNoMatch ? 'No External Catalog Match Found' : `Enrichment ${escapeHtml(st)} Details`}
                                    </div>
                                    <div style="font-family:monospace; font-size:0.78rem; color:#f8fafc; word-break:break-word;">${escapeHtml(errMsg)}</div>
                                </div>
                                ${showModalAiBtn ? `<button type="button" id="modal-err-ai-desc-btn" onclick="window.generateAiDescriptionForItem({ itemId: '${escapeJsString(targetId)}', mode: 'modal', btn: this })" style="background:rgba(168,85,247,0.26); border:1px solid rgba(168,85,247,0.62); color:#f3e8ff; border-radius:6px; padding:0.32rem 0.65rem; font-size:0.75rem; font-weight:700; cursor:pointer; white-space:nowrap;"><i class="fa-solid fa-wand-magic-sparkles"></i> AI Describe from Content</button>` : ''}
                            </div>
                        `;
                    }
                }
            })
            .catch(err => {
                activeEnrichControllers.delete(abortCtrl);
                if (reBtn) {
                    reBtn.disabled = false;
                    reBtn.innerHTML = `<i class="fa-solid fa-arrows-rotate"></i> Re-Enrich`;
                }
                if (err && err.name === 'AbortError') return;
                btn.disabled = false;
                delete btn.dataset.batchTouched;
                btn.innerHTML = `<i class="fa-solid fa-rotate-right"></i> Retry Enrich`;
                btn.title = `Temporary connection error (${err.message || 'server busy'}) — Click to retry`;
                console.error('Error during on-demand enrichment:', err);
            });
    }

    updateEnrichPageButtons();

    // Helper functions
    function escapeHtml(str) {
                if (!str) return '';
                return str
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#039;');
            }

    function escapeJsString(str) {
                if (!str) return '';
                return str.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
            }

    function getTypeBadgeClass(type) {
                if (type === 'BOOK') return 'type-book';
                if (type === 'MAGAZINE') return 'type-magazine';
                if (type === 'KINDLE') return 'type-kindle';
                if (type === 'AUDIBLE') return 'type-audible';
                return 'type-book';
            }

    function createBadge(text) {
                const badge = document.createElement('span');
                badge.className = 'modal-badge';
                badge.textContent = text;
                return badge;
            }

    function getCookie(name) {
                let cookieValue = null;
                if (document.cookie && document.cookie !== '') {
                    const cookies = document.cookie.split(';');
                    for (let i = 0; i < cookies.length; i++) {
                        const cookie = cookies[i].trim();
                        if (cookie.substring(0, name.length + 1) === (name + '=')) {
                            cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                            break;
                        }
                    }
                }
                return cookieValue;
            }

    // ----------------------------------------------------
    // Top Controls Bar: Sort By, Filter by Type, Enrichment Status, Read/Unread Status
    // ----------------------------------------------------
    function updateQueryParamAndNavigate(paramName, paramValue) {
                abortAllEnrichRequests();
                const url = new URL(window.location.href);
                url.pathname = url.pathname.replace(/^\/mylibrary\/mylibrary\b/, '/mylibrary');
                url.searchParams.delete('page');
                if (paramName === 'type') {
                    url.searchParams.delete('type');
                    if (paramValue) {
                        url.searchParams.set('type', paramValue);
                    }
                } else if (paramName === 'enrich_status') {
                    if (['needs_enriching', 'needs_review', 'in_review', 'no_match', 'no_isbn', 'not_found', 'enriched'].includes(url.searchParams.get('shelf') || '')) {
                        url.searchParams.delete('shelf');
                    }
                    if (paramValue) {
                        url.searchParams.set('enrich_status', paramValue);
                    } else {
                        url.searchParams.delete('enrich_status');
                    }
                } else {
                    if (paramValue) {
                        url.searchParams.set(paramName, paramValue);
                    } else {
                        url.searchParams.delete(paramName);
                    }
                }
                window.location.href = url.toString();
            }

    const topTypeSelect = document.getElementById('top-type-select');
        if (topTypeSelect) {
            topTypeSelect.addEventListener('change', () => {
                updateQueryParamAndNavigate('type', topTypeSelect.value);
            });
        }

        const topEnrichStatusSelect = document.getElementById('top-enrich-status-select');
        if (topEnrichStatusSelect) {
            topEnrichStatusSelect.addEventListener('change', () => {
                updateQueryParamAndNavigate('enrich_status', topEnrichStatusSelect.value);
            });
        }

        const topReadStatusSelect = document.getElementById('top-read-status-select');
        if (topReadStatusSelect) {
            topReadStatusSelect.addEventListener('change', () => {
                updateQueryParamAndNavigate('read_status', topReadStatusSelect.value);
            });
        }

        const topSortSelect = document.getElementById('top-sort-select');
        if (topSortSelect) {
            topSortSelect.addEventListener('change', () => {
                updateQueryParamAndNavigate('sort', topSortSelect.value);
            });
        }

        const aiDescNoMatchToggle = document.getElementById('opt-ai-desc-no-match');
        if (aiDescNoMatchToggle) {
            aiDescNoMatchToggle.checked = localStorage.getItem('mylibrary_ai_desc_no_match') === '1';
            aiDescNoMatchToggle.addEventListener('change', () => {
                localStorage.setItem('mylibrary_ai_desc_no_match', aiDescNoMatchToggle.checked ? '1' : '0');
            });
        }

        // ----------------------------------------------------
        // Grid / List View Toggle Logic
        // ----------------------------------------------------
        const gridBtn = document.getElementById('view-grid-btn');
        const listBtn = document.getElementById('view-list-btn');
        const catalogGrid = document.querySelector('.catalog-grid');

        if (gridBtn && listBtn) {
            // Load preference
            const currentView = localStorage.getItem('catalogView') || 'grid';
            setView(currentView);

            gridBtn.addEventListener('click', () => setView('grid'));
            listBtn.addEventListener('click', () => setView('list'));

            function setView(view) {
                if (view === 'list') {
                    if (catalogGrid) catalogGrid.classList.add('list-view');
                    gridBtn.classList.remove('active');
                    listBtn.classList.add('active');
                    localStorage.setItem('catalogView', 'list');
                } else {
                    if (catalogGrid) catalogGrid.classList.remove('list-view');
                    gridBtn.classList.add('active');
                    listBtn.classList.remove('active');
                    localStorage.setItem('catalogView', 'grid');
                }
            }
        }
};

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCatalogSearch);
} else {
    initCatalogSearch();
}
