import asyncio
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any, AsyncIterator, Literal, Optional
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .aula import AulaError, AulaSync
from .config import get_settings
from .db import Database
from .icloud_notes import ICloudNotes
from .icloud_reminders import RemindersSync
from .reminders_bridge import normalize_bridge_url
from .reolink import (
    STREAM_BOUNDARY,
    CameraMonitor,
    ReolinkCamera,
    ReolinkError,
    is_streaming_url,
    normalize_host,
    stream_camera_mjpeg,
)
from .schemas import (
    EventReminderCreate,
    EventReminderUpdate,
    BirthdayCreate,
    BirthdayUpdate,
    CalendarCreate,
    CalendarUpdate,
    CameraCreate,
    CameraUpdate,
    ChecklistCreate,
    ChecklistUpdate,
    FrameCreate,
    FrameUpdate,
    LoginRequest,
    MemberCreate,
    MemberUpdate,
    NoteCreate,
    NotesIcloudSaveRequest,
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
    active_event_hints,
    get_timezone,
    timezone_is_valid,
    upcoming_birthdays,
)
from .sync import CalendarSynchronizer
from .version import BUILD_ID, VERSION, banner, build_info, mark_started

logging.basicConfig(level=os.getenv("FAMILY_DASHBOARD_LOG_LEVEL", "INFO"))
# httpx/httpcore dumps every request and response body at DEBUG, which drowns the
# useful log. Keep them at WARNING unless the operator explicitly lowers them.
for _noisy in ("httpx", "httpcore", "hpack", "h2", "httpcore._async.http11"):
    logging.getLogger(_noisy).setLevel(
        os.getenv("FAMILY_DASHBOARD_HTTP_LOG_LEVEL", "WARNING").upper()
    )
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
reminders = RemindersSync(database)
aula = AulaSync(database)
icloud_notes = ICloudNotes(database)
camera_monitor = CameraMonitor(database)

SECRET_SETTING_KEYS = {
    "reminders_app_password",
    "notes_imap_app_password",
    "reminders_bridge_token",
    "aula_access_token",
    "aula_refresh_token",
    "aula_csrf_token",
    "aula_session_cookie",
    "aula_login_verifier",
    "aula_login_state",
}


def public_settings(raw: dict[str, Any]) -> dict[str, Any]:
    public = dict(raw)
    source = (raw.get("reminders_source") or "caldav").lower()
    public["reminders_source"] = source
    if source == "bridge":
        public["reminders_configured"] = bool(
            raw.get("reminders_bridge_url")
            and raw.get("reminders_bridge_token")
            and raw.get("reminders_list_href")
        )
    else:
        public["reminders_configured"] = bool(
            raw.get("reminders_app_password") and raw.get("reminders_list_href")
        )
    public["reminders_bridge_configured"] = bool(
        raw.get("reminders_bridge_url") and raw.get("reminders_bridge_token")
    )
    notes_source = (raw.get("notes_source") or "bridge").lower()
    public["notes_source"] = notes_source
    public["notes_imap_configured"] = bool(
        raw.get("notes_imap_note_title")
        and (
            (raw.get("reminders_bridge_url") and raw.get("reminders_bridge_token"))
            if notes_source == "bridge"
            else raw.get("notes_imap_app_password")
        )
    )
    for key in SECRET_SETTING_KEYS:
        public[key] = ""
    # Aula-loginnet er gemt i settings, så klienten skal kun se om det virker.
    public["aula_configured"] = aula.configured()
    public["aula_login_pending"] = bool(raw.get("aula_login_verifier"))
    return public


