#!/usr/bin/env python3
# The Blair Report — build.py (failsafe, wide-net)
# Outputs:
#   data/headlines.json  (breaking/day/week/month + generated_at)
#   data/prices.json     (top 50 snapshot)

import os, json, time, hashlib, sys, re
from datetime import datetime, timezone
from urllib.parse import urlparse
from collections import defaultdict, deque

import yaml
import feedparser
import requests

# ---------------- Paths ----------------
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(ROOT, "data")
CONF = os.path.join(ROOT, "config", "sources.yaml")
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------- Config ----------------
def load_cfg():
    try:
        with open(CONF, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print("WARN: cannot read config/sources.yaml; using defaults:", e, file=sys.stderr)
        return {
            "limits": {"per_category": 15},
            "sources": [
                {"name":"CoinDesk", "url":"https://www.coindesk.com/arc/outboundfeeds/rss/"},
                {"name":"CoinTelegraph", "url":"https://cointelegraph.com/rss"},
                {"name":"Decrypt", "url":"https://decrypt.co/feed"},
                {"name":"XRPL Blog", "url":"https://xrpl.org/blog/index.xml"},
                {"name":"Ripple", "url":"https://www.ripple.com/insights/feed/"},
            ]
        }

cfg = load_cfg()
PER_BUCKET = int(cfg.get("limits", {}).get("per_category", 15))
SOURCES = cfg.get("sources", [])

# ---------------- HTTP ----------------
UA = "BlairReportBot/1.3 (+https://theblairreport.com)"
HEADERS = {"User-Agent": UA}

def get_json(url, params=None, timeout=20, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=HEADERS)
            r.raise_for_status()
            return r.json()
        except Exception as ex:
            last = ex
            if i < retries:
                time.sleep(1.2 * (i + 1))
    raise last

# ---------------- Token universe (dynamic + fallback) ----------------
def fetch_top_tokens(max_coins=250, vs="usd"):
    try:
        coins = get_json(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": vs,
                "order": "market_cap_desc",
                "per_page": max_coins,
                "page": 1,
                "price_change_percentage": "24h",
            },
            timeout=20, retries=2
        ) or []
        tickers = set()
        names = set()
        for c in coins:
            sym = (c.get("symbol") or "").strip().lower()
            nm  = (c.get("name") or "").strip().lower()
            if sym: tickers.add(sym)
            if nm:
                names.add(nm)
                # split multi-word names so "bitcoin cash" also matches "bitcoin" & "cash"
                for w in re.split(r"[\s\-/]+", nm):
                    if len(w) >= 3:
                        names.add(w)
        majors = {"btc","bitcoin","eth","ethereum","xrp","ripple","sol","solana","ada","cardano"}
        tickers |= majors
        names   |= majors
        if not tickers or not names:
            raise RuntimeError("empty token sets")
        return tickers, names
    except Exception as ex:
        print("[WARN] CoinGecko token list fetch failed; using static fallback:", ex, file=sys.stderr)
        tickers = {
            "xrp","xdc","xlm","zbcn","hbar","link","flr","sgb",
            "btc","eth","sol","ada","avax","dot","matic","atom","near",
            "algo","apt","sui","inj","stx","op","arb","bnb","ton","doge","shib","trx","ltc"
        }
        names = {
            "ripple","xinfin","stellar","zebec","hedera","chainlink","flare","songbird",
            "bitcoin","ethereum","solana","cardano","avalanche","polkadot","polygon",
            "cosmos","near","algorand","aptos","sui","injective","stacks",
            "optimism","arbitrum","binance","toncoin","dogecoin","shiba","tron","litecoin"
        }
        return tickers, names

DYN_TICKERS, DYN_NAMES = fetch_top_tokens(250)
FOCUS_TICKERS = {"xrp","xdc","xlm","zbcn","hbar","link","flr","sgb"}  # scoring boost

