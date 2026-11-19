import json

from app.db import get_db
from app.services.errors import ServiceError

STRATEGIES = {"lock": "锁表切流", "dual": "旁路双写"}
WRITABLE_STATES = ("现行", "双写")
JOB_ALIGNED = "已对齐"
JOB_DUAL = "双写过渡"
JOB_STOPPED = "旧列停写"
JOB_UNKNOWN = "未知列名"
JOB_HALTED = "双现行停机"


def strategy_label(key: str) -> str:
    return STRATEGIES.get(key, "初始")


def mode_key(label: str | None) -> str:
    if not label:
        return ""
    for key, text in STRATEGIES.items():
        if text == label:
            return key
    return ""


def normalize_strategy(value) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if text in STRATEGIES:
        return text
    for key, label in STRATEGIES.items():
        if text == label:
            return key
    raise ServiceError("切流方式只能二选一：锁表切流 / 旁路双写", 400)


def get_strategy(conn) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key='cutover_strategy'").fetchone()
    key = row["value"] if row else "lock"
    return key if key in STRATEGIES else "lock"


def set_strategy(conn, key: str) -> str:
    key = normalize_strategy(key) or "lock"
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('cutover_strategy', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key,),
    )
    return key


def contract_fields(conn, contract_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT field_name, field_type FROM contract_fields WHERE contract_id=?",
        (contract_id,),
    ).fetchall()
    return {r["field_name"]: r["field_type"] for r in rows}


def live_contracts(conn, table_name: str) -> list:
    return conn.execute(
        "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行' "
        "ORDER BY version_no",
        (table_name,),
    ).fetchall()


def catalog_column_map(conn, table_name: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT * FROM catalog_columns WHERE table_name=?", (table_name,)
    ).fetchall()
    return {r["column_name"]: dict(r) for r in rows}


def live_guard(db_path: str | None = None) -> dict:
    """页上 / 本地库都能核对的「现行是否唯一」。"""
    with get_db(db_path) as conn:
        names = [
            r["table_name"]
            for r in conn.execute(
                "SELECT table_name FROM schema_contracts "
                "UNION SELECT table_name FROM catalog_tables ORDER BY table_name"
            )
        ]
        tables = []
        duplicates = []
        for name in names:
            lives = live_contracts(conn, name)
            versions = [r["version_no"] for r in lives]
            item = {
                "table_name": name,
                "live_versions": versions,
                "live_count": len(versions),
                "unique": len(versions) <= 1,
                "has_live": bool(versions),
            }
            if len(versions) > 1:
                duplicates.append(item)
            tables.append(item)
        return {
            "ok": not duplicates,
            "halted": bool(duplicates),
            "tables": tables,
            "duplicates": duplicates,
            "message": (
                "现行唯一，可继续切流"
                if not duplicates
                else "发现双现行，切流已停机；请先人工清点现行契约"
            ),
        }


def _field_states(
    conn, table_name: str, columns: list[str]
) -> tuple[list[str], list[str]]:
    catalog = catalog_column_map(conn, table_name)
    unknown = [c for c in columns if c not in catalog]
    stopped = [
        c for c in columns if c in catalog and catalog[c]["write_state"] == "已停写"
    ]
    return unknown, stopped


