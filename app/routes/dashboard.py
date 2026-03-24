import os
import io
import base64
import json
import pypdf
from fastapi import APIRouter, Request, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path
from anthropic import Anthropic
import httpx

from app.services.auth import login, logout, verify_token, create_user, get_users, hash_password
from app.services import database as db

router = APIRouter()
STATIC = Path(__file__).parent.parent / "static"

async def geocode_address(address: str) -> tuple:
    search_query = address
    if not any(x in address.lower() for x in ["colombia","bogot","medell","cali","barranquilla","cartagena"]):
        search_query = address + ", Colombia"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://geocode.maps.co/search", params={"q": search_query, "limit": 1}, headers={"User-Agent": "Mesio/1.0"})
            if r.status_code == 200 and r.json():
                return float(r.json()[0]["lat"]), float(r.json()[0]["lon"]), r.json()[0].get("display_name","")
    except Exception as e:
        pass
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://photon.komoot.io/api/", params={"q": search_query, "limit": 1, "lang": "es"}, headers={"User-Agent": "Mesio/1.0"})
            if r.status_code == 200 and r.json().get("features"):
                coords = r.json()["features"][0]["geometry"]["coordinates"]
                props = r.json()["features"][0].get("properties", {})
                display = ", ".join(filter(None, [props.get("name",""), props.get("city",""), props.get("country","")]))
                return float(coords[1]), float(coords[0]), display
    except Exception as e:
        pass
    return None, None, None

async def require_auth(request: Request) -> str:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="No autorizado")
    return username

async def get_current_user(request: Request) -> dict:
    username = await require_auth(request)
    user = await db.db_get_user(username)
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")
    return user

