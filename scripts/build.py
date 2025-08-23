#!/usr/bin/env python3
# The Blair Report - Aggregator build script (wide-net edition)
# - Buckets by freshness with non-overlapping categories
# - Crypto-only gating using dynamic token lists (top ~200-250 by mcap from CoinGecko)
# - Quality scoring + per-source diversity
# - Outputs:
#     data/headlines.json
#     data/prices.json

import os, json, time, hashlib, sys, re
from datetime import datetime, timezone
from urllib.parse import urlparse
from collections import defaultdict, deque

import yaml
import feedparser
import requests

# ---------------- Paths & setup ----------------
ROOT = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(ROOT, 'data')
CONF = os.path.join(ROOT, 'config', 'sources.yaml')
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------- Config read ----------------
try:
    with open(CONF, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
except Exception as e:
    print("ERROR: cannot read config/sources.yaml:", e, file=sys.stderr)
    cfg = {}

limits = cfg.get('limits', {})
PER_BUCKET = int(limits.get('per_category', 15))  # tighter curation

# ---------------- HTTP defaults ----------------
UA = "BlairReportBot/1.1 (+https://theblairreport.com)"
headers = {"User-Agent": UA}

# ============================================================================
# Dynamic token universe (top ~200–250 by market cap from CoinGecko)
# Falls back to a strong static set if API fails.
# ============================================================================
def fetch_top_tokens(max_coins=250, vs="usd"):
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": vs,
                "order": "market_cap_desc",
                "per_page": max_coins,
                "page": 1,
                "price_change_percentage": "24h",
                # no sparkline to keep payload lean
            },
            timeout=20,
            headers=headers,
        )
        r.raise_for_status()
        coins = r.json() or []
        tickers = set()
        names = set()
        for c in coins:
            sym = (c.get("symbol") or "").strip().lower()
            nm = (c.get("name") or "").strip().lower()
            if sym:
                tickers.add(sym)  # e.g., "xrp"
            if nm:
                # accept full name and split words (e.g., "bitcoin cash" -> "bitcoin", "cash")
                names.add(nm)
                for w in re.split(r"[\s\-/]+", nm):
                    if len(w) >= 3:
                        names.add(w)
        # keep common majors explicitly even if pagination changes
        majors = {"btc","bitcoin","eth","ethereum","xrp","ripple","sol","solana","ada","cardano"}
        tickers |= majors
        names |= majors
        return tickers, names
    except Exception as ex:
        print("[WARN] CoinGecko token list fetch failed; using static fallback:", ex, file=sys.stderr)
        # Fallback includes focus stack + majors + a few more L1s
        fallback_tickers = {
            "xrp","xdc","xlm","zbcn","hbar","link","flr","sgb",
            "btc","eth","sol","ada","avax","dot","matic","atom","near","algo","apt","sui","inj","stx",
            "op","arb","bnb","ton","doge","shib","trx","ltc"
        }
        fallback_names = {
            "ripple","xinfin","stellar","zebec","hedera","chainlink","flare","songbird",
            "bitcoin","ethereum","solana","cardano","avalanche","polkadot",
            "polygon","cosmos","near","algorand","aptos","sui","injective","stacks",
            "optimism","arbitrum","binance","toncoin","dogecoin","shiba","tron","litecoin"
        }
        return fallback_tickers, fallback_names

DYN_TICKERS, DYN_NAMES = fetch_top_tokens(250)

# Your focus set gets special handling (boosts, whitelist tolerance)
FOCUS_TICKERS = {"xrp","xdc","xlm","zbcn","hbar","link","flr","sgb"}
FOCUS_TERMS = {
    # ledgers / projects
    "xrp","xrpl","ripple","xdc","xinfin","xlm","stellar","zbcn","zebec","hbar","hedera",
    "link","chainlink","flr","flare","sgb","songbird","xdc network","r3","corda","cordapp",
    # rails / infra / standards
    "swift","iso 20022","dtcc","euroclear","clearstream","t+1","nostro","vostro",
    "securities depository","instant payments","rtgs","sepa","fednow","cbdc",
    # tokenization / rwa
    "tokenization","tokenised","tokenized","rwa","real world asset","real-world asset",
    # general crypto so we don’t miss broad breaking stories
    "crypto","cryptocurrency","blockchain","onchain","web3","defi","l2","layer 2",
    "stablecoin","usdc","usdt","etf","spot etf","smart contract","wallet","custody",
    "staking","dex","cex","tokenomics","airdrop","interoperability"
}

