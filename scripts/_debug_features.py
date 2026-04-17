import asyncio
import asyncpg
import json
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DB_URL = "postgresql://postgres:trDkVeDkhTuDaeoCpeIfOfDpDfwpxOuu@nozomi.proxy.rlwy.net:55326/railway"

def decode_jsonb(raw):
    """Double-decode a jsonb value that may be stored as a JSON-encoded string."""
    if raw is None:
        return None
    val = json.loads(raw)
    if isinstance(val, str):
        val = json.loads(val)
    return val

async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        print("=== Features for rid 8 (Herradura Matriz), 19, 20 ===")
        rows = await conn.fetch("SELECT id, name, whatsapp_number, latitude, longitude, features::text as f, menu::text as m FROM restaurants WHERE id IN (8,19,20)")
        for r in rows:
            feats = decode_jsonb(r['f'])
            print(f"\nid={r['id']} name={r['name']} bot={r['whatsapp_number']}")
            print(f"  lat={r['latitude']} lon={r['longitude']}")
            print(f"  allow_manual_table_number={feats.get('allow_manual_table_number') if isinstance(feats, dict) else '???'}")
            print(f"  module_delivery={feats.get('module_delivery') if isinstance(feats, dict) else '???'}")
            print(f"  module_pickup={feats.get('module_pickup') if isinstance(feats, dict) else '???'}")
            print(f"  delivery_fee={feats.get('delivery_fee') if isinstance(feats, dict) else '???'}")
            print(f"  currency={feats.get('currency') if isinstance(feats, dict) else '???'}")
            print(f"  payment_methods={feats.get('payment_methods') if isinstance(feats, dict) else '???'}")
            pi = feats.get('payment_instructions', {}) if isinstance(feats, dict) else {}
            print(f"  payment_instructions_keys={list(pi.keys())}")

            menu = decode_jsonb(r['m'])
            if isinstance(menu, dict):
                for cat, items in menu.items():
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if isinstance(item, dict) and 'empanad' in item.get('name','').lower():
                            print(f"  MENU: cat={cat} -> {item.get('name')} ${item.get('price')} active={item.get('active', True)}")

        print()
        print("=== restaurant_tables in rid 8,19,20 ===")
        rows = await conn.fetch("SELECT id, name, branch_id, number, active FROM restaurant_tables WHERE branch_id IS NULL OR branch_id IN (8,19,20) ORDER BY branch_id, number")
        for r in rows:
            print(f"  id={r['id']} name={r['name']} branch={r['branch_id']} num={r['number']} active={r['active']}")

    finally:
        await conn.close()

asyncio.run(main())
