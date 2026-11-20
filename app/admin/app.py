"""Developer portal: a private, server-rendered admin app (Jinja2 + HTMX + Pico CSS).

Runs as its own listener bound to 127.0.0.1 (ADMIN_HOST/ADMIN_PORT), separate from the
public webhook app, so nothing here is reachable through the Cloudflare Tunnel.

    uvicorn app.admin.app:app --host 127.0.0.1 --port 8001
"""

import json
import logging
import secrets
import shutil
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.admin import auth
from app.config import Settings, get_settings
from app.database.bootstrap import prepare
from app.database.connection import connect
from app.deps import get_db
from app.integrations import checks
from app.agent.state_machine import OrderStatus
from app.integrations import twilio
from app.services import conversation as conv_svc
from app.services import customers, menu_admin, owner_commands
from app.services.business import load_business
from app.services.phone import display as phone_display
from app.services.pricing import money
from app.settings import runtime
from app.settings.secret_store import SecretStore
from app.timeutil import from_db, to_db, utcnow
from app.workers import queue

log = logging.getLogger(__name__)
PORTAL_DIR = Path(__file__).resolve().parents[2] / "developer_portal"
templates = Jinja2Templates(directory=PORTAL_DIR / "templates")


def _localtime(ts: str | None, tz: str = "America/Chicago") -> str:
    if not ts:
        return "never"
    if not ts[:4].isdigit():
        return ts
    dt = from_db(ts).astimezone(ZoneInfo(tz))
    return f"{dt:%b} {dt.day}, {dt:%-I:%M %p}"


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    return f"{int(seconds // 3600)} h ago"


templates.env.filters["money"] = money
templates.env.filters["localtime"] = _localtime
templates.env.filters["ago"] = _ago
templates.env.filters["phone"] = phone_display
templates.env.filters["fromjson"] = lambda v: json.loads(v) if v else None

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


class LoginRequired(Exception):
    pass


def bootstrap_code_file(settings: Settings) -> Path:
    return Path(settings.database_path).parent / "bootstrap_code.txt"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.db_path = settings.database_path
    app.state.secret_store = SecretStore.from_path(settings.master_key_path)
    app.state.http = httpx.AsyncClient(timeout=15.0)
    conn = connect(settings.database_path)
    prepare(settings, conn)
    code = auth.issue_bootstrap_code(conn, utcnow(), bootstrap_code_file(settings))
    conn.close()
    if code:
        banner = "=" * 60
        log.warning("\n%s\n  First-time portal setup code: %s\n  Open http://%s:%s/admin/setup\n%s",
                    banner, code, settings.admin_host, settings.admin_port, banner)
        print(f"\n{banner}\n  First-time portal setup code: {code}\n"
              f"  Open http://{settings.admin_host}:{settings.admin_port}/admin/setup\n{banner}\n", flush=True)
    yield
    await app.state.http.aclose()


# ---------------------------------------------------------------- dependencies

def runtime_settings(request: Request, conn: sqlite3.Connection = Depends(get_db),
                     base: Settings = Depends(get_settings)) -> Settings:
    return runtime.effective_settings(conn, base, request.app.state.secret_store)


def get_settings_for(request: Request) -> Settings:
    """Base settings, honoring dependency overrides (used by tests)."""
    override = request.app.dependency_overrides.get(get_settings)
    return override() if override else get_settings()


def current_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> auth.AdminUser:
    user = auth.session_user(conn, request.cookies.get(auth.COOKIE), utcnow())
    if user is None:
        raise LoginRequired
    return user


async def admin_user(request: Request, user: auth.AdminUser = Depends(current_user)) -> auth.AdminUser:
    """Logged in as developer-admin, with a valid CSRF token on state-changing requests."""
    if user.role != "developer_admin":
        raise PermissionError("Only a developer admin can do this.")
    if request.method not in ("GET", "HEAD"):
        sent = request.headers.get("x-csrf-token") or (await request.form()).get("csrf_token", "")
        if not secrets.compare_digest(str(sent), user.csrf_token):
            raise PermissionError("Your session form expired. Reload the page and try again.")
    return user


def page(request: Request, name: str, user: auth.AdminUser | None, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"user": user, "nav": name.split(".")[0], **ctx})