def _classify_job(conn, table: str, columns: list[str]) -> dict:
    lives = live_contracts(conn, table)
    live = lives[0] if len(lives) == 1 else None
    live_version = live["version_no"] if live else None
    live_fields = contract_fields(conn, live["id"]) if live else {}
    cat = conn.execute(
        "SELECT cutover_mode FROM catalog_tables WHERE table_name=?", (table,)
    ).fetchone()
    mode = cat["cutover_mode"] if cat else "初始"
    unknown, stopped = _field_states(conn, table, columns)
    catalog = catalog_column_map(conn, table)
    non_live = [c for c in columns if c in catalog and c not in live_fields]
    non_live_writable = [
        c for c in non_live if catalog[c]["write_state"] in WRITABLE_STATES
    ]
    dual_only = bool(non_live) and len(non_live_writable) == len(non_live)

    if len(lives) > 1:
        status = JOB_HALTED
        detail = (
            f"表 {table} 出现双现行契约（"
            + "、".join(f"v{r['version_no']}" for r in lives)
            + "），清洗对齐已停机；请先人工清点现行。"
        )
    elif not lives:
        status = JOB_UNKNOWN
        detail = f"表 {table} 没有现行契约，清洗作业无从对齐。"
    elif unknown:
        status = JOB_UNKNOWN
        detail = (
            f"列 {'、'.join(unknown)} 在目录里查无此名（未知列名），"
            f"清洗落库会撞未知列名；请改绑到现行 v{live_version} 的列。"
        )
    elif stopped:
        status = JOB_STOPPED
        detail = (
            f"列 {'、'.join(stopped)} 已停写（锁表切流后只认现行 v{live_version} 的字段），"
            "列名仍在目录里可查，但落库会被整笔拒绝；请改绑列名后再跑。"
        )
    elif non_live and not dual_only:
        status = JOB_STOPPED
        detail = (
            f"列 {'、'.join(non_live)} 不在现行契约 v{live_version} 内，"
            "落库会被拒；请改绑到现行字段。"
        )
    elif non_live and dual_only:
        status = JOB_DUAL
        detail = (
            f"列 {'、'.join(non_live)} 处于双写过渡（新旧列都可写），"
            f"读指针已切到现行 v{live_version}；双写窗口收口前仍可落库。"
        )
    else:
        status = JOB_ALIGNED
        detail = f"列名与现行契约 v{live_version} 一致。"

    return {
        "status": status,
        "detail": detail,
        "live": live,
        "live_version": live_version,
        "live_fields": live_fields,
        "mode": mode,
        "unknown": unknown,
        "stopped": stopped,
        "non_live": non_live,
        "dual_only": dual_only,
    }


def refresh_cleaning_jobs(conn, table_name: str) -> list[dict]:
    out = []
    for row in conn.execute(
        "SELECT * FROM cleaning_jobs WHERE table_name=? ORDER BY id", (table_name,)
    ).fetchall():
        job = dict(row)
        columns = json.loads(job["columns_json"] or "[]")
        info = _classify_job(conn, table_name, columns)
        bound = (
            info["live_version"]
            if info["status"] in (JOB_ALIGNED, JOB_DUAL)
            else job["bound_version"]
        )
        conn.execute(
            "UPDATE cleaning_jobs SET status=?, last_detail=?, bound_version=?, "
            "last_aligned_at=CASE WHEN ?=? THEN datetime('now') ELSE last_aligned_at END "
            "WHERE id=?",
            (
                info["status"],
                info["detail"],
                bound,
                info["status"],
                JOB_ALIGNED,
                job["id"],
            ),
        )
        job.update(
            {
                "status": info["status"],
                "last_detail": info["detail"],
                "bound_version": bound,
                "columns": columns,
                "unknown_columns": info["unknown"],
                "stopped_columns": info["stopped"],
                "cutover_mode": info["mode"],
            }
        )
        job.pop("columns_json", None)
        out.append(job)
    return out


