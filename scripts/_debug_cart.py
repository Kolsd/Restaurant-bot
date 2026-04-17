import asyncio
import asyncpg
import json
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DB_URL = "postgresql://postgres:trDkVeDkhTuDaeoCpeIfOfDpDfwpxOuu@nozomi.proxy.rlwy.net:55326/railway"
PHONE = '573144914554'

async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        print("=== ALL carts ever for this phone ===")
        rows = await conn.fetch("SELECT phone, bot_number, restaurant_id, updated_at, cart_data::text FROM carts WHERE phone=$1", PHONE)
        for r in rows:
            print(f"  bot={r['bot_number']} rid={r['restaurant_id']} at={r['updated_at']}")
            print(f"  cart_data: {r['cart_data']!r}")

        print()
        print("=== ALL orders ever for this phone ===")
        rows = await conn.fetch("SELECT id, status, total, bot_number, restaurant_id, created_at FROM orders WHERE phone=$1 ORDER BY created_at DESC LIMIT 20", PHONE)
        for r in rows:
            print(f"  {r['id']} rid={r['restaurant_id']} bot={r['bot_number']} status={r['status']!r} total={r['total']} at={r['created_at']}")

        print()
        print("=== Conversation branch/rid ===")
        row = await conn.fetchrow("SELECT phone, bot_number, restaurant_id, branch_id, bot_paused, updated_at FROM conversations WHERE phone=$1", PHONE)
        print(f"  {dict(row) if row else None}")

        print()
        print("=== All webhook_inbox for this phone last 4h (full payloads) ===")
        rows = await conn.fetch(
            """
            SELECT id, received_at, processed_at, attempts, last_error, payload::text as p
            FROM webhook_inbox
            WHERE (payload->>'user_phone')=$1 AND received_at > NOW() - INTERVAL '4 hours'
            ORDER BY id
            """,
            PHONE,
        )
        for r in rows:
            print(f"  id={r['id']} recv={r['received_at']} proc={r['processed_at']} att={r['attempts']} err={(r['last_error'] or '')[:120]!r}")
            print(f"    payload: {r['p']}")

        print()
        print("=== Herradura (rid 8,19,20) features for delivery ===")
        rows = await conn.fetch("SELECT id, name, whatsapp_number, latitude, longitude, features::text as f FROM restaurants WHERE id IN (8,19,20)")
        for r in rows:
            print(f"  id={r['id']} name={r['name']} bot={r['whatsapp_number']} lat={r['latitude']} lon={r['longitude']}")
            feats = r['f']
            try:
                fj = json.loads(feats) if feats else {}
                # Print only keys we care about
                pi = fj.get('payment_instructions', {})
                print(f"    payment_instructions={list(pi.keys()) if isinstance(pi, dict) else 'n/a'}")
                print(f"    payment_methods={fj.get('payment_methods')}")
                print(f"    currency={fj.get('currency')}")
                print(f"    delivery_fee={fj.get('delivery_fee')}")
                print(f"    has delivery_zones={'delivery_zones' in fj}")
            except Exception as e:
                print(f"    could not parse features: {e}")

        print()
        print("=== Menu for rid=8 (Herradura) — search 'Empanad' ===")
        row = await conn.fetchrow("SELECT menu::text as m FROM restaurants WHERE id=8")
        if row:
            try:
                menu = json.loads(row['m']) if row['m'] else {}
                for cat, items in menu.items():
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if isinstance(item, dict) and 'empanad' in (item.get('name','').lower()):
                            print(f"  cat={cat}  item={item.get('name')} price={item.get('price')} active={item.get('active', True)}")
            except Exception as e:
                print(f"  menu parse error: {e}")

        print()
        print("=== Sessions for phone ===")
        rows = await conn.fetch("SELECT * FROM sessions WHERE phone=$1 ORDER BY created_at DESC LIMIT 5", PHONE)
        for r in rows:
            d = {k: v for k, v in dict(r).items() if k not in ('token_hash','token')}
            print(f"  {d}")

    finally:
        await conn.close()

asyncio.run(main())
