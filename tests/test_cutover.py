async def _items(authed):
    return (await (await authed.get("/api/contracts")).json())["items"]


async def _draft(authed, table="dw_orders_d"):
    items = await _items(authed)
    return next(
        c for c in items if c["table_name"] == table and c["status"] == "草稿"
    )


async def test_contract_list_carries_fields_and_pointers(authed):
    items = await _items(authed)
    draft = next(c for c in items if c["status"] == "草稿")
    names = [f["field_name"] for f in draft["fields"]]
    live = next(c for c in items if c["status"] == "现行")
    assert "amount" in [f["field_name"] for f in live["fields"]]
    assert "currency" in names
    assert draft["live_version"] == 1
    assert draft["catalog_live_version"] == 1
    assert draft["read_version"] == 1


async def test_lock_fate_rejects_old_column_after_cutover(authed):
    draft = await _draft(authed)
    r = await authed.post(
        f"/api/contracts/{draft['id']}/apply",
        json={"strategy": "lock", "expected_live_version": draft["live_version"]},
    )
    assert r.status == 200
    applied = (await r.json())
    assert applied["strategy"] == "lock"
    assert applied["to_version"] == 2
    assert applied["read_version"] == 2
    assert applied["write_versions"] == [2]

    catalog = (await (await authed.get("/api/catalog")).json())["catalog"]
    table = next(t for t in catalog["tables"] if t["table_name"] == "dw_orders_d")
    assert table["cutover_mode"] == "锁表切流"
    assert table["live_version"] == table["read_version"] == 2
    states = {c["column_name"]: c["write_state"] for c in table["columns"]}
    assert states["amount"] == "已停写"
    assert states["currency"] == "现行"
    assert "amount" not in table["writable_columns"]

    jobs = (await (await authed.get("/api/cleaning-jobs")).json())["items"]
    amount_job = next(j for j in jobs if j["job_name"] == "orders_amount_clean")
    assert amount_job["status"] == "旧列停写"

    write = await authed.post(
        f"/api/cleaning-jobs/{amount_job['id']}/write", json={}
    )
    payload = (await write.json())["write"]
    assert payload["accepted"] is False
    assert payload["persisted_columns"] == []
    assert "已经" in payload["detail"] or "旧列" in payload["detail"] or "停写" in payload["detail"]


async def test_unknown_column_write_is_rejected_in_plain_words(authed):
    created = await authed.post(
        "/api/cleaning-jobs",
        json={
            "job_name": "ghost_clean",
            "table_name": "dw_orders_d",
            "columns": ["order_id", "ghost_col"],
        },
    )
    job = (await created.json())["job"]
    assert job["status"] == "未知列名"

    write = await authed.post(f"/api/cleaning-jobs/{job['id']}/write", json={})
    payload = (await write.json())["write"]
    assert payload["accepted"] is False
    assert payload["unknown_columns"] == ["ghost_col"]
    assert "ghost_col" in payload["detail"]
    assert "回滚" in payload["detail"]


async def test_dual_fate_keeps_old_and_new_columns_writable(authed):
    draft = await _draft(authed)
    r = await authed.post(
        f"/api/contracts/{draft['id']}/apply",
        json={"strategy": "dual", "expected_live_version": 1},
    )
    assert r.status == 200
    applied = await r.json()
    assert applied["strategy_label"] == "旁路双写"
    assert applied["read_version"] == 2
    assert applied["write_versions"] == [1, 2]

    catalog = (await (await authed.get("/api/catalog")).json())["catalog"]
    table = next(t for t in catalog["tables"] if t["table_name"] == "dw_orders_d")
    states = {c["column_name"]: c["write_state"] for c in table["columns"]}
    assert states["amount"] == "双写"
    assert states["currency"] == "现行"
    assert "amount" in table["writable_columns"]

    job = next(j for j in table["cleaning_jobs"] if j["job_name"] == "orders_amount_clean")
    assert job["status"] == "双写过渡"
    write = await authed.post(f"/api/cleaning-jobs/{job['id']}/write", json={})
    payload = (await write.json())["write"]
    assert payload["accepted"] is True
    assert payload["persisted_columns"] == ["order_id", "amount"]


