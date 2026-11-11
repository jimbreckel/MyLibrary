const DEFAULT_SETTINGS = {
  mylibraryBaseUrl: 'http://192.168.1.43:8080/mylibrary',
  annasBaseUrl: 'https://annas-archive.pk',
  zlibBaseUrl: 'https://singlelogin.re',
  showZlibButton: false,
  customSites: []
};

function updatePresetHighlight(annasUrl) {
  const clean = String(annasUrl || '').trim().replace(/\/+$/, '').toLowerCase();
  document.querySelectorAll('#annas-presets .preset-chip').forEach((chip) => {
    const chipUrl = String(chip.getAttribute('data-url') || '').toLowerCase();
    chip.classList.toggle('active', chipUrl === clean);
  });
  const quickAnnas = document.getElementById('quick-annas-link');
  if (quickAnnas && clean) {
    quickAnnas.href = `${clean}/`;
    quickAnnas.textContent = `↗ Open Anna's Archive (${clean.replace(/^https?:\/\//, '')})`;
  }
  const badgeAnnas = document.getElementById('badge-annas-site');
  if (badgeAnnas && clean) {
    badgeAnnas.href = `${clean}/`;
  }
}

function renderCustomSitesList(customSites) {
  const listEl = document.getElementById('custom-sites-list');
  if (!listEl) return;
  listEl.innerHTML = '';

  const sites = Array.isArray(customSites) ? customSites : [];
  sites.forEach((site, idx) => {
    const hostClean = String(site.hostPattern || '').replace(/^https?:\/\//i, '').replace(/\/.*$/, '');
    const pathClean = site.pathPattern ? (site.pathPattern.startsWith('/') ? site.pathPattern : `/${site.pathPattern}`) : '/';
    const siteHref = `https://${hostClean}${pathClean}`;
    const row = document.createElement('div');
    row.className = 'custom-site-item';
    row.innerHTML = `
      <a href="${siteHref}" target="_blank" rel="noopener" class="site-badge" title="Open ${siteHref} in a new tab">
        ✓ <strong>${site.name || hostClean}</strong> <span style="color:#cbd5e1;">(${hostClean})</span> ↗
      </a>
      <button type="button" class="custom-site-del" data-idx="${idx}">✕ Remove</button>
    `;
    listEl.appendChild(row);
  });

  listEl.querySelectorAll('.custom-site-del').forEach((btn) => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.getAttribute('data-idx'), 10);
      chrome.storage.sync.get({ customSites: [] }, (items) => {
        const next = (Array.isArray(items.customSites) ? items.customSites : []).filter((_, i) => i !== idx);
        chrome.storage.sync.set({ customSites: next }, () => {
          renderCustomSitesList(next);
        });
      });
    });
  });
}

