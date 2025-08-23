#!/usr/bin/env python3
# The Blair Report — build.py v1.5 (failsafe)
# Always writes:
#   data/headlines.json  {breaking, day, week, month, generated_at}
#   data/prices.json     [ {rank,symbol,price,change24h}, ... ]

import os, json, time, hashlib, sys, re
from datetime import datetime, timezone
from urllib.parse import urlparse
from collections import defaultdict, deque

import yaml
import feedparser
import requests

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(ROOT, "data")
CONF = os.path.join(ROOT, "config", "sources.yaml")
os.makedirs(DATA_DIR, exist_ok=True)

# =============== helpers ===============
def log(msg): print(msg, file=sys.stderr)

def now_utc(): return datetime.now(timezone.utc)

def safe_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# =============== config ===============
def load_cfg():
    try:
        with open(CONF, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
            if not isinstance(cfg, dict): raise ValueError("YAML not a mapping")
            return cfg
    except Exception as e:
        log(f"WARN: sources.yaml not loaded, using safe defaults: {e}")
        return {
            "limits": {"per_category": 15},
            "sources": [
                {"name":"CoinDesk",      "url":"https://www.coindesk.com/arc/outboundfeeds/rss/"},
                {"name":"CoinTelegraph", "url":"https://cointelegraph.com/rss"},
                {"name":"Decrypt",       "url":"https://decrypt.co/feed"},
                {"name":"XRPL Blog",     "url":"https://xrpl.org/blog/index.xml"},
                {"name":"Ripple",        "url":"https://www.ripple.com/insights/feed/"},
            ]
        }

cfg = load_cfg()
PER_BUCKET = int(cfg.get("limits", {}).get("per_category", 15))
SOURCES    = cfg.get("sources", []) or []
if not SOURCES:
    log("WARN: no sources configured – using built-ins.")
    SOURCES = [
        {"name":"CoinDesk", "url":"https://www.coindesk.com/arc/outboundfeeds/rss/"},
        {"name":"CoinTelegraph", "url":"https://cointelegraph.com/rss"},
        {"name":"Decrypt", "url":"https://decrypt.co/feed"},
    ]

UA      = "BlairReportBot/1.5 (+https://theblairreport.com)"
HEADERS = {"User-Agent": UA}

def get_json(url, params=None, timeout=20, retries=2, name="json"):
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=HEADERS)
            r.raise_for_status()
            return r.json()
        except Exception as ex:
            last = ex
            log(f"WARN: {name} fetch attempt {i+1} failed: {ex}")
            if i < retries: time.sleep(1.2 * (i + 1))
    raise last

# =============== token universe (wide net) ===============
def fetch_top_tokens(max_coins=250):
    try:
        coins = get_json(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency":"usd","order":"market_cap_desc","per_page":max_coins,"page":1},
            timeout=20, retries=2, name="coingecko"
        ) or []
        tickers, names = set(), set()
        for c in coins:
            sym = (c.get("symbol") or "").strip().lower()
            nm  = (c.get("name") or "").strip().lower()
            if sym: tickers.add(sym)
            if nm:
                names.add(nm)
                for w in re.split(r"[\s\-/]+", nm):
                    if len(w) >= 3: names.add(w)
        majors = {"btc","bitcoin","eth","ethereum","xrp","ripple"}
        tickers |= majors; names |= majors
        if not tickers or not names: raise RuntimeError("empty token sets")
        return tickers, names
    except Exception as ex:
        log(f"WARN: CoinGecko token list failed, using fallback: {ex}")
        tickers = {"xrp","xdc","xlm","zbcn","hbar","link","flr","sgb","btc","eth","sol","ada","bnb","ton","doge","trx","ltc"}
        names   = {"ripple","xinfin","stellar","zebec","hedera","chainlink","flare","songbird","bitcoin","ethereum","solana","cardano","binance","toncoin","dogecoin","tron","litecoin"}
        return tickers, names