async def test_fates_do_not_coexist_for_same_table(authed):
    draft = await _draft(authed)
    first = await authed.post(
        f"/api/contracts/{draft['id']}/apply",
        json={"strategy": "lock", "expected_live_version": 1},
    )
    assert first.status == 200
    card = (await (await authed.get(f"/api/contracts/{draft['id']}/cutover-card")).json())["card"]
    assert card["cutover_mode"] == "锁表切流"
    assert card["strategy_locked"] is True

    second = await authed.post(
        f"/api/contracts/{draft['id']}/apply",
        json={"strategy": "dual", "expected_live_version": 2},
    )
    assert second.status == 409
    assert "已经" in (await second.json())["error"]


async def test_failure_is_rolled_back_and_reason_visible(authed):
    bad = await authed.post(
        "/api/cleaning-jobs",
        json={
            "job_name": "misaligned_clean",
            "table_name": "dw_orders_d",
            "columns": ["order_id", "ghost_col"],
        },
    )
    assert bad.status == 200
    draft = await _draft(authed)
    r = await authed.post(
        f"/api/contracts/{draft['id']}/apply",
        json={"strategy": "lock", "expected_live_version": 1},
    )
    assert r.status == 409
    body = await r.json()
    assert "回滚" in body["error"]
    assert "ghost_col" in body["error"]

    items = await _items(authed)
    row = next(c for c in items if c["id"] == draft["id"])
    assert row["status"] == "草稿"
    assert row["live_version"] == 1
    assert row["last_apply"]["result"] == "失败"
    assert "ghost_col" in row["last_apply"]["failure_reason"]

    card = (await (await authed.get(f"/api/contracts/{draft['id']}/cutover-card")).json())["card"]
    assert "ghost_col" in card["last_apply"]["failure_reason"]
    assert card["catalog_live_version"] == 1


async def test_stale_pointer_from_second_operator_is_refused(authed, client):
    draft = await _draft(authed)
    first = await authed.post(
        f"/api/contracts/{draft['id']}/apply",
        json={"strategy": "lock", "expected_live_version": 1},
    )
    assert first.status == 200

    # 第二名值班还拿着旧页面（现行 v1），直接改绑同一张表的时间点已被抢走。
    conn = __import__("sqlite3").connect(client.app["db_path"])
    conn.execute(
        "INSERT INTO schema_contracts(table_name, version_no, status, note) "
        "VALUES ('dw_orders_d', 3, '草稿', '第二名值班的草稿')"
    )
    cid = conn.execute(
        "SELECT id FROM schema_contracts WHERE version_no=3"
    ).fetchone()[0]
    for name, ftype in [
        ("order_id", "INT"),
        ("currency", "STRING"),
        ("biz_date", "DATE"),
        ("channel", "STRING"),
    ]:
        conn.execute(
            "INSERT INTO contract_fields(contract_id, field_name, field_type) VALUES (?,?,?)",
            (cid, name, ftype),
        )
    conn.commit()
    conn.close()

    second = await authed.post(
        f"/api/contracts/{cid}/apply",
        json={"strategy": "lock", "expected_live_version": 1},
    )
    assert second.status == 409
    assert "现行指针已被另一名值班改动" in (await second.json())["error"]

    catalog = (await (await authed.get("/api/catalog")).json())["catalog"]
    table = next(t for t in catalog["tables"] if t["table_name"] == "dw_orders_d")
    assert table["live_version"] == 2
    assert table["read_version"] == 2


async def test_duplicate_live_halts_cutover_and_align_checks(authed, client):
    import sqlite3

    conn = sqlite3.connect(client.app["db_path"])
    conn.execute(
        "INSERT INTO schema_contracts(table_name, version_no, status, note) "
        "VALUES ('dw_orders_d', 3, '现行', '手工制造双现行')"
    )
    conn.commit()
    conn.close()

    guard = (await (await authed.get("/api/live-guard")).json())["guard"]
    assert guard["halted"] is True
    assert guard["duplicates"][0]["table_name"] == "dw_orders_d"
    assert guard["duplicates"][0]["live_versions"] == [1, 3]

    cat = (await (await authed.get("/api/catalog")).json())["catalog"]
    table = next(t for t in cat["tables"] if t["table_name"] == "dw_orders_d")
    assert table["duplicate_live"] is True

    run = await authed.post("/api/progress-checks", json={"scope": "both"})
    payload = (await run.json())["run"]
    assert payload["passed"] is False
    assert any("双现行" in f["detail"] for f in payload["failures"])

    draft = next(c for c in await _items(authed) if c["status"] == "草稿")
    blocked = await authed.post(f"/api/contracts/{draft['id']}/apply", json={})
    assert blocked.status == 409
    assert "双现行" in (await blocked.json())["error"]


