# Frontend: pages, JS patterns, visual catalog v2

> Moved verbatim from CLAUDE.md (2026-09-12) so it isn't loaded on every turn.

## Frontend — Served pages

**Shared chrome**: `app/static/css/tokens.css` + `shared.css` + `pages/<page>.css`. JS: `mesio-utils.js` → `pages/sidebar.js` → `pages/<page>.js`.

**Admin (served via `app/routes/dashboard.py`)**: `/dashboard`, `/orders`, `/reservations`, `/menu-admin`, `/menu-engineering`, `/nps`, `/loyalty`, `/customers-at-risk`, `/payroll`, `/locations`, `/floorplan`, `/team`, `/settings`, `/billing`, `/staff-hq` (alias `/staff-clock`).

**Operational (dark theme)**: `/cashier` (POS), `/kitchen` (KDS), `/bar` (KDS variant), `/waiter` (tablet grid), `/courier` (mobile).

**Public**: `/login.html`, `/menu.html` (public QR), `/demo`, `/dashboard-demo`, `/chat/{table_id}` (diner's web channel: `diner-chat.html` + `pages/diner-chat.js` + `diner-session.js`).

Sprint A-W redesign detail in [docs/history/sprints.md](docs/history/sprints.md). For "what got deferred" see "Calendar pending items" above.

## Visual Catalog v2 — Image Endpoints (`/api/menu/image/...`)

Let the admin editor upload and delete dish images directly on Cloudinary from the browser. The backend only signs — the bytes never pass through our server.

```
POST   /api/menu/image/sign
  Auth:      Admin/owner Bearer token (get_current_restaurant)
  Body:      {"folder_suffix": "menu"}  (optional, default "menu")
  Response:  {signature, timestamp, api_key, cloud_name, folder, public_id_prefix}
  Rate limit: 30 req/min per restaurant via state_store.rate_limit_check (Redis, cross-worker)
  503 if the CLOUDINARY_* env vars aren't configured
  429 if the rate limit is exceeded

DELETE /api/menu/image
  Auth:      Admin/owner Bearer token (get_current_restaurant)
  Body:      {"public_id": "mesio/r_{id}/menu/dish_abc"}
  Response:  {"success": true, "public_id": "..."}
  403 if public_id doesn't belong to the authenticated restaurant (cross-tenant check)
  200 whenever ownership is valid — idempotent (image already deleted → 200)
  Implementation: app/routes/settings_routes.py | image_host: app/services/image_host.py
```

Related feature flags:
- `bot_visual_menu` (opt-in, default false) — enables sending photos from the bot (Phase 4)
- `catalog_v2_enabled` (opt-out, default true) — global kill-switch for the visual catalog

## Frontend — Patterns and Conventions

### Design System (`tokens.css`)
Single source of truth for design tokens: `--brand: #1D9E75`, surfaces, text, semantic colors, spacing (8pt grid), radii, shadows, transitions. Includes a unified button system (`.m-btn`), modals, toasts, skeletons, connection badges.

### Shared Utilities (`mesio-utils.js`)
Loaded before page scripts. Provides:
- `_escHtml(s)` — XSS prevention
- `mesioFmt(n)` — currency formatting (COP zero-decimal)
- `mesioHeaders()` — auth + branch headers (replaces duplicated `_apiHeaders()`)
- `mesioLogout()` — centralized logout
- `mesioToast(msg, type, duration)` — accessible notifications
- `mesioConfirm(msg, opts)` — replaces `window.confirm`
- `mesioTrackFetch(ok)` — connection monitor
- `mesioInterval(fn, ms)` — visibility-aware setInterval
- `mesioDate(iso)` — locale-aware date formatting

### `_staffFetch(path, method='GET', body=null)`
Wrapper over `fetch` that:
- Prefixes `/api/staff` to the path.
- Uses `mesioHeaders()` (reads the token from `localStorage.rb_token` and the branch ID from the global selector).
- Throws `Error(detail || 'HTTP NNN')` if the response isn't 2xx.

### MesioComponent
Factory for components with reactive state. Pattern:
```javascript
const MyComponent = MesioComponent({
  state: { loading: true, data: [] },
  render(state, el) { ... },
  async onMount(self) { ... },
});
MyComponent.mount('#selector');
```

### `_staffFmt(n)` and currency
Universal formatter that reads `rb_restaurant` from localStorage to get `locale` and `currency`. Supports currencies without decimals (COP, CLP).

### Days of the week
`day_of_week`: 0=Monday, 1=Tuesday, ..., 6=Sunday. JS: `(d.getDay() + 6) % 7`.

## Visual Catalog v2 — Extended dish JSONB shape

### Full schema (catalog v2, backward-compatible)

Each dish in `restaurants.menu` is a JSON object inside a per-category list:
```json
{
  "name":            "Bandeja Paisa",
  "description":     "Beans, pork rind, ground beef, chorizo, corn arepa, avocado and rice",
  "price":           28000,
  "image_url":       "https://res.cloudinary.com/mesio/image/upload/c_fill,w_600,h_450/v1/mesio/r_42/dish_abc.webp",
  "image_public_id": "mesio/r_42/dish_abc",
  "tags":            ["popular_latam"],
  "badges":          ["chef_pick"],
  "allergens":       ["gluten"],
  "featured":        false,
  "sort_order":      3,
  "calories":        850,
  "prep_time_min":   15,
  "active":          true
}
```

### Fields
| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | str | — | Required |
| `description` | str | `""` | |
| `price` | Decimal/int | — | Required. NEVER float in calculations |
| `image_url` | str\|None | `null` | Full Cloudinary URL |
| `image_public_id` | str\|None | `null` | `mesio/r_{id}/...` — scoped to the restaurant |
| `tags` | list[str] | `[]` | Stable slugs: `vegan`, `gluten_free`, `spicy`, `popular` |
| `badges` | list[str] | `[]` | `chef_pick`, `new`, `popular` |
| `allergens` | list[str] | `[]` | `gluten`, `lacteos` (dairy), `nueces` (nuts), etc. |
| `featured` | bool | `false` | Appears in the hero carousel |
| `sort_order` | int | `999` | Order within the category (lower = first) |
| `calories` | int\|None | `null` | |
| `prep_time_min` | int\|None | `null` | |
| `active` | bool | `true` | `false` = hidden from the public catalog |

### Backward compatibility
Old-style dishes (`{name, description, price}`) keep working. `normalize_dish_shape(dish)` in `app/repositories/restaurant_repo.py` applies all defaults on read (`db_get_menu`, `db_get_public_menu_data`) and write (`db_update_menu`). No keys are ever lost downstream.

### Multi-tenant security
`validate_dish_image_ownership(dish, restaurant_id)` in `restaurant_repo.py` verifies that `image_public_id` starts with `mesio/r_{restaurant_id}/`. `db_update_menu` raises `ValueError` if there's an image from another restaurant. `image_host.delete_image(public_id, restaurant_id)` performs the same validation before calling Cloudinary.

### `app/services/image_host.py`
Cloudinary wrapper. Key functions:
- `sign_upload_params(restaurant_id, folder_suffix="menu")` → params for a direct browser→Cloudinary upload
- `delete_image(public_id, restaurant_id)` → deletes with ownership validation
- `build_transform_url(url, variant)` → variants `"thumb"` (300×300), `"card"` (600×450), `"hero"` (1200×900)
- `is_cloudinary_url(url)` → bool
