import pytest

from app.db import get_db, init_db
from app.services import apply_contract, run_breaking_check, run_reconcile


async def test_breaking_draft_is_hard_blocked(authed):
    items = (await (await authed.get("/api/contracts")).json())["items"]
    draft = next(c for c in items if c["status"] == "草稿")

    check = await (await authed.post(
        f"/api/contracts/{draft['id']}/breaking-check", json={})).json()
    assert check["check"]["breaking"] is True
    assert "amount" in check["check"]["removed_fields"]
    assert check["check"]["verdict"] == "破坏性"

    # 硬闸：删列草稿不得占现行，409 + 白话短句
    blocked = await authed.post(f"/api/contracts/{draft['id']}/apply", json={})
    assert blocked.status == 409
    body = await blocked.json()
    assert body["blocked"] is True
    assert "硬拒" in body["error"] and "amount" in body["error"]

    # 现行仍是 v1，目录未被删列版本污染
    after = (await (await authed.get("/api/contracts")).json())["items"]
    live = next(c for c in after if c["id"] == draft["id"])
    assert live["status"] == "草稿"
    assert live["gate_verdict"] == "破坏性"
    v1 = next(c for c in after if c["table_name"] == "dw_orders_d" and c["version_no"] == 1)
    assert v1["status"] == "现行"

    # 硬拒留档：apply_runs 与 gate_events 各一条
    events = (await (await authed.get("/api/gate-events")).json())["items"]
    assert events[0]["event"] == "硬拒"
    assert "amount" in events[0]["reason"]

    # 水线亮同一命运
    rep = await (await authed.post(
        "/api/reconcile-reports", json={"as_of": "2026-11-18"})).json()
    assert rep["report"]["blocked"] >= 1
    assert "硬拒" in rep["report"]["summary"]
    assert rep["report"]["drifted"] == 0


async def test_compatible_draft_passes_gate(authed, tmp_path):
    """HTTP 层：先检查后升版，兼容草稿可放行且升版后再点 409。"""
    items = (await (await authed.get("/api/contracts")).json())["items"]
    # 种子草稿是破坏性的；通过服务层在同库造一个兼容草稿再走 HTTP
    db_path = authed.app["db_path"]
    with get_db(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO schema_contracts(table_name, version_no, status, note) "
            "VALUES ('dw_payments_d', 2, '草稿', '加列兼容', '')"
        )
        cid = cur.lastrowid
        for fname, ftype in [("pay_id", "INT"), ("order_id", "INT"),
                             ("paid_at", "TIMESTAMP"), ("channel", "STRING")]:
            conn.execute(
                "INSERT INTO contract_fields(contract_id, field_name, field_type) "
                "VALUES (?,?,?)", (cid, fname, ftype))

    check = await (await authed.post(
        f"/api/contracts/{cid}/breaking-check", json={})).json()
    assert check["check"]["verdict"] == "兼容"
    applied = await authed.post(f"/api/contracts/{cid}/apply", json={})
    assert applied.status == 200
    again = await authed.post(f"/api/contracts/{cid}/apply", json={})
    assert again.status == 409


def test_compatible_draft_service_layer(tmp_path):
    db = str(tmp_path / "compat.db")
    init_db(db)
    with get_db(db) as conn:
        cur = conn.execute(
            "INSERT INTO schema_contracts(table_name, version_no, status, note) "
            "VALUES ('dw_payments_d', 2, '草稿', '加列兼容', '')"
        )
        cid = cur.lastrowid
        for fname, ftype in [("pay_id", "INT"), ("order_id", "INT"),
                             ("paid_at", "TIMESTAMP"), ("channel", "STRING")]:
            conn.execute(
                "INSERT INTO contract_fields(contract_id, field_name, field_type) "
                "VALUES (?,?,?)", (cid, fname, ftype))
    check = run_breaking_check(cid, db)
    assert check["verdict"] == "兼容"
    applied = apply_contract(cid, db)
    assert applied["status"] == "现行"
    with pytest.raises(Exception):
        apply_contract(cid, db)


