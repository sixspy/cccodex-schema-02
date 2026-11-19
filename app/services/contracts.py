import json

from app.db import get_db, transaction
from app.services.errors import ServiceError


# ---------------------------------------------------------------------------
# 同源判定：破坏性检查与升版硬闸只能调用这一处，结论永远一致
# ---------------------------------------------------------------------------
def _fields(conn, contract_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT field_name, field_type FROM contract_fields WHERE contract_id=?",
        (contract_id,),
    ).fetchall()
    return {r["field_name"]: r["field_type"] for r in rows}


def evaluate_diff(old_f: dict[str, str], new_f: dict[str, str]) -> dict:
    """纯函数：按现行→草稿字段差异给出硬闸结论。检查报告与升版硬闸同源。"""
    removed = sorted(set(old_f) - set(new_f))
    type_changed = sorted(
        k for k in set(old_f) & set(new_f) if old_f[k] != new_f[k]
    )
    added = sorted(set(new_f) - set(old_f))
    breaking = bool(removed or type_changed)
    return {
        "removed_fields": removed,
        "type_changed_fields": type_changed,
        "added_fields": added,
        "breaking": breaking,
        "verdict": "破坏性" if breaking else "兼容",
    }


def _gate_reason(diff: dict) -> str:
    """硬拒白话短句，失败时原样吐给页面。"""
    parts = []
    if diff["removed_fields"]:
        parts.append("删除字段 " + "、".join(diff["removed_fields"]))
    if diff["type_changed_fields"]:
        parts.append("改动字段类型 " + "、".join(diff["type_changed_fields"]))
    return "硬拒升版：" + "；".join(parts) + "，下游会缺列解析失败"


def _load_draft_live(conn, contract_id: int):
    draft = conn.execute(
        "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
    ).fetchone()
    if not draft:
        raise ServiceError("契约不存在", 404)
    if draft["status"] != "草稿":
        raise ServiceError("仅草稿可换版应用", 409)
    live = conn.execute(
        "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
        (draft["table_name"],),
    ).fetchone()
    if not live:
        raise ServiceError("无现行契约可对照", 409)
    return draft, live


def _record_block(conn, draft, diff, reason: str) -> None:
    """硬拒留档：在同一连接内先落库再提交，随后抛错回滚也冲不掉。"""
    conn.execute(
        "INSERT INTO apply_runs(contract_id, table_name, from_version, to_version, "
        "result, gate_verdict, reason) VALUES (?,?,?,?,?,?,?)",
        (
            draft["id"],
            draft["table_name"],
            None,
            draft["version_no"],
            "硬拒",
            diff["verdict"],
            reason,
        ),
    )
    conn.execute(
        "INSERT INTO gate_events(contract_id, table_name, event, reason) VALUES (?,?,?,?)",
        (draft["id"], draft["table_name"], "硬拒", reason),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 契约列表（带每张草稿最近一次硬闸结论，供四处页面亮同一颗芯片）
# ---------------------------------------------------------------------------
def list_contracts(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM contract_fields f WHERE f.contract_id=c.id) AS field_count "
            "FROM schema_contracts c ORDER BY c.table_name, c.version_no"
        ).fetchall()
        items = [dict(r) for r in rows]
        for item in items:
            last_check = conn.execute(
                "SELECT verdict FROM breaking_checks WHERE contract_id=? "
                "ORDER BY id DESC LIMIT 1",
                (item["id"],),
            ).fetchone()
            item["gate_verdict"] = last_check["verdict"] if last_check else ""
        return items


# ---------------------------------------------------------------------------
# 破坏性检查：结论留存进只读历史（只追加，无任何改写/删除入口）
# ---------------------------------------------------------------------------
def run_breaking_check(contract_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可做破坏性检查", 409)
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (draft["table_name"],),
        ).fetchone()
        if not live:
            raise ServiceError("无现行契约可对照", 409)
        diff = evaluate_diff(_fields(conn, live["id"]), _fields(conn, draft["id"]))
        cur = conn.execute(
            "INSERT INTO breaking_checks(contract_id, table_name, from_version, to_version, "
            "removed_fields, type_changed_fields, added_fields, verdict) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                contract_id,
                draft["table_name"],
                live["version_no"],
                draft["version_no"],
                json.dumps(diff["removed_fields"], ensure_ascii=False),
                json.dumps(diff["type_changed_fields"], ensure_ascii=False),
                json.dumps(diff["added_fields"], ensure_ascii=False),
                diff["verdict"],
            ),
        )
        return {
            "check_id": cur.lastrowid,
            "contract_id": contract_id,
            "table_name": draft["table_name"],
            "from_version": live["version_no"],
            "to_version": draft["version_no"],
            **diff,
        }