async def test_alignment_rerun_both_scopes_after_cutover(authed):
    draft = await _draft(authed)
    applied = await authed.post(
        f"/api/contracts/{draft['id']}/apply",
        json={"strategy": "lock", "expected_live_version": 1},
    )
    assert applied.status == 200
    run = await authed.post("/api/progress-checks", json={"scope": "both"})
    payload = (await run.json())["run"]
    assert set(payload["checks"]) == {"清洗", "目录"}
    assert payload["passed"] is True, payload["failures"]
    for scope in ("清洗", "目录"):
        assert all(item["result"] == "通过" for item in payload["checks"][scope])

    again = await authed.post("/api/progress-checks", json={"scope": "both"})
    second = (await again.json())["run"]
    assert second["run_id"] == payload["run_id"] + 1

    history = (await (await authed.get("/api/progress-checks")).json())["items"]
    assert {h["scope"] for h in history} == {"清洗", "目录"}


async def test_reconcile_flags_pointer_drift(authed, client):
    import sqlite3

    conn = sqlite3.connect(client.app["db_path"])
    conn.execute(
        "UPDATE catalog_tables SET read_version=2 WHERE table_name='dw_orders_d'"
    )
    conn.commit()
    conn.close()
    rep = (await (await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-18"})).json())["report"]
    assert rep["drifted"] >= 1
    assert "读指针" in rep["summary"]


async def test_concurrent_cutover_is_serialized_by_write_lock(authed, client):
    """两名仓管同时抢切流：BEGIN IMMEDIATE 排队，后到者看到新现行后拒绝。"""
    import sqlite3
    import threading

    from app.services.contracts import apply_contract
    from app.services.errors import ServiceError

    db = client.app["db_path"]
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO schema_contracts(table_name, version_no, status, note) "
        "VALUES ('dw_orders_d', 3, '草稿', '并发草稿')"
    )
    cid3 = conn.execute(
        "SELECT id FROM schema_contracts WHERE version_no=3 AND table_name='dw_orders_d'"
    ).fetchone()[0]
    for name, ftype in [
        ("order_id", "INT"),
        ("currency", "STRING"),
        ("biz_date", "DATE"),
        ("channel", "STRING"),
    ]:
        conn.execute(
            "INSERT INTO contract_fields(contract_id, field_name, field_type) VALUES (?,?,?)",
            (cid3, name, ftype),
        )
    conn.commit()
    conn.close()

    results = {}

    def run(key, cid, expected):
        try:
            apply_contract(cid, db, strategy="lock", expected_live_version=expected)
            results[key] = ("ok", None)
        except ServiceError as exc:
            results[key] = ("err", exc.message)

    t1 = threading.Thread(target=run, args=("a", 2, 1))
    t2 = threading.Thread(target=run, args=("b", cid3, 1))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(v[0] for v in results.values())
    assert statuses == ["err", "ok"], results
    loser = next(v for v in results.values() if v[0] == "err")[1]
    assert "现行指针已被另一名值班改动" in loser

    conn = sqlite3.connect(db)
    lives = conn.execute(
        "SELECT version_no FROM schema_contracts "
        "WHERE table_name='dw_orders_d' AND status='现行'"
    ).fetchall()
    read_version = conn.execute(
        "SELECT read_version FROM catalog_tables WHERE table_name='dw_orders_d'"
    ).fetchone()[0]
    conn.close()
    assert len(lives) == 1
    assert read_version == lives[0][0]


async def test_field_detail_joins_contract_catalog_and_cleaning(authed):
    detail = (await (await authed.get("/api/contracts/2/fields")).json())["detail"]
    by_name = {f["field_name"]: f for f in detail["fields"]}
    assert by_name["currency"]["catalog_state"] == "未登记"
    assert by_name["currency"]["referenced_by"] == []
    assert by_name["order_id"]["in_live"] is True
    assert "orders_amount_clean" in by_name["order_id"]["referenced_by"]
    assert detail["live_version"] == 1
    assert detail["read_version"] == 1
