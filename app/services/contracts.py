import json

from app.db import get_db, immediate_tx
from app.services.catalog import (
    catalog_column_map,
    contract_fields,
    get_strategy,
    live_contracts,
    mode_key,
    next_run_id,
    normalize_strategy,
    persist_alignment,
    refresh_cleaning_jobs,
    strategy_label,
)
from app.services.errors import ServiceError


def _last_apply(conn, table_name: str, contract_id: int | None = None) -> dict | None:
    if contract_id is None:
        row = conn.execute(
            "SELECT * FROM apply_runs WHERE table_name=? ORDER BY id DESC LIMIT 1",
            (table_name,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM apply_runs WHERE table_name=? AND contract_id=? "
            "ORDER BY id DESC LIMIT 1",
            (table_name, contract_id),
        ).fetchone()
    return dict(row) if row else None


def _catalog_row(conn, table_name: str):
    return conn.execute(
        "SELECT * FROM catalog_tables WHERE table_name=?", (table_name,)
    ).fetchone()


def list_contracts(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM contract_fields f WHERE f.contract_id=c.id) AS field_count "
            "FROM schema_contracts c ORDER BY c.table_name, c.version_no"
        ).fetchall()
        items = []
        cache: dict[str, dict] = {}
        for row in rows:
            item = dict(row)
            table = item["table_name"]
            if table not in cache:
                lives = live_contracts(conn, table)
                cat = _catalog_row(conn, table)
                cache[table] = {
                    "live_versions": [r["version_no"] for r in lives],
                    "live_version": lives[0]["version_no"] if len(lives) == 1 else None,
                    "duplicate_live": len(lives) > 1,
                    "catalog_live_version": cat["live_version"] if cat else None,
                    "read_version": cat["read_version"] if cat else None,
                    "cutover_mode": cat["cutover_mode"] if cat else "初始",
                }
            item.update(cache[table])
            item["last_apply"] = _last_apply(conn, table, item["id"])
            item["fields"] = [
                dict(f)
                for f in conn.execute(
                    "SELECT field_name, field_type FROM contract_fields "
                    "WHERE contract_id=? ORDER BY id",
                    (item["id"],),
                ).fetchall()
            ]
            items.append(item)
        return items


