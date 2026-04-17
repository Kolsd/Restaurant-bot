import asyncio
import asyncpg
import json
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DB_URL = "postgresql://postgres:trDkVeDkhTuDaeoCpeIfOfDpDfwpxOuu@nozomi.proxy.rlwy.net:55326/railway"

async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        row = await conn.fetchrow(
            """
            SELECT phone, bot_number, restaurant_id, branch_id, bot_paused, updated_at,
                   jsonb_typeof(history) as htype, history::text as hjson
            FROM conversations
            WHERE phone='573144914554' AND bot_number='573108187460'
            """
        )
        print(f"phone={row['phone']} bot={row['bot_number']} rid={row['restaurant_id']} branch={row['branch_id']}")
        print(f"paused={row['bot_paused']} at={row['updated_at']} htype={row['htype']}")

        # history is jsonb type=string (the jsonb value is a JSON-encoded string wrapping the real array)
        # asyncpg returns jsonb as text. We double-decode.
        raw = row['hjson']  # asyncpg text of the jsonb value
        # raw is like: "\"[{...},{...}]\"" - a JSON string of a JSON string
        parsed = json.loads(raw)  # gives us the inner string
        if isinstance(parsed, str):
            parsed = json.loads(parsed)
        hist = parsed
        print(f"len(history) = {len(hist) if isinstance(hist, list) else 'not-list'}")

        for i, msg in enumerate(hist):
            role = msg.get('role', '?')
            content = msg.get('content', '')
            if isinstance(content, list):
                parts = []
                for c in content:
                    if isinstance(c, dict):
                        t = c.get('type')
                        if t == 'text':
                            parts.append(f"TEXT: {c.get('text', '')[:500]}")
                        elif t == 'tool_use':
                            parts.append(f"TOOL_USE name={c.get('name')!r} input={json.dumps(c.get('input', {}), ensure_ascii=False)[:400]}")
                        elif t == 'tool_result':
                            cc = c.get('content', '')
                            if isinstance(cc, list):
                                cc = cc[0].get('text', '') if cc else ''
                            parts.append(f"TOOL_RESULT: {str(cc)[:400]}")
                        elif t == 'image':
                            parts.append("[IMAGE]")
                content_str = "\n      ".join(parts)
            else:
                content_str = str(content)[:500]
            print(f"\n  [{i}] role={role}")
            print(f"    {content_str}")

    finally:
        await conn.close()

asyncio.run(main())