# Build regex: dynamic tokens (tickers + names), focus terms, majors
def _make_relevant_regex():
    parts = []

    # $TICKERS at the beginning of title or in text ($XRP, $ETH, etc.)
    parts.append(r"\$\b?(?:%s)\b" % "|".join(sorted(re.escape(t) for t in DYN_TICKERS | FOCUS_TICKERS)))

    # Bare tickers (word-bounded)
    parts.append(r"\b(?:%s)\b" % "|".join(sorted(re.escape(t) for t in DYN_TICKERS | FOCUS_TICKERS)))

    # Coin names (full or word tokens), include dynamic + focus names
    parts.append(r"\b(?:%s)\b" % "|".join(sorted(re.escape(n) for n in DYN_NAMES | FOCUS_TERMS)))

    # Keep explicit majors for safety
    parts.append(r"\b(?:btc|bitcoin|eth|ethereum|sol|solana|ada|cardano)\b")

    return re.compile("(" + "|".join(parts) + ")", re.IGNORECASE)

RELEVANT_PATTERNS = _make_relevant_regex()

# Trusted sources (auto-pass domain check)
WHITELIST_DOMAINS = {
    "xrpl.org","ripple.com","xinfin.org","xdc.org","zebec.io","hedera.com",
    "chain.link","rwa.xyz","swift.com","dtcc.com","euroclear.com","clearstream.com",
    "coindesk.com","cointelegraph.com","decrypt.co","theblock.co","r3.com","bis.org",
    "imf.org","worldbank.org","ecb.europa.eu","federalreserve.gov","sec.gov"
}

def _host(u: str) -> str:
    try:
        return (urlparse(u).hostname or "").lower().replace("www.","")
    except Exception:
        return ""

def is_crypto_relevant(title, summary, link):
    text = f"{title or ''} {summary or ''} {link or ''}"
    if RELEVANT_PATTERNS.search(text):
        return True
    # Domain whitelist (crypto/finance infra sources can be sparse in keywords)
    if _host(link) in WHITELIST_DOMAINS:
        return True
    # $TICKER at title start
    t = (title or "").strip()
    if re.match(r"^\s*\$(%s)\b" % "|".join(re.escape(x) for x in FOCUS_TICKERS | DYN_TICKERS), t, re.IGNORECASE):
        return True
    return False

# ---------------- Quality scoring (soft ranking) ----------------
GOOD_WORDS = [
    # focus stack gets explicit boosts
    'xrp','xrpl','ripple','xdc','xinfin','xlm','stellar','zbcn','zebec','hbar','hedera',
    'link','chainlink','flr','flare','sgb','songbird',
    # rails / institutions / standards
    'swift','iso 20022','dtcc','euroclear','clearstream','nostro','vostro','rtgs','securities depository',
    # tokenization / enterprise adoption
    'tokenization','tokenized','tokenised','rwa','real-world asset','pilot','production','integration',
    'partnership','institution','bank','approval','listing','launch','upgrade','roadmap','framework',
    'compliance','settlement','custody','treasury','testnet','mainnet',
    # general
    'etf','spot etf','onchain','defi','interoperability','regulation','ruling'
]

BAD_WORDS = [
    # low-signal hype / spam
    'to the moon','lambo','giveaway','airdrop scam','rug','pump and dump',
    # clickbait-y predictions
    '100x','1000x','thousandx','rocket','buy now','guaranteed profits'
]

SCORE_DROP_THRESHOLD = -1  # drop anything worse than this