document.addEventListener('DOMContentLoaded', () => {
  const libInput = document.getElementById('mylibrary-url');
  const annasInput = document.getElementById('annas-url');
  const showZlibCheckbox = document.getElementById('show-zlib');
  const zlibWrap = document.getElementById('zlib-url-wrap');
  const zlibInput = document.getElementById('zlib-url');
  const statusEl = document.getElementById('status-msg');
  const quickMylib = document.getElementById('quick-mylib-link');

  function saveAllSettings(customStatusMsg) {
    const mylibraryBaseUrl = libInput.value.trim().replace(/\/+$/, '');
    const annasBaseUrl = annasInput.value.trim().replace(/\/+$/, '');
    const zlibBaseUrl = zlibInput.value.trim().replace(/\/+$/, '');
    const showZlibButton = Boolean(showZlibCheckbox.checked);

    chrome.storage.sync.set(
      { mylibraryBaseUrl, annasBaseUrl, zlibBaseUrl, showZlibButton },
      () => {
        updatePresetHighlight(annasBaseUrl);
        if (quickMylib && mylibraryBaseUrl) {
          quickMylib.href = `${mylibraryBaseUrl}/`;
        }
        statusEl.textContent = customStatusMsg || `✅ Saved! Anna's Archive set to ${annasBaseUrl}`;
        statusEl.style.color = '#4ade80';
      }
    );
  }

  chrome.storage.sync.get(DEFAULT_SETTINGS, (items) => {
    libInput.value = items.mylibraryBaseUrl || DEFAULT_SETTINGS.mylibraryBaseUrl;
    annasInput.value = items.annasBaseUrl || DEFAULT_SETTINGS.annasBaseUrl;
    zlibInput.value = items.zlibBaseUrl || DEFAULT_SETTINGS.zlibBaseUrl;
    showZlibCheckbox.checked = Boolean(items.showZlibButton);
    zlibWrap.style.display = showZlibCheckbox.checked ? 'block' : 'none';
    updatePresetHighlight(annasInput.value);
    renderCustomSitesList(items.customSites || []);
  });

  // One-click Anna's Archive domain preset buttons
  document.querySelectorAll('#annas-presets .preset-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const targetUrl = chip.getAttribute('data-url');
      if (targetUrl) {
        annasInput.value = targetUrl;
        saveAllSettings(`✅ Switched Anna's Archive URL to ${targetUrl}`);
      }
    });
  });

  annasInput.addEventListener('input', () => {
    updatePresetHighlight(annasInput.value);
  });

  showZlibCheckbox.addEventListener('change', () => {
    zlibWrap.style.display = showZlibCheckbox.checked ? 'block' : 'none';
    saveAllSettings();
  });

  document.getElementById('btn-save').addEventListener('click', () => {
    saveAllSettings();
  });

  document.getElementById('btn-test').addEventListener('click', async () => {
    const mylibraryBaseUrl = libInput.value.trim().replace(/\/+$/, '');
    statusEl.textContent = '⏳ Testing MyLibrary API connection...';
    statusEl.style.color = '#cbd5e1';
    try {
      const r = await fetch(`${mylibraryBaseUrl}/api/wishlist/check/?title=A+Field+Guide+for+Science+Writers`);
      const data = await r.json();
      if (data && data.ok) {
        const hit = data.item && data.item.in_library;
        statusEl.textContent = `✅ Connected to MyLibrary! Test lookup works (in_library=${hit}).`;
        statusEl.style.color = '#4ade80';
      } else {
        statusEl.textContent = '⚠️ Reached server, but unexpected JSON response.';
        statusEl.style.color = '#fbbf24';
      }
    } catch (err) {
      statusEl.textContent = `❌ Connection failed: ${err.message}`;
      statusEl.style.color = '#f87171';
    }
  });

  // Add Custom Book Site handler
  document.getElementById('btn-add-site').addEventListener('click', () => {
    const name = document.getElementById('cs-name').value.trim();
    const hostPattern = document.getElementById('cs-host').value.trim().replace(/^https?:\/\//i, '').replace(/\/.*$/, '');
    const pathPattern = document.getElementById('cs-path').value.trim();
    const imageSelector = document.getElementById('cs-image').value.trim() || 'h1';
    const titleSelector = document.getElementById('cs-title').value.trim() || 'h1';
    const rawIsbnRule = document.getElementById('cs-isbn-sel').value.trim();

    if (!hostPattern) {
      statusEl.textContent = '⚠️ Please enter a Domain / Host (e.g. manning.com).';
      statusEl.style.color = '#fbbf24';
      return;
    }

    let isbnUrlParam = '';
    let isbnSelector = '';
    if (rawIsbnRule.startsWith('?')) {
      isbnUrlParam = rawIsbnRule.replace(/^\?/, '').replace(/=.*$/, '').trim();
    } else {
      isbnSelector = rawIsbnRule;
    }

    const newSite = {
      id: `custom_${Date.now()}`,
      name: name || hostPattern,
      hostPattern,
      pathPattern,
      imageSelector,
      titleSelector,
      isbnSelector,
      isbnUrlParam
    };

    chrome.storage.sync.get({ customSites: [] }, (items) => {
      const next = [...(Array.isArray(items.customSites) ? items.customSites : []), newSite];
      chrome.storage.sync.set({ customSites: next }, () => {
        renderCustomSitesList(next);
        document.getElementById('cs-name').value = '';
        document.getElementById('cs-host').value = '';
        document.getElementById('cs-path').value = '';
        document.getElementById('cs-isbn-sel').value = '';
        document.getElementById('add-site-details').open = false;
        statusEl.textContent = `✅ Added custom book site: ${newSite.name} (${hostPattern})`;
        statusEl.style.color = '#4ade80';
      });
    });
  });
});
