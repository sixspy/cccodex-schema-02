from app.db import get_db
from app.services.contracts import blocked_draft_tables


def run_reconcile(as_of: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        contracts = conn.execute(
            "SELECT table_name, version_no FROM schema_contracts WHERE status='现行'"
        ).fetchall()
        catalog = {
            r["table_name"]: r["live_version"]
            for r in conn.execute("SELECT table_name, live_version FROM catalog_tables")
        }
        matched = 0
        drifted = 0
        details = []
        for c in contracts:
            live = catalog.get(c["table_name"])
            if live == c["version_no"]:
                matched += 1
            else:
                drifted += 1
                details.append(f"{c['table_name']}:契约v{c['version_no']}/目录v{live}")

        # 水线亮同一命运：被硬闸压住的删列草稿在此计数，复验不得再吃到删列现行
        blocked_tables = blocked_draft_tables(db_path)
        blocked = len(blocked_tables)
        blocked_detail = "硬闸压住 " + "、".join(blocked_tables) if blocked_tables else ""

        summary = (
            f"对账{as_of}：对齐{matched}，漂移{drifted}，硬拒{blocked}"
            + (("；" + "，".join(details)) if details else "")
            + (("；" + blocked_detail) if blocked_detail else "")
        )
        cur = conn.execute(
            "INSERT INTO reconcile_reports(as_of, matched, drifted, blocked, summary) "
            "VALUES (?,?,?,?,?)",
            (as_of, matched, drifted, blocked, summary),
        )
        row = conn.execute(
            "SELECT * FROM reconcile_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        result = dict(row)
        result["blocked_tables"] = blocked_tables
        return result


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM reconcile_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