def score_text(title, summary):
    t = (title or '').lower()
    s = (summary or '').lower()
    score = 0
    for w in GOOD_WORDS:
        if w in t or w in s:
            score += 2
    for w in BAD_WORDS:
        if w in t or w in s:
            score -= 3
    # bonus for focus tickers/terms
    if re.search(r"\b(?:%s)\b" % "|".join(FOCUS_TICKERS), t) or re.search(r"\b(?:%s)\b" % "|".join(FOCUS_TICKERS), s):
        score += 3
    if any(k in t for k in ("tokenization","tokenized","rwa","iso 20022","swift","dtcc","euroclear","clearstream")):
        score += 2
    # slight boost for more specific titles
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

def diverse_pick(items, total_limit, per_source_cap=2):
    """Round-robin by source with strict per-source cap for variety."""
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
            sources.pop()
    return chosen

# ============================================================================
# Ingest
# ============================================================================
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
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        d = feedparser.parse(resp.content)
    except Exception as ex:
        print(f"[WARN] fetch/parse error {name}: {ex}", file=sys.stderr)
        continue

    for e in d.entries[:150]:
        title = (e.get('title') or '').strip()
        link = (e.get('link') or '').strip()
        if not title or not link:
            continue

        summary = (getattr(e, 'summary', '') or '')
        if not is_crypto_relevant(title, summary, link):
            continue  # hard gate: crypto-only

        sc = score_text(title, summary)
        if sc < SCORE_DROP_THRESHOLD:
            continue  # drop low-quality items early

        h = hashlib.sha1(link.encode('utf-8')).hexdigest()
        if h in seen_links:
            continue
        seen_links.add(h)

        # date extraction
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

# ---------------- Dedupe (by normalized title + source) ----------------
seen = set()
deduped = []
for it in sorted(raw, key=lambda x:(x['score'], x['published_at']), reverse=True):
    key = (it['ntitle'], it['source'])
    if key in seen:
        continue
    seen.add(key)
    deduped.append(it)

# ============================================================================
# Buckets (non-overlapping)
#   breaking: <= 12h
#   day:      > 12h and <= 24h
#   week:     > 24h and <= 7d
#   month:    > 7d and <= 21d
# ============================================================================
now = datetime.now(timezone.utc)

def age_minutes(iso):
    try:
        dt = datetime.fromisoformat(iso)
        return (now - dt).total_seconds() / 60.0
    except Exception:
        return 1e9

buckets = { 'breaking': [], 'day': [], 'week': [], 'month': [] }

for it in deduped:
    mins = age_minutes(it['published_at'])
    if mins <= 12*60:
        buckets['breaking'].append(it)
    elif mins <= 24*60:
        buckets['day'].append(it)
    elif mins <= 7*24*60:
        buckets['week'].append(it)
    elif mins <= 21*24*60:
        buckets['month'].append(it)
    # older than 21 days -> drop

# ---------------- Rank + diversify per bucket ----------------
# Breaking: recency-first (then score)
arr = buckets['breaking']
arr.sort(key=lambda x: (x['published_at'], x['score']), reverse=True)
arr = diverse_pick(arr, PER_BUCKET, per_source_cap=2)
for it in arr:
    it.pop('score', None); it.pop('ntitle', None)
buckets['breaking'] = arr

# Others: score-first (then recency)
for k in ['day','week','month']:
    arr = buckets[k]
    arr.sort(key=lambda x: (x['score'], x['published_at']), reverse=True)
    arr = diverse_pick(arr, PER_BUCKET, per_source_cap=2)
    for it in arr:
        it.pop('score', None); it.pop('ntitle', None)
    buckets[k] = arr

buckets['generated_at'] = now.isoformat()

# ---------------- Write headlines ----------------
with open(os.path.join(DATA_DIR, 'headlines.json'), 'w', encoding='utf-8') as f:
    json.dump(buckets, f, ensure_ascii=False, indent=2)

print("WROTE headlines.json with counts:",
      {k: len(v) for k, v in buckets.items() if isinstance(v, list)})

# ---------------- Prices snapshot (top 50) ----------------
prices = []
try:
    r = requests.get(
        'https://api.coingecko.com/api/v3/coins/markets',
        params={'vs_currency':'usd','order':'market_cap_desc','per_page':50,'page':1,'price_change_percentage':'24h'},
        timeout=20,
        headers=headers
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
