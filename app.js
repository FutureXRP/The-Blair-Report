(async function () {
  const year = document.getElementById('year');
  year.textContent = new Date().getFullYear();

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

  function renderList(el, items) {
    el.innerHTML = '';
    for (const it of items) {
      const li = document.createElement('li');
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

  function setupTicker(prices) {
    const wrap = document.querySelector('.ticker-wrap');
    const tick = document.getElementById('ticker');
    if (!wrap || !tick) return;

    // Clear any old content
    tick.innerHTML = '';

    if (!Array.isArray(prices) || !prices.length) return;

    // Build a single run of items
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

    // Duplicate content until it's at least 2x container width for a smooth loop
    const containerW = wrap.clientWidth || 800;
    let totalW = 0;
    while (totalW < containerW * 2) {
      const clone = oneRun.cloneNode(true);
      tick.appendChild(clone);
      totalW += clone.scrollWidth || containerW;
    }

    // Set speed: e.g., 100 px/sec
    const speed = 100; // px per second
    const duration = Math.max(20, Math.round((totalW / speed)));
    tick.style.setProperty('--ticker-duration', duration + 's');
  }

  try {
    const [headlines, prices] = await Promise.all([
      loadJSON('data/headlines.json'),
      loadJSON('data/prices.json')
    ]);

    // Breaking
    const breaking = headlines.breaking || [];
    const breakingEl = document.getElementById('breaking');
    if (breaking.length) {
      breakingEl.classList.remove('hidden');
      breakingEl.textContent = breaking.map(b => b.title).join('  •  ');
    }

    // Ticker
    setupTicker(prices);

    // Sections
    const map = {
      top: 'top',
      regulation: 'regulation',
      tokenization: 'tokenization',
      research: 'research',
      culture: 'culture',
      markets: 'markets'
    };
    for (const [key, id] of Object.entries(map)) {
      const el = document.getElementById(id);
      renderList(el, headlines[key] || []);
    }

    // Recalculate ticker on resize for smoother behavior
    window.addEventListener('resize', () => setupTicker(prices));

  } catch (e) {
    console.error(e);
  }
})();
