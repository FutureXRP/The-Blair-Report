#!/usr/bin/env python3
# The Blair Report — build.py (5-minute cycle, breaking = strict recency)

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

UA = "BlairReportBot/1.4 (+https://theblairreport.com)"
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
            if i < retries: time.sleep(1.2*(i+1))
    raise last

# ---------------- Token universe ----------------
def fetch_top_tokens(max_coins=250, vs="usd"):
    try:
        coins = get_json(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": vs,"order":"market_cap_desc","per_page":max_coins,"page":1},
            timeout=20, retries=2
        ) or []
        tickers, names = set(), set()
        for c in coins:
            sym = (c.get("symbol") or "").lower()
            nm  = (c.get("name") or "").lower()
            if sym: tickers.add(sym)
            if nm: names.add(nm)
        majors = {"btc","bitcoin","eth","ethereum","xrp","ripple"}
        return tickers|majors, names|majors
    except Exception as ex:
        print("[WARN] CoinGecko fetch failed, fallback tokens:", ex, file=sys.stderr)
        return {"xrp","xdc","xlm","zbcn","hbar","btc","eth"}, {"ripple","xinfin","stellar","hedera","bitcoin","ethereum"}

DYN_TICKERS, DYN_NAMES = fetch_top_tokens()
FOCUS_TICKERS = {"xrp","xdc","xlm","zbcn","hbar","link","flr","sgb"}
FOCUS_TERMS   = {"tokenization","rwa","iso 20022","swift","dtcc","euroclear","clearstream","cbdc"}

WHITELIST_DOMAINS = {
    "xrpl.org","ripple.com","xinfin.org","xdc.org","zebec.io","hedera.com",
    "chain.link","rwa.xyz","swift.com","dtcc.com","euroclear.com","clearstream.com",
    "coindesk.com","cointelegraph.com","decrypt.co","theblock.co","r3.com"
}

def host_of(u:str) -> str:
    try: return (urlparse(u).hostname or "").lower().replace("www.","")
    except: return ""

def is_crypto_relevant(title, summary, link):
    text = " ".join([(title or "").lower(), (summary or "").lower(), (link or "").lower()])
    if re.search(r"\$([a-z0-9]{2,10})\b", title): return True
    words = set(re.findall(r"[a-z0-9]+", text))
    if words & (DYN_TICKERS | FOCUS_TICKERS): return True
    if any(n in text for n in DYN_NAMES|FOCUS_TERMS): return True
    if host_of(link) in WHITELIST_DOMAINS: return True
    return False

GOOD_WORDS = ['xrp','xdc','xlm','zbcn','hbar','link','flr','sgb','tokenization','rwa','iso 20022','swift','dtcc']
BAD_WORDS  = ['to the moon','lambo','giveaway','rug','pump and dump','100x','rocket']
SCORE_DROP_THRESHOLD = -1

def score_text(title, summary):
    t, s = title.lower(), summary.lower()
    score = 0
    for w in GOOD_WORDS:
        if w in t or w in s: score += 2
    for w in BAD_WORDS:
        if w in t or w in s: score -= 3
    if set(re.findall(r"[a-z0-9]+", t)) & FOCUS_TICKERS: score += 3
    return score

def canonical_source(link, fallback):
    try: return (urlparse(link).hostname or '').lower().replace('www.','') or (fallback or '').lower()
    except: return (fallback or '').lower()

def normalize_title(t):
    t = re.sub(r'[^a-z0-9\s]', ' ', (t or '').lower())
    t = re.sub(r'\s+', ' ', t).strip()
    STOP = {'the','a','an','to','of','for','on','in','and','with','by','from','is','are'}
    return ' '.join([w for w in t.split() if w not in STOP])

def diverse_pick(items, total_limit, per_source_cap=2):
    buckets, count_by_src = defaultdict(deque), defaultdict(int)
    for it in items: buckets[it['source']].append(it)
    sources = deque(sorted(buckets.keys()))
    chosen = []
    while sources and len(chosen) < total_limit:
        s = sources[0]
        if buckets[s]:
            if count_by_src[s] < per_source_cap:
                chosen.append(buckets[s].popleft()); count_by_src[s]+=1; sources.rotate(-1)
            else: sources.popleft()
        else: sources.popleft()
    return chosen

# ---------------- Ingest ----------------
raw, seen_links = [], set()
print(f"Starting ingest: {len(SOURCES)} sources")
for i, src in enumerate(SOURCES, start=1):
    name, url = src.get("name","source"), src.get("url","")
    if not url: continue
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15); resp.raise_for_status()
        d = feedparser.parse(resp.content)
    except Exception as ex:
        print(f"[WARN] fetch/parse error {name}: {ex}", file=sys.stderr); continue
    for e in d.entries[:150]:
        title, link = (e.get("title") or "").strip(), (e.get("link") or "").strip()
        if not title or not link: continue
        summary = (getattr(e,"summary","") or "")
        if not is_crypto_relevant(title, summary, link): continue
        sc = score_text(title, summary)
        if sc < SCORE_DROP_THRESHOLD: continue
        h = hashlib.sha1(link.encode("utf-8")).hexdigest()
        if h in seen_links: continue
        seen_links.add(h)
        published_dt = datetime.now(timezone.utc)
        for k in ("published_parsed","updated_parsed","created_parsed"):
            val = getattr(e, k, None)
            if val:
                try: published_dt = datetime.fromtimestamp(time.mktime(val), tz=timezone.utc); break
                except: pass
        raw.append({
            "title": title, "link": link, "published_at": published_dt.isoformat(),
            "source": canonical_source(link, name),
            "score": sc, "ntitle": normalize_title(title)
        })

print(f"Ingest complete. Items (pre-dedupe): {len(raw)}")

# ---------------- Dedupe ----------------
seen, deduped = set(), []
for it in sorted(raw, key=lambda x:(x["score"], x["published_at"]), reverse=True):
    key = (it["ntitle"], it["source"])
    if key in seen: continue
    seen.add(key); deduped.append(it)

# ---------------- Buckets ----------------
now = datetime.now(timezone.utc)
def age_minutes(iso):
    try: return (now - datetime.fromisoformat(iso)).total_seconds()/60.0
    except: return 1e9
buckets = {"breaking": [],"day": [],"week": [],"month": []}
for it in deduped:
    mins = age_minutes(it["published_at"])
    if mins <= 12*60: buckets["breaking"].append(it)
    elif mins <= 24*60: buckets["day"].append(it)
    elif mins <= 7*24*60: buckets["week"].append(it)
    elif mins <= 21*24*60: buckets["month"].append(it)

# Breaking = strict newest first
arr = sorted(buckets["breaking"], key=lambda x: x["published_at"], reverse=True)[:PER_BUCKET]
for it in arr: it.pop("score", None); it.pop("ntitle", None)
buckets["breaking"] = arr

# Others = score-first w/ diversity
for k in ["day","week","month"]:
    arr = buckets[k]; arr.sort(key=lambda x:(x["score"], x["published_at"]), reverse=True)
    arr = diverse_pick(arr, PER_BUCKET, per_source_cap=2)
    for it in arr: it.pop("score", None); it.pop("ntitle", None)
    buckets[k] = arr

buckets["generated_at"] = now.isoformat()

with open(os.path.join(DATA_DIR, "headlines.json"), "w", encoding="utf-8") as f:
    json.dump(buckets, f, ensure_ascii=False, indent=2)
print("WROTE headlines.json counts:", {k: len(v) for k,v in buckets.items() if isinstance(v,list)})

# ---------------- Prices ----------------
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
