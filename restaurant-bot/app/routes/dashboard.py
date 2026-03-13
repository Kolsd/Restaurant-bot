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


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return (STATIC / "login.html").read_text()

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    return (STATIC / "dashboard.html").read_text()

@router.get("/demo", response_class=HTMLResponse)
async def demo_page():
    return (STATIC / "dashboard-demo.html").read_text()

@router.get("/", response_class=RedirectResponse)
async def root_redirect():
    return RedirectResponse(url="/demo")


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

@router.get("/api/admin/users")
async def admin_list_users(admin_key: str = ""):
    import os
    if admin_key != os.getenv("ADMIN_KEY", "restaurantbot2024"):
        raise HTTPException(status_code=403, detail="No autorizado")
    return {"users": await get_users()}
