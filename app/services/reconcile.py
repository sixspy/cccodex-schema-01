from app.db import get_db


def run_reconcile(as_of: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        tables = [
            r["table_name"]
            for r in conn.execute(
                "SELECT table_name FROM schema_contracts "
                "UNION SELECT table_name FROM catalog_tables ORDER BY table_name"
            )
        ]
        matched = 0
        drifted = 0
        details = []
        for name in tables:
            lives = conn.execute(
                "SELECT version_no FROM schema_contracts "
                "WHERE table_name=? AND status='现行'",
                (name,),
            ).fetchall()
            cat = conn.execute(
                "SELECT live_version, read_version FROM catalog_tables WHERE table_name=?",
                (name,),
            ).fetchone()
            if len(lives) > 1:
                drifted += 1
                details.append(
                    f"{name}:双现行("
                    + "、".join(f"v{r['version_no']}" for r in lives)
                    + ")已停机"
                )
                continue
            if not lives:
                drifted += 1
                details.append(f"{name}:契约无现行")
                continue
            version = lives[0]["version_no"]
            catalog_version = cat["live_version"] if cat else None
            read_version = cat["read_version"] if cat else None
            if catalog_version == version and read_version == version:
                matched += 1
            else:
                drifted += 1
                details.append(
                    f"{name}:契约v{version}/目录v{catalog_version}/读指针v{read_version}"
                )
        summary = (
            f"对账{as_of}：对齐{matched}，漂移{drifted}"
            + (("；" + "，".join(details)) if details else "")
        )
        cur = conn.execute(
            "INSERT INTO reconcile_reports(as_of, matched, drifted, summary) VALUES (?,?,?,?)",
            (as_of, matched, drifted, summary),
        )
        row = conn.execute(
            "SELECT * FROM reconcile_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM reconcile_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