class LoginRequest(BaseModel): username: str; password: str
class CreateUserRequest(BaseModel): username: str; password: str; restaurant_name: str; admin_key: str
class CreateRestaurantRequest(BaseModel): admin_key: str; name: str; whatsapp_number: str; address: str; menu: str; features: dict = {}; wa_phone_id: str = ""; wa_access_token: str = ""
class SetSubscriptionRequest(BaseModel): admin_key: str; restaurant_id: int; status: str
class TeamInviteRequest(BaseModel): username: str; password: str; role: str; branch_id: int = None
class CreateBranchRequest(BaseModel): name: str; whatsapp_number: str = ""; address: str; menu: dict = {}

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
    # Devuelve el menú y nombre del restaurante de forma pública
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rest = await conn.fetchrow(
            "SELECT name, menu FROM restaurants WHERE whatsapp_number = $1", 
            bot_number
        )
        if not rest:
            raise HTTPException(status_code=404, detail="Restaurante no encontrado")
        
        # Validar si el menú es string o dict
        menu_data = rest["menu"]
        if isinstance(menu_data, str):
            try: menu_data = json.loads(menu_data)
            except: menu_data = {}
            
        return {
            "restaurant_name": rest["name"],
            "menu": menu_data,
            "bot_number": bot_number
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
        "pickup_message", "welcome_message"
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
async def auth_login(request: LoginRequest):
    result = await login(request.username, request.password)
    if not result["success"]: raise HTTPException(status_code=401, detail=result["error"])
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
    if request.admin_key != os.getenv("ADMIN_KEY"): raise HTTPException(status_code=403, detail="Clave incorrecta")
    result = await create_user(request.username, request.password, request.restaurant_name)
    if not result["success"]: raise HTTPException(status_code=400, detail=result["error"])
    return result

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
    if "owner" in role: return {"branches": await db.db_get_all_restaurants()}
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
        wa_number = all_r[0]["whatsapp_number"] + f"_b{len(all_r)+1}" if all_r else "15556293573"
    lat, lon, display = await geocode_address(body.address)
    await db.db_create_restaurant(body.name, wa_number, body.address, body.menu, lat, lon)
    return {"success": True, "latitude": lat, "longitude": lon, "display_name": display}


@router.get("/api/team/users")
async def list_team_users(request: Request, branch_id: int = None):
    user = await get_current_user(request)
    role = user.get("role", "owner")
    all_users = await db.db_get_all_users()
    if "owner" in role:
        return {"users": [u for u in all_users if u.get("branch_id") == branch_id] if branch_id else all_users}
    if "admin" in role and user.get("branch_id"):
        return {"users": [u for u in all_users if u.get("branch_id") == user["branch_id"]]}
    raise HTTPException(status_code=403, detail="No autorizado")


@router.post("/api/team/invite")
async def team_invite(request: Request, body: TeamInviteRequest):
    creator = await get_current_user(request)
    role = creator.get("role", "owner")
    if "owner" not in role and "admin" not in role: raise HTTPException(status_code=403, detail="No autorizado")
    
    branch_id = body.branch_id if "owner" in role else creator.get("branch_id")
    if not branch_id: raise HTTPException(status_code=400, detail="Sucursal requerida")
    branch = await db.db_get_restaurant_by_id(branch_id)
    
    success = await db.db_create_user(body.username, hash_password(body.password), branch["name"], role=body.role, branch_id=branch_id, parent_user=creator["username"])
    if not success: raise HTTPException(status_code=400, detail="Usuario ya existe")
    return {"success": True}


@router.delete("/api/team/users/{username}")
async def delete_user(username: str, request: Request):
    creator = await get_current_user(request)
    role = creator.get("role", "owner")
    target = await db.db_get_user(username)
    if not target: raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    if "admin" in role and "owner" not in role and target.get("branch_id") != creator.get("branch_id"):
        raise HTTPException(status_code=403, detail="No autorizado")
    elif "owner" not in role and "admin" not in role:
        raise HTTPException(status_code=403, detail="No autorizado")
        
    pool = await db.get_pool()
    async with pool.acquire() as conn: await conn.execute("DELETE FROM users WHERE username=$1", username.lower().strip())
    return {"success": True}


@router.delete("/api/team/branches/{branch_id}")
async def delete_branch(branch_id: int, request: Request):
    user = await get_current_user(request)
    if "owner" not in user.get("role", "owner"): raise HTTPException(status_code=403, detail="Solo el dueño puede eliminar sucursales")
    pool = await db.get_pool()
    async with pool.acquire() as conn: 
        # FIX: Eliminamos primero a todos los usuarios que pertenecen a esta sucursal
        await conn.execute("DELETE FROM users WHERE branch_id=$1", branch_id)
        # Luego eliminamos la sucursal
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
    async with pool.acquire() as conn: await conn.execute("UPDATE conversations SET bot_number=$1 WHERE bot_number='' OR bot_number IS NULL", body.get("bot_number", "15556293573"))
    return {"success": True}
# ════════════════════════════════════════════════════════════════
# ── MÓDULOS DE DATOS PARA EL DASHBOARD (FRONTEND JAVASCRIPT) ──
# ════════════════════════════════════════════════════════════════
from datetime import datetime, timedelta

async def get_dashboard_filters(request: Request, period: str, custom_start: str = None, custom_end: str = None, tz_offset: int = 0):
    """Ayudante para filtrar por sucursal y rango exacto, calculando el 'Hoy' dinámicamente según la zona horaria del cliente"""
    username = await require_auth(request)
    user = await db.db_get_user(username)
    
    branch_id = user.get("branch_id")
    bot_number = None
    if branch_id:
        r = await db.db_get_restaurant_by_id(branch_id)
        if r: bot_number = r.get("whatsapp_number")
    
    # 1. Calculamos la hora local EXACTA del usuario que está viendo la pantalla
    now_utc = datetime.utcnow()
    # En JS, getTimezoneOffset() devuelve minutos positivos para zonas al Oeste (ej. 300 para UTC-5)
    now_local = now_utc - timedelta(minutes=tz_offset)
    
    # 2. Definimos los límites del día en SU hora local
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
    
    # 3. Devolvemos los límites nuevamente a UTC para que la base de datos filtre correctamente
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
                    "total": float(r["total"]),
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
            q_mesa = """
                SELECT o.* FROM table_orders o
                LEFT JOIN restaurant_tables t ON o.table_id = t.id
                WHERE o.created_at >= $1 AND o.created_at < $2
            """
            p_mesa = [start_date, end_date]
            if branch_id:
                q_mesa += " AND t.branch_id = $3"
                p_mesa.append(branch_id)
            
            rows_mesa = await conn.fetch(q_mesa, *p_mesa)
            
            mesa_groups = {}
            for r in rows_mesa:
                base_id = r["base_order_id"] if r.get("base_order_id") else r["id"]
                if base_id not in mesa_groups:
                    mesa_groups[base_id] = {
                        "id": base_id, "items": [], "status": r.get("status", "recibido"),
                        "total": 0.0, "is_paid": False,
                        "time": r["created_at"].strftime("%H:%M"),
                        "created_at": r["created_at"].isoformat() + "Z"
                    }
                mesa_groups[base_id]["total"] += float(r["total"])
                
                try:
                    parsed_items = json.loads(r["items"]) if isinstance(r["items"], str) else r["items"]
                    if isinstance(parsed_items, list): mesa_groups[base_id]["items"].extend(parsed_items)
                except: pass
                
                if r["status"] in ["factura_generada", "factura_entregada", "cerrar_mesa"]:
                    mesa_groups[base_id]["is_paid"] = True
                    mesa_groups[base_id]["status"] = r["status"]

            for base_id, g in mesa_groups.items():
                orders.append({
                    "id": g["id"], "items": json.dumps(g["items"]), "type": "mesa",
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
    # FIX: Ahora desempaquetamos 4 valores en lugar de 3
    _, bot_number, _, _ = await get_dashboard_filters(request, "today")
    
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        try:
            query = f"SELECT * FROM table_sessions WHERE closed_at IS NOT NULL AND closed_at >= NOW() - INTERVAL '{hours} hours'"
            params = []
            if bot_number:
                query += " AND bot_number = $1"
                params.append(bot_number)
            query += " ORDER BY closed_at DESC"
            rows = await conn.fetch(query, *params)
        except Exception as e:
            print(f"Aviso - Error en table_sessions (intentando modo seguro): {e}")
            query = f"SELECT * FROM table_sessions WHERE closed_at IS NOT NULL AND closed_at >= NOW() - INTERVAL '{hours} hours' ORDER BY closed_at DESC"
            rows = await conn.fetch(query)
            
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
    
    wa_number = "15556293573" # Fallback
    if user and user.get("branch_id"):
        r = await db.db_get_restaurant_by_id(user["branch_id"])
        if r: wa_number = r.get("whatsapp_number", wa_number)
    else:
        all_r = await db.db_get_all_restaurants()
        if all_r: wa_number = all_r[0].get("whatsapp_number", wa_number)
        
    menu = await db.db_get_menu(wa_number) or {}
    return {"menu": menu}    

# ── ENDPOINTS DE SESIONES (PARA PESTAÑA AUDITORÍA) ──

router.get("/api/table-sessions/closed")
async def get_closed_sessions(request: Request, hours: int = 24):
    _, bot_number, _ = await get_dashboard_filters(request, "today")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        try:
            query = f"SELECT * FROM table_sessions WHERE closed_at IS NOT NULL AND closed_at >= NOW() - INTERVAL '{hours} hours'"
            params = []
            if bot_number:
                query += " AND bot_number = $1"
                params.append(bot_number)
            query += " ORDER BY closed_at DESC"
            rows = await conn.fetch(query, *params)
        except Exception as e:
            print(f"Aviso - Error en table_sessions (intentando modo seguro): {e}")
            query = f"SELECT * FROM table_sessions WHERE closed_at IS NOT NULL AND closed_at >= NOW() - INTERVAL '{hours} hours' ORDER BY closed_at DESC"
            rows = await conn.fetch(query)
            
    sessions = []
    for r in rows:
        s = dict(r)
        if s.get("started_at"): s["started_at"] = s["started_at"].isoformat() + "Z" # <--- Z AÑADIDA
        if s.get("closed_at"): s["closed_at"] = s["closed_at"].isoformat() + "Z"    # <--- Z AÑADIDA
        sessions.append(s)
        
    return {"sessions": sessions}

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