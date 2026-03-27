import os
import io
import time
import base64
import json
import pypdf
from collections import defaultdict
from fastapi import APIRouter, Request, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from pathlib import Path
from anthropic import Anthropic
import httpx

from app.services.auth import login, logout, create_user, get_users, hash_password
from app.services import database as db
from app.routes.deps import require_auth, get_current_user

router = APIRouter()
STATIC = Path(__file__).parent.parent / "static"

# ── LOGIN RATE LIMITER (in-process, resets on restart) ────────────────
_login_attempts: dict = defaultdict(list)
_LOGIN_MAX    = 10   # max attempts
_LOGIN_WINDOW = 900  # 15 minutes in seconds

def _check_login_rate_limit(ip: str) -> None:
    now = time.time()
    attempts = _login_attempts[ip]
    _login_attempts[ip] = [t for t in attempts if now - t < _LOGIN_WINDOW]
    if len(_login_attempts[ip]) >= _LOGIN_MAX:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 15 minutes.")
    _login_attempts[ip].append(now)

async def geocode_address(address: str) -> tuple:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://geocode.maps.co/search", params={"q": address, "limit": 1}, headers={"User-Agent": "Mesio/1.0"})
            if r.status_code == 200 and r.json():
                return float(r.json()[0]["lat"]), float(r.json()[0]["lon"]), r.json()[0].get("display_name","")
    except Exception as e:
        pass
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://photon.komoot.io/api/", params={"q": address, "limit": 1, "lang": "en"})
            if r.status_code == 200 and r.json().get("features"):
                coords = r.json()["features"][0]["geometry"]["coordinates"]
                props = r.json()["features"][0].get("properties", {})
                display = ", ".join(filter(None, [props.get("name",""), props.get("city",""), props.get("country","")]))
                return float(coords[1]), float(coords[0]), display
    except Exception as e:
        pass
    return None, None, None

class LoginRequest(BaseModel): username: str; password: str
class CreateUserRequest(BaseModel): username: str; password: str; restaurant_id: int; admin_key: str
class CreateRestaurantRequest(BaseModel): admin_key: str; name: str; whatsapp_number: str; address: str; menu: str; features: dict = {}; wa_phone_id: str = ""; wa_access_token: str = ""
class SetSubscriptionRequest(BaseModel): admin_key: str; restaurant_id: int; status: str
class UpdateRestaurantRequest(BaseModel):
    admin_key: str; restaurant_id: int
    name: str = None; address: str = None; whatsapp_number: str = None
    wa_phone_id: str = None; wa_access_token: str = None
    features: dict = None; menu: str = None
class TeamInviteRequest(BaseModel):
    username: str
    password: str = ""
    pin: str = ""
    role: str = "mesero"
    phone: str = ""
    branch_id: int = None
class CreateBranchRequest(BaseModel): name: str; whatsapp_number: str = ""; address: str; menu: dict = {}

# ── SERVICE WORKER (must be served at root scope, not /static/) ───────
@router.get("/sw.js")
async def service_worker():
    content = (STATIC / "sw.js").read_text(encoding="utf-8")
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )

# ── PÁGINAS PÚBLICAS / AUTENTICADAS ──────────────────────────────────
@router.get("/login", response_class=HTMLResponse)
async def login_page(): return (STATIC / "login.html").read_text(encoding="utf-8")
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(): return (STATIC / "dashboard.html").read_text(encoding="utf-8")
@router.get("/demo", response_class=HTMLResponse)
async def demo_page(): return (STATIC / "dashboard-demo.html").read_text(encoding="utf-8")
@router.get("/landing", response_class=HTMLResponse)
async def landing_page(): return (STATIC / "landing.html").read_text(encoding="utf-8")
@router.get("/", response_class=HTMLResponse)
async def root_redirect(): return (STATIC / "landing.html").read_text(encoding="utf-8")
@router.get("/superadmin", response_class=HTMLResponse)
async def superadmin_page():
    p = STATIC / "superadmin.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>No disponible</h1>")
@router.get("/staff", response_class=HTMLResponse)
async def staff_portal_page():
    p = STATIC / "staff-portal.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Portal no disponible</h1>")

@router.get("/api/public/restaurant-info")
async def public_restaurant_info(id: int):
    """Return the restaurant name for a given restaurant ID (public, read-only)."""
    restaurant = await db.db_get_restaurant_by_id(id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    return {"name": restaurant.get("name", "")}

@router.get("/mesero", response_class=HTMLResponse)
async def mesero_page(): return (STATIC / "mesero.html").read_text(encoding="utf-8")
@router.get("/caja", response_class=HTMLResponse)
async def caja_page(): 
    p = STATIC / "caja.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Caja no disponible</h1>")
@router.get("/crm", response_class=HTMLResponse)
async def crm_page():
    return (STATIC / "crm.html").read_text(encoding="utf-8")  

@router.get("/demo-chat", response_class=HTMLResponse)
async def demo_chat_bot_page(): 
    p = STATIC / "demo-chat.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Falta el archivo demo-chat.html en la carpeta static</h1>", status_code=404)

@router.get("/catalog", response_class=HTMLResponse)
async def catalog_page():
    # Renderiza el frontend del carrito/catálogo móvil
    p = STATIC / "catalog.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Catálogo no disponible</h1>")

