#!/usr/bin/env python3
import os, json, time, hashlib, sys, re
from datetime import datetime, timezone
from urllib.parse import urlparse
from collections import defaultdict, deque

import yaml
import feedparser
import requests

ROOT = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(ROOT, 'data')
CONF = os.path.join(ROOT, 'config', 'sources.yaml')

os.makedirs(DATA_DIR, exist_ok=True)

# ---------------- Crypto relevance gating ----------------
# A story must match at least one of these terms in title/summary/link to be included.
RELEVANT_PATTERNS = re.compile(
    r"(crypto|cryptocurrency|bitcoin|btc|ethereum|eth|solana|sol|xrp|binance|coinbase|kraken|"
    r"stablecoin|usdt|usdc|defi|web3|nft|layer\s*2|l2|blockchain|onchain|token(ization|ized|omics)?|"
    r"rwa|real[-\s]?world\s*assets?|mi[cs]a\b|digital\s+asset|virtual\s+asset|etl?\b|etf\b|spot\s+etf|"
    r"smart\s+contract|wallet|custody|staking|airdrops?|dex|cex)",
    re.IGNORECASE
)

GOOD_WORDS = [
    'etf','tokenization','rwa','real-world asset','adoption','integration','partnership',
    'approval','listing','launch','upgrade','roadmap','institution','bank','exchange',
    'regulation','ruling','settlement','framework','compliance','pilot','testnet','mainnet'
]
BAD_WORDS = [
    'price prediction','to the moon','burning','giveaway','airdrop scam',
    'meme coin will','shib to','doge to','$1','$10','100x','thousandx'
]

def is_crypto_relevant(title, summary, link):
    text = f"{title or ''} {summary or ''} {link or ''}"
    return bool(RELEVANT_PATTERNS.search(text))

def score_text(title, summary):
    t = (title or '').lower()
    s = (summary or '').lower()
    score = 0
    for w in GOOD_WORDS:
        if w in t or w in s: score += 2
    for w in BAD_WORDS:
        if w in t or w in s: score -= 3
    score += min(len(t)//40, 3)
    return score

def canonical_source(link, fallback):
    try:
        host = urlparse(link).hostname or ''
        return host.lower().replace('www.','') or (fallback or '').lower()
    except Exception:
        return (fallback or '').lower()

def normalize_title(t):
    t = (t or '').lower()
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    STOP = {'the','a','an','to','of','for','on','in','and','with','by','from','is','are'}
    toks = [w for w in t.split() if w not in STOP]
    return ' '.join(toks)

def diverse_pick(items, total_limit, per_source_cap=3):
    buckets = defaultdict(deque)
    count_by_src = defaultdict(int)
    for it in items:
        buckets[it['source']].append(it)
    sources = deque(sorted(buckets.keys()))
    chosen = []
    while sources and len(chosen) < total_limit:
        s = sources[0]
        if buckets[s]:
            if count_by_src[s] < per_source_cap:
                chosen.append(buckets[s].popleft())
                count_by_src[s] += 1
                sources.rotate(-1)
            else:
                sources.popleft()
        else:
            sources.popleft()
    return chosen

# ---------------- read config ----------------
try:
    with open(CONF, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
except Exception as e:
    print("ERROR: cannot read config/sources.yaml:", e, file=sys.stderr)
    cfg = {}

limits = cfg.get('limits', {})
PER_BUCKET = int(limits.get('per_category', 18))

# ---------------- ingest with timeouts ----------------
headers = {"User-Agent": "BlairReportBot/1.0 (+https://example.com)"}
raw = []
seen_links = set()
sources = cfg.get('sources', [])
print(f"Starting ingest: {len(sources)} sources")

for i, src in enumerate(sources, start=1):
    name = src.get('name','source')
    url = src.get('url','')
    if not url:
        continue
    print(f"[{i}/{len(sources)}] Fetching: {name} -> {url}")
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        d = feedparser.parse(resp.content)
    except Exception as ex:
        print(f"[WARN] fetch/parse error {name}: {ex}", file=sys.stderr)
        continue

    for e in d.entries[:100]:
        title = (e.get('title') or '').strip()
        link = (e.get('link') or '').strip()
        if not title or not link:
            continue
        # hard relevance gate
        summary = (getattr(e, 'summary', '') or '')
        if not is_crypto_relevant(title, summary, link):
            continue

        h = hashlib.sha1(link.encode('utf-8')).hexdigest()
        if h in seen_links:
            continue
        seen_links.add(h)

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

        sc = score_text(title, summary)
        src_domain = canonical_source(link, name)
        raw.append({
            'title': title,
            'link': link,
            'published_at': published_dt.isoformat(),
            'source': src_domain,
            'score': sc,
            'ntitle': normalize_title(title)
        })

print(f"Ingest complete. Items (pre-dedupe): {len(raw)}")

# ---------------- dedupe ----------------
seen = set()
deduped = []
for it in sorted(raw, key=lambda x:(x['score'], x['published_at']), reverse=True):
    key = (it['ntitle'], it['source'])
    if key in seen: 
        continue
    seen.add(key)
    deduped.append(it)

# ---------------- age buckets ----------------
now = datetime.now(timezone.utc)

def age_minutes(iso):
    try:
        dt = datetime.fromisoformat(iso)
        return (now - dt).total_seconds() / 60.0
    except Exception:
        return 1e9

buckets = {
    'breaking': [],  # <= 30m
    'day': [],       # 31m–24h
    'week': [],      # 2–7d
    'month': []      # 8–31d
}

for it in deduped:
    mins = age_minutes(it['published_at'])
    if mins <= 30:
        buckets['breaking'].append(it)
    elif mins <= 24*60:
        buckets['day'].append(it)
    elif mins <= 7*24*60:
        buckets['week'].append(it)
    elif mins <= 31*24*60:
        buckets['month'].append(it)

for k in list(buckets.keys()):
    arr = buckets[k]
    arr.sort(key=lambda x:(x['score'], x['published_at']), reverse=True)
    buckets[k] = diverse_pick(arr, PER_BUCKET, per_source_cap=2 if k=='breaking' else 3)
    for it in buckets[k]:
        it.pop('score', None)
        it.pop('ntitle', None)

buckets['generated_at'] = now.isoformat()

# ---------------- write headlines ----------------
with open(os.path.join(DATA_DIR, 'headlines.json'), 'w', encoding='utf-8') as f:
    json.dump(buckets, f, ensure_ascii=False, indent=2)

print("WROTE headlines.json with counts:", {k: len(v) for k,v in buckets.items() if isinstance(v, list)})

# ---------------- prices (top 50) ----------------
prices = []
try:
    r = requests.get(
        'https://api.coingecko.com/api/v3/coins/markets',
        params={'vs_currency':'usd','order':'market_cap_desc','per_page':50,'page':1,'price_change_percentage':'24h'},
        timeout=20
    )
    r.raise_for_status()
    for coin in r.json():
        prices.append({
            'rank': coin.get('market_cap_rank'),
            'symbol': (coin.get('symbol') or '').upper(),
            'price': coin.get('current_price'),
            'change24h': coin.get('price_change_percentage_24h')
        })
except Exception as ex:
    print('[WARN] prices fetch failed:', ex, file=sys.stderr)

with open(os.path.join(DATA_DIR, 'prices.json'), 'w', encoding='utf-8') as f:
    json.dump(prices, f, ensure_ascii=False, indent=2)

print("WROTE prices.json (count):", len(prices))
