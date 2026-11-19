import json
from pathlib import Path

from aiohttp import web
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import get_db, init_db
from app.services import (
    apply_contract,
    catalog_progress,
    create_cleaning_job,
    cutover_state,
    field_detail,
    get_strategy,
    list_cleaning_jobs,
    list_contracts,
    list_progress_checks,
    list_reports,
    live_guard,
    rebind_cleaning_job,
    run_breaking_check,
    run_progress_checks,
    run_reconcile,
    set_strategy,
    strategy_label,
    write_cleaning,
)
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-schema-session")


def create_app(db_path: str | None = None) -> web.Application:
    if db_path:
        import os

        os.environ["DATABASE_PATH"] = db_path
    init_db(db_path)
    app = web.Application()
    app["db_path"] = db_path
    app.router.add_static("/static/", STATIC)
    app.router.add_get("/", root)
    app.router.add_get("/login", login_page)
    app.router.add_get("/app/", spa_page)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/meta", api_meta)
    app.router.add_get("/api/contracts", api_contracts)
    app.router.add_get("/api/contracts/{cid}", api_contract_detail)
    app.router.add_get("/api/contracts/{cid}/fields", api_field_detail)
    app.router.add_get("/api/contracts/{cid}/cutover-card", api_cutover_card)
    app.router.add_post("/api/contracts/{cid}/breaking-check", api_breaking)
    app.router.add_post("/api/contracts/{cid}/apply", api_apply)
    app.router.add_get("/api/live-guard", api_live_guard)
    app.router.add_get("/api/catalog", api_catalog)
    app.router.add_get("/api/cleaning-jobs", api_cleaning_jobs)
    app.router.add_post("/api/cleaning-jobs", api_create_cleaning_job)
    app.router.add_post("/api/cleaning-jobs/{jid}/rebind", api_rebind_cleaning_job)
    app.router.add_post("/api/cleaning-jobs/{jid}/write", api_cleaning_write)
    app.router.add_get("/api/progress-checks", api_progress_checks)
    app.router.add_post("/api/progress-checks", api_run_progress_checks)
    app.router.add_get("/api/cutover-strategy", api_strategy)
    app.router.add_post("/api/cutover-strategy", api_set_strategy)
    app.router.add_get("/api/reconcile-reports", api_reports)
    app.router.add_post("/api/reconcile-reports", api_reconcile)
    return app