def evaluate_cleaning(conn, table_name: str | None = None) -> list[dict]:
    """清洗列名对齐：作业引用的每个列名都要能在目录里落位。

    「未知列名」才是硬失败——落库会撞上不存在的列。
    锁表切流把旧列登记为「已停写」，名字仍然认识，只是拒写；
    这类作业算对齐通过，同时带一条改绑提醒。
    """
    if table_name:
        names = [table_name]
    else:
        names = [
            r["table_name"]
            for r in conn.execute(
                "SELECT DISTINCT table_name FROM cleaning_jobs ORDER BY table_name"
            )
        ]
    results = []
    for name in names:
        for row in conn.execute(
            "SELECT * FROM cleaning_jobs WHERE table_name=? ORDER BY id", (name,)
        ).fetchall():
            job = dict(row)
            columns = json.loads(job["columns_json"] or "[]")
            info = _classify_job(conn, name, columns)
            passed = info["status"] in (JOB_ALIGNED, JOB_DUAL, JOB_STOPPED)
            warning = ""
            if passed and info["status"] == JOB_STOPPED:
                warning = f"列名可落位，但 {'、'.join(info['stopped'] or info['non_live'])} 已停写，落库会被白话拒绝；请择机改绑。"
            results.append(
                {
                    "scope": "清洗",
                    "table_name": name,
                    "version_no": info["live_version"] or 0,
                    "job_id": job["id"],
                    "job_name": job["job_name"],
                    "status": info["status"],
                    "result": "通过" if passed else "未通过",
                    "detail": info["detail"],
                    "warning": warning,
                    "columns": columns,
                    "unknown_columns": info["unknown"],
                    "stopped_columns": info["stopped"],
                }
            )
    return results


def evaluate_catalog(conn, table_name: str | None = None) -> list[dict]:
    """目录列名对齐：契约版号、目录现行、读指针同源，且现行字段都登记可写。"""
    if table_name:
        names = [table_name]
    else:
        names = [
            r["table_name"]
            for r in conn.execute(
                "SELECT table_name FROM schema_contracts "
                "UNION SELECT table_name FROM catalog_tables ORDER BY table_name"
            )
        ]
    results = []
    for name in names:
        lives = live_contracts(conn, name)
        cat = conn.execute(
            "SELECT * FROM catalog_tables WHERE table_name=?", (name,)
        ).fetchone()
        version_no = lives[0]["version_no"] if len(lives) == 1 else 0
        problems: list[str] = []
        if not lives:
            problems.append("没有现行契约")
        elif len(lives) > 1:
            problems.append(
                "双现行（"
                + "、".join(f"v{r['version_no']}" for r in lives)
                + "），版号与目录现行不同源，必须立刻停"
            )
        else:
            live = lives[0]
            if not cat:
                problems.append(f"目录未登记此表（契约现行 v{live['version_no']}）")
            else:
                if cat["live_version"] != live["version_no"]:
                    problems.append(
                        f"契约现行 v{live['version_no']} / 目录现行 v{cat['live_version']} 不同源"
                    )
                if cat["read_version"] != live["version_no"]:
                    problems.append(
                        f"读指针 v{cat['read_version']} 未切到现行 v{live['version_no']}"
                    )
                if cat["read_version"] != cat["live_version"]:
                    problems.append(
                        f"目录现行 v{cat['live_version']} 与读指针 v{cat['read_version']} 不一致"
                    )
                try:
                    write_versions = json.loads(cat["write_versions"] or "[]")
                except ValueError:
                    write_versions = []
                if live["version_no"] not in write_versions:
                    problems.append(
                        f"写集 {write_versions} 未包含现行 v{live['version_no']}"
                    )
            fields = contract_fields(conn, live["id"])
            cols = catalog_column_map(conn, name)
            for fname, ftype in fields.items():
                col = cols.get(fname)
                if not col:
                    problems.append(f"现行字段 {fname} 未登记进目录")
                elif col["column_type"] != ftype:
                    problems.append(
                        f"字段 {fname} 类型 {col['column_type']} 与契约 {ftype} 不一致"
                    )
                elif col["write_state"] not in WRITABLE_STATES:
                    problems.append(f"现行字段 {fname} 当前不可写（{col['write_state']}）")
        result = "通过" if not problems else "未通过"
        detail = (
            f"契约现行 v{version_no} = 目录现行 v{version_no} = 读指针 v{version_no}，列名对齐。"
            if result == "通过"
            else "；".join(problems)
        )
        results.append(
            {
                "scope": "目录",
                "table_name": name,
                "version_no": version_no,
                "result": result,
                "detail": detail,
                "problems": problems,
            }
        )
    return results