FOCUS_TERMS = {
    "xrp","xrpl","ripple","xdc","xinfin","xlm","stellar","zbcn","zebec","hbar","hedera",
    "link","chainlink","flr","flare","sgb","songbird","xdc network","r3","corda","cordapp",
    "swift","iso 20022","dtcc","euroclear","clearstream","t+1","nostro","vostro",
    "securities depository","instant payments","rtgs","sepa","fednow","cbdc",
    "tokenization","tokenised","tokenized","rwa","real world asset","real-world asset",
    "crypto","cryptocurrency","blockchain","onchain","web3","defi","l2","layer 2",
    "stablecoin","usdc","usdt","etf","spot etf","smart contract","wallet","custody",
    "staking","dex","cex","tokenomics","airdrop","interoperability"
}

WHITELIST_DOMAINS = {
    "xrpl.org","ripple.com","xinfin.org","xdc.org","zebec.io","hedera.com",
    "chain.link","rwa.xyz","swift.com","dtcc.com","euroclear.com","clearstream.com",
    "coindesk.com","cointelegraph.com","decrypt.co","theblock.co","r3.com","bis.org",
    "imf.org","worldbank.org","ecb.europa.eu","federalreserve.gov","sec.gov"
}

def host_of(u: str) -> str:
    try:
        return (urlparse(u).hostname or "").lower().replace("www.","")
    except Exception:
        return ""

# Fast, robust relevance (no giant regex)
def is_crypto_relevant(title, summary, link):
    t = (title or "").lower()
    s = (summary or "").lower()
    l = (link or "").lower()
    blob = " ".join([t, s, l])

    # 1) $TICKER anywhere (e.g., "$xrp")
    if re.search(r"\$([a-z0-9]{2,10})\b", t) or re.search(r"\$([a-z0-9]{2,10})\b", s):
        return True

    # 2) bare ticker words
    words = set(re.findall(r"[a-z0-9]+", blob))
    if words & (DYN_TICKERS | FOCUS_TICKERS):
        return True

    # 3) coin names / focus terms contained
    for name in DYN_NAMES:
        if name in blob:
            return True
    for term in FOCUS_TERMS:
        if term in blob:
            return True

    # 4) trusted domains
    if host_of(link) in WHITELIST_DOMAINS:
        return True

    return False

# ---------------- Scoring ----------------
GOOD_WORDS = [
    'xrp','xrpl','ripple','xdc','xinfin','xlm','stellar','zbcn','zebec','hbar','hedera',
    'link','chainlink','flr','flare','sgb','songbird',
    'swift','iso 20022','dtcc','euroclear','clearstream','nostro','vostro','rtgs','securities depository',
    'tokenization','tokenized','tokenised','rwa','real-world asset','pilot','production','integration',
    'partnership','institution','bank','approval','listing','launch','upgrade','roadmap','framework',
    'compliance','settlement','custody','treasury','testnet','mainnet',
    'etf','spot etf','onchain','defi','interoperability','regulation','ruling'
]
BAD_WORDS = [
    'to the moon','lambo','giveaway','airdrop scam','rug','pump and dump',
    '100x','1000x','thousandx','rocket','buy now','guaranteed profits'
]
SCORE_DROP_THRESHOLD = -1

def score_text(title, summary):
    t = (title or '').lower()
    s = (summary or '').lower()
    score = 0
    for w in GOOD_WORDS:
        if w in t or w in s: score += 2
    for w in BAD_WORDS:
        if w in t or w in s: score -= 3
    # Boost for focus tickers/terms
    if (set(re.findall(r"[a-z0-9]+", t)) & FOCUS_TICKERS) or (set(re.findall(r"[a-z0-9]+", s)) & FOCUS_TICKERS):
        score += 3
    if any(k in t for k in ("tokenization","tokenized","rwa","iso 20022","swift","dtcc","euroclear","clearstream")):
        score += 2
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

# ---------------- Ingest ----------------
raw = []
seen_links = set()
print(f"Starting ingest: {len(SOURCES)} sources")

