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

from app.services.auth import login, logout, verify_token, create_user, get_users
from app.services import database as db

router = APIRouter()
STATIC = Path(__file__).parent.parent / "static"

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
    return (STATIC / "superadmin.html").read_text(encoding="utf-8")

@router.post("/api/auth/login")
async def auth_login(request: LoginRequest):
    result = await login(request.username, request.password)
    if not result["success"]: raise HTTPException(status_code=401, detail=result["error"])
    return result

@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    logout(token)
    return {"success": True}


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

@router.post("/api/admin/create-user")
async def admin_create_user(request: CreateUserRequest):
    if request.admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="Clave de administrador incorrecta")
    result = await create_user(request.username, request.password, request.restaurant_name)
    if not result["success"]: raise HTTPException(status_code=400, detail=result["error"])
    return result

@router.get("/api/admin/users")
async def admin_list_users(admin_key: str = ""):
    if admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="No autorizado")
    return {"users": await get_users()}


class TeamInviteRequest(BaseModel):
    username: str
    password: str
    role: str  # owner/admin/cook/waiter
    branch_id: int | None = None


class CreateBranchRequest(BaseModel):
    name: str
    whatsapp_number: str
    address: str
    menu: dict


@router.get("/api/team/branches")
async def list_team_branches(request: Request):
    """
    Lista sucursales visibles para el usuario actual.
    - owner: todas las sucursales (restaurants)
    - admin: solo su sucursal (branch_id)
    """
    user = await get_current_user(request)
    role = user.get("role", "owner")
    if role == "owner":
        restaurants = await db.db_get_all_restaurants()
        return {"branches": restaurants}
    branch_id = user.get("branch_id")
    if role == "admin" and branch_id:
        restaurant = await db.db_get_restaurant_by_id(branch_id)
        if restaurant:
            return {"branches": [restaurant]}
    raise HTTPException(status_code=403, detail="No autorizado")


@router.post("/api/team/branches")
async def create_branch(request: Request, body: CreateBranchRequest):
    """
    Crea una nueva sucursal (restaurant). Solo dueño.
    """
    user = await get_current_user(request)
    role = user.get("role", "owner")
    if role != "owner":
        raise HTTPException(status_code=403, detail="Solo el dueño puede crear sucursales")
    await db.db_create_restaurant(body.name, body.whatsapp_number, body.address, body.menu)
    return {"success": True}


@router.get("/api/team/users")
async def list_team_users(request: Request):
    """
    Lista usuarios del sistema. Owner ve todos, admin solo los de su sucursal.
    """
    user = await get_current_user(request)
    role = user.get("role", "owner")
    branch_id = user.get("branch_id")
    all_users = await db.db_get_all_users()
    if role == "owner":
        return {"users": all_users}
    if role == "admin" and branch_id:
        filtered = [u for u in all_users if u.get("branch_id") == branch_id]
        return {"users": filtered}
    raise HTTPException(status_code=403, detail="No autorizado")


@router.post("/api/team/invite")
async def team_invite(request: Request, body: TeamInviteRequest):
    """
    Crea un usuario dentro de la jerarquía:
    - owner: puede crear admins para cualquier sucursal (branch_id requerido)
    - admin: puede crear cooks/waiters para su propia sucursal
    """
    creator = await get_current_user(request)
    creator_role = creator.get("role", "owner")
    creator_username = creator["username"]
    target_role = body.role

    if creator_role == "owner":
        if target_role not in ("admin", "cook", "waiter"):
            raise HTTPException(status_code=400, detail="Rol inválido para invitación")
        if not body.branch_id:
            raise HTTPException(status_code=400, detail="branch_id requerido para crear usuarios")
        branch = await db.db_get_restaurant_by_id(body.branch_id)
        if not branch:
            raise HTTPException(status_code=404, detail="Sucursal no encontrada")
        restaurant_name = branch["name"]
        success = await db.db_create_user(
            body.username,
            hash_password(body.password),
            restaurant_name,
            role=target_role,
            branch_id=body.branch_id,
            parent_user=creator_username,
        )
    elif creator_role == "admin":
        if target_role not in ("cook", "waiter"):
            raise HTTPException(status_code=403, detail="Un administrador solo puede crear cocineros o meseros")
        branch_id = creator.get("branch_id")
        if not branch_id:
            raise HTTPException(status_code=400, detail="Administrador sin sucursal asignada")
        branch = await db.db_get_restaurant_by_id(branch_id)
        if not branch:
            raise HTTPException(status_code=404, detail="Sucursal no encontrada")
        restaurant_name = branch["name"]
        success = await db.db_create_user(
            body.username,
            hash_password(body.password),
            restaurant_name,
            role=target_role,
            branch_id=branch_id,
            parent_user=creator_username,
        )
    else:
        raise HTTPException(status_code=403, detail="No autorizado para invitar usuarios")

    if not success:
        raise HTTPException(status_code=400, detail="Usuario ya existe")
    return {"success": True}