def result(request: Request, ok: bool, message: str, steps=None, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/result.html", {"ok": ok, "message": message, "steps": steps},
                                      status_code=status_code)


# ---------------------------------------------------------------- status helpers

def _age_seconds(ts: str | None) -> float | None:
    return (utcnow() - from_db(ts)).total_seconds() if ts else None


def integration_status(conn: sqlite3.Connection, s: Settings) -> dict[str, dict]:
    def last(name):
        return conn.execute("SELECT * FROM integration_checks WHERE integration = ?", (name,)).fetchone()

    out = {}
    missing = {
        "meta": checks.meta_missing(s) + ([] if s.public_base_url else ["Public URL"]),
        "openrouter": [n for n, v in (("API key", s.openrouter_api_key.get_secret_value()), ("Model", s.ai_model)) if not v],
        "twilio": [n for n, v in (("Account SID", s.twilio_account_sid), ("Auth token", s.twilio_auth_token.get_secret_value()),
                                  ("Sending number", s.twilio_phone_number), ("Owner phones", s.owner_phone_numbers)) if not v],
    }
    for name, miss in missing.items():
        chk = last(name)
        if miss:
            out[name] = {"status": "UNCONFIGURED", "detail": "Missing: " + ", ".join(miss), "checked_at": chk["checked_at"] if chk else None}
        elif chk:
            out[name] = {"status": chk["status"], "detail": chk["detail"], "checked_at": chk["checked_at"]}
        else:
            out[name] = {"status": "UNKNOWN", "detail": "Not tested yet.", "checked_at": None}
    return out


def health_snapshot(conn: sqlite3.Connection, s: Settings) -> dict:
    hb = runtime.get_marker(conn, "worker.heartbeat")
    hb_age = _age_seconds(hb["value"]) if hb else None
    worker = "UNKNOWN" if hb_age is None else ("HEALTHY" if hb_age < 60 else "DOWN")
    last_event = conn.execute("SELECT MAX(created_at) FROM webhook_events WHERE provider = 'messenger'").fetchone()[0]
    sig = runtime.get_marker(conn, "meta.last_signature_failure")
    verify = runtime.get_marker(conn, "meta.last_verification")
    oldest = conn.execute("SELECT MIN(created_at) FROM webhook_events WHERE status = 'pending'").fetchone()[0]
    disk = shutil.disk_usage(Path(s.database_path).resolve().parent)
    return {
        "bot_mode": s.bot_mode,
        "worker": {"status": worker, "age": hb_age},
        "queue": queue.stats(conn),
        "oldest_pending_age": _age_seconds(oldest),
        "last_webhook": last_event,
        "last_verification": verify["updated_at"] if verify else None,
        "last_signature_failure": dict(sig) if sig else None,
        "orders_pending": conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'PENDING_ACCEPTANCE'").fetchone()[0],
        "sms_pending": conn.execute("SELECT COUNT(*) FROM notifications WHERE status IN ('pending','sending')").fetchone()[0],
        "sms_failed": conn.execute("SELECT COUNT(*) FROM notifications WHERE status = 'failed'").fetchone()[0],
        "disk_free_pct": round(disk.free / disk.total * 100, 1),
        "integrations": integration_status(conn, s),
        "callback_url": checks.callback_url(s),
    }