def run_breaking_check(contract_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可做破坏性检查", 409)
        lives = live_contracts(conn, draft["table_name"])
        if len(lives) > 1:
            raise ServiceError(
                f"表 {draft['table_name']} 出现双现行契约（"
                + "、".join(f"v{r['version_no']}" for r in lives)
                + "），已停机；请先人工清点现行。",
                409,
            )
        if not lives:
            raise ServiceError("无现行契约可对照", 409)
        live = lives[0]
        old_f = contract_fields(conn, live["id"])
        new_f = contract_fields(conn, draft["id"])
        removed = sorted(set(old_f) - set(new_f))
        type_changed = sorted(
            k for k in set(old_f) & set(new_f) if old_f[k] != new_f[k]
        )
        added = sorted(set(new_f) - set(old_f))
        breaking = bool(removed or type_changed)
        return {
            "contract_id": contract_id,
            "table_name": draft["table_name"],
            "from_version": live["version_no"],
            "to_version": draft["version_no"],
            "removed_fields": removed,
            "type_changed_fields": type_changed,
            "added_fields": added,
            "breaking": breaking,
            "verdict": "破坏性" if breaking else "兼容",
        }


def _upsert_column(
    conn, table_name: str, name: str, ftype: str, state: str, version: int
) -> None:
    row = conn.execute(
        "SELECT id FROM catalog_columns WHERE table_name=? AND column_name=?",
        (table_name, name),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE catalog_columns SET column_type=?, write_state=? WHERE id=?",
            (ftype, state, row["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO catalog_columns"
            "(table_name, column_name, column_type, added_version, write_state) "
            "VALUES (?,?,?,?,?)",
            (table_name, name, ftype, version, state),
        )


def _record_failure(
    contract_id: int,
    table_name: str,
    from_version: int | None,
    to_version: int,
    reason: str,
    strategy: str,
    db_path: str | None,
) -> None:
    try:
        with get_db(db_path) as conn:
            conn.execute(
                "INSERT INTO apply_runs"
                "(contract_id, table_name, from_version, to_version, result, strategy, failure_reason) "
                "VALUES (?,?,?,?,?,?,?)",
                (contract_id, table_name, from_version, to_version, "失败", strategy, reason),
            )
    except Exception:
        pass


def _resolve_strategy(conn, cat, requested: str | None) -> str:
    wanted = normalize_strategy(requested)
    current = mode_key(cat["cutover_mode"]) if cat else ""
    if wanted and current and wanted != current:
        raise ServiceError(
            f"表 {cat['table_name']} 已经选过「{cat['cutover_mode']}」这条命运，"
            "不能再改成另一条；两种落点不要并存。",
            409,
        )
    if wanted:
        return wanted
    if current:
        return current
    return get_strategy(conn)


def apply_contract(
    contract_id: int,
    db_path: str | None = None,
    strategy: str | None = None,
    expected_live_version: int | None = None,
) -> dict:
    draft = None
    chosen_label = ""
    from_ver: int | None = None
    table = ""
    try:
        with immediate_tx(db_path) as conn:
            draft = conn.execute(
                "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
            ).fetchone()
            if not draft:
                raise ServiceError("契约不存在", 404)
            table = draft["table_name"]
            if draft["status"] != "草稿":
                raise ServiceError(
                    "只有草稿能换版应用；这张契约已经是现行或已停用，刷新页面看最新版号。",
                    409,
                )

            lives = live_contracts(conn, table)
            if len(lives) > 1:
                raise ServiceError(
                    f"表 {table} 出现双现行契约（"
                    + "、".join(f"v{r['version_no']}" for r in lives)
                    + "）。版号与目录现行必须同源，双现行立刻停；本次未改库。",
                    409,
                )
            live = lives[0] if lives else None
            from_ver = live["version_no"] if live else None

            if expected_live_version is not None and expected_live_version != from_ver:
                current = f"v{from_ver}" if from_ver is not None else "没有现行契约"
                raise ServiceError(
                    f"现行指针已被另一名值班改动：页面给的是 v{expected_live_version}，"
                    f"库里现在是{current}。本次未改库，请刷新后再切。",
                    409,
                )

            cat = _catalog_row(conn, table)
            key = _resolve_strategy(conn, cat, strategy)
            chosen_label = strategy_label(key)
            old_f = contract_fields(conn, live["id"]) if live else {}
            new_f = contract_fields(conn, draft["id"])
            removed = sorted(set(old_f) - set(new_f))
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('cutover_strategy', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key,),
            )

            write_versions = [draft["version_no"]]
            for name, ftype in new_f.items():
                _upsert_column(conn, table, name, ftype, "现行", draft["version_no"])
            if key == "lock":
                for name in removed:
                    _upsert_column(
                        conn, table, name, old_f[name], "已停写", draft["version_no"]
                    )
            else:
                for name in removed:
                    _upsert_column(
                        conn, table, name, old_f[name], "双写", draft["version_no"]
                    )
                if from_ver is not None and from_ver != draft["version_no"]:
                    write_versions = sorted({from_ver, draft["version_no"]})

            if live:
                conn.execute(
                    "UPDATE schema_contracts SET status='停用' WHERE id=?", (live["id"],)
                )
            conn.execute(
                "UPDATE schema_contracts SET status='现行' WHERE id=?", (contract_id,)
            )

            if cat:
                conn.execute(
                    "UPDATE catalog_tables SET live_version=?, read_version=?, "
                    "write_versions=?, cutover_mode=?, registered_at=datetime('now') "
                    "WHERE id=?",
                    (
                        draft["version_no"],
                        draft["version_no"],
                        json.dumps(write_versions),
                        chosen_label,
                        cat["id"],
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO catalog_tables"
                    "(table_name, live_version, read_version, write_versions, cutover_mode) "
                    "VALUES (?,?,?,?,?)",
                    (
                        table,
                        draft["version_no"],
                        draft["version_no"],
                        json.dumps(write_versions),
                        chosen_label,
                    ),
                )

            conn.execute(
                "INSERT INTO apply_runs"
                "(contract_id, table_name, from_version, to_version, result, strategy) "
                "VALUES (?,?,?,?,?,?)",
                (contract_id, table, from_ver, draft["version_no"], "已应用", chosen_label),
            )

            run_id = next_run_id(conn)
            checks = persist_alignment(conn, run_id, table)
            if not checks["passed"]:
                raise ServiceError(
                    "切流已回滚：改库后清洗 / 目录两轮列名对齐复跑未通过——"
                    + "；".join(
                        f"{f['scope']}·{f['table_name']}：{f['detail']}"
                        for f in checks["failures"]
                    ),
                    409,
                )

            contract = dict(
                conn.execute(
                    "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
                ).fetchone()
            )
            return {
                "contract": contract,
                "strategy": key,
                "strategy_label": chosen_label,
                "from_version": from_ver,
                "to_version": draft["version_no"],
                "read_version": draft["version_no"],
                "write_versions": write_versions,
                "checks": checks,
            }
    except ServiceError as exc:
        if draft is not None and draft["status"] == "草稿":
            _record_failure(
                contract_id,
                table,
                from_ver,
                draft["version_no"],
                exc.message,
                chosen_label,
                db_path,
            )
        exc.failure_reason = exc.message
        raise


def field_detail(contract_id: int, db_path: str | None = None) -> dict:
    """字段详情：这一版每个字段在现行 / 目录 / 清洗作业里的落点。"""
    with get_db(db_path) as conn:
        contract = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not contract:
            raise ServiceError("契约不存在", 404)
        table = contract["table_name"]
        lives = live_contracts(conn, table)
        cat = _catalog_row(conn, table)
        live = lives[0] if len(lives) == 1 else None
        live_fields = contract_fields(conn, live["id"]) if live else {}
        fields = contract_fields(conn, contract_id)
        cols = catalog_column_map(conn, table)
        refresh_cleaning_jobs(conn, table)
        jobs = [
            {
                "job_name": r["job_name"],
                "columns": json.loads(r["columns_json"] or "[]"),
                "status": r["status"],
            }
            for r in conn.execute(
                "SELECT * FROM cleaning_jobs WHERE table_name=? ORDER BY id", (table,)
            ).fetchall()
        ]
        items = []
        for name, ftype in fields.items():
            col = cols.get(name)
            items.append(
                {
                    "field_name": name,
                    "field_type": ftype,
                    "in_live": name in live_fields,
                    "live_type": live_fields.get(name),
                    "catalog_state": col["write_state"] if col else "未登记",
                    "catalog_type": col["column_type"] if col else None,
                    "referenced_by": [
                        j["job_name"] for j in jobs if name in j["columns"]
                    ],
                    "note": (
                        "现行字段"
                        if name in live_fields
                        else "不在现行契约内"
                        if live
                        else "无现行契约"
                    ),
                }
            )
        return {
            "contract": dict(contract),
            "fields": items,
            "live_versions": [r["version_no"] for r in lives],
            "live_version": live["version_no"] if live else None,
            "duplicate_live": len(lives) > 1,
            "catalog_live_version": cat["live_version"] if cat else None,
            "read_version": cat["read_version"] if cat else None,
            "cutover_mode": cat["cutover_mode"] if cat else "初始",
            "cleaning_jobs": jobs,
        }


def cutover_state(contract_id: int, db_path: str | None = None) -> dict:
    """换版卡片：草稿、对照现行、目录读指针、最近一次失败因。"""
    with get_db(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        table = draft["table_name"]
        lives = live_contracts(conn, table)
        cat = _catalog_row(conn, table)
        last = _last_apply(conn, table, contract_id)
        try:
            check = run_breaking_check(contract_id, db_path)
        except ServiceError as exc:
            check = {"error": exc.message}
        refresh_cleaning_jobs(conn, table)
        cleaning = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM cleaning_jobs WHERE table_name=? ORDER BY id", (table,)
            ).fetchall()
        ]
        jobs = []
        for job in cleaning:
            job["columns"] = json.loads(job.pop("columns_json") or "[]")
            jobs.append(job)
        write_versions = json.loads(cat["write_versions"] or "[]") if cat else []
        return {
            "contract": dict(draft),
            "fields": [
                dict(r)
                for r in conn.execute(
                    "SELECT field_name, field_type FROM contract_fields "
                    "WHERE contract_id=? ORDER BY id",
                    (contract_id,),
                ).fetchall()
            ],
            "live_versions": [r["version_no"] for r in lives],
            "live_version": lives[0]["version_no"] if len(lives) == 1 else None,
            "duplicate_live": len(lives) > 1,
            "catalog_live_version": cat["live_version"] if cat else None,
            "read_version": cat["read_version"] if cat else None,
            "write_versions": write_versions,
            "cutover_mode": cat["cutover_mode"] if cat else "初始",
            "catalog_columns": list(catalog_column_map(conn, table).values()),
            "cleaning_jobs": jobs,
            "breaking_check": check,
            "last_apply": last,
            "strategy_default": get_strategy(conn),
            "strategy_locked": bool(mode_key(cat["cutover_mode"])) if cat else False,
        }
