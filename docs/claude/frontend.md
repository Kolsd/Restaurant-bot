# Frontend: páginas, patrones JS, catálogo visual v2

> Movido verbatim desde CLAUDE.md (2026-09-12) para no cargarlo en cada turno.

## Frontend — Pages servidas

**Chrome compartido**: `app/static/css/tokens.css` + `shared.css` + `pages/<page>.css`. JS: `mesio-utils.js` → `pages/sidebar.js` → `pages/<page>.js`.

**Admin (servidos via `app/routes/dashboard.py`)**: `/dashboard`, `/pedidos`, `/reservaciones`, `/menu-admin`, `/menu-engineering`, `/nps`, `/fidelizacion`, `/clientes-riesgo`, `/nomina`, `/sucursales`, `/floorplan`, `/equipo`, `/settings`, `/billing`, `/staff-hq` (alias `/staff-clock`).

**Operacionales (dark theme)**: `/caja` (POS), `/kitchen` (KDS), `/bar` (KDS variante), `/mesero` (tablet grid), `/domiciliario` (mobile).

**Públicas**: `/login.html`, `/menu.html` (QR público), `/demo`, `/dashboard-demo`, `/chat/{table_id}` (canal web del comensal: `diner-chat.html` + `pages/diner-chat.js` + `diner-session.js`).

Detalle de sprints A-W del rediseño en [docs/history/sprints.md](docs/history/sprints.md). Para "lo que quedó para después" ver "Pendientes de calendario" arriba.

## Catálogo Visual v2 — Endpoints de Imagen (`/api/menu/image/...`)

Permiten al editor admin subir y borrar imágenes de platos directamente en Cloudinary desde el browser. El backend solo firma — los bytes nunca pasan por nuestro servidor.

```
POST   /api/menu/image/sign
  Auth:      Bearer token de admin/owner (get_current_restaurant)
  Body:      {"folder_suffix": "menu"}  (opcional, default "menu")
  Response:  {signature, timestamp, api_key, cloud_name, folder, public_id_prefix}
  Rate limit: 30 req/min por restaurante via state_store.rate_limit_check (Redis cross-worker)
  503 si CLOUDINARY_* env vars no están configuradas
  429 si se supera el rate limit

DELETE /api/menu/image
  Auth:      Bearer token de admin/owner (get_current_restaurant)
  Body:      {"public_id": "mesio/r_{id}/menu/dish_abc"}
  Response:  {"success": true, "public_id": "..."}
  403 si public_id no pertenece al restaurante autenticado (cross-tenant check)
  200 siempre que ownership sea válida — idempotente (imagen ya borrada → 200)
  Implementación: app/routes/settings_routes.py | image_host: app/services/image_host.py
```

Feature flags relacionados:
- `bot_visual_menu` (opt-in, default false) — activa envío de fotos desde el bot (Fase 4)
- `catalog_v2_enabled` (opt-out, default true) — kill-switch global del catálogo visual

## Frontend — Patrones y Convenciones

### Design System (`tokens.css`)
Fuente única de verdad para tokens de diseño: `--brand: #1D9E75`, superficies, texto, semánticos, spacing (8pt grid), radii, sombras, transiciones. Incluye sistema unificado de botones (`.m-btn`), modals, toasts, skeletons, badges de conexión.

### Shared Utilities (`mesio-utils.js`)
Cargado antes de scripts de página. Provee:
- `_escHtml(s)` — prevención XSS
- `mesioFmt(n)` — formato moneda (COP zero-decimal)
- `mesioHeaders()` — auth + branch headers (reemplaza `_apiHeaders()` duplicados)
- `mesioLogout()` — logout centralizado
- `mesioToast(msg, type, duration)` — notificaciones accesibles
- `mesioConfirm(msg, opts)` — reemplaza `window.confirm`
- `mesioTrackFetch(ok)` — monitor de conexión
- `mesioInterval(fn, ms)` — setInterval visibility-aware
- `mesioDate(iso)` — formato fecha locale-aware

### `_staffFetch(path, method='GET', body=null)`
Wrapper sobre `fetch` que:
- Prefija `/api/staff` al path.
- Usa `mesioHeaders()` (lee token de `localStorage.rb_token` y branch ID del selector global).
- Lanza `Error(detail || 'HTTP NNN')` si la respuesta no es 2xx.

### MesioComponent
Factory para componentes con estado reactivo. Patrón:
```javascript
const MiComponent = MesioComponent({
  state: { loading: true, data: [] },
  render(state, el) { ... },
  async onMount(self) { ... },
});
MiComponent.mount('#selector');
```

### `_staffFmt(n)` y moneda
Formateador universal que lee `rb_restaurant` de localStorage para obtener `locale` y `currency`. Soporta monedas sin decimales (COP, CLP).

### Días de semana
`day_of_week`: 0=Lunes, 1=Martes, ..., 6=Domingo. JS: `(d.getDay() + 6) % 7`.

## Catálogo Visual v2 — Shape extendido del plato JSONB

### Schema completo (catálogo v2, backward-compatible)

Cada plato en `restaurants.menu` es un JSON object dentro de una lista por categoría:
```json
{
  "name":            "Bandeja Paisa",
  "description":     "Fríjoles, chicharrón, carne molida, chorizo, arepa, aguacate y arroz",
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

### Campos
| Campo | Tipo | Default | Notas |
|---|---|---|---|
| `name` | str | — | Requerido |
| `description` | str | `""` | |
| `price` | Decimal/int | — | Requerido. NUNCA float en cálculos |
| `image_url` | str\|None | `null` | URL completa Cloudinary |
| `image_public_id` | str\|None | `null` | `mesio/r_{id}/...` — scoped al restaurante |
| `tags` | list[str] | `[]` | Slugs estables: `vegan`, `gluten_free`, `spicy`, `popular` |
| `badges` | list[str] | `[]` | `chef_pick`, `new`, `popular` |
| `allergens` | list[str] | `[]` | `gluten`, `lacteos`, `nueces`, etc. |
| `featured` | bool | `false` | Aparece en hero carousel |
| `sort_order` | int | `999` | Orden dentro de la categoría (menor = primero) |
| `calories` | int\|None | `null` | |
| `prep_time_min` | int\|None | `null` | |
| `active` | bool | `true` | `false` = oculto en catálogo público |

### Backward compatibility
Los platos viejos (`{name, description, price}`) siguen funcionando. `normalize_dish_shape(dish)` en `app/repositories/restaurant_repo.py` aplica todos los defaults en lectura (`db_get_menu`, `db_get_public_menu_data`) y escritura (`db_update_menu`). Nunca se pierden keys en downstream.

### Seguridad multi-tenant
`validate_dish_image_ownership(dish, restaurant_id)` en `restaurant_repo.py` verifica que `image_public_id` empiece con `mesio/r_{restaurant_id}/`. `db_update_menu` lanza `ValueError` si hay una imagen de otro restaurante. `image_host.delete_image(public_id, restaurant_id)` hace la misma validación antes de llamar a Cloudinary.

### `app/services/image_host.py`
Wrapper Cloudinary. Funciones clave:
- `sign_upload_params(restaurant_id, folder_suffix="menu")` → params para upload directo browser→Cloudinary
- `delete_image(public_id, restaurant_id)` → borra con validación ownership
- `build_transform_url(url, variant)` → variantes `"thumb"` (300×300), `"card"` (600×450), `"hero"` (1200×900)
- `is_cloudinary_url(url)` → bool

