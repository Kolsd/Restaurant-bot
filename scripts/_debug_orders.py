import asyncio
import asyncpg
import json

DB_URL = "postgresql://postgres:trDkVeDkhTuDaeoCpeIfOfDpDfwpxOuu@nozomi.proxy.rlwy.net:55326/railway"

async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        print("=" * 80)
        print("0) ALL-TIME order counts")
        print("=" * 80)
        rows = await conn.fetch("SELECT COUNT(*) as n, MAX(created_at) as last FROM orders")
        r = rows[0]
        print(f"  total orders all-time: {r['n']}  last: {r['last']}")

        rows = await conn.fetch(
            """
            SELECT id, restaurant_id, status, paid, total, created_at, phone, bot_number, order_type
            FROM orders
            ORDER BY created_at DESC
            LIMIT 20
            """
        )
        print("  RECENT (all-time):")
        for r in rows:
            print(f"    id={r['id']} rid={r['restaurant_id']} status={r['status']!r} paid={r['paid']} type={r['order_type']} total={r['total']} phone={r['phone']} at={r['created_at']}")

        print()
        print("=" * 80)
        print("1) INBOX FULL PAYLOADS (last 2h, latest 15)")
        print("=" * 80)
        rows = await conn.fetch(
            """
            SELECT id, provider, external_id, processed_at, attempts, last_error, received_at, payload
            FROM webhook_inbox
            WHERE received_at > NOW() - INTERVAL '2 hours'
            ORDER BY id DESC
            LIMIT 15
            """
        )
        for r in rows:
            print(f"\n  --- id={r['id']} received={r['received_at']} attempts={r['attempts']} processed={bool(r['processed_at'])} err={(r['last_error'] or '')[:100]!r}")
            payload = r['payload']
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    pass
            print(f"    payload keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload)}")
            if isinstance(payload, dict):
                print(f"    payload: {json.dumps(payload, default=str)[:600]}")

        print()
        print("=" * 80)
        print("2) CONVERSATIONS (last 2h)")
        print("=" * 80)
        rows = await conn.fetch(
            """
            SELECT phone, bot_number, restaurant_id, branch_id, bot_paused, updated_at,
                   CASE WHEN jsonb_typeof(history) = 'array' THEN jsonb_array_length(history) ELSE NULL END as hlen
            FROM conversations
            WHERE updated_at > NOW() - INTERVAL '2 hours'
            ORDER BY updated_at DESC
            LIMIT 20
            """
        )
        for r in rows:
            print(f"  phone={r['phone']} bot={r['bot_number']} rid={r['restaurant_id']} branch={r['branch_id']} paused={r['bot_paused']} hlen={r['hlen']} at={r['updated_at']}")

        print()
        print("=" * 80)
        print("3) CARTS (recent)")
        print("=" * 80)
        rows = await conn.fetch(
            """
            SELECT phone, bot_number, restaurant_id, updated_at, cart_data
            FROM carts
            WHERE updated_at > NOW() - INTERVAL '24 hours'
            ORDER BY updated_at DESC
            LIMIT 20
            """
        )
        for r in rows:
            cart = r['cart_data']
            if isinstance(cart, str):
                try:
                    cart = json.loads(cart)
                except Exception:
                    pass
            items = cart.get('items') if isinstance(cart, dict) else None
            n = len(items) if isinstance(items, list) else None
            print(f"  phone={r['phone']} bot={r['bot_number']} rid={r['restaurant_id']} items={n} at={r['updated_at']}")

        print()
        print("=" * 80)
        print("4) LAST CONVERSATION MESSAGES (for Herradura, last 2h)")
        print("=" * 80)
        row = await conn.fetchrow(
            """
            SELECT phone, bot_number, restaurant_id, history, updated_at
            FROM conversations
            WHERE updated_at > NOW() - INTERVAL '2 hours'
            ORDER BY updated_at DESC
            LIMIT 1
            """
        )
        if row:
            print(f"  phone={row['phone']} bot={row['bot_number']} rid={row['restaurant_id']} at={row['updated_at']}")
            hist = row['history']
            if isinstance(hist, str):
                hist = json.loads(hist)
            # Show last 15 messages
            for msg in (hist[-20:] if isinstance(hist, list) else []):
                role = msg.get('role', '?')
                content = msg.get('content', '')
                if isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict):
                            t = c.get('type')
                            if t == 'text':
                                parts.append(c.get('text', '')[:200])
                            elif t == 'tool_use':
                                parts.append(f"[TOOL_USE name={c.get('name')} input={json.dumps(c.get('input'))[:200]}]")
                            elif t == 'tool_result':
                                cc = c.get('content', '')
                                if isinstance(cc, list):
                                    cc = cc[0].get('text', '') if cc else ''
                                parts.append(f"[TOOL_RESULT {cc[:200]}]")
                    content_str = " | ".join(parts)
                else:
                    content_str = str(content)[:400]
                print(f"    [{role}] {content_str}")

        print()
        print("=" * 80)
        print("5) Checkout_proposals table?")
        print("=" * 80)
        rows = await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'checkout_proposals' ORDER BY ordinal_position
            """
        )
        print("  cols:", [r['column_name'] for r in rows])
        if rows:
            rows2 = await conn.fetch("SELECT * FROM checkout_proposals ORDER BY created_at DESC LIMIT 10")
            for r in rows2:
                print(f"  {dict(r)}")

    finally:
        await conn.close()

asyncio.run(main())
