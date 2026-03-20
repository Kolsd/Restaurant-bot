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
class CreateRestaurantRequest(BaseModel): admin_key: str; name: str; whatsapp_number: str; address: str; menu: str; features: dict = {}
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

# ── BILLING PAGE (NUEVO) ──────────────────────────────────────────────
@router.get("/billing", response_class=HTMLResponse)
async def billing_page():
    p = STATIC / "billing.html"
    return p.read_text(encoding="utf-8") if p.exists() else HTMLResponse("<h1>Billing no disponible</h1>")

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
    if admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"): 
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
    if admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"): raise HTTPException(status_code=403)
    return {"restaurants": await db.db_get_all_restaurants()}

@router.post("/api/admin/create-user")
async def admin_create_user(request: CreateUserRequest):
    if request.admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"): raise HTTPException(status_code=403, detail="Clave incorrecta")
    result = await create_user(request.username, request.password, request.restaurant_name)
    if not result["success"]: raise HTTPException(status_code=400, detail=result["error"])
    return result

@router.get("/api/admin/users")
async def admin_list_users(admin_key: str = ""):
    if admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"): raise HTTPException(status_code=403, detail="No autorizado")
    return {"users": await get_users()}

@router.post("/api/admin/create-restaurant")
async def admin_create_restaurant(request: CreateRestaurantRequest):
    if request.admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"): raise HTTPException(status_code=403, detail="Clave incorrecta")
    try: menu_dict = json.loads(request.menu)
    except: raise HTTPException(status_code=400, detail="Menú no es JSON válido")
    lat, lon, _ = await geocode_address(request.address)
    await db.db_create_restaurant(request.name, request.whatsapp_number, request.address, menu_dict, lat, lon, request.features)
    return {"success": True}

@router.post("/api/admin/set-subscription")
async def admin_set_subscription(request: SetSubscriptionRequest):
    if request.admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"): raise HTTPException(status_code=403, detail="Clave incorrecta")
    await db.db_update_subscription(request.restaurant_id, request.status)
    return {"success": True}

# ── TEAM / BRANCHES ───────────────────────────────────────────────────
@router.get("/api/team/branches")
async def list_team_branches(request: Request):
    user = await get_current_user(request)
    role = user.get("role", "owner")
    if role == "owner": return {"branches": await db.db_get_all_restaurants()}
    if role == "admin" and user.get("branch_id"):
        r = await db.db_get_restaurant_by_id(user["branch_id"])
        return {"branches": [r] if r else []}
    raise HTTPException(status_code=403, detail="No autorizado")

@router.post("/api/team/branches")
async def create_branch(request: Request, body: CreateBranchRequest):
    user = await get_current_user(request)
    if user.get("role", "owner") != "owner": raise HTTPException(status_code=403, detail="Solo el dueño puede crear sucursales")
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
    all_users = await db.db_get_all_users()
    if user.get("role", "owner") == "owner":
        return {"users": [u for u in all_users if u.get("branch_id") == branch_id] if branch_id else all_users}
    if user.get("role") == "admin" and user.get("branch_id"):
        return {"users": [u for u in all_users if u.get("branch_id") == user["branch_id"]]}
    raise HTTPException(status_code=403, detail="No autorizado")

@router.post("/api/team/invite")
async def team_invite(request: Request, body: TeamInviteRequest):
    creator = await get_current_user(request)
    if creator.get("role", "owner") not in ["owner", "admin"]: raise HTTPException(status_code=403, detail="No autorizado")
    branch_id = body.branch_id if creator.get("role", "owner") == "owner" else creator.get("branch_id")
    if not branch_id: raise HTTPException(status_code=400, detail="Sucursal requerida")
    branch = await db.db_get_restaurant_by_id(branch_id)
    success = await db.db_create_user(body.username, hash_password(body.password), branch["name"], role=body.role, branch_id=branch_id, parent_user=creator["username"])
    if not success: raise HTTPException(status_code=400, detail="Usuario ya existe")
    return {"success": True}

@router.delete("/api/team/users/{username}")
async def delete_user(username: str, request: Request):
    creator = await get_current_user(request)
    target  = await db.db_get_user(username)
    if not target: raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if creator.get("role", "owner") == "admin" and target.get("branch_id") != creator.get("branch_id"):
        raise HTTPException(status_code=403, detail="No autorizado")
    elif creator.get("role", "owner") not in ["owner", "admin"]:
        raise HTTPException(status_code=403, detail="No autorizado")
    pool = await db.get_pool()
    async with pool.acquire() as conn: await conn.execute("DELETE FROM users WHERE username=$1", username.lower().strip())
    return {"success": True}

@router.delete("/api/team/branches/{branch_id}")
async def delete_branch(branch_id: int, request: Request):
    user = await get_current_user(request)
    if user.get("role", "owner") != "owner": raise HTTPException(status_code=403, detail="Solo el dueño puede eliminar sucursales")
    pool = await db.get_pool()
    async with pool.acquire() as conn: await conn.execute("DELETE FROM restaurants WHERE id=$1", branch_id)
    return {"success": True}

@router.post("/api/admin/parse-menu")
async def admin_parse_menu(admin_key: str, file: UploadFile = File(...)):
    if admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"): raise HTTPException(status_code=403, detail="Clave incorrecta")
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
    if body.get("admin_key") != os.getenv("ADMIN_KEY", "restaurantbot2024"):
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
    if body.get("admin_key") != os.getenv("ADMIN_KEY", "restaurantbot2024"): raise HTTPException(status_code=403)
    pool = await db.get_pool()
    async with pool.acquire() as conn: await conn.execute("UPDATE conversations SET bot_number=$1 WHERE bot_number='' OR bot_number IS NULL", body.get("bot_number", "15556293573"))
    return {"success": True}