def test_rollback_wrongly_promoted_then_gate_rejudges(tmp_path):
    """误升整笔回滚 → 旧现行复位 → 硬闸重判，删列草稿再点仍硬拒。"""
    from app.services import rollback_contract
    from app.services.errors import ServiceError

    db = str(tmp_path / "rb.db")
    init_db(db)
    draft_id = 2  # 种子里的删列草稿 dw_orders_d v2

    # 检查报告先留存（只读历史，回滚也不清空）
    check = run_breaking_check(draft_id, db)
    assert check["verdict"] == "破坏性"

    # 模拟历史误升（绕过闸门直接置现行）
    with get_db(db) as conn:
        conn.execute("UPDATE schema_contracts SET status='停用' WHERE id=1")
        conn.execute("UPDATE schema_contracts SET status='现行' WHERE id=2")
        conn.execute(
            "INSERT INTO catalog_tables(table_name, live_version) VALUES ('dw_orders_d',2) "
            "ON CONFLICT(table_name) DO UPDATE SET live_version=2"
        )

    result = rollback_contract(draft_id, db)
    assert result["restored_live"] == 1
    assert result["rolled_back_to_draft"] == 2
    assert result["gate_verdict"] == "硬拒"
    assert "amount" in result["reason"]

    with get_db(db) as conn:
        live = conn.execute(
            "SELECT status FROM schema_contracts WHERE id=1").fetchone()
        back = conn.execute(
            "SELECT status FROM schema_contracts WHERE id=2").fetchone()
        catalog = conn.execute(
            "SELECT live_version FROM catalog_tables WHERE table_name='dw_orders_d'"
        ).fetchone()
    assert live["status"] == "现行" and back["status"] == "草稿"
    assert catalog["live_version"] == 1

    # 回滚后按硬闸重判：再点升版照样硬拒，对账复验吃不到删列现行
    with pytest.raises(ServiceError) as exc:
        apply_contract(draft_id, db)
    assert exc.value.status_code == 409 and "硬拒" in exc.value.message
    # 检查历史只追加、回滚不清空：水线始终亮同一命运，且无漂移
    report = run_reconcile("2026-11-18", db)
    assert report["drifted"] == 0
    assert report["blocked"] == 1
    assert "dw_orders_d" in report["blocked_tables"]


def test_concurrent_apply_exclusive_lock_no_double_write(tmp_path):
    """两侧调度并发点升版：排他锁串行化，只可能一笔成功，绝不双写现行。"""
    db = str(tmp_path / "conc.db")
    init_db(db)
    # 造两个兼容草稿（加列），并发升同一草稿 + 不同草稿混合压力
    with get_db(db) as conn:
        cur = conn.execute(
            "INSERT INTO schema_contracts(table_name, version_no, status, note) "
            "VALUES ('dw_payments_d', 2, '草稿', '加列', '')"
        )
        cid = cur.lastrowid
        for fname, ftype in [("pay_id", "INT"), ("order_id", "INT"),
                             ("paid_at", "TIMESTAMP"), ("channel", "STRING")]:
            conn.execute(
                "INSERT INTO contract_fields(contract_id, field_name, field_type) "
                "VALUES (?,?,?)", (cid, fname, ftype))

    outcomes = []

    def hit():
        try:
            apply_contract(cid, db)
            outcomes.append("applied")
        except Exception as exc:
            outcomes.append(f"rejected:{getattr(exc, 'status_code', 0)}")

    threads = [__import__("threading").Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("applied") == 1
    assert all(o == "applied" or o.startswith("rejected:409") for o in outcomes)
    with get_db(db) as conn:
        lives = conn.execute(
            "SELECT COUNT(*) AS n FROM schema_contracts "
            "WHERE table_name='dw_payments_d' AND status='现行'"
        ).fetchone()
        catalog = conn.execute(
            "SELECT live_version FROM catalog_tables WHERE table_name='dw_payments_d'"
        ).fetchone()
    assert lives["n"] == 1
    assert catalog["live_version"] == 2


def test_stale_check_verdict_rejected(tmp_path):
    """检查结论偏了就拒：留存报告与当下硬闸重算不一致时硬拒。"""
    from app.services.errors import ServiceError

    db = str(tmp_path / "stale.db")
    init_db(db)
    # 造一个未升版的纯加列兼容草稿，真实检查结论为兼容
    with get_db(db) as conn:
        cur = conn.execute(
            "INSERT INTO schema_contracts(table_name, version_no, status, note) "
            "VALUES ('dw_payments_d', 2, '草稿', '加列', '')"
        )
        cid = cur.lastrowid
        for fname, ftype in [("pay_id", "INT"), ("order_id", "INT"),
                             ("paid_at", "TIMESTAMP"), ("channel", "STRING")]:
            conn.execute(
                "INSERT INTO contract_fields(contract_id, field_name, field_type) "
                "VALUES (?,?,?)", (cid, fname, ftype))
    run_breaking_check(cid, db)
    # 篡改历史报告结论模拟“结论偏了”（报告只追加，但服务端重判不信它）
    with get_db(db) as conn:
        conn.execute(
            "UPDATE breaking_checks SET verdict='破坏性' WHERE contract_id=?",
            (cid,),
        )
    with pytest.raises(ServiceError) as exc:
        apply_contract(cid, db)
    assert exc.value.status_code == 409
    assert "不一致" in exc.value.message


def test_check_history_is_append_only(tmp_path):
    """检查报告留存只读历史：服务层无改写入口，重复检查只追加。"""
    db = str(tmp_path / "hist.db")
    init_db(db)
    run_breaking_check(2, db)
    run_breaking_check(2, db)
    with get_db(db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM breaking_checks WHERE contract_id=2"
        ).fetchone()["n"]
    assert n == 2
    # 硬拒也在历史里留痕，且硬拒后草稿依旧是草稿
    from app.services.errors import ServiceError
    with pytest.raises(ServiceError):
        apply_contract(2, db)
    with get_db(db) as conn:
        run = conn.execute(
            "SELECT result, gate_verdict FROM apply_runs WHERE contract_id=2"
        ).fetchone()
    assert run["result"] == "硬拒" and run["gate_verdict"] == "破坏性"