DYN_TICKERS, DYN_NAMES = fetch_top_tokens()
FOCUS_TICKERS = {"xrp","xdc","xlm","zbcn","hbar","link","flr","sgb"}
FOCUS_TERMS   = {
    "xrp","xrpl","ripple","xdc","xinfin","xlm","stellar","zbcn","zebec","hbar","hedera",
    "link","chainlink","flr","flare","sgb","songbird","r3","corda","cordapp","xdc network",
    "swift","iso 20022","dtcc","euroclear","clearstream","t+1","nostro","vostro",
    "securities depository","instant payments","rtgs","sepa","fednow","cbdc",
    "tokenization","tokenised","tokenized","rwa","real world asset","real-world asset",
    "crypto","cryptocurrency","blockchain","onchain","web3","defi","l2","layer 2",
    "stablecoin","usdc","usdt","etf","spot etf","smart contract","wallet","custody",
    "staking","dex","cex","tokenomics","airdrop","interoperability"
}

WHITELIST_DOMAINS = {
    "xrpl.org","ripple.com","xinfin.org","xdc.org","zebec.io","hedera.com","chain.link","rwa.xyz",
    "swift.com","dtcc.com","euroclear.com","clearstream.com",
    "coindesk.com","cointelegraph.com","decrypt.co","theblock.co","r3.com","bis.org",
    "imf.org","worldbank.org","ecb.europa.eu","federalreserve.gov","sec.gov"
}

def host_of(u: str) -> str:
    try: return (urlparse(u).hostname or "").lower().replace("www.","")
    except: return ""

def is_crypto_relevant(title, summary, link):
    t = (title or "").lower()
    s = (summary or "").lower()
    l = (link or "").lower()
    blob = " ".join([t, s, l])

    # $TICKER signal
    if re.search(r"\$([a-z0-9]{2,10})\b", t) or re.search(r"\$([a-z0-9]{2,10})\b", s):
        return True

    # bare tickers
    words = set(re.findall(r"[a-z0-9]+", blob))
    if words & (DYN_TICKERS | FOCUS_TICKERS):
        return True

    # names / terms
    for n in DYN_NAMES:
        if n in blob: return True
    for term in FOCUS_TERMS:
        if term in blob: return True

    # trusted sources
    if host_of(link) in WHITELIST_DOMAINS:
        return True

    return False

GOOD_WORDS = [
    'xrp','xrpl','ripple','xdc','xinfin','xlm','stellar','zbcn','zebec','hbar','hedera','link','chainlink','flr','flare','sgb','songbird',
    'swift','iso 20022','dtcc','euroclear','clearstream','nostro','vostro','rtgs','securities depository',
    'tokenization','tokenized','tokenised','rwa','real-world asset','pilot','production','integration',
    'partnership','institution','bank','approval','listing','launch','upgrade','framework','compliance','settlement','custody','treasury',
    'testnet','mainnet','etf','spot etf','onchain','defi','interoperability','regulation','ruling'
]
BAD_WORDS  = ['to the moon','lambo','giveaway','airdrop scam','rug','pump and dump','100x','1000x','thousandx','rocket','buy now','guaranteed profits']
SCORE_DROP_THRESHOLD = -1

def score_text(title, summary):
    t = (title or '').lower()
    s = (summary or '').lower()
    score = 0
    for w in GOOD_WORDS:
        if w in t or w in s: score += 2
    for w in BAD_WORDS:
        if w in t or w in s: score -= 3
    # boosts
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
    except:
        return (fallback or '').lower()

def normalize_title(t):
    t = re.sub(r'[^a-z0-9\s]', ' ', (t or '').lower())
    t = re.sub(r'\s+', ' ', t).strip()
    STOP = {'the','a','an','to','of','for','on','in','and','with','by','from','is','are'}
    return ' '.join([w for w in t.split() if w not in STOP])

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

# =============== ingest ===============
raw = []
seen_links = set()
log(f"INFO: ingesting {len(SOURCES)} sources")

for i, src in enumerate(SOURCES, start=1):