def list_checks(contract_id: int | None = None, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if contract_id is None:
            rows = conn.execute(
                "SELECT * FROM breaking_checks ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM breaking_checks WHERE contract_id=? ORDER BY id DESC",
                (contract_id,),
            ).fetchall()
        items = []
        for r in rows:
            item = dict(r)
            for key in ("removed_fields", "type_changed_fields", "added_fields"):
                item[key] = json.loads(item[key])
            items.append(item)
        return items


def list_gate_events(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gate_events ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 换版应用：排他锁内一次判写，硬闸同源，硬拒留档
# ---------------------------------------------------------------------------
def apply_contract(contract_id: int, db_path: str | None = None) -> dict:
    # BEGIN IMMEDIATE：两侧调度并发点升版时在此串行，缺锁双写现行行被杜绝
    with transaction(db_path, immediate=True) as conn:
        draft, live = _load_draft_live(conn, contract_id)

        # 硬闸重判：不信前端、不信旧报告，与破坏性检查同一个 evaluate_diff
        diff = evaluate_diff(_fields(conn, live["id"]), _fields(conn, draft["id"]))
        if diff["breaking"]:
            reason = _gate_reason(diff)
            _record_block(conn, draft, diff, reason)
            raise ServiceError(reason, 409)

        # 检查结论偏了也拒：留存报告与当下重算不一致，说明报告已过时
        last_check = conn.execute(
            "SELECT * FROM breaking_checks WHERE contract_id=? ORDER BY id DESC LIMIT 1",
            (contract_id,),
        ).fetchone()
        if last_check and last_check["verdict"] != diff["verdict"]:
            reason = (
                f"硬拒升版：检查结论（{last_check['verdict']}）与当前硬闸重判"
                f"（{diff['verdict']}）不一致，请重跑破坏性检查"
            )
            _record_block(conn, draft, diff, reason)
            raise ServiceError(reason, 409)

        from_ver = live["version_no"]
        conn.execute(
            "UPDATE schema_contracts SET status='停用' WHERE id=?",
            (live["id"],),
        )
        conn.execute(
            "UPDATE schema_contracts SET status='现行' WHERE id=?",
            (contract_id,),
        )
        conn.execute(
            "INSERT INTO catalog_tables(table_name, live_version) VALUES (?,?) "
            "ON CONFLICT(table_name) DO UPDATE SET live_version=excluded.live_version, "
            "registered_at=datetime('now')",
            (draft["table_name"], draft["version_no"]),
        )
        conn.execute(
            "INSERT INTO apply_runs(contract_id, table_name, from_version, to_version, "
            "result, gate_verdict, reason) VALUES (?,?,?,?,?,?,?)",
            (
                contract_id,
                draft["table_name"],
                from_ver,
                draft["version_no"],
                "已应用",
                diff["verdict"],
                "",
            ),
        )
        conn.execute(
            "INSERT INTO gate_events(contract_id, table_name, event, reason) VALUES (?,?,?,?)",
            (contract_id, draft["table_name"], "已应用", f"{from_ver}→{draft['version_no']}"),
        )
        row = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        return dict(row)


# ---------------------------------------------------------------------------
# 误升回滚：把已误升的现行整笔退回草稿，旧现行复位，再按硬闸重新判定
# ---------------------------------------------------------------------------
def rollback_contract(contract_id: int, db_path: str | None = None) -> dict:
    with transaction(db_path, immediate=True) as conn:
        current = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not current:
            raise ServiceError("契约不存在", 404)
        if current["status"] != "现行":
            raise ServiceError("仅现行契约可回滚", 409)
        previous = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='停用' "
            "ORDER BY version_no DESC LIMIT 1",
            (current["table_name"],),
        ).fetchone()
        if not previous:
            raise ServiceError("无停用的旧现行可回滚", 409)

        conn.execute(
            "UPDATE schema_contracts SET status='草稿' WHERE id=?", (contract_id,)
        )
        conn.execute(
            "UPDATE schema_contracts SET status='现行' WHERE id=?", (previous["id"],)
        )
        conn.execute(
            "INSERT INTO catalog_tables(table_name, live_version) VALUES (?,?) "
            "ON CONFLICT(table_name) DO UPDATE SET live_version=excluded.live_version, "
            "registered_at=datetime('now')",
            (current["table_name"], previous["version_no"]),
        )
        conn.execute(
            "INSERT INTO apply_runs(contract_id, table_name, from_version, to_version, "
            "result, gate_verdict, reason) VALUES (?,?,?,?,?,?,?)",
            (
                contract_id,
                current["table_name"],
                current["version_no"],
                previous["version_no"],
                "已回滚",
                "",
                f"现行 v{current['version_no']} 退回草稿，v{previous['version_no']} 复位",
            ),
        )

        # 回滚后按硬闸重判：删列草稿回到草稿态即被硬闸标记，再点升版照样硬拒
        diff = evaluate_diff(
            _fields(conn, previous["id"]), _fields(conn, contract_id)
        )
        re_verdict = "硬拒" if diff["breaking"] else "兼容放行"
        reason = (
            "回滚后硬闸重判："
            + (_gate_reason(diff) if diff["breaking"] else "字段兼容，可重新升版")
        )
        conn.execute(
            "INSERT INTO gate_events(contract_id, table_name, event, reason) VALUES (?,?,?,?)",
            (contract_id, current["table_name"], "已回滚", reason),
        )
        return {
            "table_name": current["table_name"],
            "rolled_back_to_draft": current["version_no"],
            "restored_live": previous["version_no"],
            "gate_verdict": re_verdict,
            "reason": reason,
        }


# 供对账水线使用：最近一次检查结论为“破坏性”的草稿（被硬闸压住、不得占现行）
def blocked_draft_tables(db_path: str | None = None) -> list[str]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT c.table_name FROM schema_contracts c "
            "WHERE c.status='草稿' AND ("
            "  SELECT verdict FROM breaking_checks b WHERE b.contract_id=c.id "
            "  ORDER BY b.id DESC LIMIT 1)='破坏性' "
            "ORDER BY c.table_name"
        ).fetchall()
        return [r["table_name"] for r in rows]