def _session(request: web.Request) -> dict:
    raw = request.cookies.get("schema_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: web.Request) -> str:
    user = _session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_resp(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
    )


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def root(request: web.Request):
    if _session(request).get("user") == Config.ADMIN_USER:
        raise web.HTTPFound("/app/")
    raise web.HTTPFound("/login")


async def login_page(_request: web.Request):
    return web.FileResponse(STATIC / "login.html")


async def spa_page(request: web.Request):
    if _session(request).get("user") != Config.ADMIN_USER:
        raise web.HTTPFound("/login")
    return web.FileResponse(STATIC / "index.html")


async def api_login(request: web.Request):
    body = await _body(request)
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_resp({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_resp({"ok": True, "user": user})
    resp.set_cookie(
        "schema_session",
        serializer.dumps({"user": user}),
        httponly=True,
        path="/",
    )
    return resp


async def api_logout(_request: web.Request):
    resp = json_resp({"ok": True})
    resp.del_cookie("schema_session", path="/")
    return resp


async def api_meta(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    db_path = request.app.get("db_path")
    guard = live_guard(db_path)
    with get_db(db_path) as conn:
        strategy = get_strategy(conn)
    return json_resp(
        {
            "lab": Config.LAB_NAME,
            "product": "cc-schema-lab",
            "strategy": strategy,
            "guard": guard,
        }
    )


async def api_contracts(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp({"items": list_contracts(request.app.get("db_path"))})


async def api_contract_detail(request: web.Request):
    try:
        require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"card": cutover_state(cid, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    except ValueError:
        return json_resp({"error": "契约编号不合法"}, 400)


async def api_field_detail(request: web.Request):
    try:
        require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"detail": field_detail(cid, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    except ValueError:
        return json_resp({"error": "契约编号不合法"}, 400)


async def api_cutover_card(request: web.Request):
    try:
        require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"card": cutover_state(cid, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    except ValueError:
        return json_resp({"error": "契约编号不合法"}, 400)


async def api_breaking(request: web.Request):
    try:
        require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"check": run_breaking_check(cid, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    except ValueError:
        return json_resp({"error": "契约编号不合法"}, 400)


async def api_apply(request: web.Request):
    try:
        require_user(request)
        cid = int(request.match_info["cid"])
        body = await _body(request)
        strategy = body.get("strategy") or None
        expected = body.get("expected_live_version")
        expected_live = int(expected) if expected not in (None, "") else None
        result = apply_contract(
            cid,
            request.app.get("db_path"),
            strategy=strategy,
            expected_live_version=expected_live,
        )
        return json_resp(
            {
                "contract": result["contract"],
                "strategy": result["strategy"],
                "strategy_label": result["strategy_label"],
                "from_version": result["from_version"],
                "to_version": result["to_version"],
                "read_version": result["read_version"],
                "write_versions": result["write_versions"],
                "checks": result["checks"],
            }
        )
    except ServiceError as exc:
        return json_resp(
            {
                "error": exc.message,
                "failure_reason": exc.message,
            },
            exc.status_code,
        )
    except ValueError:
        return json_resp({"error": "契约编号不合法"}, 400)


async def api_live_guard(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp({"guard": live_guard(request.app.get("db_path"))})


async def api_catalog(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp({"catalog": catalog_progress(request.app.get("db_path"))})


async def api_cleaning_jobs(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp({"items": list_cleaning_jobs(request.app.get("db_path"))})


async def api_create_cleaning_job(request: web.Request):
    try:
        require_user(request)
        body = await _body(request)
        job = create_cleaning_job(
            body.get("job_name", ""),
            body.get("table_name", ""),
            body.get("columns") or [],
            request.app.get("db_path"),
        )
        return json_resp({"job": job})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_rebind_cleaning_job(request: web.Request):
    try:
        require_user(request)
        jid = int(request.match_info["jid"])
        body = await _body(request)
        job = rebind_cleaning_job(jid, body.get("columns") or [], request.app.get("db_path"))
        return json_resp({"job": job})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    except ValueError:
        return json_resp({"error": "作业编号不合法"}, 400)


async def api_cleaning_write(request: web.Request):
    try:
        require_user(request)
        jid = int(request.match_info["jid"])
        body = await _body(request)
        return json_resp(
            {"write": write_cleaning(jid, body.get("columns"), request.app.get("db_path"))}
        )
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    except ValueError:
        return json_resp({"error": "作业编号不合法"}, 400)


async def api_progress_checks(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    table = request.query.get("table_name") or None
    return json_resp({"items": list_progress_checks(table, request.app.get("db_path"))})


async def api_run_progress_checks(request: web.Request):
    try:
        require_user(request)
        body = await _body(request)
        scope = str(body.get("scope") or "both")
        table = body.get("table_name") or None
        return json_resp(
            {"run": run_progress_checks(scope, table, request.app.get("db_path"))}
        )
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_strategy(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    with get_db(request.app.get("db_path")) as conn:
        key = get_strategy(conn)
    return json_resp({"strategy": key, "strategy_label": strategy_label(key)})


async def api_set_strategy(request: web.Request):
    try:
        require_user(request)
        body = await _body(request)
        with get_db(request.app.get("db_path")) as conn:
            key = set_strategy(conn, body.get("strategy", ""))
        return json_resp({"strategy": key, "strategy_label": strategy_label(key)})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_reports(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp({"items": list_reports(request.app.get("db_path"))})


async def api_reconcile(request: web.Request):
    try:
        require_user(request)
        body = await _body(request)
        as_of = str(body.get("as_of") or "").strip() or "2026-11-18"
        return json_resp({"report": run_reconcile(as_of, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