@router.get("/api/public/menu/{bot_number}")
async def get_public_menu(bot_number: str):
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rest = await conn.fetchrow("SELECT name, menu, features FROM restaurants WHERE whatsapp_number = $1", bot_number)
        if not rest:
            raise HTTPException(status_code=404, detail="Restaurante no encontrado")

        menu_data = rest["menu"]
        if isinstance(menu_data, str):
            try: menu_data = json.loads(menu_data)
            except: menu_data = {}

        features = rest["features"]
        if isinstance(features, str):
            try: features = json.loads(features)
            except: features = {}
        elif features is None:
            features = {}

        inv_rows = await conn.fetch(
            "SELECT dish_name, available FROM menu_availability"
        )
        availability = {r["dish_name"]: r["available"] for r in inv_rows}

        return {
            "restaurant_name": rest["name"],
            "menu": menu_data,
            "availability": availability,
            "bot_number": bot_number,
            "locale": features.get("locale", "en-US"),
            "currency": features.get("currency", "USD")
        }
        
@router.get("/privacidad", response_class=HTMLResponse)
async def privacidad_page(): 
    return (STATIC / "privacidad.html").read_text(encoding="utf-8")

@router.get("/terminos", response_class=HTMLResponse)
async def terminos_page(): 
    return (STATIC / "terminos.html").read_text(encoding="utf-8")

# ── BILLING PAGE (NUEVO) ──────────────────────────────────────────────
@router.get("/billing", response_class=HTMLResponse)
async def billing_page():
    p = STATIC / "billing.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Billing no disponible</h1>")

@router.get("/domiciliario", response_class=HTMLResponse)
async def domiciliario_page():
    p = STATIC / "domiciliario.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Página no encontrada</h1>", status_code=404)    

# ── SETTINGS ─────────────────────────────────────────────────────────
@router.get("/settings", response_class=HTMLResponse)
async def settings_page():
    p = STATIC / "settings.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Settings no disponible</h1>")

@router.get("/api/settings")
async def get_settings(request: Request):
    user = await get_current_user(request)
    branch_id = user.get("branch_id")
    if not branch_id:
        all_r = await db.db_get_all_restaurants()
        if not all_r:
            raise HTTPException(status_code=404, detail="Restaurante no encontrado")
        restaurant = all_r[0]
    else:
        restaurant = await db.db_get_restaurant_by_id(branch_id)

    raw_features = restaurant.get("features", {}) or {}
    if isinstance(raw_features, str):
        try:
            import json as _json
            features = _json.loads(raw_features)
        except Exception:
            features = {}
    else:
        features = raw_features

    return {
        "restaurant_id": restaurant["id"],
        "name": restaurant["name"],
        "whatsapp_number": restaurant.get("whatsapp_number", ""),
        "address": restaurant.get("address", ""),
        "features": features,
        "payment_methods": features.get("payment_methods", []),
        "google_maps_url": features.get("google_maps_url", ""),
        "bot_active": features.get("bot_active", True),
        "upsell_active": features.get("upsell_active", True),
        "domicilio_active": features.get("domicilio_active", True),
        "recoger_active": features.get("recoger_active", True),
        "delivery_fee": features.get("delivery_fee", 0),
        "min_order": features.get("min_order", 0),
        "timezone": features.get("timezone", "America/Bogota"),
        "currency": features.get("currency", "COP"),
        "locale": features.get("locale", "es-CO"),
    }

@router.post("/api/settings")
async def save_settings(request: Request):
    import json as _json
    user = await get_current_user(request)
    body = await request.json()
    branch_id = user.get("branch_id")
    if not branch_id:
        all_r = await db.db_get_all_restaurants()
        if not all_r:
            raise HTTPException(status_code=404, detail="No hay restaurante")
        branch_id = all_r[0]["id"]

    restaurant = await db.db_get_restaurant_by_id(branch_id)
    raw_features = restaurant.get("features", {}) or {}
    if isinstance(raw_features, str):
        try:
            current_features = _json.loads(raw_features)
        except Exception:
            current_features = {}
    else:
        current_features = dict(raw_features)

    updatable = [
        "payment_methods", "google_maps_url", "bot_active",
        "upsell_active", "domicilio_active", "recoger_active",
        "delivery_fee", "min_order", "delivery_message",
        "pickup_message", "welcome_message",
        "timezone", "currency", "locale"
    ]
    for key in updatable:
        if key in body:
            current_features[key] = body[key]

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE restaurants SET features = $1::jsonb WHERE id = $2",
            _json.dumps(current_features), branch_id
        )
    return {"success": True, "features": current_features}

# ── AUTH ──────────────────────────────────────────────────────────────
@router.post("/api/auth/login")
async def auth_login(request: Request, body: LoginRequest):
    ip = request.client.host if request.client else "unknown"
    _check_login_rate_limit(ip)
    result = await login(body.username, body.password)
    if not result["success"]:
        raise HTTPException(status_code=401, detail=result["error"])
    return result

@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    await logout(token)
    return {"success": True}

@router.get("/api/geocode")
async def geocode_endpoint(address: str):
    lat, lon, display = await geocode_address(address)
    if lat is None: raise HTTPException(status_code=404, detail="No se encontró la dirección.")
    return {"latitude": lat, "longitude": lon, "display_name": display, "maps_url": f"https://www.google.com/maps?q={lat},{lon}"}

