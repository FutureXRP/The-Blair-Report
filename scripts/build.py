#!/usr/bin/env python3
import os, json, time, hashlib, sys, re
from datetime import datetime, timezone
from urllib.parse import urlparse

import yaml
import feedparser
import requests
from collections import defaultdict, deque

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

def canonical_source(link, fallback):
    try:
        host = urlparse(link).hostname or ''
        host = host.lower().replace('www.','')
        return host or fallback
    except Exception:
        return (fallback or '').lower()

def normalize_title(t):
    t = (t or '').lower()
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    # remove trivial words
    STOP = {'the','a','an','to','of','for','on','in','and','with','by','from','is','are'}
    toks = [w for w in t.split() if w not in STOP]
    return ' '.join(toks)

def text_score(title, summary):
    t = (title or '').lower()
    s = (summary or '').lower()
    score = 0
    for w in GOOD_WORDS:
        if w in t or w in s: score += 2
    for w in BAD_WORDS:
        if w in t or w in s: score -= 3
    score += min(len(t)//40, 3)  # prefer more specific titles a bit
    return score

# Load config
with open(CONF, 'r', encoding='utf-8') as f:
    cfg = yaml.safe_load(f) or {}

limits = cfg.get('limits', {})
PER_CAT = int(limits.get('per_category', 18))
TOP_LIMIT = int(limits.get('top', 24))

# ingest
raw_by_cat = defaultdict(list)
seen_link_hash = set()
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
            if h in seen_link_hash: 
                continue
            seen_link_hash.add(h)

            # date
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
            domain = canonical_source(link, name)
            raw_by_cat[cat].append({
                'title': title,
                'link': link,
                'published_at': published_dt.isoformat(),
                'source': domain,
                'score': score,
                'ntitle': normalize_title(title)
            })
    except Exception as ex:
        print(f"[WARN] feed error {name}: {ex}", file=sys.stderr)

def dedupe(items):
    # de-dupe by (normalized title, domain prefix)
    seen = set()
    out = []
    for it in items:
        key = (it['ntitle'], it['source'])
        if key in seen: 
            continue
        seen.add(key)
        out.append(it)
    return out

def diverse_pick(items, total_limit, per_source_cap=3):
    """
    Round-robin by source with a per-source cap to ensure diversity.
    Items should already be sorted by (score, recency) desc.
    """
    buckets = defaultdict(deque)
    count_by_source = defaultdict(int)
    for it in items:
        buckets[it['source']].append(it)

    # interleave sources
    sources = deque(sorted(buckets.keys()))
    chosen = []
    while sources and len(chosen) < total_limit:
        src = sources[0]
        if buckets[src]:
            if count_by_source[src] < per_source_cap:
                chosen.append(buckets[src].popleft())
                count_by_source[src] += 1
                # rotate to next source
                sources.rotate(-1)
            else:
                # remove source if cap hit
                sources.popleft()
        else:
            # empty bucket; drop source
            sources.popleft()
    return chosen

items_by_cat = {k: [] for k in CATS}

# rank, dedupe, diversify per category
for cat, arr in raw_by_cat.items():
    if cat not in items_by_cat:  # skip unknown cats
        continue
    arr.sort(key=lambda x: (x['score'], x['published_at']), reverse=True)
    arr = dedupe(arr)
    items_by_cat[cat] = diverse_pick(arr, PER_CAT, per_source_cap=3)

# Build Top from all other cats (except breaking)
all_items = []
for k, arr in items_by_cat.items():
    if k in ('top','breaking'): continue
    all_items.extend(arr)
all_items.sort(key=lambda x: (x['score'], x['published_at']), reverse=True)
all_items = dedupe(all_items)
items_by_cat['top'] = diverse_pick(all_items, TOP_LIMIT, per_source_cap=2)

# Breaking (last 30 minutes across high-signal cats)
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

# clean up helper fields
for k in list(items_by_cat.keys()):
    for it in items_by_cat[k]:
        it.pop('score', None)
        it.pop('ntitle', None)

items_by_cat['generated_at'] = datetime.now(timezone.utc).isoformat()

# write headlines
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
