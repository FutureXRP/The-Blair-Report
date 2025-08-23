#!/usr/bin/env python3
import os, json, time, hashlib, sys, re
from datetime import datetime, timezone
from urllib.parse import urlparse

import yaml
import feedparser
import requests

ROOT = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(ROOT, 'data')
CONF = os.path.join(ROOT, 'config', 'sources.yaml')

os.makedirs(DATA_DIR, exist_ok=True)

CATS = ['top','regulation','tokenization','research','culture','markets','breaking']

GOOD_WORDS = [
    'etf','tokenization','rwa','real-world asset','adoption','integration','partnership',
    'approval','listing','launch','upgrade','roadmap','institution','bank','exchange',
    'regulation','ruling','settlement','framework','compliance','pilot','testnet','mainnet'
]
BAD_WORDS = [
    'price prediction','to the moon','burning','giveaway','airdrop scam',
    'meme coin will','shib to','doge to','$1','$10','100x','thousandx'
]

def text_score(title, summary):
    t = (title or '').lower()
    s = (summary or '').lower()
    score = 0
    for w in GOOD_WORDS:
      if w in t or w in s: score += 2
    for w in BAD_WORDS:
      if w in t or w in s: score -= 3
    # prefer longer, specific titles a bit
    score += min(len(t)//40, 3)
    return score

def canonical_source(link, fallback):
    try:
        host = urlparse(link).hostname or ''
        return host.replace('www.', '')
    except Exception:
        return fallback

# Load config
with open(CONF, 'r', encoding='utf-8') as f:
    cfg = yaml.safe_load(f) or {}

limits = cfg.get('limits', {})
PER_CAT = limits.get('per_category', 18)
TOP_LIMIT = limits.get('top', 24)

items_by_cat = {k: [] for k in CATS}
seen_links = set()

# Ingest feeds
for src in cfg.get('sources', []):
    name = src.get('name','source')
    url = src.get('url','')
    cat = src.get('category','top')
    if not url: continue
    try:
        d = feedparser.parse(url)
        for e in d.entries[:100]:
            title = (e.get('title') or '').strip()
            link = (e.get('link') or '').strip()
            if not title or not link: continue

            h = hashlib.sha1(link.encode('utf-8')).hexdigest()
            if h in seen_links: continue
            seen_links.add(h)

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

            summary = (getattr(e, 'summary', '') or '')
            score = text_score(title, summary)

            items_by_cat.setdefault(cat, [])
            items_by_cat[cat].append({
                'title': title,
                'link': link,
                'published_at': published_dt.isoformat(),
                'source': canonical_source(link, name),
                'score': score
            })
    except Exception as ex:
        print(f"[WARN] feed error {name}: {ex}", file=sys.stderr)

# Rank & trim per category
for k, arr in items_by_cat.items():
    if k == 'breaking': continue
    arr.sort(key=lambda x: (x['score'], x['published_at']), reverse=True)
    items_by_cat[k] = arr[:PER_CAT]

# Build Top Stories from all other cats
all_items = []
for k, arr in items_by_cat.items():
    if k in ('top','breaking'): continue
    all_items.extend(arr)
all_items.sort(key=lambda x: (x['score'], x['published_at']), reverse=True)
items_by_cat['top'] = all_items[:TOP_LIMIT]

# Breaking (last 30 mins, any cat)
now = datetime.now(timezone.utc)
breaking = []
for k in ('regulation','tokenization','markets','top'):
    for it in items_by_cat.get(k, []):
        try:
            t = datetime.fromisoformat(it['published_at'])
            if (now - t).total_seconds() <= 30*60:
                breaking.append(it)
        except Exception:
            pass
items_by_cat['breaking'] = sorted(breaking, key=lambda x: x['published_at'], reverse=True)[:5]

# Remove scores from final output
for k in list(items_by_cat.keys()):
    for it in items_by_cat[k]:
        it.pop('score', None)

# Timestamp (helps ensure commits even if content repeats)
items_by_cat['generated_at'] = datetime.now(timezone.utc).isoformat()

# Write headlines.json
with open(os.path.join(DATA_DIR, 'headlines.json'), 'w', encoding='utf-8') as f:
    json.dump(items_by_cat, f, ensure_ascii=False, indent=2)

print("WROTE headlines.json with counts:",
      {k: len(v) for k, v in items_by_cat.items() if isinstance(v, list)})

# Prices: Top 50 by market cap (CoinGecko)
prices = []
try:
    r = requests.get(
        'https://api.coingecko.com/api/v3/coins/markets',
        params={'vs_currency':'usd','order':'market_cap_desc','per_page':50,'page':1,'price_change_percentage':'24h'},
        timeout=25
    )
    r.raise_for_status()
    data = r.json()
    for coin in data:
        prices.append({
            'rank': coin.get('market_cap_rank'),
            'symbol': (coin.get('symbol') or '').upper(),
            'price': coin.get('current_price')
        })
except Exception as ex:
    print('[WARN] prices fetch failed:', ex, file=sys.stderr)

with open(os.path.join(DATA_DIR, 'prices.json'), 'w', encoding='utf-8') as f:
    json.dump(prices, f, ensure_ascii=False, indent=2)

print("WROTE prices.json (count):", len(prices))
