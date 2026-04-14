"""
app/routes/internal/admin.py

Superadmin CRUD endpoints for Mesio internal use only.
These are NOT restaurant-facing features — they are tools for the Mesio team.

Endpoints (all under /api/internal/admin, all require verify_superadmin):
  POST /login            → Exchange ADMIN_KEY for session token
  POST /logout           → Invalidate superadmin session
  GET  /stats            → Platform aggregate stats
  GET  /restaurants      → All restaurants list
  POST /create-user      → Create a user tied to a restaurant
  POST /delete-user      → Delete a user
  GET  /users            → List all users
  POST /create-restaurant → Create a new restaurant
  POST /set-subscription → Set subscription status
  GET  /restaurant/{id}  → Detail + stats for one restaurant
  POST /update-restaurant → Update restaurant fields
  GET  /billing-stats    → Billing aggregate stats
  POST /fix-branch-ids   → Fix branch IDs (maintenance tool)
  POST /fix-conversations → Fix conversation bot numbers (maintenance tool)
  POST /parse-menu       → Parse PDF/image into JSON menu via Claude
"""
import os
import io
import base64
import json
from fastapi import APIRouter, Request, HTTPException, File, UploadFile, Depends
from pydantic import BaseModel
from anthropic import Anthropic

from app.services.auth import create_user, get_users, hash_password
from app.services import database as db
from app.routes.deps import verify_superadmin
from app.repositories import sessions_repo, restaurant_repo
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/internal/admin", tags=["internal-admin"])


# ── Pydantic models ──────────────────────────────────────────────────────────
class AdminLoginRequest(BaseModel): key: str
class CreateUserRequest(BaseModel): username: str; password: str; restaurant_id: int; admin_key: str = ""
class CreateRestaurantRequest(BaseModel): admin_key: str = ""; name: str; whatsapp_number: str; address: str; menu: str; features: dict = {}; wa_phone_id: str = ""; wa_access_token: str = ""
class SetSubscriptionRequest(BaseModel): admin_key: str = ""; restaurant_id: int; status: str
class UpdateRestaurantRequest(BaseModel):
    admin_key: str = ""; restaurant_id: int
    name: str = None; address: str = None; whatsapp_number: str = None
    wa_phone_id: str = None; wa_access_token: str = None
    features: dict = None; menu: str = None


# ── SUPER ADMIN SESSION ──────────────────────────────────────────────────────

@router.post("/login")
async def admin_login(payload: AdminLoginRequest):
    """Exchange ADMIN_KEY for a session token. The raw key is never stored client-side."""
    if not payload.key or payload.key != os.getenv("ADMIN_KEY"):
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    token = await sessions_repo.create_session("superadmin")
    return {"token": token}


@router.post("/logout")
async def admin_logout_session(req: Request):
    """Invalidate the current superadmin session token."""
    token = req.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if token:
        await sessions_repo.delete_session(token)
    return {"success": True}


@router.get("/stats")
async def admin_get_stats(_: None = Depends(verify_superadmin)):
    return await restaurant_repo.db_get_admin_stats()


@router.get("/restaurants")
async def admin_get_restaurants(_: None = Depends(verify_superadmin)):
    return {"restaurants": await db.db_get_all_restaurants()}


@router.post("/create-user")
async def admin_create_user(request: CreateUserRequest, _: None = Depends(verify_superadmin)):
    rest = await db.db_get_restaurant_by_id(request.restaurant_id)
    if not rest:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

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


@router.post("/delete-user")
async def admin_delete_user(username: str, _: None = Depends(verify_superadmin)):
    await restaurant_repo.db_delete_user(username)
    return {"success": True}


@router.get("/users")
async def admin_list_users(_: None = Depends(verify_superadmin)):
    return {"users": await get_users()}


