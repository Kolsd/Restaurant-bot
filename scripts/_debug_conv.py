import asyncio
import asyncpg
import json

DB_URL = "postgresql://postgres:trDkVeDkhTuDaeoCpeIfOfDpDfwpxOuu@nozomi.proxy.rlwy.net:55326/railway"

async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        row = await conn.fetchrow(
            """
            SELECT phone, bot_number, restaurant_id, branch_id, bot_paused, updated_at,
                   jsonb_typeof(history) as htype, history
            FROM conversations
            WHERE phone='573144914554' AND bot_number='573108187460'
            """
        )
        print(f"phone={row['phone']} bot={row['bot_number']} rid={row['restaurant_id']} branch={row['branch_id']}")
        print(f"paused={row['bot_paused']} at={row['updated_at']} htype={row['htype']}")
        hist = row['history']
        if isinstance(hist, str):
            hist = json.loads(hist)
        if isinstance(hist, list):
            print(f"  len={len(hist)}")
            for i, msg in enumerate(hist[-30:]):
                role = msg.get('role', '?')
                content = msg.get('content', '')
                if isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict):
                            t = c.get('type')
                            if t == 'text':
                                parts.append(f"TEXT: {c.get('text', '')[:400]}")
                            elif t == 'tool_use':
                                parts.append(f"TOOL_USE name={c.get('name')!r} input={json.dumps(c.get('input', {}), ensure_ascii=False)[:300]}")
                            elif t == 'tool_result':
                                cc = c.get('content', '')
                                if isinstance(cc, list):
                                    cc = cc[0].get('text', '') if cc else ''
                                parts.append(f"TOOL_RESULT: {str(cc)[:300]}")
                            elif t == 'image':
                                parts.append("[IMAGE]")
                    content_str = "\n    ".join(parts)
                else:
                    content_str = str(content)[:500]
                print(f"\n  [{i}] role={role}")
                print(f"    {content_str}")
        else:
            print(f"  history is not a list: {type(hist)} = {str(hist)[:500]}")

        # Check checkout_proposals exists
        print()
        print("=== Tables ===")
        rows = await conn.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE '%checkout%' OR table_name LIKE '%proposal%' OR table_name LIKE '%order%' ORDER BY table_name")
        for r in rows:
            print(f"  {r['table_name']}")

        # Check last inbox with full payload
        print()
        print("=== Last inbox 207,208 payloads ===")
        rows = await conn.fetch("SELECT id, payload FROM webhook_inbox WHERE id IN (201,202,203,204,205,206,207,208) ORDER BY id")
        for r in rows:
            p = r['payload']
            if isinstance(p, str):
                try:
                    p = json.loads(p)
                except Exception:
                    pass
            print(f"  id={r['id']} payload={json.dumps(p, ensure_ascii=False)[:400]}")
    finally:
        await conn.close()

asyncio.run(main())
