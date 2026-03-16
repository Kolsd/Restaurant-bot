from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from pathlib import Path
from app.services.auth import login, logout, verify_token, create_user, get_users

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
    name: str
    whatsapp_number: str
    address: str
    menu: str
    admin_key: str

class SetSubscriptionRequest(BaseModel):
    restaurant_id: int
    status: str
    admin_key: str

@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return (STATIC / "login.html").read_text()

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    return (STATIC / "dashboard.html").read_text()

@router.get("/demo", response_class=HTMLResponse)
async def demo_page():
    return (STATIC / "dashboard-demo.html").read_text()

@router.get("/landing", response_class=HTMLResponse)
async def landing_page():
    return (STATIC / "landing.html").read_text()

@router.get("/", response_class=HTMLResponse)
async def root_redirect():
    return (STATIC / "landing.html").read_text()


@router.post("/api/auth/login")
async def auth_login(request: LoginRequest):
    result = await login(request.username, request.password)
    if not result["success"]:
        raise HTTPException(status_code=401, detail=result["error"])
    return result

@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    logout(token)
    return {"success": True}

@router.post("/api/admin/create-user")
async def admin_create_user(request: CreateUserRequest):
    import os
    if request.admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="Clave de administrador incorrecta")
    result = await create_user(request.username, request.password, request.restaurant_name)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result

@router.post("/api/admin/create-restaurant")
async def admin_create_restaurant(request: CreateRestaurantRequest):
    import os
    if request.admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="Clave de administrador incorrecta")
    from app.services.database import db_create_restaurant
    result = await db_create_restaurant(
        request.name,
        request.whatsapp_number,
        request.address,
        request.menu
    )
    if not result.get("success", False):
        raise HTTPException(status_code=400, detail=result.get("error", "No se pudo crear el restaurante"))
    return result

@router.post("/api/admin/set-subscription")
async def admin_set_subscription(request: SetSubscriptionRequest):
    import os
    from app.services.database import db_update_subscription
    if request.admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="No autorizado")
    try:
        await db_update_subscription(request.restaurant_id, request.status)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "restaurant_id": request.restaurant_id, "status": request.status}

@router.get("/api/admin/users")
async def admin_list_users(admin_key: str = ""):
    import os
    if admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="No autorizado")
    return {"users": await get_users()}
    @router.get("/superadmin")
    async def superadmin_page():
        from fastapi.responses import FileResponse
        import os
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "STATIC")
        file_path = os.path.join(static_dir, "superadmin.html")
        if not os.path.isfile(file_path):
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="superadmin.html no encontrado")
        return FileResponse(file_path, media_type="text/html")