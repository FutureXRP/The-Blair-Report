#!/usr/bin/env python3
import os, json, time, hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse

import yaml
import feedparser
import requests

ROOT = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(ROOT, 'data')
CONF = os.path.join(ROOT, 'config', 'sources.yaml')

os.makedirs(DATA_DIR, exist_ok=True)

def iso_now():
    return datetime.now(timezone.utc).isoformat()

def canonical_source(link, fallback):
    try:
        host = urlparse(link).hostname or ''
        return host.replace('www.', '')
    except Exception:
        return fallback

with open(CONF, 'r', encoding='utf-8') as f:
    cfg = yaml.safe_load(f)

limits = cfg.get('limits', {})
per_category = limits.get('per_category', 15)
limit_top = limits.get('top', 18)

# Pull feeds
items_by_cat = {
    'top': [], 'regulation': [], 'tokenization': [], 'xrp': [], 'xdc': [], 'zbcn': [],
    'bluechips': [], 'research': [], 'memes': [], 'breaking': []
}

seen = set()

for src in cfg['sources']:
    name = src['name']
    url = src['url']
    cat = src.get('category', 'top')

    try:
        d = feedparser.parse(url)
        for e in d.entries[:100]:
            title = e.get('title', '').strip()
            link = e.get('link', '').strip()
            if not title or not link:
                continue

            # dedupe by link hash
            h = hashlib.sha1(link.encode('utf-8')).hexdigest()
            if h in seen:
                continue
            seen.add(h)

            # dates
            published = None
            for k in ('published_parsed','updated_parsed','created_parsed'):
                if getattr(e, k, None):
                    try:
                        published = datetime.fromtimestamp(time.mktime(getattr(e, k))).astimezone(timezone.utc)
                        break
                    except Exception:
                        pass
            if not published:
                published = datetime.now(timezone.utc)

            items_by_cat[cat].append({
                'title': title,
                'link': link,
                'published_at': published.isoformat(),
                'source': canonical_source(link, name)
            })
    except Exception as ex:
        print(f"ERR {name}: {ex}")

# Promote the freshest across all to Top Stories
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

# Optional: simple breaking rule (last 30 minutes across key categories)
now = datetime.now(timezone.utc)
breaking_candidates = []
for k in ('regulation','xrp','xdc','tokenization'):
    for it in items_by_cat[k]:
        try:
            t = datetime.fromisoformat(it['published_at'])
            if (now - t).total_seconds() <= 30*60:
                breaking_candidates.append(it)
        except Exception:
            pass
items_by_cat['breaking'] = sorted(breaking_candidates, key=lambda x: x['published_at'], reverse=True)[:5]

# Write headlines.json
head_path = os.path.join(DATA_DIR, 'headlines.json')
with open(head_path, 'w', encoding='utf-8') as f:
    json.dump(items_by_cat, f, ensure_ascii=False, indent=2)

print(f"Wrote {head_path}")

# Prices via CoinGecko Simple Price API
COINS = {
    'xrp': 'ripple',
    'xdc': 'xdce-crowd-sale',
    'zbcn': 'zebec-network',
    'btc': 'bitcoin',
    'eth': 'ethereum'
}
ids = ','.join(COINS.values())
try:
    r = requests.get(
        'https://api.coingecko.com/api/v3/simple/price',
        params={'ids': ids, 'vs_currencies':'usd'},
        timeout=20
    )
    r.raise_for_status()
    data = r.json()
    out = {}
    for sym, cid in COINS.items():
        v = data.get(cid, {}).get('usd')
        if v is not None:
            out[sym] = float(v)
    price_path = os.path.join(DATA_DIR, 'prices.json')
    with open(price_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Wrote {price_path}")
except Exception as ex:
    print('Price fetch failed:', ex)
