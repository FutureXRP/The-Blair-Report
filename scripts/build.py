#!/usr/bin/env python3
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