def payload_values(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


def apply_log_level(database: Database) -> None:
    raw = (database.get_setting("log_level") or "").strip().lower()
    level = getattr(logging, raw.upper(), None)
    if not isinstance(level, int):
        level = logging.WARNING
    for name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "uvicorn.asgi",
        "family-dashboard",
        "app",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(name).setLevel(level)


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
    mark_started()
    apply_log_level(database)
    logger.warning(banner())
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task] = []

    def start(coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    if settings.background_sync:
        start(synchronizer.run_periodically(stop_event))
    start(reminders.run_periodically(stop_event))
    start(aula.run_periodically(stop_event))
    start(camera_monitor.run(stop_event))
    yield
    stop_event.set()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# Hvor mange huskelinjer der højst vises på forsideklokken ad gangen.
# Flere på skærmen endnu ville fylde panelet under fødselsdagen op.
WALL_HINT_LIMIT = 3

app = FastAPI(
    title="Family Dashboard",
    version=VERSION,
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
            "img-src 'self' data: http: https:; connect-src 'self' http: https:; "
            "frame-src http: https:; media-src 'self' http: https:; "
            "object-src 'none'; base-uri 'self'; form-action 'self'",
        )
    return response


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    html = (settings.static_dir / "index.html").read_text(encoding="utf-8")
    # Byg-id på assets, så en browser aldrig kører gammel JS/CSS efter en opdatering.
    for asset in ("/assets/styles.css", "/assets/app.js", "/manifest.webmanifest"):
        html = html.replace(f'"{asset}"', f'"{asset}?v={BUILD_ID}"')
    html = html.replace("{{BUILD}}", f"{VERSION} · build {BUILD_ID}")
    html = html.replace("{{BUILD_ID}}", BUILD_ID)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> FileResponse:
    return FileResponse(
        settings.static_dir / "manifest.webmanifest", media_type="application/manifest+json"
    )


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> HTMLResponse:
    script = (settings.static_dir / "sw.js").read_text(encoding="utf-8")
    script = script.replace("__BUILD__", BUILD_ID)
    return HTMLResponse(
        script,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "family-dashboard",
        "version": app.version,
        "build": BUILD_ID,
        "started_at": build_info()["started_at"],
    }


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
    # Huskelinjer slås op på hele døgnet og ikke kun den valgte dag, så
    # en aftale i morgen kan give en påmindelse i aften.
    hints_window = events_for_range(
        database, _today(timezone_name), _today(timezone_name) + timedelta(days=2), timezone_name
    )
    # Vi viser kun de tre første på væggen, så den ikke bliver fyldt
    # op. Tallet med sendes med, så indstillingssiden kan fortælle at
    # noget er holdt tilbage i stedet for at det bare forsvinder.
    alle_hints = active_event_hints(
        database.list_event_reminders(),
        hints_window,
        datetime.now(get_timezone(timezone_name)),
        limit=99,
    )
    hints = alle_hints[:WALL_HINT_LIMIT]
    # Tilføj Aula-opslag som huskelinjer (de sidste X timer). Gør intervallet konfigurerbart.
    try:
        aula_hint_hours = int(current_settings.get("aula_posts_hint_hours") or 24)
    except (TypeError, ValueError):
        aula_hint_hours = 24
    if aula_hint_hours < 1:
        aula_hint_hours = 1
    if aula_hint_hours > 720:
        aula_hint_hours = 720
    now_utc = datetime.now(get_timezone(timezone_name))
    now_aware = now_utc
    cutoff = now_aware - timedelta(hours=aula_hint_hours)
    aula_post_hints = []
    for post in database.list_aula_posts():
        pub = post.get("published_at")
        if not pub:
            continue
        try:
            dt = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=get_timezone(timezone_name))
        else:
            dt = dt.astimezone(get_timezone(timezone_name))
        if dt >= cutoff:
            title = post.get("title") or "(uden titel)"
            author = post.get("author") or ""
            text = title if not author else f"{title} · {author}"
            aula_post_hints.append(
                {
                    "id": f"aula_post_{post.get('post_id')}",
                    "text": text,
                    "event_title": title,
                    "event_id": post.get("post_id"),
                    "local_time": dt.strftime("%H:%M"),
                    "in_progress": False,
                    "all_day": False,
                    "source_kind": "aula_post",
                }
            )
    aula_post_hints.sort(key=lambda item: item["local_time"])
    # Slå sammen: eksisterende hints først (behold deres prioritet), derefter aula-posts
    merged_hints = list(hints) + aula_post_hints
    # Unik på id hvis nogen skulle kollidere
    seen = {}
    unique = []
    for h in merged_hints:
        hid = h.get("id")
        if hid not in seen:
            seen[hid] = True
            unique.append(h)
    hints_final = unique[:WALL_HINT_LIMIT]
    hints_total_final = len(alle_hints) + len(aula_post_hints)
    # Tilføj accepterede Aula-kalenderbegivenheder af typen 'event' til forsiden
    try:
        aula_events_all = database.list_aula_events()
    except Exception:
        aula_events_all = []
    aula_family_events = []
    for ae in aula_events_all:
        if str(ae.get("category") or "").lower() == "lesson":
            continue
        acc = ae.get("accepted")
        if acc is not True:
            continue
        # konverter til kalender-lignende format til forsiden
        aula_family_events.append(
            {
                "id": f"aula_{ae.get('event_id')}",
                "title": ae.get("title") or "(uden titel)",
                "start_at": ae.get("start_at"),
                "end_at": ae.get("end_at"),
                "all_day": False,
                "location": ae.get("location") or "",
                "source_kind": "aula",
                "source_name": "Aula",
                "source_color": "#38bdf8",
                "local_date": ae.get("start_at")[:10] if isinstance(ae.get("start_at"), str) and len(ae.get("start_at")) >= 10 else None,
            }
        )
    merged_events = list(events) + aula_family_events
    return {
        "date": selected_day.isoformat(),
        "generated_at": datetime.now(get_timezone(timezone_name)).isoformat(),
        "settings": public_settings(current_settings),
        "members": database.list_members(),
        "events": merged_events,
        "event_hints": hints_final,
        "event_hints_total": hints_total_final,
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


@app.get("/api/event-reminders", dependencies=[Depends(require_auth)])
async def list_event_reminders() -> dict[str, Any]:
    """Alle huskelinjer. Det er ikke det samme som iCloud-påmindelser."""
    return {"event_reminders": database.list_event_reminders()}


@app.post("/api/event-reminders", status_code=201, dependencies=[Depends(require_auth)])
async def create_event_reminder(payload: EventReminderCreate) -> dict[str, Any]:
    match_value = payload.match_value.strip()
    text = payload.text.strip()
    if not match_value:
        raise HTTPException(status_code=422, detail="match_value må ikke være tom")
    if not text:
        raise HTTPException(status_code=422, detail="text må ikke være tom")
    source_id = payload.source_id
    if source_id is not None:
        known = {int(source["id"]) for source in database.list_sources()}
        if int(source_id) not in known:
            raise HTTPException(status_code=422, detail="Ukendt kalender")
    return {
        "event_reminder": database.create_event_reminder(
            payload.source_kind,
            source_id,
            payload.match_mode,
            match_value,
            text,
            payload.lead_hours,
        )
    }


@app.patch("/api/event-reminders/{item_id}", dependencies=[Depends(require_auth)])
async def update_event_reminder(
    item_id: int, payload: EventReminderUpdate
) -> dict[str, Any]:
    if not database.get_event_reminder(item_id):
        raise HTTPException(status_code=404, detail="Huskelinje ikke fundet")
    values = payload_values(payload)
    for key in ("match_value", "text"):
        if key in values:
            values[key] = str(values[key]).strip()
            if not values[key]:
                raise HTTPException(status_code=422, detail=f"{key} må ikke være tom")
    # payload_values dropper null, så en eksplicit null på source_id ellers
    # ikke kunne rydde en regel tilbage til "alle kalendere". Det er præcis
    # den bevægelse brugeren laver, når en regel var låst til én kalender.
    if "source_id" in payload.model_fields_set:
        values["source_id"] = payload.source_id
    if values.get("source_id") is not None:
        known = {int(source["id"]) for source in database.list_sources()}
        if int(values["source_id"]) not in known:
            raise HTTPException(status_code=422, detail="Ukendt kalender")
    return {"event_reminder": database.update_event_reminder(item_id, values)}


@app.delete("/api/event-reminders/{item_id}", dependencies=[Depends(require_auth)])
async def delete_event_reminder(item_id: int) -> dict[str, bool]:
    if not database.delete_event_reminder(item_id):
        raise HTTPException(status_code=404, detail="Huskelinje ikke fundet")
    return {"deleted": True}


@app.post("/api/checklist", status_code=201, dependencies=[Depends(require_auth)])
async def create_checklist_item(payload: ChecklistCreate) -> dict[str, Any]:
    text = payload.text.strip()
    due = payload.due_date if payload.due_date else None
    if reminders.enabled():
        item = await reminders.create(text, due)
        return {"item": item}
    return {
        "item": database.create_checklist_item(
            text, due.isoformat() if due else None
        )
    }


@app.patch("/api/checklist/{item_id}", dependencies=[Depends(require_auth)])
async def update_checklist_item(item_id: int, payload: ChecklistUpdate) -> dict[str, Any]:
    item = database.get_checklist_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Checklist item not found")
    values = payload_values(payload)
    if "due_date" in values:
        values["due_date"] = values["due_date"].isoformat() if values["due_date"] else None
    if reminders.enabled() and item.get("source") == "icloud":
        return {"item": await reminders.update(item, values)}
    return {"item": database.update_checklist_item(item_id, values)}


@app.delete("/api/checklist/{item_id}", dependencies=[Depends(require_auth)])
async def delete_checklist_item(item_id: int) -> dict[str, bool]:
    item = database.get_checklist_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Checklist item not found")
    if reminders.enabled() and item.get("source") == "icloud":
        await reminders.delete(item)
    else:
        database.delete_checklist_item(item_id)
    return {"deleted": True}


@app.get("/api/settings", dependencies=[Depends(require_auth)])
async def get_settings_route() -> dict[str, Any]:
    return {"settings": public_settings(database.get_settings())}


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
    for key in SECRET_SETTING_KEYS:
        if key in values and not values[key]:
            del values[key]
    if "reminders_bridge_url" in values:
        try:
            values["reminders_bridge_url"] = normalize_bridge_url(values["reminders_bridge_url"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if "today_layout" in values and values["today_layout"]:
        # Layout gemmes som JSON-streng, så browseren kan sende den uændret
        # videre. Ugyldig JSON afvises, ellers ville alle kort forsvinde.
        try:
            parsed = json.loads(values["today_layout"])
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail="Layout er ikke gyldig JSON") from exc
        if not isinstance(parsed, list):
            raise HTTPException(status_code=422, detail="Layout skal være en liste af kort")
        if len(parsed) > 40:
            raise HTTPException(status_code=422, detail="Layout har for mange kort")
    if values.get("reminders_source") == "bridge":
        missing = [
            key
            for key in ("reminders_bridge_url", "reminders_bridge_token")
            if not (values.get(key) or database.get_setting(key))
        ]
        if missing:
            raise HTTPException(
                status_code=422,
                detail="Mac mini-bridge kræver både adresse og token",
            )
    updated = database.update_settings(values)
    if "log_level" in values:
        apply_log_level(database)
    return {"settings": public_settings(updated)}


@app.get("/api/cameras", dependencies=[Depends(require_auth)])
async def list_cameras() -> dict[str, Any]:
    return {"cameras": database.list_cameras()}


@app.post("/api/cameras", status_code=201, dependencies=[Depends(require_auth)])
async def create_camera(payload: CameraCreate) -> dict[str, Any]:
    try:
        host = normalize_host(payload.host)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not payload.password:
        raise HTTPException(
            status_code=422,
            detail="Kameraet kræver en adgangskode. Reolink afviser login uden den.",
        )
    return {
        "camera": database.create_camera(
            payload.name.strip(),
            host,
            payload.username.strip(),
            payload.password,
            payload.channel,
            payload.person_enabled,
            payload.vehicle_enabled,
            payload.snapshots_enabled,
            payload.live_stream_url.strip(),
            payload.popup_enabled,
        )
    }


@app.patch("/api/cameras/{camera_id}", dependencies=[Depends(require_auth)])
async def update_camera(camera_id: int, payload: CameraUpdate) -> dict[str, Any]:
    if not database.get_camera(camera_id):
        raise HTTPException(status_code=404, detail="Camera not found")
    values = payload_values(payload)
    if "host" in values:
        try:
            values["host"] = normalize_host(values["host"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if "password" in values and not values["password"]:
        del values["password"]
    if "name" in values:
        values["name"] = values["name"].strip()
    if "host" in values:
        camera_monitor.invalidate_snapshot(camera_id)
    return {"camera": database.update_camera(camera_id, values)}


@app.delete("/api/cameras/{camera_id}", dependencies=[Depends(require_auth)])
async def delete_camera(camera_id: int) -> dict[str, bool]:
    if not database.delete_camera(camera_id):
        raise HTTPException(status_code=404, detail="Camera not found")
    camera_monitor.invalidate_snapshot(camera_id)
    return {"deleted": True}


@app.get("/api/cameras/activity", dependencies=[Depends(require_auth)])
async def camera_activity() -> dict[str, Any]:
    return camera_monitor.activity()


@app.post("/api/cameras/{camera_id}/test", dependencies=[Depends(require_auth)])
async def test_camera(camera_id: int) -> dict[str, Any]:
    camera = database.get_camera_raw(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return await camera_monitor.test(camera)


@app.get("/api/cameras/{camera_id}/snapshot", dependencies=[Depends(require_auth)])
async def camera_snapshot(camera_id: int) -> Response:
    camera = database.get_camera_raw(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    snapshot = camera_monitor.snapshot_bytes(camera_id)
    if not snapshot:
        try:
            async with httpx.AsyncClient() as client:
                snapshot = await ReolinkCamera(camera).snapshot(client)
            camera_monitor.invalidate_snapshot(camera_id)
        except ReolinkError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(
        content=snapshot,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/cameras/{camera_id}/stream", dependencies=[Depends(require_auth)])
async def camera_stream(
    camera_id: int,
    max_seconds: int = Query(default=60, ge=5, le=600),
    height: int = Query(default=480, ge=160, le=1080),
) -> StreamingResponse:
    camera = database.get_camera(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    rtsp = str(camera.get("live_stream_url") or "").strip()
    if not is_streaming_url(rtsp):
        raise HTTPException(
            status_code=422,
            detail="Kameraet har ingen RTSP-live-stream-URL – udfyld fx rtsp://brugernavn:kode@IP:554/h264Preview_01_main",
        )
    if shutil.which("ffmpeg") is None:
        raise HTTPException(status_code=503, detail="ffmpeg er ikke installeret i containeren")

    async def generator() -> AsyncIterator[bytes]:
        try:
            async for chunk in stream_camera_mjpeg(
                camera, max_seconds=float(max_seconds), height=height
            ):
                yield chunk
        except ReolinkError as exc:
            yield b"--" + STREAM_BOUNDARY.encode() + b"\r\nContent-Type: text/plain\r\n\r\n" + str(exc).encode("utf-8") + b"\r\n"
        finally:
            yield b"--" + STREAM_BOUNDARY.encode() + b"--\r\n"

    return StreamingResponse(
        generator(),
        media_type=f"multipart/x-mixed-replace; boundary={STREAM_BOUNDARY}",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/reminders/lists", dependencies=[Depends(require_auth)])
async def reminder_lists() -> dict[str, Any]:
    lists, error = await reminders.discover_lists()
    return {"lists": lists, "error": error}


@app.post("/api/reminders/sync", dependencies=[Depends(require_auth)])
async def sync_reminders() -> dict[str, Any]:
    return await reminders.sync()


@app.post("/api/reminders/test", dependencies=[Depends(require_auth)])
async def test_reminders() -> dict[str, Any]:
    return await reminders.test_connection()


@app.get("/api/notes/icloud", dependencies=[Depends(require_auth)])
async def icloud_note() -> dict[str, Any]:
    return await icloud_notes.fetch()


@app.post("/api/notes/icloud/save", dependencies=[Depends(require_auth)])
async def save_icloud_note(payload: NotesIcloudSaveRequest) -> dict[str, Any]:
    return await icloud_notes.save(payload.content)


@app.get("/api/aula", dependencies=[Depends(require_auth)])
async def aula_overview() -> dict[str, Any]:
    """Alt Aula-siden skal bruge i ét kald, så skærmen kun henter én gang."""
    threads = database.list_aula_threads()
    return {
        "configured": aula.configured(),
        "enabled": aula.enabled(),
        "children": database.list_aula_profiles(),
        "threads": threads,
        "posts": database.list_aula_posts(),
        "events": database.list_aula_events(),
        "unread_threads": sum(1 for thread in threads if thread["is_unread"]),
        "unread_posts": sum(
            1 for post in database.list_aula_posts() if post["is_unread"]
        ),
        "last_sync": database.get_setting("aula_last_sync", ""),
        "last_error": database.get_setting("aula_last_error", ""),
    }


@app.get("/api/aula/threads/{thread_id}/messages", dependencies=[Depends(require_auth)])
async def aula_thread_messages(thread_id: str) -> dict[str, Any]:
    return {"messages": database.list_aula_messages(thread_id)}


@app.post("/api/aula/login/start", dependencies=[Depends(require_auth)])
async def aula_login_start(scope: str = "aula") -> dict[str, Any]:
    """Laver et PKCE-link, som brugeren åbner på sin telefon med MitID."""
    try:
        return await aula.login_url(scope)
    except AulaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/aula/login/complete", dependencies=[Depends(require_auth)])
async def aula_login_complete(payload: dict[str, Any]) -> dict[str, Any]:
    """Bytter koden fra MitID-login'et til tokens og henter data med det samme."""
    try:
        return await aula.complete_login(
            str(payload.get("code") or ""), str(payload.get("state") or "")
        )
    except AulaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/aula/logout", dependencies=[Depends(require_auth)])
async def aula_logout() -> dict[str, Any]:
    return await aula.logout()


@app.post("/api/aula/sync", dependencies=[Depends(require_auth)])
async def aula_sync(payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Synkroniserer nu. Kalenderfejl må ikke tage beskeder og opslag med."""
    values = payload or {}
    result = await aula.sync(
        child_ids=values.get("child_ids") or None,
        with_messages=bool(values.get("messages", True)),
        with_posts=bool(values.get("posts", True)),
        with_calendar=bool(values.get("calendar", True)),
    )
    return result


@app.post("/api/aula/test", dependencies=[Depends(require_auth)])
async def aula_test() -> dict[str, Any]:
    return await aula.test_connection()


@app.post("/api/aula/{kind}/{item_id}/star", dependencies=[Depends(require_auth)])
async def aula_star(kind: str, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return aula.set_starred(kind, item_id, bool(payload.get("starred", True)))
    except AulaError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/aula/{kind}/{item_id}/read", dependencies=[Depends(require_auth)])
async def aula_read(kind: str, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return aula.set_read(kind, item_id, bool(payload.get("read", True)))
    except AulaError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