for i, src in enumerate(SOURCES, start=1):
    name = src.get("name","source")
    url  = src.get("url","")
    if not url: continue
    print(f"[{i}/{len(SOURCES)}] Fetching: {name} -> {url}")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        d = feedparser.parse(resp.content)
    except Exception as ex:
        print(f"[WARN] fetch/parse error {name}: {ex}", file=sys.stderr)
        continue

    for e in d.entries[:150]:
        title = (e.get("title") or "").strip()
        link  = (e.get("link")  or "").strip()
        if not title or not link: continue

        summary = (getattr(e, "summary", "") or "")
        if not is_crypto_relevant(title, summary, link):
            continue

        sc = score_text(title, summary)
        if sc < SCORE_DROP_THRESHOLD:
            continue

        h = hashlib.sha1(link.encode("utf-8")).hexdigest()
        if h in seen_links: continue
        seen_links.add(h)

        published_dt = None
        for k in ("published_parsed","updated_parsed","created_parsed"):
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
            "title": title,
            "link": link,
            "published_at": published_dt.isoformat(),
            "source": src_domain,
            "score": sc,
            "ntitle": normalize_title(title),
        })

print(f"Ingest complete. Items (pre-dedupe): {len(raw)}")

# ---------------- Dedupe ----------------
seen = set()
deduped = []
for it in sorted(raw, key=lambda x: (x["score"], x["published_at"]), reverse=True):
    key = (it["ntitle"], it["source"])
    if key in seen: continue
    seen.add(key)
    deduped.append(it)

# ---------------- Buckets ----------------
now = datetime.now(timezone.utc)

def age_minutes(iso):
    try:
        dt = datetime.fromisoformat(iso)
        return (now - dt).total_seconds() / 60.0
    except Exception:
        return 1e9

buckets = {"breaking": [], "day": [], "week": [], "month": []}
for it in deduped:
    mins = age_minutes(it["published_at"])
    if mins <= 12*60: buckets["breaking"].append(it)
    elif mins <= 24*60: buckets["day"].append(it)
    elif mins <= 7*24*60: buckets["week"].append(it)
    elif mins <= 21*24*60: buckets["month"].append(it)

# rank + diversify
arr = buckets["breaking"]; arr.sort(key=lambda x: (x["published_at"], x["score"]), reverse=True)
buckets["breaking"] = [ {k:v for k,v in it.items() if k not in ("score","ntitle")} for it in diverse_pick(arr, PER_BUCKET, 2) ]

for k in ["day","week","month"]:
    arr = buckets[k]; arr.sort(key=lambda x: (x["score"], x["published_at"]), reverse=True)
    buckets[k] = [ {kk:vv for kk,vv in it.items() if kk not in ("score","ntitle")} for it in diverse_pick(arr, PER_BUCKET, 2) ]

buckets["generated_at"] = now.isoformat()

with open(os.path.join(DATA_DIR, "headlines.json"), "w", encoding="utf-8") as f:
    json.dump(buckets, f, ensure_ascii=False, indent=2)
print("WROTE headlines.json counts:", {k: len(v) for k,v in buckets.items() if isinstance(v, list)})

# ---------------- Prices (top 50) ----------------
prices = []
try:
    coins = get_json(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={"vs_currency":"usd","order":"market_cap_desc","per_page":50,"page":1,"price_change_percentage":"24h"},
        timeout=20, retries=2
    )
    for coin in coins:
        prices.append({
            "rank": coin.get("market_cap_rank"),
            "symbol": (coin.get("symbol") or "").upper(),
            "price": coin.get("current_price"),
            "change24h": coin.get("price_change_percentage_24h")
        })
except Exception as ex:
    print("[WARN] prices fetch failed:", ex, file=sys.stderr)

with open(os.path.join(DATA_DIR, "prices.json"), "w", encoding="utf-8") as f:
    json.dump(prices, f, ensure_ascii=False, indent=2)
print("WROTE prices.json count:", len(prices))