# ── SUPER DASHBOARD (HQ) ─────────────────────────────────────────────
@router.get("/api/admin/stats")
async def admin_get_stats(admin_key: str):
    if admin_key != os.getenv("ADMIN_KEY"): 
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        total_rest  = await conn.fetchval("SELECT COUNT(*) FROM restaurants")
        active_rest = await conn.fetchval("SELECT COUNT(*) FROM restaurants WHERE subscription_status='active'")
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders")
        mrr = (active_rest or 0) * 99
        return {
            "total_restaurants": total_rest or 0,
            "active_restaurants": active_rest or 0,
            "total_orders": total_orders or 0,
            "mrr": mrr
        }

@router.get("/api/admin/restaurants")
async def admin_get_restaurants(admin_key: str):
    if admin_key != os.getenv("ADMIN_KEY"): raise HTTPException(status_code=403)
    return {"restaurants": await db.db_get_all_restaurants()}

@router.post("/api/admin/create-user")
async def admin_create_user(request: CreateUserRequest):
    if request.admin_key != os.getenv("ADMIN_KEY"): 
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    
    # 🛡️ Obtenemos el restaurante por ID para asegurar el vínculo
    rest = await db.db_get_restaurant_by_id(request.restaurant_id)
    if not rest: 
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    
    # Creamos el usuario owner amarrado al branch_id
    success = await db.db_create_user(
        username=request.username, 
        password_hash=hash_password(request.password), 
        restaurant_name=rest["name"],
        role="owner",
        branch_id=request.restaurant_id
    )
    
    if not success: 
        raise HTTPException(status_code=400, detail="El usuario ya existe")
    return {"success": True}

# 🗑️ NUEVO: Endpoint para borrar usuarios desde el SuperAdmin
@router.post("/api/admin/delete-user")
async def admin_delete_user(admin_key: str, username: str):
    if admin_key != os.getenv("ADMIN_KEY"): 
        raise HTTPException(status_code=403, detail="No autorizado")
    
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE username=$1", username.lower().strip())
    return {"success": True}

@router.get("/api/admin/users")
async def admin_list_users(admin_key: str = ""):
    if admin_key != os.getenv("ADMIN_KEY"): raise HTTPException(status_code=403, detail="No autorizado")
    return {"users": await get_users()}

