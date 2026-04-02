/**
 * The Blair Report — app.js v2.0
 * Reads from: data/headlines.json  {breaking, day, week, month, generated_at}
 *             data/prices.json     [{rank, symbol, price, change24h}, ...]
 * No build step. Drop in, works instantly.
 */
(async function () {

  // ── CONSTANTS ────────────────────────────────────────────────────
  const REFRESH_MS     = 120_000;   // 2-minute auto-refresh (matches GitHub Actions)
  const MARKET_ROWS    = 10;        // rows shown in the sidebar market table
  const GAINER_ROWS    = 5;
  const LOSER_ROWS     = 5;
  // Stablecoins to skip in gainers/losers — they're boring
  const STABLES = new Set(['USDT','USDC','BUSD','DAI','TUSD','USDP','FRAX','USDE','USDS',
                           'SUSDE','BSC-USD','STETH','WSTETH','WBETH','WEETH','WBTC','CBBTC','WETH']);

  // ── STATE ─────────────────────────────────────────────────────────
  let allArticles  = [];   // flat array of every article, any bucket
  let activeTab    = 'all';
  let searchQuery  = '';
  let pricesCache  = [];

  // ── HELPERS ──────────────────────────────────────────────────────
  async function loadJSON(path) {
    const res = await fetch(path + '?v=' + Date.now());
    if (!res.ok) throw new Error('HTTP ' + res.status + ' loading ' + path);
    return res.json();
  }

  function fmtTimeAgo(dateStr) {
    try {
      const delta = Math.max(0, Date.now() - new Date(dateStr).getTime());
      const mins  = Math.floor(delta / 60_000);
      if (mins <  1)  return 'just now';
      if (mins < 60)  return mins + 'm ago';
      const hrs = Math.floor(mins / 60);
      if (hrs  < 24)  return hrs  + 'h ago';
      return Math.floor(hrs / 24) + 'd ago';
    } catch { return ''; }
  }

  function fmtPrice(p) {
    const n = Number(p);
    if (isNaN(n)) return '—';
    if (n >= 1000)  return '$' + n.toLocaleString('en-US', {maximumFractionDigits: 0});
    if (n >= 1)     return '$' + n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    if (n >= 0.01)  return '$' + n.toFixed(4);
    return '$' + n.toExponential(3);
  }

  function fmtChg(c) {
    if (typeof c !== 'number') return '—';
    const sign = c >= 0 ? '+' : '';
    return sign + c.toFixed(2) + '%';
  }

  // Derive a dot color from source domain
  const SRC_COLORS = {
    'coindesk.com':    '#f5c842',
    'cointelegraph.com': '#2563eb',
    'theblock.co':     '#e8432d',
    'decrypt.co':      '#22c55e',
    'blockworks.co':   '#8b5cf6',
    'thedefiant.io':   '#f97316',
    'bitcoinmagazine.com': '#f59e0b',
    'bankless.com':    '#60a5fa',
    'messari.io':      '#a78bfa',
    'glassnode.com':   '#34d399',
  };
  function dotColor(source) {
    for (const [k, v] of Object.entries(SRC_COLORS)) {
      if (source && source.includes(k.replace('www.',''))) return v;
    }
    return 'var(--accent)';
  }

  // ── BUILD ARTICLE HTML ────────────────────────────────────────────
  function articleHTML(item, index) {
    const num   = String(index + 1).padStart(2, '0');
    const color = dotColor(item.source);
    const src   = (item.source || '').replace('www.','');
    const time  = fmtTimeAgo(item.published_at);
    const title = escHtml(item.title || '');
    const href  = escHtml(item.link  || '#');
    return `
      <li>
        <a class="article" href="${href}" target="_blank" rel="noopener noreferrer">
          <div class="article-num">${num}</div>
          <div class="article-body">
            <div class="article-meta">
              <span class="src-dot" style="background:${color}"></span>
              <span class="src-name">${escHtml(src)}</span>
              <span class="src-time">${time}</span>
            </div>
            <div class="article-title">${title}</div>
          </div>
        </a>
      </li>`;
  }

  function escHtml(s) {
    return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // ── RENDER BUCKET ─────────────────────────────────────────────────
  function renderBucket(bucketId, items) {
    const ul  = document.getElementById(bucketId);
    const cnt = document.getElementById('count-' + bucketId);
    if (!ul) return;
    if (!items || !items.length) {
      ul.innerHTML = '<li style="padding:12px 0;font-family:\'IBM Plex Mono\',monospace;font-size:11px;color:var(--dim);">No headlines.</li>';
      if (cnt) cnt.textContent = '';
      return;
    }
    ul.innerHTML = items.map((it, i) => articleHTML(it, i)).join('');
    if (cnt) cnt.textContent = items.length + ' stories';
  }

  // ── TAB FILTER ────────────────────────────────────────────────────
  function applyTabFilter(tab) {
    activeTab = tab;
    const buckets = ['breaking','day','week','month'];
    buckets.forEach(b => {
      const section = document.getElementById('bucket-' + b);
      if (section) section.classList.toggle('hidden', tab !== 'all' && tab !== b);
    });
    // update article count label
    const total = allArticles.length;
    const el = document.getElementById('article-count');
    if (el) el.textContent = total + ' headlines';
  }

  // ── SEARCH ────────────────────────────────────────────────────────
  function applySearch(q) {
    searchQuery = q.trim().toLowerCase();
    const section = document.getElementById('search-results-section');
    const ul      = document.getElementById('search-list');
    const noRes   = document.getElementById('no-results');
    const cnt     = document.getElementById('search-count');

    if (!searchQuery) {
      section.classList.remove('visible');
      // restore tab state
      applyTabFilter(activeTab);
      return;
    }

    // hide all buckets while searching
    ['breaking','day','week','month'].forEach(b => {
      const s = document.getElementById('bucket-' + b);
      if (s) s.classList.add('hidden');
    });

    section.classList.add('visible');

    const results = allArticles.filter(it => {
      const hay = ((it.title || '') + ' ' + (it.source || '')).toLowerCase();
      return hay.includes(searchQuery);
    });

    if (!results.length) {
      ul.innerHTML = '';
      noRes.style.display = 'block';
      cnt.textContent = '0 results';
    } else {
      noRes.style.display = 'none';
      ul.innerHTML = results.map((it, i) => articleHTML(it, i)).join('');
      cnt.textContent = results.length + ' result' + (results.length !== 1 ? 's' : '');
    }
  }

  // ── TICKER ────────────────────────────────────────────────────────
  function buildTickerTrack(prices) {
    return prices.map(p => {
      const sym  = (p.symbol || '').toUpperCase();
      const rank = p.rank ? `<span class="t-rank">#${p.rank}</span>` : '';
      const price = fmtPrice(p.price);
      const chg   = typeof p.change24h === 'number' ? p.change24h : null;
      const chgClass = chg === null ? '' : (chg >= 0 ? 't-up' : 't-dn');
      const chgStr   = chg === null ? '' : `<span class="t-chg ${chgClass}">${fmtChg(chg)}</span>`;
      return `<span class="ticker-item">${rank}<span class="t-sym">${sym}</span>${price}${chgStr}</span>`;
    }).join('');
  }

  function setupTicker(prices) {
    if (!Array.isArray(prices) || !prices.length) return;
    const track1 = document.getElementById('ticker-track-1');
    const track2 = document.getElementById('ticker-track-2');
    if (!track1 || !track2) return;

    const html = buildTickerTrack(prices);
    track1.innerHTML = html;
    track2.innerHTML = html;   // second copy for seamless loop

    // Compute duration based on actual content width
    requestAnimationFrame(() => {
      const w     = track1.scrollWidth || 2000;
      const speed = 80; // px/s — smooth but readable
      const dur   = Math.max(30, Math.round(w / speed));
      document.getElementById('ticker').style.setProperty('--ticker-duration', dur + 's');
    });
  }

  // ── SIDEBAR: MARKET TABLE ─────────────────────────────────────────
  function renderMarkets(prices) {
    const tbody = document.getElementById('market-body');
    if (!tbody) return;
    const top = (prices || []).slice(0, MARKET_ROWS);
    if (!top.length) { tbody.innerHTML = ''; return; }
    tbody.innerHTML = top.map(c => {
      const sym = (c.symbol || '').toUpperCase();
      const chg = typeof c.change24h === 'number' ? c.change24h : null;
      const cls = chg === null ? '' : (chg >= 0 ? 'pos' : 'neg');
      return `<tr>
        <td>${sym}</td>
        <td>${fmtPrice(c.price)}</td>
        <td class="${cls}">${fmtChg(chg)}</td>
      </tr>`;
    }).join('');
  }

  // ── SIDEBAR: GAINERS & LOSERS ─────────────────────────────────────
  function renderGainersLosers(prices) {
    // Filter out stablecoins and wrapped tokens
    const filtered = (prices || []).filter(p => !STABLES.has((p.symbol || '').toUpperCase()));
    const sorted   = [...filtered].sort((a, b) => (b.change24h || 0) - (a.change24h || 0));
    const gainers  = sorted.slice(0, GAINER_ROWS);
    const losers   = sorted.slice(-LOSER_ROWS).reverse();

    function rowHTML(item, isGain) {
      const sym   = (item.symbol || '').toUpperCase();
      const price = fmtPrice(item.price);
      const chg   = fmtChg(item.change24h);
      const cls   = isGain ? 'pos' : 'neg';
      return `<div class="gl-row">
        <span class="gl-sym">${sym}</span>
        <span class="gl-price">${price}</span>
        <span class="gl-chg ${cls}">${chg}</span>
      </div>`;
    }

    const gEl = document.getElementById('gainers-list');
    const lEl = document.getElementById('losers-list');
    if (gEl) gEl.innerHTML = gainers.map(g => rowHTML(g, true)).join('');
    if (lEl) lEl.innerHTML = losers.map(l  => rowHTML(l, false)).join('');
  }

  // ── LAST UPDATED ─────────────────────────────────────────────────
  function setLastUpdated(iso) {
    const el = document.getElementById('lastUpdated');
    if (!el || !iso) return;
    try {
      const d = new Date(iso);
      el.textContent = 'Updated ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) +
                       ' · ' + d.toLocaleDateString([], {month:'short', day:'numeric'});
    } catch { el.textContent = ''; }
  }

  // ── MAIN REFRESH ─────────────────────────────────────────────────
  async function refreshAll() {
    try {
      const [headlines, prices] = await Promise.all([
        loadJSON('data/headlines.json'),
        loadJSON('data/prices.json'),
      ]);

      // Flatten all articles for search
      allArticles = [
        ...(headlines.breaking || []).map(a => ({...a, _bucket:'breaking'})),
        ...(headlines.day      || []).map(a => ({...a, _bucket:'day'})),
        ...(headlines.week     || []).map(a => ({...a, _bucket:'week'})),
        ...(headlines.month    || []).map(a => ({...a, _bucket:'month'})),
      ];

      // Render the four buckets
      renderBucket('breaking', headlines.breaking || []);
      renderBucket('day',      headlines.day      || []);
      renderBucket('week',     headlines.week     || []);
      renderBucket('month',    headlines.month    || []);

      setLastUpdated(headlines.generated_at);

      // Prices
      pricesCache = Array.isArray(prices) ? prices : [];
      setupTicker(pricesCache);
      renderMarkets(pricesCache);
      renderGainersLosers(pricesCache);

      // Re-apply search if active
      if (searchQuery) applySearch(searchQuery);

      // Update count
      const el = document.getElementById('article-count');
      if (el) el.textContent = allArticles.length + ' headlines';

    } catch (e) {
      console.error('[Blair] Refresh failed:', e);
    }
  }

  // ── CLOCK ─────────────────────────────────────────────────────────
  function startClock() {
    // No separate clock — last-updated timestamp serves that purpose
  }

  // ── WIRE UP TABS ─────────────────────────────────────────────────
  function initTabs() {
    document.querySelectorAll('.tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        applyTabFilter(tab.dataset.bucket);
        // clear search when switching tabs
        if (searchQuery) {
          const inp = document.getElementById('search-input');
          if (inp) inp.value = '';
          applySearch('');
        }
      });
    });
  }

  // ── WIRE UP SEARCH ────────────────────────────────────────────────
  function initSearch() {
    const input = document.getElementById('search-input');
    const clear = document.getElementById('search-clear');

    let debounce;
    input && input.addEventListener('input', () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => applySearch(input.value), 220);
    });

    clear && clear.addEventListener('click', () => {
      if (input) input.value = '';
      applySearch('');
    });
  }

  // ── RESIZE: recompute ticker ──────────────────────────────────────
  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => setupTicker(pricesCache), 300);
  });

  // ── BOOT ─────────────────────────────────────────────────────────
  // Year in footer
  const yearEl = document.getElementById('year');
  if (yearEl) yearEl.textContent = new Date().getFullYear();

  initTabs();
  initSearch();
  startClock();

  await refreshAll();
  setInterval(refreshAll, REFRESH_MS);

})();
