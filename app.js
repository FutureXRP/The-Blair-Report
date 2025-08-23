(async function () {
  const year = document.getElementById('year');
  if (year) year.textContent = new Date().getFullYear();

  // ---------- helpers ----------
  function fmtTimeAgo(dateStr) {
    try {
      const d = new Date(dateStr);
      const delta = Math.max(0, Date.now() - d.getTime());
      const mins = Math.floor(delta / 60000);
      if (mins < 1) return 'just now';
      if (mins < 60) return mins + 'm ago';
      const hrs = Math.floor(mins / 60);
      if (hrs < 24) return hrs + 'h ago';
      const days = Math.floor(hrs / 24);
      return days + 'd ago';
    } catch { return ''; }
  }
  async function loadJSON(path) {
    const res = await fetch(path + '?v=' + Date.now());
    if (!res.ok) throw new Error('Failed to load ' + path);
    return res.json();
  }
  function renderList(el, items, opts = {}) {
    if (!el) return;
    el.innerHTML = '';
    for (const it of items) {
      const li = document.createElement('li');
      if (opts.breaking) li.classList.add('breaking-item');
      const a = document.createElement('a');
      a.href = it.link; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = it.title;
      const meta = document.createElement('span');
      meta.className = 'meta';
      const src = it.source ? ` • ${it.source}` : '';
      meta.textContent = `${fmtTimeAgo(it.published_at)}${src}`;
      li.appendChild(a); li.appendChild(meta);
      el.appendChild(li);
    }
  }

  // ---------- ticker ----------
  function setupTicker(prices) {
    const wrap = document.querySelector('.ticker-wrap');
    const tick = document.getElementById('ticker');
    if (!wrap || !tick) return;

    tick.innerHTML = '';
    if (!Array.isArray(prices) || !prices.length) return;

    const oneRun = document.createElement('div');
    oneRun.className = 'ticker-track';
    for (const p of prices) {
      const span = document.createElement('span');
      const sym = (p.symbol || '').toUpperCase();
      const rank = p.rank ? `#${p.rank}` : '';
      const price = (p.price !== undefined) ? `$${Number(p.price).toLocaleString()}` : '';
      span.textContent = `${rank} ${sym} ${price}`;
      oneRun.appendChild(span);
    }

    const containerW = wrap.clientWidth || 800;
    let totalW = 0;
    while (totalW < containerW * 2) {
      const clone = oneRun.cloneNode(true);
      tick.appendChild(clone);
      totalW += clone.scrollWidth || containerW;
    }

    const speed = 100; // px/s
    const duration = Math.max(20, Math.round((totalW / speed)));
    tick.style.setProperty('--ticker-duration', duration + 's');
  }

  // ---------- markets table ----------
  function renderMarkets(prices) {
    const tbody = document.getElementById('market-body');
    if (!tbody) return;
    tbody.innerHTML = '';
    const rows = (Array.isArray(prices) ? prices.slice(0, 12) : []);
    for (const c of rows) {
      const tr = document.createElement('tr');
      const nameTd = document.createElement('td');
      nameTd.textContent = `${(c.symbol || '').toUpperCase()}`;
      const priceTd = document.createElement('td');
      priceTd.textContent = (c.price !== undefined) ? `$${Number(c.price).toLocaleString()}` : '';
      const changeTd = document.createElement('td');
      const change = c.change24h;
      if (typeof change === 'number') {
        changeTd.textContent = `${change.toFixed(2)}%`;
        changeTd.classList.add(change >= 0 ? 'positive' : 'negative');
      } else {
        changeTd.textContent = '—';
      }
      tr.append(nameTd, priceTd, changeTd);
      tbody.appendChild(tr);
    }
  }

  // ---------- fallback RSS (Grok-style) ----------
  async function fetchRSSItems(rssUrl) {
    try {
      const apiUrl = `https://api.rss2json.com/v1/api.json?rss_url=${encodeURIComponent(rssUrl)}`;
      const res = await fetch(apiUrl);
      const data = await res.json();
      if (data && data.status === 'ok') {
        return data.items.slice(0, 10).map(it => ({
          title: it.title,
          link: it.link,
          published_at: it.pubDate || new Date().toISOString(),
          source: (new URL(it.link)).hostname.replace('www.','')
        }));
      }
    } catch (e) { console.warn('RSS fallback error:', e); }
    return [];
  }

  async function loadFallbackHeadlines() {
    // A small, reliable trio — extend if you like
    const feeds = [
      'https://cointelegraph.com/rss',
      'https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml',
      'https://www.coindesk.com/tag/real-world-assets/feed/'
    ];
    const [a,b,c] = await Promise.all(feeds.map(fetchRSSItems));
    return {
      top: [...a.slice(0,5), ...b.slice(0,5)],
      tokenization: c.slice(0,8),
      regulation: [],
      research: [],
      culture: [],
      markets: [],
      breaking: (a.slice(0,2)).concat(b.slice(0,1))
    };
  }

  try {
    // Try your server-generated data first
    let headlines = null, prices = null;
    try {
      [headlines, prices] = await Promise.all([
        loadJSON('data/headlines.json'),
        loadJSON('data/prices.json')
      ]);
    } catch (e) {
      console.warn('Primary JSON failed, will use fallback:', e);
    }

    // If headlines missing or empty, use fallback (Grok-like)
    const emptyTop = !headlines || !Array.isArray(headlines.top) || headlines.top.length === 0;
    if (emptyTop) {
      headlines = await loadFallbackHeadlines();
    }

    // render headlines
    renderList(document.getElementById('breaking'), headlines.breaking || [], {breaking:true});
    renderList(document.getElementById('top'), headlines.top || []);
    renderList(document.getElementById('tokenization'), headlines.tokenization || []);
    renderList(document.getElementById('regulation'), headlines.regulation || []);
    renderList(document.getElementById('research'), headlines.research || []);
    renderList(document.getElementById('culture'), headlines.culture || []);

    // enrich prices for table (add 24h change if present)
    if (Array.isArray(prices)) {
      // if your build.py doesn’t include change%, it’s fine; table will show “—”
      renderMarkets(prices);
      setupTicker(prices);
    } else {
      renderMarkets([]);
      setupTicker([]);
    }

    // Recalc ticker on resize
    window.addEventListener('resize', () => setupTicker(prices || []));

  } catch (e) {
    console.error(e);
  }
})();