@router.post("/create-restaurant")
async def admin_create_restaurant(request: CreateRestaurantRequest, _: None = Depends(verify_superadmin)):
    from app.routes.dashboard import geocode_address
    try:
        menu_dict = json.loads(request.menu)
    except Exception:
        raise HTTPException(status_code=400, detail="Menú no es JSON válido")
    lat, lon, _ = await geocode_address(request.address)

    await db.db_create_restaurant(request.name, request.whatsapp_number, request.address, menu_dict, lat, lon, request.features)

    if request.wa_access_token:
        await restaurant_repo.db_set_restaurant_wa_credentials(
            request.whatsapp_number, request.wa_phone_id, request.wa_access_token
        )

    return {"success": True}


@router.post("/set-subscription")
async def admin_set_subscription(request: SetSubscriptionRequest, _: None = Depends(verify_superadmin)):
    await db.db_update_subscription(request.restaurant_id, request.status)
    return {"success": True}


@router.get("/restaurant/{restaurant_id}")
async def admin_get_restaurant_detail(restaurant_id: int, _: None = Depends(verify_superadmin)):
    rest = await db.db_get_restaurant_by_id(restaurant_id)
    if not rest:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    wa = rest.get("whatsapp_number", "")
    stats = await restaurant_repo.db_get_restaurant_detail_stats(restaurant_id, wa)
    return {"restaurant": rest, "stats": stats}


@router.post("/update-restaurant")
async def admin_update_restaurant(request: UpdateRestaurantRequest, _: None = Depends(verify_superadmin)):
    from app.routes.dashboard import geocode_address
    rest = await db.db_get_restaurant_by_id(request.restaurant_id)
    if not rest:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")

    lat = lon = None
    if request.address is not None:
        lat, lon, _ = await geocode_address(request.address)

    merged_features = None
    if request.features is not None:
        raw = rest.get("features") or {}
        current = json.loads(raw) if isinstance(raw, str) else dict(raw)
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except Exception:
                current = {}
        current.update(request.features)
        merged_features = current

    parsed_menu = None
    if request.menu is not None:
        try:
            parsed_menu = json.loads(request.menu)
        except Exception:
            raise HTTPException(status_code=400, detail="Menú no es JSON válido")

    await restaurant_repo.db_update_restaurant_fields(
        request.restaurant_id,
        name=request.name,
        address=request.address,
        latitude=lat,
        longitude=lon,
        whatsapp_number=request.whatsapp_number,
        wa_phone_id=request.wa_phone_id,
        wa_access_token=request.wa_access_token,
        features=merged_features,
        menu=parsed_menu,
    )
    return {"success": True, "restaurant": await db.db_get_restaurant_by_id(request.restaurant_id)}


@router.get("/billing-stats")
async def admin_billing_stats(_: None = Depends(verify_superadmin)):
    stats = await restaurant_repo.db_get_billing_stats()
    return {"stats": stats}


@router.post("/fix-branch-ids")
async def fix_branch_ids(request: Request, _: None = Depends(verify_superadmin)):
    fixed = await restaurant_repo.db_fix_branch_ids()
    return {"success": True, "fixed": fixed}


@router.post("/fix-conversations")
async def fix_conversations_bot_number(request: Request, _: None = Depends(verify_superadmin)):
    body = await request.json()
    await restaurant_repo.db_fix_conversations_bot_number(body.get("bot_number", ""))
    return {"success": True}


@router.post("/parse-menu")
async def admin_parse_menu(file: UploadFile = File(...), _: None = Depends(verify_superadmin)):
    import pypdf
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
        else:
            raise HTTPException(status_code=400, detail="Sube PDF, PNG o JPG")
        response = client.messages.create(
            model="claude-3-haiku-20240307", max_tokens=4000, temperature=0,
            system='Extrae menús a JSON puro: {"Categoría": [{"name":"","price":0.0,"description":""}]}',
            messages=[{"role": "user", "content": messages_content}]
        )
        return {"success": True, "json_menu": json.loads(response.content[0].text.replace("```json","").replace("```","").strip())}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")
