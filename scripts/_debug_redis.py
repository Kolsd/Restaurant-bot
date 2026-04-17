import redis
import json
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

URL = "redis://default:mNpRRmtVqaiaSvoirhOkkuqoewNWaBwt@nozomi.proxy.rlwy.net:11250"
PHONE = '573144914554'
BOT = '573108187460'

r = redis.Redis.from_url(URL, decode_responses=True)

print("=== keys containing phone ===")
for k in r.scan_iter(match=f"mesio:*{PHONE}*", count=500):
    print(f"  {k}")
    ttl = r.ttl(k)
    val = r.get(k)
    print(f"    ttl={ttl}  val={val[:500] if val else val}")

print("\n=== keys containing bot ===")
for k in r.scan_iter(match=f"mesio:*{BOT}*", count=500):
    print(f"  {k}")
    ttl = r.ttl(k)
    val = r.get(k)
    print(f"    ttl={ttl}  val={(val or '')[:300]}")

print("\n=== order_dedup keys ===")
for k in r.scan_iter(match="mesio:*dedup*", count=500):
    print(f"  {k}")
for k in r.scan_iter(match="*dedup*", count=500):
    print(f"  (no prefix) {k}")

print("\n=== all keys (up to 200) ===")
for i, k in enumerate(r.scan_iter(count=500)):
    if i >= 200:
        print("  ... (truncated)")
        break
    print(f"  {k}")