def _persist(conn, scope: str, results: list[dict], run_id: int) -> None:
    for item in results:
        conn.execute(
            "INSERT INTO progress_checks"
            "(scope, table_name, version_no, run_id, result, detail) VALUES (?,?,?,?,?,?)",
            (
                scope,
                item["table_name"],
                item["version_no"],
                run_id,
                item["result"],
                item["detail"],
            ),
        )


def next_run_id(conn) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(run_id), 0) + 1 AS n FROM progress_checks"
    ).fetchone()
    return int(row["n"])


def persist_alignment(
    conn,
    run_id: int,
    table_name: str | None = None,
    gate_scopes: tuple[str, ...] = ("清洗", "目录"),
) -> dict:
    cleaning = evaluate_cleaning(conn, table_name)
    catalog = evaluate_catalog(conn, table_name)
    _persist(conn, "清洗", cleaning, run_id)
    _persist(conn, "目录", catalog, run_id)
    gated = {
        "清洗": cleaning if "清洗" in gate_scopes else [],
        "目录": catalog if "目录" in gate_scopes else [],
    }
    failures = [
        item
        for items in gated.values()
        for item in items
        if item["result"] != "通过"
    ]
    return {
        "run_id": run_id,
        "checks": {"清洗": cleaning, "目录": catalog},
        "gate_scopes": list(gate_scopes),
        "passed": not failures,
        "failures": failures,
    }


def run_progress_checks(
    scope: str = "both", table_name: str | None = None, db_path: str | None = None
) -> dict:
    scope = (scope or "both").strip()
    if scope not in ("both", "清洗", "目录"):
        raise ServiceError("对齐范围只支持 清洗 / 目录 / both", 400)
    with get_db(db_path) as conn:
        run_id = next_run_id(conn)
        payload = {"run_id": run_id, "checks": {}}
        if scope in ("both", "清洗"):
            cleaning = evaluate_cleaning(conn, table_name)
            _persist(conn, "清洗", cleaning, run_id)
            payload["checks"]["清洗"] = cleaning
        if scope in ("both", "目录"):
            catalog = evaluate_catalog(conn, table_name)
            _persist(conn, "目录", catalog, run_id)
            payload["checks"]["目录"] = catalog
        payload["passed"] = all(
            item["result"] == "通过"
            for items in payload["checks"].values()
            for item in items
        )
        payload["failures"] = [
            item
            for items in payload["checks"].values()
            for item in items
            if item["result"] != "通过"
        ]
        return payload


