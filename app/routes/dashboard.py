import os
import io
import base64
import json
import hashlib
import pypdf
from fastapi import APIRouter, Request, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path
from anthropic import Anthropic

import httpx
from app.services.auth import login, logout, verify_token, create_user, get_users
from app.services import database as db

import hashlib
def hash_password(p: str) -> str:
    return hashlib.sha256(p.encode()).hexdigest()

async def geocode_address(address: str) -> tuple:
    """Geocodifica con multiples proveedores: Nominatim → Photon → None."""
    # Add Colombia context if not present
    search_query = address
    if "colombia" not in address.lower() and "bogot" not in address.lower() and        "medell" not in address.lower() and "cali" not in address.lower():
        search_query = address + ", Colombia"

    headers_ua = {"User-Agent": "Mesio/1.0 (restaurante bot colombia; contact@mesioai.com)"}

    # 1. Nominatim con Colombia
    for query in [search_query, address]:
        for cc in ["co", None]:
            try:
                params = {"q": query, "format": "json", "limit": 1, "addressdetails": 1}
                if cc:
                    params["countrycodes"] = cc
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        "https://nominatim.openstreetmap.org/search",
                        params=params, headers=headers_ua
                    )
                    if r.status_code == 200:
                        data = r.json()
                        if data:
                            return float(data[0]["lat"]), float(data[0]["lon"]), data[0].get("display_name", "")
            except Exception as e:
                print(f"Nominatim error ({query}): {e}")

    # 2. Photon (Komoot) como fallback
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://photon.komoot.io/api/",
                params={"q": search_query, "limit": 1, "lang": "es"},
                headers=headers_ua
            )
            if r.status_code == 200:
                data = r.json()
                features = data.get("features", [])
                if features:
                    coords = features[0]["geometry"]["coordinates"]
                    props = features[0].get("properties", {})
                    display = f"{props.get('name','')}, {props.get('city','')}, {props.get('country','')}".strip(", ")
                    return float(coords[1]), float(coords[0]), display
    except Exception as e:
        print(f"Photon error: {e}")

    return None, None, None

router = APIRouter()
STATIC = Path(__file__).parent.parent / "static"


def hash_password(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()


def require_auth(request: Request) -> str:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="No autorizado")
    return username


async def get_current_user(request: Request) -> dict:
    username = require_auth(request)
    user = await db.db_get_user(username)
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")
    return user


class LoginRequest(BaseModel):
    username: str
    password: str

class CreateUserRequest(BaseModel):
    username: str
    password: str
    restaurant_name: str
    admin_key: str

class CreateRestaurantRequest(BaseModel):
    admin_key: str
    name: str
    whatsapp_number: str
    address: str
    menu: str

class SetSubscriptionRequest(BaseModel):
    admin_key: str
    restaurant_id: int
    status: str

class TeamInviteRequest(BaseModel):
    username: str
    password: str
    role: str
    branch_id: int = None

class CreateBranchRequest(BaseModel):
    name: str
    whatsapp_number: str = ""
    address: str
    menu: dict = {}


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


@router.post("/api/auth/login")
async def auth_login(request: LoginRequest):
    result = await login(request.username, request.password)
    if not result["success"]: raise HTTPException(status_code=401, detail=result["error"])
    return result

@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    logout(token); return {"success": True}


@router.post("/api/admin/create-user")
async def admin_create_user(request: CreateUserRequest):
    if request.admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    result = await create_user(request.username, request.password, request.restaurant_name)
    if not result["success"]: raise HTTPException(status_code=400, detail=result["error"])
    return result

@router.get("/api/admin/users")
async def admin_list_users(admin_key: str = ""):
    if admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="No autorizado")
    return {"users": await get_users()}

@router.post("/api/admin/create-restaurant")
async def admin_create_restaurant(request: CreateRestaurantRequest):
    if request.admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    try: menu_dict = json.loads(request.menu)
    except Exception: raise HTTPException(status_code=400, detail="Menu no es JSON valido")
    await db.db_create_restaurant(request.name, request.whatsapp_number, request.address, menu_dict)
    return {"success": True}

@router.post("/api/admin/set-subscription")
async def admin_set_subscription(request: SetSubscriptionRequest):
    if request.admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    await db.db_update_subscription(request.restaurant_id, request.status)
    return {"success": True}


@router.get("/api/team/branches")
async def list_team_branches(request: Request):
    user = await get_current_user(request)
    role = user.get("role", "owner")
    if role == "owner":
        return {"branches": await db.db_get_all_restaurants()}
    branch_id = user.get("branch_id")
    if role == "admin" and branch_id:
        r = await db.db_get_restaurant_by_id(branch_id)
        return {"branches": [r] if r else []}
    raise HTTPException(status_code=403, detail="No autorizado")

@router.post("/api/team/branches")
async def create_branch(request: Request, body: CreateBranchRequest):
    user = await get_current_user(request)
    if user.get("role", "owner") != "owner":
        raise HTTPException(status_code=403, detail="Solo el dueno puede crear sucursales")
    await db.db_create_restaurant(body.name, body.whatsapp_number, body.address, body.menu)
    return {"success": True}