@router.post("/api/admin/create-restaurant")
async def admin_create_restaurant(request: CreateRestaurantRequest):
    if request.admin_key != os.getenv("ADMIN_KEY"): raise HTTPException(status_code=403, detail="Clave incorrecta")
    try: menu_dict = json.loads(request.menu)
    except: raise HTTPException(status_code=400, detail="Menú no es JSON válido")
    lat, lon, _ = await geocode_address(request.address)
    
    # 1. Crear el restaurante (como antes)
    await db.db_create_restaurant(request.name, request.whatsapp_number, request.address, menu_dict, lat, lon, request.features)
    
    # 2. Si vienen credenciales de Meta, actualizamos el registro
    if request.wa_access_token:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE restaurants 
                   SET wa_phone_id = $1, wa_access_token = $2 
                   WHERE whatsapp_number = $3""",
                request.wa_phone_id, request.wa_access_token, request.whatsapp_number
            )
            
    return {"success": True}
    
@router.post("/api/admin/set-subscription")
async def admin_set_subscription(request: SetSubscriptionRequest):
    if request.admin_key != os.getenv("ADMIN_KEY"): raise HTTPException(status_code=403, detail="Clave incorrecta")
    await db.db_update_subscription(request.restaurant_id, request.status)
    return {"success": True}

# ── TEAM / BRANCHES ───────────────────────────────────────────────────
@router.get("/api/team/branches")
async def list_team_branches(request: Request):
    user = await get_current_user(request)
    role = user.get("role", "owner")
    
    # Obtenemos el ID del restaurante del usuario actual
    my_restaurant_id = user.get("branch_id")
    if not my_restaurant_id:
        all_r = await db.db_get_all_restaurants()
        my_restaurant_id = all_r[0]["id"] if all_r else None

    if "owner" in role:
        # 🛡️ FIX: Solo listamos los restaurantes cuyo PADRE sea mi ID. 
        # Esto hace que las sucursales aparezcan en 0 si no has creado hijos.
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM restaurants WHERE parent_restaurant_id = $1 ORDER BY name ASC", 
                my_restaurant_id
            )
            return {"branches": [db._serialize(dict(r)) for r in rows]}
            
    if "admin" in role and user.get("branch_id"):
        r = await db.db_get_restaurant_by_id(user["branch_id"])
        return {"branches": [r] if r else []}
        
    raise HTTPException(status_code=403, detail="No autorizado")


@router.post("/api/team/branches")
async def create_branch(request: Request, body: CreateBranchRequest):
    user = await get_current_user(request)
    if "owner" not in user.get("role", "owner"): raise HTTPException(status_code=403, detail="Solo el dueño puede crear sucursales")
    wa_number = body.whatsapp_number.strip()
    if not wa_number:
        all_r = await db.db_get_all_restaurants()
        wa_number = all_r[0]["whatsapp_number"] + f"_b{len(all_r)+1}" if all_r else ""
    lat, lon, display = await geocode_address(body.address)
    await db.db_create_restaurant(body.name, wa_number, body.address, body.menu, lat, lon)
    return {"success": True, "latitude": lat, "longitude": lon, "display_name": display}


_STAFF_ROLES = {"mesero", "cocina", "caja", "gerente", "domiciliario", "bar", "otro"}


@router.get("/api/team/users")
async def list_team_users(request: Request, branch_id: int = None):
    user = await get_current_user(request)
    role = user.get("role", "owner")
    all_users = await db.db_get_all_users()

    if "owner" in role:
        filtered = [u for u in all_users if u.get("branch_id") == branch_id] if branch_id else all_users
        effective_branch = branch_id
    elif "admin" in role and user.get("branch_id"):
        filtered = [u for u in all_users if u.get("branch_id") == user["branch_id"]]
        effective_branch = user["branch_id"]
    else:
        raise HTTPException(status_code=403, detail="No autorizado")

    # Mis Sucursales: solo admins y gerentes de la tabla users (CERO staff operativo)
    admin_roles = {"admin", "gerente", "owner"}
    filtered = [u for u in filtered if any(r.strip() in admin_roles for r in (u.get("role") or "").split(","))]
    for u in filtered:
        u["source"] = "user"

    return {"users": filtered}


@router.post("/api/team/invite")
async def team_invite(request: Request, body: TeamInviteRequest):
    from passlib.context import CryptContext as _CC
    _pin_ctx = _CC(schemes=["bcrypt"], deprecated="auto")

    creator = await get_current_user(request)
    role = creator.get("role", "owner")
    if "owner" not in role and "admin" not in role:
        raise HTTPException(status_code=403, detail="No autorizado")

    branch_id = body.branch_id if "owner" in role else creator.get("branch_id")
    if not branch_id:
        raise HTTPException(status_code=400, detail="Sucursal requerida")
    branch = await db.db_get_restaurant_by_id(branch_id)

    # Admin/Gerente → dashboard user account (password login)
    if body.role in ("admin", "gerente"):
        if not body.password:
            raise HTTPException(status_code=400, detail="Contraseña requerida para administrador o gerente")
        success = await db.db_create_user(
            body.username, hash_password(body.password), branch["name"],
            role=body.role, branch_id=branch_id, parent_user=creator["username"],
        )
        if not success:
            raise HTTPException(status_code=400, detail="Usuario ya existe")
    else:
        # Operational roles → staff table with PIN
        if not body.pin:
            raise HTTPException(status_code=400, detail="PIN requerido para este rol")
        if len(body.pin) < 4:
            raise HTTPException(status_code=400, detail="El PIN debe tener al menos 4 dígitos")
        roles = [r.strip() for r in body.role.split(",") if r.strip() in _STAFF_ROLES]
        if not roles:
            roles = ["mesero"]
        pin_hash = _pin_ctx.hash(body.pin)
        await db.db_create_staff(
            restaurant_id=branch_id,
            name=body.username,
            role=roles[0],
            pin_hash=pin_hash,
            phone=body.phone,
            roles=roles,
        )

    return {"success": True}


@router.delete("/api/team/users/{user_id}")
async def delete_user(user_id: str, request: Request):
    creator = await get_current_user(request)
    role = creator.get("role", "owner")
    if "owner" not in role and "admin" not in role:
        raise HTTPException(status_code=403, detail="No autorizado")

    # Try dashboard user first
    target = await db.db_get_user(user_id)
    if target:
        if "admin" in role and "owner" not in role and target.get("branch_id") != creator.get("branch_id"):
            raise HTTPException(status_code=403, detail="No autorizado")
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE username=$1", user_id.lower().strip())
        return {"success": True}

    # Try staff member by UUID
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM staff WHERE id=$1::uuid RETURNING id", user_id
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return {"success": True}


@router.delete("/api/team/branches/{branch_id}")
async def delete_branch(branch_id: int, request: Request):
    user = await get_current_user(request)
    if "owner" not in user.get("role", "owner"): 
        raise HTTPException(status_code=403, detail="Solo el dueño puede eliminar sucursales")
    
    # Obtenemos mi ID principal
    my_main_id = user.get("branch_id")
    if not my_main_id:
        all_r = await db.db_get_all_restaurants()
        my_main_id = all_r[0]["id"] if all_r else None

    # 🛡️ ESCUDO ANTI-SUICIDIO: Si intentas borrarte a ti mismo, el sistema te detiene.
    if branch_id == my_main_id:
        raise HTTPException(status_code=400, detail="No puedes eliminar la Casa Matriz desde aquí.")

    pool = await db.get_pool()
    async with pool.acquire() as conn: 
        # 🛡️ SEGUNDO FILTRO: Aseguramos que el restaurante a borrar sea REALMENTE una sucursal nuestra
        is_my_branch = await conn.fetchval(
            "SELECT id FROM restaurants WHERE id = $1 AND parent_restaurant_id = $2",
            branch_id, my_main_id
        )
        if not is_my_branch:
            raise HTTPException(status_code=404, detail="La sucursal no existe o no pertenece a tu cuenta.")

        # Eliminamos usuarios de la sucursal
        await conn.execute("DELETE FROM users WHERE branch_id=$1", branch_id)
        # Eliminamos la sucursal
        await conn.execute("DELETE FROM restaurants WHERE id=$1", branch_id)
        
    return {"success": True}

@router.post("/api/admin/parse-menu")
async def admin_parse_menu(admin_key: str, file: UploadFile = File(...)):
    if admin_key != os.getenv("ADMIN_KEY"): raise HTTPException(status_code=403, detail="Clave incorrecta")
    content  = await file.read()
    filename = file.filename.lower()
    client   = Anthropic()
    messages_content = []
    try:
        if filename.endswith(".pdf"):
            pdf_reader = pypdf.PdfReader(io.BytesIO(content))
            text = "".join(p.extract_text() + "\n" for p in pdf_reader.pages)
            messages_content.append({"type": "text", "text": f"Extrae el menú:\n{text}"})
        elif filename.endswith((".png", ".jpg", ".jpeg")):
            mt = "image/png" if filename.endswith(".png") else "image/jpeg"
            messages_content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(content).decode()}})
            messages_content.append({"type": "text", "text": "Extrae el menú de esta imagen."})
        else: raise HTTPException(status_code=400, detail="Sube PDF, PNG o JPG")
        response = client.messages.create(model="claude-3-haiku-20240307", max_tokens=4000, temperature=0, system='Extrae menús a JSON puro: {"Categoría": [{"name":"","price":0.0,"description":""}]}', messages=[{"role": "user", "content": messages_content}])
        return {"success": True, "json_menu": json.loads(response.content[0].text.replace("```json","").replace("```","").strip())}
    except Exception as e: raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@router.get("/api/admin/restaurant/{restaurant_id}")
async def admin_get_restaurant_detail(restaurant_id: int, admin_key: str):
    if admin_key != os.getenv("ADMIN_KEY"): raise HTTPException(status_code=403)
    rest = await db.db_get_restaurant_by_id(restaurant_id)
    if not rest: raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    wa = rest.get("whatsapp_number", "")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        orders_30d  = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(total),0) AS rev FROM orders WHERE bot_number=$1 AND created_at >= NOW()-INTERVAL '30 days'", wa)
        orders_today = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM orders WHERE bot_number=$1 AND created_at >= CURRENT_DATE", wa)
        table_30d   = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM table_orders WHERE created_at >= NOW()-INTERVAL '30 days' AND status NOT IN ('cancelado') AND (SELECT whatsapp_number FROM restaurants WHERE id=table_orders.branch_id OR id=$1 LIMIT 1)=$1", restaurant_id)
        convs       = await conn.fetchval("SELECT COUNT(*) FROM conversations WHERE bot_number=$1", wa)
        users_cnt   = await conn.fetchval("SELECT COUNT(*) FROM users WHERE branch_id=$1", restaurant_id)
        invoices_30d = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(total_cents),0) AS total FROM fiscal_invoices WHERE restaurant_id=$1 AND created_at >= NOW()-INTERVAL '30 days'",
            restaurant_id) if await conn.fetchval("SELECT to_regclass('fiscal_invoices')") else None
        invoices_all = await conn.fetchval(
            "SELECT COUNT(*) FROM fiscal_invoices WHERE restaurant_id=$1", restaurant_id
        ) if await conn.fetchval("SELECT to_regclass('fiscal_invoices')") else 0
        last_order  = await conn.fetchval(
            "SELECT MAX(created_at) FROM orders WHERE bot_number=$1", wa)
    return {
        "restaurant": rest,
        "stats": {
            "orders_30d":       int(orders_30d["cnt"])  if orders_30d else 0,
            "revenue_30d":      float(orders_30d["rev"]) if orders_30d else 0,
            "orders_today":     int(orders_today["cnt"]) if orders_today else 0,
            "table_orders_30d": int(table_30d["cnt"])   if table_30d else 0,
            "active_convs":     int(convs or 0),
            "users":            int(users_cnt or 0),
            "invoices_30d":     int(invoices_30d["cnt"]) if invoices_30d else 0,
            "invoices_all":     int(invoices_all or 0),
            "last_order":       last_order.isoformat() if last_order else None,
        }
    }

@router.post("/api/admin/update-restaurant")
async def admin_update_restaurant(request: UpdateRestaurantRequest):
    if request.admin_key != os.getenv("ADMIN_KEY"): raise HTTPException(status_code=403)
    rest = await db.db_get_restaurant_by_id(request.restaurant_id)
    if not rest: raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if request.name is not None:
            await conn.execute("UPDATE restaurants SET name=$1 WHERE id=$2", request.name, request.restaurant_id)
        if request.address is not None:
            lat, lon, _ = await geocode_address(request.address)
            await conn.execute("UPDATE restaurants SET address=$1, latitude=$2, longitude=$3 WHERE id=$4",
                               request.address, lat, lon, request.restaurant_id)
        if request.whatsapp_number is not None:
            await conn.execute("UPDATE restaurants SET whatsapp_number=$1 WHERE id=$2", request.whatsapp_number, request.restaurant_id)
        if request.wa_phone_id is not None:
            await conn.execute("UPDATE restaurants SET wa_phone_id=$1 WHERE id=$2", request.wa_phone_id, request.restaurant_id)
        if request.wa_access_token is not None:
            await conn.execute("UPDATE restaurants SET wa_access_token=$1 WHERE id=$2", request.wa_access_token, request.restaurant_id)
        if request.features is not None:
            raw = rest.get("features") or {}
            current = json.loads(raw) if isinstance(raw, str) else dict(raw)
            current.update(request.features)
            await conn.execute("UPDATE restaurants SET features=$1::jsonb WHERE id=$2",
                               json.dumps(current), request.restaurant_id)
        if request.menu is not None:
            try: menu_dict = json.loads(request.menu)
            except: raise HTTPException(status_code=400, detail="Menú no es JSON válido")
            await conn.execute("UPDATE restaurants SET menu=$1::jsonb WHERE id=$2",
                               json.dumps(menu_dict), request.restaurant_id)
    return {"success": True, "restaurant": await db.db_get_restaurant_by_id(request.restaurant_id)}

@router.get("/api/admin/billing-stats")
async def admin_billing_stats(admin_key: str):
    if admin_key != os.getenv("ADMIN_KEY"): raise HTTPException(status_code=403)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        table_exists = await conn.fetchval("SELECT to_regclass('fiscal_invoices')")
        if not table_exists:
            return {"stats": []}
        rows = await conn.fetch("""
            SELECT fi.restaurant_id, r.name AS restaurant_name,
                   COUNT(fi.id) AS total_invoices,
                   COUNT(fi.id) FILTER (WHERE fi.created_at >= NOW()-INTERVAL '30 days') AS invoices_30d,
                   COUNT(fi.id) FILTER (WHERE fi.dian_status='accepted') AS accepted,
                   COUNT(fi.id) FILTER (WHERE fi.dian_status='pending')  AS pending,
                   COALESCE(SUM(fi.total_cents) FILTER (WHERE fi.dian_status='accepted'),0) AS total_billed_cents,
                   MAX(fi.created_at) AS last_invoice_at
            FROM fiscal_invoices fi
            JOIN restaurants r ON r.id = fi.restaurant_id
            GROUP BY fi.restaurant_id, r.name
            ORDER BY total_invoices DESC
        """)
        return {"stats": [dict(r) for r in rows]}

@router.post("/api/admin/fix-branch-ids")
async def fix_branch_ids(request: Request):
    body = await request.json()
    if body.get("admin_key") != os.getenv("ADMIN_KEY"):
        raise HTTPException(status_code=403, detail="No autorizado")
    pool  = await db.get_pool()
    fixed = []
    async with pool.acquire() as conn:
        restaurants = await conn.fetch("SELECT id, name, whatsapp_number FROM restaurants")
        rest_map    = {r['name'].lower().strip(): dict(r) for r in restaurants}
        users       = await conn.fetch("SELECT username, restaurant_name, role FROM users WHERE branch_id IS NULL")
        for user in users:
            rname = user['restaurant_name'].lower().strip()
            if rname in rest_map:
                rest = rest_map[rname]
                await conn.execute("UPDATE users SET branch_id=$1, role='owner' WHERE username=$2", rest['id'], user['username'])
                fixed.append({"username": user['username'], "branch_id": rest['id']})
    return {"success": True, "fixed": fixed}

@router.post("/api/admin/fix-conversations")
async def fix_conversations_bot_number(request: Request):
    body = await request.json()
    if body.get("admin_key") != os.getenv("ADMIN_KEY"): raise HTTPException(status_code=403)
    pool = await db.get_pool()
    async with pool.acquire() as conn: await conn.execute("UPDATE conversations SET bot_number=$1 WHERE bot_number='' OR bot_number IS NULL", body.get("bot_number", ""))
    return {"success": True}
# ════════════════════════════════════════════════════════════════
# ── MÓDULOS DE DATOS PARA EL DASHBOARD (FRONTEND JAVASCRIPT) ──
# ════════════════════════════════════════════════════════════════
from datetime import datetime, timedelta

async def get_dashboard_filters(request: Request, period: str, custom_start: str = None, custom_end: str = None, tz_offset: int = 0):
    """Ayudante para filtrar por sucursal y rango exacto, calculando el 'Hoy' dinámicamente"""
    # 🛡️ FIX: Usamos la función centralizada que ya sabe distinguir entre Staff y Admin
    user = await get_current_user(request)
    
    # Si por alguna razón extrema no hay usuario, lanzamos el error antes de que falle el .get()
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado o sesión expirada")
    
    branch_id = user.get("branch_id")
    bot_number = None
    
    if branch_id:
        r = await db.db_get_restaurant_by_id(branch_id)
        if r: 
            bot_number = r.get("whatsapp_number")
    else:
        # Si es un Admin de la Matrix, intentamos obtener el restaurante principal
        # pero con un seguro por si la base de datos está vacía
        all_r = await db.db_get_all_restaurants()
        if all_r:
            # Si el usuario es Staff de la matrix, su restaurant_id está en su dict
            target_id = user.get("restaurant_id") or all_r[0].get("id")
            restaurant = await db.db_get_restaurant_by_id(target_id)
            if restaurant:
                branch_id = restaurant["id"]
                bot_number = restaurant.get("whatsapp_number")

    # 1. Calculamos la hora local EXACTA del usuario
    now_utc = datetime.utcnow()
    now_local = now_utc - timedelta(minutes=tz_offset)
    
    # 2. Definimos los límites del día
    end_local = now_local + timedelta(days=1)
    end_local = end_local.replace(hour=0, minute=0, second=0, microsecond=0)
    
    if period == "custom" and custom_start and custom_end:
        start_local = datetime.strptime(custom_start, "%Y-%m-%d")
        end_local = datetime.strptime(custom_end, "%Y-%m-%d") + timedelta(days=1)
    elif period == "week": 
        start_local = now_local - timedelta(days=7)
    elif period == "month": 
        start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "semester": 
        start_local = now_local - timedelta(days=180)
    elif period == "year": 
        start_local = now_local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else: # 'today'
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # 3. Devolvemos los límites a UTC para la DB
    start_date = start_local + timedelta(minutes=tz_offset)
    end_date = end_local + timedelta(minutes=tz_offset)
    
    return branch_id, bot_number, start_date, end_date
    
@router.get("/api/dashboard/orders")
async def get_dashboard_orders(request: Request, period: str = "today", custom_start: str = None, custom_end: str = None, tz_offset: int = 0):
    branch_id, bot_number, start_date, end_date = await get_dashboard_filters(request, period, custom_start, custom_end, tz_offset)
    
    pool = await db.get_pool()
    orders = []
    async with pool.acquire() as conn:
        try:
            q_wa = "SELECT * FROM orders WHERE created_at >= $1 AND created_at < $2"
            p_wa = [start_date, end_date]
            if bot_number:
                q_wa += " AND bot_number = $3"
                p_wa.append(bot_number)
            q_wa += " ORDER BY created_at DESC"
            
            rows_wa = await conn.fetch(q_wa, *p_wa)
            for r in rows_wa:
                orders.append({
                    "id": r["id"],
                    "items": r["items"],
                    "type": r.get("order_type", "domicilio"),
                    "status": r.get("status", "pendiente"),
                    "paid": r.get("payment_status") == "paid" or r.get("paid") == True,
                    "total": float(r["total"] or 0),
                    "time": r["created_at"].strftime("%H:%M"),
                    "created_at": r["created_at"].isoformat() + "Z",
                    "address": r.get("address", ""),
                    "payment_method": r.get("payment_method", ""),
                    "notes": r.get("notes", ""),
                    "phone": r.get("phone", ""),
                })
        except Exception as e:
            print(f"Error cargando orders: {e}", flush=True)

        try:
            if branch_id:
                q_mesa = """
                    SELECT o.* FROM table_orders o
                    LEFT JOIN restaurant_tables t ON o.table_id = t.id
                    WHERE o.created_at >= $1 AND o.created_at < $2
                    AND (t.branch_id = $3 OR t.branch_id IS NULL)
                    ORDER BY o.created_at DESC
                """
                p_mesa = [start_date, end_date, branch_id]
            else:
                q_mesa = """
                    SELECT * FROM table_orders
                    WHERE created_at >= $1 AND created_at < $2
                    ORDER BY created_at DESC
                """
                p_mesa = [start_date, end_date]

            rows_mesa = await conn.fetch(q_mesa, *p_mesa)
            
            mesa_groups = {}
            for r in rows_mesa:
                if not r["created_at"]:
                    continue  # skip rows with NULL timestamp
                base_id = r["base_order_id"] if r.get("base_order_id") else r["id"]
                if base_id not in mesa_groups:
                    mesa_groups[base_id] = {
                        "id": base_id, "items": [], "status": r.get("status") or "recibido",
                        "total": 0.0, "is_paid": False,
                        "time": r["created_at"].strftime("%H:%M"),
                        "created_at": r["created_at"].isoformat() + "Z"
                    }
                mesa_groups[base_id]["total"] += float(r["total"] or 0)

                try:
                    raw_items = r["items"]
                    if isinstance(raw_items, str):
                        parsed_items = json.loads(raw_items)
                    elif isinstance(raw_items, list):
                        parsed_items = raw_items
                    else:
                        parsed_items = []
                    if isinstance(parsed_items, list):
                        mesa_groups[base_id]["items"].extend(parsed_items)
                except Exception:
                    pass

                row_status = r.get("status") or ""
                if row_status in ["factura_generada", "factura_entregada", "cerrar_mesa"]:
                    mesa_groups[base_id]["is_paid"] = True
                    mesa_groups[base_id]["status"] = row_status

            for base_id, g in mesa_groups.items():
                orders.append({
                    "id": g["id"], "items": json.dumps(g["items"], default=str), "type": "mesa",
                    "status": g["status"], "paid": g["is_paid"], "total": g["total"],
                    "time": g["time"], "created_at": g["created_at"]
                })
        except Exception as e:
            print(f"Error cargando table_orders: {e}", flush=True)

    orders.sort(key=lambda x: x["created_at"], reverse=True)
    return {"orders": orders}

@router.post("/api/orders/{order_id}/status")
async def update_order_status(order_id: str, request: Request):
    await require_auth(request)
    body = await request.json()
    new_status = body.get("status", "")
    if not new_status:
        raise HTTPException(status_code=400, detail="status requerido")
    await db.db_update_order_status(order_id, new_status)
    return {"success": True}

@router.get("/api/table-sessions/closed")
async def get_closed_sessions(request: Request, hours: int = 24):
    hours = max(1, min(hours, 720))  # clamp: 1h – 30 days
    _, bot_number, _, _ = await get_dashboard_filters(request, "today")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        try:
            if bot_number:
                rows = await conn.fetch(
                    "SELECT * FROM table_sessions WHERE closed_at IS NOT NULL"
                    " AND closed_at >= NOW() - ($1 * INTERVAL '1 hour')"
                    " AND bot_number = $2 ORDER BY closed_at DESC",
                    hours, bot_number,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM table_sessions WHERE closed_at IS NOT NULL"
                    " AND closed_at >= NOW() - ($1 * INTERVAL '1 hour')"
                    " ORDER BY closed_at DESC",
                    hours,
                )
        except Exception as e:
            print(f"Warning - table_sessions query error: {e}", flush=True)
            rows = []
            
    sessions = []
    for r in rows:
        s = dict(r)
        if s.get("started_at"): s["started_at"] = s["started_at"].isoformat() + "Z"
        if s.get("closed_at"): s["closed_at"] = s["closed_at"].isoformat() + "Z"
        sessions.append(s)
        
    return {"sessions": sessions}

@router.get("/api/dashboard/reservations")
async def get_dashboard_reservations(request: Request, period: str = "today", custom_start: str = None, custom_end: str = None, tz_offset: int = 0):
    _, bot_number, start_date, end_date = await get_dashboard_filters(request, period, custom_start, custom_end, tz_offset)
    
    pool = await db.get_pool()
    reservations = []
    async with pool.acquire() as conn:
        try:
            query = "SELECT * FROM reservations WHERE created_at >= $1 AND created_at < $2"
            params = [start_date, end_date]
            if bot_number:
                query += " AND bot_number = $3"
                params.append(bot_number)
            query += " ORDER BY date ASC, time ASC"
            rows = await conn.fetch(query, *params)
            
            for r in rows:
                reservations.append({
                    "id": r["id"], "name": r["name"], "date": str(r["date"]),
                    "time": str(r["time"])[:5], "guests": r["guests"],
                    "phone": r["phone"], "notes": r["notes"]
                })
        except Exception as e: pass
            
    return {"reservations": reservations}

@router.get("/api/dashboard/conversations")
async def get_dashboard_conversations(request: Request):
    # FIX: Ahora desempaquetamos 4 valores en lugar de 3
    _, bot_number, _, _ = await get_dashboard_filters(request, "today")
    
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        query = "SELECT * FROM conversations"
        params = []
        if bot_number:
            query += " WHERE bot_number = $1"
            params.append(bot_number)
            
        query += " ORDER BY updated_at DESC"
        rows = await conn.fetch(query, *params)
        
    convs = []
    for r in rows:
        try:
            history = json.loads(r["history"]) if isinstance(r["history"], str) else r["history"]
            preview = history[-1]["content"] if history else "Conversación iniciada..."
            if isinstance(preview, dict): preview = "Multimedia/Sistema"
        except:
            history = []
            preview = "Conversación activa..."
            
        convs.append({
            "phone": r["phone"],
            "messages": len(history),
            "preview": preview[:60] + "..." if len(preview) > 60 else preview,
            "last_updated": r["updated_at"].isoformat() + "Z"
        })
    return {"conversations": convs}

@router.get("/api/dashboard/menu")
async def get_dashboard_menu(request: Request):
    username = await require_auth(request)
    user = await db.db_get_user(username)
    
    wa_number = ""
    if user and user.get("branch_id"):
        r = await db.db_get_restaurant_by_id(user["branch_id"])
        if r: wa_number = r.get("whatsapp_number", wa_number)
    else:
        all_r = await db.db_get_all_restaurants()
        if all_r: wa_number = all_r[0].get("whatsapp_number", wa_number)
        
    menu = await db.db_get_menu(wa_number) or {}
    return {"menu": menu}    

@router.get("/api/table-sessions/{session_id}/history")
async def get_session_history(request: Request, session_id: int):
    await require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        session = await conn.fetchrow("SELECT * FROM table_sessions WHERE id = $1", session_id)
        if not session: 
            raise HTTPException(404, "Sesión no encontrada")
        
        conv = await conn.fetchrow("SELECT history FROM conversations WHERE phone = $1", session["phone"])
        history = []
        if conv and conv["history"]:
            try: 
                history = json.loads(conv["history"]) if isinstance(conv["history"], str) else conv["history"]
            except: 
                pass
            
    s_dict = dict(session)
    if s_dict.get("started_at"): s_dict["started_at"] = s_dict["started_at"].isoformat()
    if s_dict.get("closed_at"): s_dict["closed_at"] = s_dict["closed_at"].isoformat()
    
    return {"session": s_dict, "history": history}

@router.post("/api/table-sessions/{session_id}/reopen")
async def reopen_session(request: Request, session_id: int):
    await require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE table_sessions SET closed_at = NULL, closed_by = NULL, closed_by_username = NULL WHERE id = $1", session_id)
    return {"success": True}

@router.post("/api/table-sessions/{session_id}/alert-waiter")
async def session_alert_waiter(request: Request, session_id: int):
    body = await request.json()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        session = await conn.fetchrow("SELECT * FROM table_sessions WHERE id = $1", session_id)
        if session:
            await conn.execute(
                "INSERT INTO waiter_alerts (table_id, table_name, message, status) VALUES ($1, $2, $3, 'active')",
                session["table_id"], session["table_name"], body.get("message", "Alerta de dashboard")
            )
    return {"success": True}