def list_progress_checks(
    table_name: str | None = None, db_path: str | None = None
) -> list[dict]:
    sql = "SELECT * FROM progress_checks"
    args: tuple = ()
    if table_name:
        sql += " WHERE table_name=?"
        args = (table_name,)
    sql += " ORDER BY id DESC LIMIT 200"
    with get_db(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def list_cleaning_jobs(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        tables = [
            r["table_name"]
            for r in conn.execute(
                "SELECT DISTINCT table_name FROM cleaning_jobs ORDER BY table_name"
            )
        ]
        jobs = []
        for name in tables:
            jobs.extend(refresh_cleaning_jobs(conn, name))
        return jobs


def _clean_columns(columns) -> list[str]:
    return [str(c).strip() for c in (columns or []) if str(c).strip()]


def _ensure_table_known(conn, table_name: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM schema_contracts WHERE table_name=? "
        "UNION SELECT 1 FROM catalog_tables WHERE table_name=? LIMIT 1",
        (table_name, table_name),
    ).fetchone()
    if not row:
        raise ServiceError(f"表 {table_name} 未在契约或目录登记，先登记再建清洗作业", 400)


def create_cleaning_job(
    job_name: str, table_name: str, columns: list[str], db_path: str | None = None
) -> dict:
    job_name = (job_name or "").strip()
    table_name = (table_name or "").strip()
    columns = _clean_columns(columns)
    if not job_name or not table_name or not columns:
        raise ServiceError("作业名、表名、列名都要填", 400)
    with get_db(db_path) as conn:
        _ensure_table_known(conn, table_name)
        if conn.execute(
            "SELECT id FROM cleaning_jobs WHERE job_name=?", (job_name,)
        ).fetchone():
            raise ServiceError("同名清洗作业已存在", 409)
        cur = conn.execute(
            "INSERT INTO cleaning_jobs"
            "(job_name, table_name, columns_json, status, last_detail) "
            "VALUES (?,?,?,?,?)",
            (job_name, table_name, json.dumps(columns, ensure_ascii=False), "未对齐", "新建待对齐"),
        )
        refresh_cleaning_jobs(conn, table_name)
        row = conn.execute(
            "SELECT * FROM cleaning_jobs WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        info = _classify_job(conn, table_name, columns)
        job = dict(row)
        job["columns"] = columns
        job["unknown_columns"] = info["unknown"]
        job["stopped_columns"] = info["stopped"]
        job.pop("columns_json", None)
        return job


def rebind_cleaning_job(
    job_id: int, columns: list[str], db_path: str | None = None
) -> dict:
    columns = _clean_columns(columns)
    if not columns:
        raise ServiceError("改绑至少给一列", 400)
    with get_db(db_path) as conn:
        job = conn.execute("SELECT * FROM cleaning_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise ServiceError("清洗作业不存在", 404)
        conn.execute(
            "UPDATE cleaning_jobs SET columns_json=? WHERE id=?",
            (json.dumps(columns, ensure_ascii=False), job_id),
        )
        refresh_cleaning_jobs(conn, job["table_name"])
        row = conn.execute("SELECT * FROM cleaning_jobs WHERE id=?", (job_id,)).fetchone()
        info = _classify_job(conn, job["table_name"], columns)
        out = dict(row)
        out["columns"] = columns
        out["unknown_columns"] = info["unknown"]
        out["stopped_columns"] = info["stopped"]
        out.pop("columns_json", None)
        return out


def write_cleaning(
    job_id: int, columns: list[str] | None = None, db_path: str | None = None
) -> dict:
    """模拟清洗作业按自己的列落库：要么整笔写入，要么被白话拒绝。"""
    with get_db(db_path) as conn:
        job = conn.execute("SELECT * FROM cleaning_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise ServiceError("清洗作业不存在", 404)
        table = job["table_name"]
        requested = _clean_columns(columns) or json.loads(job["columns_json"] or "[]")
        if not requested:
            raise ServiceError("没有要写入的列", 400)
        catalog = catalog_column_map(conn, table)
        lives = live_contracts(conn, table)
        live_version = lives[0]["version_no"] if len(lives) == 1 else None
        cat = conn.execute(
            "SELECT cutover_mode FROM catalog_tables WHERE table_name=?", (table,)
        ).fetchone()
        label = cat["cutover_mode"] if cat else "初始"
        key = mode_key(label)

        unknown = [c for c in requested if c not in catalog]
        stopped = [
            c for c in requested if c in catalog and catalog[c]["write_state"] == "已停写"
        ]
        if len(lives) > 1:
            unknown = []
            stopped = []
            detail = (
                f"清洗作业 {job['job_name']} 落库停机：表 {table} 出现双现行（"
                + "、".join(f"v{r['version_no']}" for r in lives)
                + "），现行指针不唯一，先人工清点。这一笔整笔回滚。"
            )
            stopped = requested
        elif unknown:
            detail = (
                f"清洗作业 {job['job_name']} 落库失败：列 {'、'.join(unknown)} 在目录里没有名字"
                "（未知列名）。这一笔整笔回滚，没有写入任何数据；请把作业改绑到现行契约的列。"
            )
        elif stopped:
            detail = (
                f"清洗作业 {job['job_name']} 落库被拒：列 {'、'.join(stopped)} "
                f"已停写（{strategy_label(key) or '锁表切流'}后只认现行 v{live_version} 的字段），"
                "旧列名不再接收写入。这一笔整笔回滚，请改绑列名后再写。"
            )
        else:
            detail = (
                f"清洗作业 {job['job_name']} 落库成功：写入 {len(requested)} 列，"
                f"现行 v{live_version}"
                + (f"，走{strategy_label(key)}（新旧列都可写）" if key == "dual" else "")
                + "。"
            )
        accepted = not unknown and not stopped and len(lives) <= 1
        persisted = requested if accepted else []
        conn.execute(
            "INSERT INTO cleaning_writes"
            "(job_id, table_name, requested_columns, persisted_columns, accepted, detail) "
            "VALUES (?,?,?,?,?,?)",
            (
                job_id,
                table,
                json.dumps(requested, ensure_ascii=False),
                json.dumps(persisted, ensure_ascii=False),
                1 if accepted else 0,
                detail,
            ),
        )
        refresh_cleaning_jobs(conn, table)
        return {
            "job_id": job_id,
            "job_name": job["job_name"],
            "table_name": table,
            "live_version": live_version,
            "strategy": key,
            "strategy_label": strategy_label(key) if key else "初始",
            "requested_columns": requested,
            "persisted_columns": persisted,
            "unknown_columns": unknown,
            "stopped_columns": stopped,
            "accepted": accepted,
            "detail": detail,
        }


def catalog_progress(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        strategy = get_strategy(conn)
        names = [
            r["table_name"]
            for r in conn.execute(
                "SELECT table_name FROM schema_contracts "
                "UNION SELECT table_name FROM catalog_tables ORDER BY table_name"
            )
        ]
        tables = []
        for name in names:
            cat = conn.execute(
                "SELECT * FROM catalog_tables WHERE table_name=?", (name,)
            ).fetchone()
            lives = live_contracts(conn, name)
            live = lives[0] if len(lives) == 1 else None
            live_fields = contract_fields(conn, live["id"]) if live else {}
            cols = catalog_column_map(conn, name)
            columns = []
            for col in sorted(cols.values(), key=lambda c: c["id"]):
                columns.append(
                    {
                        "column_name": col["column_name"],
                        "column_type": col["column_type"],
                        "write_state": col["write_state"],
                        "added_version": col["added_version"],
                        "in_live": col["column_name"] in live_fields,
                    }
                )
            known = set(cols)
            for fname, ftype in live_fields.items():
                if fname not in known:
                    columns.append(
                        {
                            "column_name": fname,
                            "column_type": ftype,
                            "write_state": "未登记",
                            "added_version": None,
                            "in_live": True,
                        }
                    )
            tables.append(
                {
                    "table_name": name,
                    "catalog_registered": bool(cat),
                    "live_version": cat["live_version"] if cat else None,
                    "read_version": cat["read_version"] if cat else None,
                    "write_versions": json.loads(cat["write_versions"] or "[]")
                    if cat
                    else [],
                    "cutover_mode": cat["cutover_mode"] if cat else "初始",
                    "contract_live_version": live["version_no"] if live else None,
                    "live_versions": [r["version_no"] for r in lives],
                    "duplicate_live": len(lives) > 1,
                    "columns": columns,
                    "writable_columns": [
                        c["column_name"]
                        for c in columns
                        if c["write_state"] in WRITABLE_STATES
                    ],
                    "cleaning_jobs": refresh_cleaning_jobs(conn, name),
                }
            )
        checks = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM progress_checks ORDER BY id DESC LIMIT 40"
            ).fetchall()
        ]
        return {
            "strategy": strategy,
            "strategy_label": strategy_label(strategy),
            "tables": tables,
            "recent_checks": checks,
        }
