import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import Database
from .schemas import (
    BirthdayCreate,
    BirthdayUpdate,
    CalendarCreate,
    CalendarUpdate,
    ChecklistCreate,
    ChecklistUpdate,
    FrameCreate,
    FrameUpdate,
    LoginRequest,
    MemberCreate,
    MemberUpdate,
    NoteCreate,
    NoteUpdate,
    SettingsUpdate,
)
from .security import (
    create_session,
    new_csrf_token,
    secure_token_matches,
    validate_session,
    verify_password,
)
from .services import (
    WeatherService,
    events_for_range,
    get_timezone,
    timezone_is_valid,
    upcoming_birthdays,
)
from .sync import CalendarSynchronizer

logging.basicConfig(level=os.getenv("FAMILY_DASHBOARD_LOG_LEVEL", "INFO"))
logger = logging.getLogger("family-dashboard")
settings = get_settings()
database = Database(
    settings.database_path,
    {
        "timezone": settings.timezone,
        "location_name": settings.location_name,
        "latitude": str(settings.latitude),
        "longitude": str(settings.longitude),
    },
)
synchronizer = CalendarSynchronizer(database, settings)
weather = WeatherService(settings)


def payload_values(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


def validate_url(value: str, allowed_schemes: tuple[str, ...]) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in allowed_schemes or not parsed.netloc:
        raise HTTPException(status_code=422, detail="URL must use http, https, or webcal")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="Credentials in URLs are not supported")
    return value


