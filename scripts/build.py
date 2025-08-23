#!/usr/bin/env python3
import os, json, time, hashlib, sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import yaml
import feedparser
import requests

ROOT = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(ROOT, 'data')
CONF = os.path.join(ROOT, 'config', 'sources.yaml')

os.makedirs(DATA_DIR, exist_ok=True)

def canonical_source(link, fallback):
    try:
        host = urlparse(link).hostname or ''
        return host.replace('www.', '')
    except Exception:
        return fallback

# Load config
try:
    with open(CONF, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
except Exception as e:
    print("ERROR: Could not read config/sources.yaml:", e, file=sys.stderr)
    # write empty outputs so workflow still succeeds
    with open(os.path.join(DATA_DIR, 'headlines.json'), 'w', encoding='utf-8') as f:
        json.dump({}, f)
    with open(os.path.join(DATA_DIR, 'prices.json'), 'w', encoding='utf-8') as f:
        json.dump({}, f)
    sys.exit(0)

limits = cfg.get('limits', {})
per_category = limits.get('per_category', 15)
limit_top = limits.get('top', 18)

items_by_cat = {
    'top': [], 'regulation': [], 'tokenization': [], 'xrp': [], 'xdc': [], 'zbcn': [],
    'bluechips': [], 'research': [], 'memes': [], 'breaking': []
}

seen = set()

# Ingest feeds (best-effort; never crash entire run)
for src in cfg.get('sources', []):
    name = src.get('name', 'source')
    url = src.get('url', '')
    cat = src.get('category', 'top')
    if not url:
        continue
    try:
        d = feedparser.parse(url)
        for e in d.entries[:100]:
            title = (e.get('title') or '').strip()
            link = (e.get('link') or '').strip()
            if not title or not link:
                continue

            # dedupe by link hash
            h = hashlib.sha1(link.encode('utf-8')).hexdigest()
            if h in seen:
                continue
            seen.add(h)

            # pick a date
            published_dt = None
            for k in ('published_parsed','updated_parsed','created_parsed'):
                val = getattr(e, k, None)
                if val:
                    try:
                        published_dt = datetime.fromtimestamp(time.mktime(val), tz=timezone.utc)
                        break
                    except Exception:
                        pass
            if not published_dt:
                published_dt = datetime.now(timezone.utc)

            items_by_cat.setdefault(cat, [])
            items_by_cat[cat].append({
                'title': title,
                'link': link,
                'published_at': published_dt.isoformat(),
                'source': canonical_source(link, name)
            })
    except Exception as ex:
        print(f"[WARN] Feed error ({name}): {ex}", file=sys.stderr)

# Build Top Stories
all_items = []
for k, arr in items_by_cat.items():
    if k == 'breaking':
        continue
    all_items.extend(arr)
all_items.sort(key=lambda x: x['published_at'], reverse=True)
items_by_cat['top'] = all_items[:limit_top]

# Cap per-category
for k in list(items_by_cat.keys()):
    if k == 'top':
        continue
    items_by_cat[k].sort(key=lambda x: x['published_at'], reverse=True)
    items_by_cat[k] = items_by_cat[k][:per_category]

# Simple breaking rule (past 30 minutes)
now = datetime.now(timezone.utc)
breaking_candidates = []
for k in ('regulation','xrp','xdc','tokenization'):
    for it in items_by_cat.get(k, []):
        try:
            t = datetime.fromisoformat(it['published_at'])
            if (now - t).total_seconds() <= 30*60:
                breaking_candidates.append(it)
        except Exception:
            pass
items_by_cat['breaking'] = sorted(breaking_candidates, key=lambda x: x['published_at'], reverse=True)[:5]

# Write headlines.json (always)
head_path = os.path.join(DATA_DIR, 'headlines.json')
with open(head_path, 'w', encoding='utf-8') as f:
    json.dump(items_by_cat, f, ensure_ascii=False, indent=2)
print(f"Wrote {head_path}")

# Prices (best-effort)
COINS = {
    'xrp': 'ripple',
    'xdc': 'xdc-network',  # more reliable id
    'zbcn': 'zebec-network',
    'btc': 'bitcoin',
    'eth': 'ethereum'
}
ids = ','.join(COINS.values())
out = {}
try:
    r = requests.get(
        'https://api.coingecko.com/api/v3/simple/price',
        params={'ids': ids, 'vs_currencies':'usd'},
        timeout=20
    )
    r.raise_for_status()
    data = r.json()
    for sym, cid in COINS.items():
        v = data.get(cid, {}).get('usd')
        if v is not None:
            out[sym] = float(v)
except Exception as ex:
    print('[WARN] Price fetch failed:', ex, file=sys.stderr)

price_path = os.path.join(DATA_DIR, 'prices.json')
with open(price_path, 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"Wrote {price_path}")