@router.get("/api/team/users")
async def list_team_users(request: Request, branch_id: int = None):
    user = await get_current_user(request)
    role = user.get("role", "owner")
    user_branch = user.get("branch_id")
    if role == "owner":
        return {"users": await db.db_get_all_users_with_roles(branch_id=branch_id)}
    if role == "admin" and user_branch:
        return {"users": await db.db_get_all_users_with_roles(branch_id=user_branch)}
    raise HTTPException(status_code=403, detail="No autorizado")

@router.post("/api/team/invite")
async def team_invite(request: Request, body: TeamInviteRequest):
    creator = await get_current_user(request)
    creator_role = creator.get("role", "owner")
    creator_username = creator["username"]
    target_role = body.role

    if creator_role == "owner":
        if target_role not in ("admin", "cook", "waiter"):
            raise HTTPException(status_code=400, detail="Rol invalido")
        if not body.branch_id:
            raise HTTPException(status_code=400, detail="branch_id requerido")
        branch = await db.db_get_restaurant_by_id(body.branch_id)
        if not branch: raise HTTPException(status_code=404, detail="Sucursal no encontrada")
        success = await db.db_create_user(
            body.username, hash_password(body.password), branch["name"],
            role=target_role, branch_id=body.branch_id, parent_user=creator_username)
    elif creator_role == "admin":
        if target_role not in ("cook", "waiter"):
            raise HTTPException(status_code=403, detail="Admin solo puede crear cocineros o meseros")
        branch_id = creator.get("branch_id")
        if not branch_id: raise HTTPException(status_code=400, detail="Admin sin sucursal")
        branch = await db.db_get_restaurant_by_id(branch_id)
        if not branch: raise HTTPException(status_code=404, detail="Sucursal no encontrada")
        success = await db.db_create_user(
            body.username, hash_password(body.password), branch["name"],
            role=target_role, branch_id=branch_id, parent_user=creator_username)
    else:
        raise HTTPException(status_code=403, detail="No autorizado")

    if not success: raise HTTPException(status_code=400, detail="Usuario ya existe")
    return {"success": True}


@router.post("/api/admin/parse-menu")
async def admin_parse_menu(admin_key: str, file: UploadFile = File(...)):
    if admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    content = await file.read()
    filename = file.filename.lower()
    client = Anthropic()
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    messages_content = []
    system_prompt = 'Extrae menus a JSON puro: {"Categoria": [{"name":"","price":0.0,"description":""}]}'
    try:
        if filename.endswith(".pdf"):
            pdf_reader = pypdf.PdfReader(io.BytesIO(content))
            text = "".join(p.extract_text() + "\n" for p in pdf_reader.pages)
            messages_content.append({"type": "text", "text": f"Extrae el menu:\n{text}"})
        elif filename.endswith((".png", ".jpg", ".jpeg")):
            mt = "image/png" if filename.endswith(".png") else "image/jpeg"
            messages_content.append({"type": "image", "source": {
                "type": "base64", "media_type": mt, "data": base64.b64encode(content).decode()}})
            messages_content.append({"type": "text", "text": "Extrae el menu de esta imagen."})
        else:
            raise HTTPException(status_code=400, detail="Sube PDF, PNG o JPG")
        response = client.messages.create(model=model, max_tokens=4000, temperature=0,
            system=system_prompt, messages=[{"role": "user", "content": messages_content}])
        result_text = response.content[0].text.replace("```json","").replace("```","").strip()
        return {"success": True, "json_menu": json.loads(result_text)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@router.post("/api/admin/fix-branch-ids")
async def fix_branch_ids(request: Request):
    """Endpoint temporal para asignar branch_id a usuarios existentes."""
    body = await request.json()
    if body.get("admin_key") != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="No autorizado")
    
    pool = await db.get_pool()
    fixed = []
    async with pool.acquire() as conn:
        # Get all restaurants
        restaurants = await conn.fetch("SELECT id, name, whatsapp_number FROM restaurants")
        rest_map = {r['name'].lower().strip(): dict(r) for r in restaurants}
        
        # Get all users without branch_id
        users = await conn.fetch("SELECT username, restaurant_name, role FROM users WHERE branch_id IS NULL")
        
        for user in users:
            rname = user['restaurant_name'].lower().strip()
            if rname in rest_map:
                rest = rest_map[rname]
                await conn.execute(
                    "UPDATE users SET branch_id=$1, role='owner' WHERE username=$2",
                    rest['id'], user['username']
                )
                fixed.append({"username": user['username'], "branch_id": rest['id'], "restaurant": rest['name']})
    
    return {"success": True, "fixed": fixed}

@router.post("/api/admin/fix-conversations")
async def fix_conversations_bot_number(request: Request):
    """Asigna bot_number a conversaciones que tienen bot_number vacio."""
    body = await request.json()
    if body.get("admin_key") != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="No autorizado")
    bot_number = body.get("bot_number", "15556293573")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE conversations SET bot_number=$1 WHERE bot_number='' OR bot_number IS NULL",
            bot_number
        )
    return {"success": True, "result": str(result)}