@router.post("/api/admin/create-restaurant")
async def admin_create_restaurant(request: CreateRestaurantRequest):
    if request.admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    try:
        menu_dict = json.loads(request.menu)
    except Exception:
        raise HTTPException(status_code=400, detail="El menú no es un JSON válido")
    await db.db_create_restaurant(request.name, request.whatsapp_number, request.address, menu_dict)
    return {"success": True, "message": "Restaurante guardado"}

@router.post("/api/admin/set-subscription")
async def admin_set_subscription(request: SetSubscriptionRequest):
    if request.admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    await db.db_update_subscription(request.restaurant_id, request.status)
    return {"success": True, "message": f"Suscripción actualizada a {request.status}"}

@router.post("/api/admin/parse-menu")
async def admin_parse_menu(
    admin_key: str, 
    file: UploadFile = File(...)
):
    if admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    
    content = await file.read()
    filename = file.filename.lower()
    
    client = Anthropic()
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    extracted_text = ""
    messages_content = []

    # Instrucción estricta para la IA
    system_prompt = """Eres un extractor de datos experto. Tu única tarea es leer menús de restaurantes y convertirlos EXACTAMENTE en este formato JSON, sin markdown (```json), sin texto introductorio, solo el JSON puro:
    {
      "Categoria (ej. Entradas)": [
        {"name": "Nombre", "price": 10.50, "description": "Descripción"}
      ]
    }
    Convierte los precios a números decimales (ej. 15.00). Si no hay descripción, pon "".
    """

    try:
        if filename.endswith(".pdf"):
            # Leer PDF
            pdf_reader = pypdf.PdfReader(io.BytesIO(content))
            for page in pdf_reader.pages:
                extracted_text += page.extract_text() + "\n"
            
            messages_content.append({"type": "text", "text": f"Extrae el menú de este texto:\n{extracted_text}"})
            
        elif filename.endswith((".png", ".jpg", ".jpeg")):
            # Leer Imagen usando la Visión de Claude
            image_b64 = base64.b64encode(content).decode("utf-8")
            media_type = "image/png" if filename.endswith(".png") else "image/jpeg"
            
            messages_content.append({
                "type": "image", 
                "source": {"type": "base64", "media_type": media_type, "data": image_b64}
            })
            messages_content.append({"type": "text", "text": "Extrae el menú de esta imagen."})
        else:
            raise HTTPException(status_code=400, detail="Formato no soportado. Sube PDF, PNG o JPG.")

        # Llamar a Claude 3.5 Sonnet
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": messages_content}]
        )
        
        # Limpiar la respuesta para asegurar que sea JSON válido
        result_text = response.content[0].text
        result_text = result_text.replace("```json", "").replace("```", "").strip()
        
        return {"success": True, "json_menu": json.loads(result_text)}

    except Exception as e:
        print(f"Error parseando menú: {e}")
        raise HTTPException(status_code=500, detail=f"Error al procesar el archivo: {str(e)}")