def save_check(conn: sqlite3.Connection, name: str, res: checks.CheckResult) -> None:
    conn.execute(
        """INSERT INTO integration_checks (integration, status, detail, latency_ms) VALUES (?, ?, ?, ?)
           ON CONFLICT(integration) DO UPDATE SET status = excluded.status, detail = excluded.detail,
             latency_ms = excluded.latency_ms, checked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        (name, res.status, res.detail, res.latency_ms),
    )


# ---------------------------------------------------------------- app

def create_admin_app() -> FastAPI:
    app = FastAPI(title="Town Donut developer portal", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/admin/static", StaticFiles(directory=PORTAL_DIR / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith("/admin/static"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": "/admin/login"})
        return RedirectResponse("/admin/login", status_code=303)

    @app.exception_handler(PermissionError)
    async def _forbidden(request: Request, exc: PermissionError):
        return result(request, False, str(exc), status_code=403)

    @app.get("/")
    def root():
        return RedirectResponse("/admin/", status_code=303)

    # ---- first run + login
    @app.get("/admin/setup")
    def setup_form(request: Request, conn=Depends(get_db)):
        if auth.has_admin(conn):
            return RedirectResponse("/admin/login", status_code=303)
        return page(request, "setup.html", None)

    @app.post("/admin/setup")
    async def setup_submit(request: Request, conn=Depends(get_db), base: Settings = Depends(get_settings)):
        if auth.has_admin(conn):
            return RedirectResponse("/admin/login", status_code=303)
        form = await request.form()
        try:
            auth.create_first_admin(conn, str(form.get("code", "")), str(form.get("username", "")),
                                    str(form.get("password", "")), utcnow(), bootstrap_code_file(base))
        except (PermissionError, ValueError) as exc:
            return page(request, "setup.html", None, error=str(exc), username=form.get("username", ""))
        return RedirectResponse("/admin/login?created=1", status_code=303)

    @app.get("/admin/login")
    def login_form(request: Request, conn=Depends(get_db)):
        if not auth.has_admin(conn):
            return RedirectResponse("/admin/setup", status_code=303)
        return page(request, "login.html", None, created=request.query_params.get("created"))

    @app.post("/admin/login")
    async def login_submit(request: Request, conn=Depends(get_db), base: Settings = Depends(get_settings)):
        form = await request.form()
        try:
            token = auth.login(conn, str(form.get("username", "")), str(form.get("password", "")), utcnow(), base.admin_session_hours)
        except PermissionError as exc:
            return page(request, "login.html", None, error=str(exc), username=form.get("username", ""))
        resp = RedirectResponse("/admin/", status_code=303)
        resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                        max_age=base.admin_session_hours * 3600, path="/admin")
        return resp

    @app.post("/admin/logout")
    async def logout(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        auth.logout(conn, request.cookies.get(auth.COOKIE))
        resp = RedirectResponse("/admin/login", status_code=303)
        resp.delete_cookie(auth.COOKIE, path="/admin")
        return resp

    # ---- overview
    @app.get("/admin/")
    def overview(request: Request, conn=Depends(get_db), user=Depends(current_user), s=Depends(runtime_settings)):
        return page(request, "overview.html", user, h=health_snapshot(conn, s))

    @app.get("/admin/partials/health")
    def health_partial(request: Request, conn=Depends(get_db), user=Depends(current_user), s=Depends(runtime_settings)):
        return templates.TemplateResponse(request, "partials/health.html", {"h": health_snapshot(conn, s), "user": user})

    @app.get("/admin/v1/overview")
    def overview_json(conn=Depends(get_db), user=Depends(current_user), s=Depends(runtime_settings)):
        return JSONResponse(health_snapshot(conn, s))

    @app.post("/admin/bot/mode")
    async def bot_mode(request: Request, conn=Depends(get_db), user=Depends(admin_user), s=Depends(runtime_settings)):
        mode = str((await request.form()).get("mode", ""))
        if mode == "live" and s.meta_app_secret.get_secret_value() == "":
            return result(request, False, "Add your Meta App secret first, so incoming messages can be verified.")
        runtime.set_values(conn, {"bot_mode": mode}, user.username)
        auth.audit(conn, user.username, "bot.mode", mode, "ok")
        resp = result(request, True, "The bot is now live and will reply to customers." if mode == "live"
                      else "The bot is paused. Messages are still saved, but nobody gets an automatic reply.")
        resp.headers["HX-Trigger"] = "refresh-health"
        return resp

    @app.post("/admin/jobs/retry-failed")
    async def retry_failed(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        n = conn.execute(
            "UPDATE webhook_events SET status = 'pending', attempts = 0, next_attempt_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE status = 'failed'"
        ).rowcount
        auth.audit(conn, user.username, "jobs.retry_failed", str(n), "ok")
        return result(request, True, f"Queued {n} failed message(s) to try again.")

    # ---- integrations
    @app.get("/admin/integrations")
    def integrations(request: Request, conn=Depends(get_db), user=Depends(current_user), s=Depends(runtime_settings)):
        store = request.app.state.secret_store
        secrets_status = {n: store.status(conn, n) for n in runtime.SECRET_FIELDS}
        # Show secrets that only come from .env as configured too.
        for n in runtime.SECRET_FIELDS:
            if not secrets_status[n]["configured"] and getattr(s, n).get_secret_value():
                secrets_status[n] = {"configured": True, "updated_at": "from .env"}
        return page(request, "integrations.html", user, s=s, sec=secrets_status,
                    status=integration_status(conn, s), callback_url=checks.callback_url(s),
                    saved=request.query_params.get("saved"), error=request.query_params.get("error"))

    async def _apply_form(request: Request, conn, user, plain: tuple[str, ...], secret: tuple[str, ...], label: str,
                          keep_blank: bool = False) -> str | None:
        """Save whichever of these fields the form sent. Returns an error message, or None.
        keep_blank: a blank field leaves the saved value alone (used when Test/Connect auto-save,
        so a stale empty box on the page can't wipe a value saved a moment ago)."""
        form = await request.form()
        store: SecretStore = request.app.state.secret_store
        values = {k: str(form.get(k, "")).strip() for k in plain if k in form}
        if keep_blank:
            values = {k: v for k, v in values.items() if v}
        if values.get("public_base_url"):
            try:
                values["public_base_url"] = checks.validate_public_base_url(values["public_base_url"])
            except ValueError as exc:
                return str(exc)
        try:
            runtime.set_values(conn, values, user.username)
        except ValidationError as exc:
            return "Please check: " + "; ".join(e["msg"] for e in exc.errors())
        changed = []
        for name in secret:
            val = str(form.get(name, "")).strip()
            if val:  # blank means keep the current value
                store.put(conn, name, val, user.username)
                changed.append(name)
        if values or changed:
            auth.audit(conn, user.username, f"settings.save.{label}", ",".join(list(values) + changed), "ok")
        return None

    async def _save(request: Request, conn, user, plain: tuple[str, ...], secret: tuple[str, ...], label: str):
        error = await _apply_form(request, conn, user, plain, secret, label)
        if error:
            if not request.headers.get("hx-request"):
                return RedirectResponse(f"/admin/integrations?error={label}#{label}", status_code=303)
            return result(request, False, error)
        # Reload the page so the status badge and "saved" labels update right away.
        target = f"/admin/integrations?saved={label}#{label}"
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    async def _run_check(request: Request, conn, user, name: str, fn, plain=(), secret=()):
        # Buttons send the form too, so typed-but-unsaved values are saved before testing.
        error = await _apply_form(request, conn, user, plain, secret, name, keep_blank=True) if plain or secret else None
        if error:
            return result(request, False, error)
        s = runtime.effective_settings(conn, get_settings_for(request), request.app.state.secret_store)
        try:
            res = await fn(s, request.app.state.http)
        except httpx.HTTPError as exc:
            res = checks.CheckResult(False, "DOWN", f"Couldn't reach the provider ({type(exc).__name__}).")
        save_check(conn, name, res)
        auth.audit(conn, user.username, f"integration.test.{name}", None, res.status)
        resp = result(request, res.ok, res.detail, res.steps)
        resp.headers["HX-Trigger"] = "refresh-health"
        return resp

    META_PLAIN = ("meta_page_id", "meta_app_id", "meta_graph_api_version", "public_base_url")
    META_SECRET = ("meta_app_secret", "meta_page_access_token")
    OR_PLAIN, OR_SECRET = ("ai_model", "ai_fallback_model"), ("openrouter_api_key",)
    TW_PLAIN, TW_SECRET = ("twilio_account_sid", "twilio_phone_number", "owner_phone_numbers"), ("twilio_auth_token",)

    @app.post("/admin/integrations/meta")
    async def save_meta(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        return await _save(request, conn, user, META_PLAIN, META_SECRET, "meta")

    @app.post("/admin/integrations/meta/test")
    async def test_meta(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        return await _run_check(request, conn, user, "meta", checks.test_meta, META_PLAIN, META_SECRET)

    @app.post("/admin/integrations/meta/connect")
    async def connect_meta(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        return await _run_check(request, conn, user, "meta", checks.connect_meta, META_PLAIN, META_SECRET)

    @app.post("/admin/integrations/meta/verify-token")
    async def verify_token(request: Request, conn=Depends(get_db), user=Depends(admin_user), s=Depends(runtime_settings)):
        """action=generate makes a new token; action=reveal shows the current one once."""
        action = str((await request.form()).get("action", "reveal"))
        if action == "generate":
            token = secrets.token_urlsafe(24)
            request.app.state.secret_store.put(conn, "meta_verify_token", token, user.username)
        else:
            token = s.meta_verify_token.get_secret_value()
        auth.audit(conn, user.username, f"meta.verify_token.{action}", None, "ok")
        return templates.TemplateResponse(request, "partials/reveal.html", {"value": token, "generated": action == "generate"})

    @app.post("/admin/integrations/tunnel/detect")
    async def detect_tunnel(request: Request, conn=Depends(get_db), user=Depends(admin_user), s=Depends(runtime_settings)):
        url = await checks.detect_quick_tunnel(s.cloudflared_metrics_ports, request.app.state.http)
        if not url:
            return result(request, False, "No running Cloudflare quick tunnel was found on this computer.")
        runtime.set_values(conn, {"public_base_url": url}, user.username)
        auth.audit(conn, user.username, "tunnel.detect", url, "ok")
        # Reload so the Public URL box shows the saved address (and can't be re-sent stale).
        return Response(status_code=204, headers={"HX-Redirect": "/admin/integrations?saved=tunnel#meta"})

    @app.post("/admin/integrations/openrouter")
    async def save_openrouter(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        return await _save(request, conn, user, OR_PLAIN, OR_SECRET, "openrouter")

    @app.post("/admin/integrations/openrouter/test")
    async def test_openrouter(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        return await _run_check(request, conn, user, "openrouter", checks.test_openrouter, OR_PLAIN, OR_SECRET)

    @app.post("/admin/integrations/twilio")
    async def save_twilio(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        return await _save(request, conn, user, TW_PLAIN, TW_SECRET, "twilio")

    @app.post("/admin/integrations/twilio/test")
    async def test_twilio(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        return await _run_check(request, conn, user, "twilio", checks.test_twilio, TW_PLAIN, TW_SECRET)

    # ---- menu editor
    def _menu_done(request: Request, conn, user, action: str, detail: str, message: str, anchor: str = ""):
        auth.audit(conn, user.username, f"menu.{action}", detail[:200], "ok")
        target = f"/admin/menu?saved={quote(message)}" + (f"#{anchor}" if anchor else "")
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    def _menu_error(request: Request, exc: Exception):
        msg = str(exc) if isinstance(exc, menu_admin.MenuError) else "That change couldn't be saved."
        if request.headers.get("hx-request"):
            return result(request, False, msg)
        return RedirectResponse(f"/admin/menu?error={quote(msg)}", status_code=303)

    @app.get("/admin/menu")
    def menu_page(request: Request, conn=Depends(get_db), user=Depends(current_user)):
        archived = conn.execute("SELECT COUNT(*) FROM menu_items WHERE archived = 1").fetchone()[0]
        return page(request, "menu.html", user, tree=menu_admin.menu_tree(conn), cats=menu_admin.categories(conn),
                    archived=archived, edited=menu_admin.edited_in_portal(conn),
                    saved=request.query_params.get("saved"), error=request.query_params.get("error"))

    @app.get("/admin/menu/export")
    def menu_export(conn=Depends(get_db), user=Depends(current_user)):
        body = json.dumps(menu_admin.export_menu(conn), indent=2, ensure_ascii=False) + "\n"
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="menu.json"'})

    @app.post("/admin/menu/categories")
    async def menu_add_category(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        form = await request.form()
        try:
            cat_id = menu_admin.add_category(conn, str(form.get("name", "")))
        except menu_admin.MenuError as exc:
            return _menu_error(request, exc)
        return _menu_done(request, conn, user, "category.add", cat_id, "Category added.", f"cat-{cat_id}")

    @app.post("/admin/menu/categories/{cat_id}")
    async def menu_edit_category(cat_id: str, request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        form = await request.form()
        action = str(form.get("action", "rename"))
        try:
            if action == "delete":
                menu_admin.delete_category(conn, cat_id)
                return _menu_done(request, conn, user, "category.delete", cat_id, "Category deleted.")
            if action in ("up", "down"):
                menu_admin.move_category(conn, cat_id, -1 if action == "up" else 1)
                return _menu_done(request, conn, user, "category.move", cat_id, "Category moved.", f"cat-{cat_id}")
            menu_admin.rename_category(conn, cat_id, str(form.get("name", "")))
        except menu_admin.MenuError as exc:
            return _menu_error(request, exc)
        return _menu_done(request, conn, user, "category.rename", cat_id, "Category renamed.", f"cat-{cat_id}")

    @app.post("/admin/menu/items")
    async def menu_add_item(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        form = await request.form()
        try:
            item_id = menu_admin.add_item(conn, str(form.get("category", "")), str(form.get("name", "")),
                                          str(form.get("price", "")), str(form.get("unit_label", "each")))
        except menu_admin.MenuError as exc:
            return _menu_error(request, exc)
        return _menu_done(request, conn, user, "item.add", item_id, "Item added. Customers can order it now.", f"item-{item_id}")

    @app.post("/admin/menu/items/{item_id}")
    async def menu_edit_item(item_id: str, request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        form = await request.form()
        action = str(form.get("action", "save"))
        try:
            if action == "remove":
                name = menu_admin.remove_item(conn, item_id)
                return _menu_done(request, conn, user, "item.remove", item_id, f"Removed {name}.")
            old = conn.execute("SELECT category FROM menu_items WHERE id = ?", (item_id,)).fetchone()
            row = menu_admin.update_item(conn, item_id, {k: v for k, v in form.multi_items()})
        except menu_admin.MenuError as exc:
            return _menu_error(request, exc)
        if (old and old["category"] != row["category"]) or str(form.get("new_mod_name", "")).strip():
            # Layout changed (moved category or new add-on): reload the page.
            return _menu_done(request, conn, user, "item.save", item_id, "Saved.", f"item-{item_id}")
        auth.audit(conn, user.username, "menu.item.save", item_id, "ok")
        state = "shown to customers" if row["available"] else "hidden from customers"
        return result(request, True, f"Saved {row['name']}: {money(row['price_cents'])}, {state}.")

    @app.post("/admin/menu/reimport")
    async def menu_reimport(request: Request, conn=Depends(get_db), user=Depends(admin_user), s=Depends(runtime_settings)):
        from app.services.menu import seed_menu

        n = seed_menu(conn, s.menu_path)
        conn.execute("DELETE FROM runtime_markers WHERE key = ?", (menu_admin.EDITED_MARKER,))
        return _menu_done(request, conn, user, "reimport", str(s.menu_path), f"Re-imported {n} items from the menu file.")

    @app.post("/admin/integrations/twilio/connect")
    async def connect_twilio(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        return await _run_check(request, conn, user, "twilio", checks.connect_twilio, TW_PLAIN, TW_SECRET)

    @app.post("/admin/integrations/twilio/test-sms")
    async def test_sms(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        error = await _apply_form(request, conn, user, TW_PLAIN, TW_SECRET, "twilio", keep_blank=True)
        if error:
            return result(request, False, error)
        s = runtime.effective_settings(conn, get_settings_for(request), request.app.state.secret_store)
        if not s.owner_phone_numbers:
            return result(request, False, "Add at least one owner phone number first.")
        steps = []
        for phone in s.owner_phone_numbers:
            try:
                await twilio.send_sms(s, phone, "Town Donut bot: test text. Owner alerts will come from this number.",
                                      http=request.app.state.http)
                steps.append((phone, True, "sent"))
            except twilio.SMSError as exc:
                steps.append((phone, False, str(exc)))
        auth.audit(conn, user.username, "twilio.test_sms", str(len(steps)), "ok" if all(ok for _, ok, _ in steps) else "failed")
        ok = all(ok for _, ok, _ in steps)
        return result(request, ok, "Test text sent. Check the owner phones." if ok else "Some texts didn't send.", steps)

    # ---- orders and customers
    @app.post("/admin/orders/{order_id}/status")
    async def order_status(order_id: int, request: Request, conn=Depends(get_db), user=Depends(admin_user),
                           s=Depends(runtime_settings)):
        target = str((await request.form()).get("target", ""))
        allowed = {"ACCEPTED", "REJECTED", "READY", "COMPLETED", "NO_SHOW"}
        if target not in allowed:
            return result(request, False, "Unknown action.")
        ok, msg = owner_commands.apply_status(conn, s, load_business(s.business_path), order_id, OrderStatus(target),
                                              f"portal:{user.username}", utcnow())
        auth.audit(conn, user.username, f"order.{target.lower()}", str(order_id), "ok" if ok else "refused")
        if not ok:
            return result(request, False, msg)
        back = request.headers.get("hx-current-url") or "/admin/orders"
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": back})
        return RedirectResponse(back, status_code=303)

    @app.get("/admin/customers")
    def customers_page(request: Request, conn=Depends(get_db), user=Depends(current_user)):
        return page(request, "customers.html", user, rows=customers.list_customers(conn))

    @app.get("/admin/customers/{customer_id}")
    def customer_page(customer_id: int, request: Request, conn=Depends(get_db), user=Depends(current_user)):
        c = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if c is None:
            return RedirectResponse("/admin/customers", status_code=303)
        conv = conn.execute("SELECT * FROM conversations WHERE customer_id = ?", (customer_id,)).fetchone()
        orders = conn.execute(
            "SELECT * FROM orders WHERE customer_id = ? OR (customer_phone IS NOT NULL AND customer_phone = ?) ORDER BY id DESC",
            (customer_id, c["phone"]),
        ).fetchall()
        msgs = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 40",
            (conv["id"],),
        ).fetchall() if conv else []
        return page(request, "customer.html", user, c=c, conv=conv, orders=orders, msgs=list(reversed(msgs)),
                    history=customers.pickup_history(conn, customer_id, c["phone"]))

    @app.post("/admin/customers/{customer_id}/bot")
    async def customer_bot(customer_id: int, request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        on = str((await request.form()).get("enabled", "")) == "1"
        conv_svc.set_bot_enabled(conn, customer_id, on, reason=None if on else f"paused by {user.username} in the portal")
        auth.audit(conn, user.username, "customer.bot." + ("resume" if on else "pause"), str(customer_id), "ok")
        target = f"/admin/customers/{customer_id}"
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    # ---- business
    @app.get("/admin/business")
    def business(request: Request, conn=Depends(get_db), user=Depends(current_user), s=Depends(runtime_settings)):
        items = conn.execute("SELECT * FROM menu_items WHERE available = 1 AND archived = 0").fetchall()
        return page(request, "business.html", user, s=s, items=items, biz=load_business(s.business_path))

    @app.post("/admin/business")
    async def save_business(request: Request, conn=Depends(get_db), user=Depends(admin_user)):
        form = await request.form()
        values = {
            "owner_phone_numbers": str(form.get("owner_phone_numbers", "")),
            "pending_order_expiry_minutes": form.get("pending_order_expiry_minutes", "0"),
            "customer_can_cancel_accepted": form.get("customer_can_cancel_accepted") == "on",
            "pause_bot_on_staff_reply": form.get("pause_bot_on_staff_reply") == "on",
        }
        try:
            values["large_order_threshold_cents"] = round(float(form.get("large_order_threshold", "0")) * 100)
            values["tax_rate_bps"] = round(float(form.get("tax_rate_percent", "0")) * 100)
            runtime.set_values(conn, values, user.username)
        except (ValueError, ValidationError) as exc:
            msg = "; ".join(e["msg"] for e in exc.errors()) if isinstance(exc, ValidationError) else "Use numbers only."
            return result(request, False, "Please check: " + msg)
        auth.audit(conn, user.username, "settings.save.business", ",".join(values), "ok")
        return result(request, True, "Saved. It takes effect on the next order.")

    # ---- orders and audit
    @app.get("/admin/orders")
    def orders(request: Request, conn=Depends(get_db), user=Depends(current_user)):
        rows = conn.execute(
            """SELECT o.id, o.customer_id, o.customer_name, o.customer_phone, o.pickup_time, o.total_cents, o.subtotal_cents,
                      o.status, o.needs_review, o.created_at, o.pickup_check_sent_at,
                      (SELECT COUNT(*) FROM orders p WHERE p.id != o.id AND p.status = 'NO_SHOW'
                         AND (p.customer_id = o.customer_id OR (o.customer_phone IS NOT NULL AND p.customer_phone = o.customer_phone))) AS no_shows
               FROM orders o ORDER BY o.id DESC LIMIT 100"""
        ).fetchall()
        return page(request, "orders.html", user, orders=rows, now_db=to_db(utcnow()))

    @app.get("/admin/audit")
    def audit_log(request: Request, conn=Depends(get_db), user=Depends(current_user)):
        rows = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 200").fetchall()
        return page(request, "audit.html", user, events=rows)

    return app


app = create_admin_app()