def require_auth(request: Request) -> None:
    if not settings.auth_required:
        return
    authorization = request.headers.get("authorization", "")
    bearer = ""
    if authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    if settings.api_token and secure_token_matches(bearer, settings.api_token):
        return
    session = request.cookies.get("fd_session", "")
    if not validate_session(session, settings.session_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        csrf_cookie = request.cookies.get("fd_csrf", "")
        csrf_header = request.headers.get("x-fd-csrf", "")
        if not secure_token_matches(csrf_cookie, csrf_header):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed"
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop_event = asyncio.Event()
    task: Optional[asyncio.Task] = None
    if settings.background_sync:
        task = asyncio.create_task(synchronizer.run_periodically(stop_event))
    yield
    stop_event.set()
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(
    title="Family Dashboard",
    version="0.1.0",
    description="A local-first family calendar and home dashboard",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-FD-CSRF"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.url.path.startswith("/api/") or request.url.path == "/":
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; connect-src 'self' https:; frame-src http: https:; "
            "object-src 'none'; base-uri 'self'; form-action 'self'",
        )
    return response


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(settings.static_dir / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> FileResponse:
    return FileResponse(
        settings.static_dir / "manifest.webmanifest", media_type="application/manifest+json"
    )


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    return FileResponse(settings.static_dir / "sw.js", media_type="application/javascript")


@app.get("/api/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "family-dashboard", "version": app.version}


@app.get("/api/auth/status", include_in_schema=False)
async def auth_status(request: Request) -> dict[str, Any]:
    if not settings.auth_required:
        return {"authenticated": True, "auth_required": False}
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    authenticated = bool(
        settings.api_token and secure_token_matches(bearer, settings.api_token)
    ) or validate_session(request.cookies.get("fd_session", ""), settings.session_secret)
    return {
        "authenticated": authenticated,
        "auth_required": True,
        "csrf": request.cookies.get("fd_csrf") if authenticated else None,
    }


@app.post("/api/auth/login", include_in_schema=False)
async def login(payload: LoginRequest, response: Response) -> dict[str, bool]:
    if not settings.auth_required:
        return {"authenticated": True}
    if not verify_password(payload.password, settings.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong password")
    csrf = new_csrf_token()
    response.set_cookie(
        "fd_session",
        create_session(settings.session_secret),
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
    )
    response.set_cookie(
        "fd_csrf",
        csrf,
        max_age=60 * 60 * 24 * 30,
        httponly=False,
        secure=settings.secure_cookies,
        samesite="lax",
    )
    return {"authenticated": True}


@app.post("/api/auth/logout", include_in_schema=False)
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie("fd_session")
    response.delete_cookie("fd_csrf")
    return {"authenticated": False}


def _runtime_timezone() -> str:
    return database.get_setting("timezone") or settings.timezone


def _runtime_float(key: str, default: float) -> float:
    try:
        return float(database.get_setting(key, str(default)))
    except ValueError:
        return default


def _today(timezone_name: Optional[str] = None) -> date:
    return datetime.now(get_timezone(timezone_name or _runtime_timezone())).date()


def _parse_query_date(value: Optional[str], fallback: date) -> date:
    if not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Date must use YYYY-MM-DD") from exc


@app.get("/api/dashboard/summary", dependencies=[Depends(require_auth)])
async def dashboard_summary(
    day: Optional[str] = Query(default=None), days: int = Query(default=7, ge=1, le=31)
) -> dict[str, Any]:
    current_settings = database.get_settings()
    timezone_name = current_settings.get("timezone") or settings.timezone
    today = _today(timezone_name)
    selected_day = _parse_query_date(day, today)
    end_day = selected_day + timedelta(days=days - 1)
    events = events_for_range(database, selected_day, end_day, timezone_name)
    all_birthdays = upcoming_birthdays(database.list_birthdays(), today, limit=20)
    weather_value = None
    if current_settings.get("weather_enabled", "true").lower() == "true":
        weather_value = await weather.current(
            latitude=_runtime_float("latitude", settings.latitude),
            longitude=_runtime_float("longitude", settings.longitude),
            timezone_name=timezone_name,
            location_name=current_settings.get("location_name") or settings.location_name,
        )
    return {
        "date": selected_day.isoformat(),
        "generated_at": datetime.now(get_timezone(timezone_name)).isoformat(),
        "settings": current_settings,
        "members": database.list_members(),
        "events": events,
        "birthdays": all_birthdays,
        "frames": database.list_frames(visible_only=True),
        "notes": database.list_notes(),
        "checklist": database.list_checklist(),
        "weather": weather_value,
        "sources": database.list_sources(),
    }


@app.get("/api/events", dependencies=[Depends(require_auth)])
async def list_events(
    start: Optional[str] = None,
    end: Optional[str] = None,
    kind: Optional[Literal["calendar", "school"]] = None,
) -> dict[str, Any]:
    start_date = _parse_query_date(start, _today())
    end_date = _parse_query_date(end, start_date + timedelta(days=7))
    if end_date < start_date:
        raise HTTPException(status_code=422, detail="end must be on or after start")
    if (end_date - start_date).days > 366:
        raise HTTPException(status_code=422, detail="Calendar range cannot exceed 366 days")
    timezone_name = _runtime_timezone()
    return {
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "kind": kind,
        "events": events_for_range(database, start_date, end_date, timezone_name, kind=kind),
    }


@app.get("/api/birthdays", dependencies=[Depends(require_auth)])
async def list_birthdays(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return {"birthdays": upcoming_birthdays(database.list_birthdays(), _today(), limit=limit)}


@app.get("/api/calendars", dependencies=[Depends(require_auth)])
async def list_calendars() -> dict[str, Any]:
    return {"calendars": database.list_sources()}


@app.post("/api/calendars", status_code=201, dependencies=[Depends(require_auth)])
async def create_calendar(payload: CalendarCreate) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Calendar name cannot be empty")
    url = validate_url(payload.url, ("http", "https", "webcal"))
    source_type = (
        "webcal" if url.startswith("webcal://") or payload.source_type == "webcal" else "ics"
    )
    return {
        "calendar": database.create_source(
            name,
            url,
            source_type,
            payload.kind,
            payload.color,
            payload.enabled,
        )
    }


@app.patch("/api/calendars/{source_id}", dependencies=[Depends(require_auth)])
async def update_calendar(source_id: int, payload: CalendarUpdate) -> dict[str, Any]:
    if not database.get_source(source_id):
        raise HTTPException(status_code=404, detail="Calendar not found")
    values = payload_values(payload)
    if "url" in values:
        values["url"] = validate_url(values["url"], ("http", "https", "webcal"))
    if "name" in values:
        values["name"] = values["name"].strip()
        if not values["name"]:
            raise HTTPException(status_code=422, detail="Calendar name cannot be empty")
    if "url" in values and values["url"].startswith("webcal://"):
        values["source_type"] = "webcal"
    return {"calendar": database.update_source(source_id, values)}


@app.delete("/api/calendars/{source_id}", dependencies=[Depends(require_auth)])
async def delete_calendar(source_id: int) -> dict[str, bool]:
    if not database.delete_source(source_id):
        raise HTTPException(status_code=404, detail="Calendar not found")
    return {"deleted": True}


@app.post("/api/calendars/{source_id}/sync", dependencies=[Depends(require_auth)])
async def sync_calendar(source_id: int) -> dict[str, Any]:
    source = database.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Calendar not found")
    return await synchronizer.sync_source(source)


@app.post("/api/sync", dependencies=[Depends(require_auth)])
async def sync_all_calendars() -> dict[str, Any]:
    return await synchronizer.sync_all()


@app.get("/api/members", dependencies=[Depends(require_auth)])
async def list_members() -> dict[str, Any]:
    return {"members": database.list_members()}


@app.post("/api/members", status_code=201, dependencies=[Depends(require_auth)])
async def create_member(payload: MemberCreate) -> dict[str, Any]:
    return {
        "member": database.create_member(payload.name.strip(), payload.color, payload.avatar_url)
    }


@app.patch("/api/members/{member_id}", dependencies=[Depends(require_auth)])
async def update_member(member_id: int, payload: MemberUpdate) -> dict[str, Any]:
    if not database.get_member(member_id):
        raise HTTPException(status_code=404, detail="Member not found")
    values = payload_values(payload)
    if "name" in values:
        values["name"] = values["name"].strip()
    return {"member": database.update_member(member_id, values)}


@app.delete("/api/members/{member_id}", dependencies=[Depends(require_auth)])
async def delete_member(member_id: int) -> dict[str, bool]:
    if not database.delete_member(member_id):
        raise HTTPException(status_code=404, detail="Member not found")
    return {"deleted": True}


@app.post("/api/birthdays", status_code=201, dependencies=[Depends(require_auth)])
async def create_birthday(payload: BirthdayCreate) -> dict[str, Any]:
    return {
        "birthday": database.create_birthday(
            payload.name.strip(), payload.birth_date.isoformat(), payload.color, payload.notes
        )
    }


@app.patch("/api/birthdays/{birthday_id}", dependencies=[Depends(require_auth)])
async def update_birthday(birthday_id: int, payload: BirthdayUpdate) -> dict[str, Any]:
    if not database.get_birthday(birthday_id):
        raise HTTPException(status_code=404, detail="Birthday not found")
    values = payload_values(payload)
    if "birth_date" in values:
        values["birth_date"] = values["birth_date"].isoformat()
    if "name" in values:
        values["name"] = values["name"].strip()
    return {"birthday": database.update_birthday(birthday_id, values)}


@app.delete("/api/birthdays/{birthday_id}", dependencies=[Depends(require_auth)])
async def delete_birthday(birthday_id: int) -> dict[str, bool]:
    if not database.delete_birthday(birthday_id):
        raise HTTPException(status_code=404, detail="Birthday not found")
    return {"deleted": True}


@app.get("/api/frames", dependencies=[Depends(require_auth)])
async def list_frames(visible: bool = False) -> dict[str, Any]:
    return {"frames": database.list_frames(visible_only=visible)}


@app.post("/api/frames", status_code=201, dependencies=[Depends(require_auth)])
async def create_frame(payload: FrameCreate) -> dict[str, Any]:
    url = validate_url(payload.url, ("http", "https"))
    return {
        "frame": database.create_frame(
            payload.name.strip(), url, payload.height, payload.accent, payload.visible
        )
    }


@app.patch("/api/frames/{frame_id}", dependencies=[Depends(require_auth)])
async def update_frame(frame_id: int, payload: FrameUpdate) -> dict[str, Any]:
    if not database.get_frame(frame_id):
        raise HTTPException(status_code=404, detail="Frame not found")
    values = payload_values(payload)
    if "url" in values:
        values["url"] = validate_url(values["url"], ("http", "https"))
    if "name" in values:
        values["name"] = values["name"].strip()
    return {"frame": database.update_frame(frame_id, values)}


@app.delete("/api/frames/{frame_id}", dependencies=[Depends(require_auth)])
async def delete_frame(frame_id: int) -> dict[str, bool]:
    if not database.delete_frame(frame_id):
        raise HTTPException(status_code=404, detail="Frame not found")
    return {"deleted": True}


@app.get("/api/notes", dependencies=[Depends(require_auth)])
async def list_notes() -> dict[str, Any]:
    return {"notes": database.list_notes()}


@app.post("/api/notes", status_code=201, dependencies=[Depends(require_auth)])
async def create_note(payload: NoteCreate) -> dict[str, Any]:
    return {
        "note": database.create_note(
            payload.title.strip(), payload.body, payload.color, payload.pinned
        )
    }


@app.patch("/api/notes/{note_id}", dependencies=[Depends(require_auth)])
async def update_note(note_id: int, payload: NoteUpdate) -> dict[str, Any]:
    if not database.get_note(note_id):
        raise HTTPException(status_code=404, detail="Note not found")
    values = payload_values(payload)
    if "title" in values:
        values["title"] = values["title"].strip()
    return {"note": database.update_note(note_id, values)}


@app.delete("/api/notes/{note_id}", dependencies=[Depends(require_auth)])
async def delete_note(note_id: int) -> dict[str, bool]:
    if not database.delete_note(note_id):
        raise HTTPException(status_code=404, detail="Note not found")
    return {"deleted": True}


@app.get("/api/checklist", dependencies=[Depends(require_auth)])
async def list_checklist() -> dict[str, Any]:
    return {"items": database.list_checklist()}


@app.post("/api/checklist", status_code=201, dependencies=[Depends(require_auth)])
async def create_checklist_item(payload: ChecklistCreate) -> dict[str, Any]:
    return {
        "item": database.create_checklist_item(
            payload.text.strip(), payload.due_date.isoformat() if payload.due_date else None
        )
    }


@app.patch("/api/checklist/{item_id}", dependencies=[Depends(require_auth)])
async def update_checklist_item(item_id: int, payload: ChecklistUpdate) -> dict[str, Any]:
    if not database.get_checklist_item(item_id):
        raise HTTPException(status_code=404, detail="Checklist item not found")
    values = payload_values(payload)
    if "due_date" in values:
        values["due_date"] = values["due_date"].isoformat() if values["due_date"] else None
    return {"item": database.update_checklist_item(item_id, values)}


@app.delete("/api/checklist/{item_id}", dependencies=[Depends(require_auth)])
async def delete_checklist_item(item_id: int) -> dict[str, bool]:
    if not database.delete_checklist_item(item_id):
        raise HTTPException(status_code=404, detail="Checklist item not found")
    return {"deleted": True}


@app.get("/api/settings", dependencies=[Depends(require_auth)])
async def get_settings_route() -> dict[str, Any]:
    return {"settings": database.get_settings()}


@app.patch("/api/settings", dependencies=[Depends(require_auth)])
async def update_settings_route(payload: SettingsUpdate) -> dict[str, Any]:
    values = payload_values(payload)
    if "timezone" in values:
        values["timezone"] = values["timezone"].strip()
        if not timezone_is_valid(values["timezone"]):
            raise HTTPException(status_code=422, detail="Unknown timezone")
    for key in ("latitude", "longitude"):
        if key in values:
            values[key] = str(values[key])
    return {"settings": database.update_settings(values)}


if settings.static_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(settings.static_dir)), name="assets")


def run() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=os.getenv("FAMILY_DASHBOARD_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    run()
