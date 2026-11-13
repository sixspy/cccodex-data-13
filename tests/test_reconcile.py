"""七段验收：目录声明 / 账册 / 附页库 / 核齐 全链路。"""

import threading

import pytest

from app.db import get_db
from app.services import reconcile as rc
from app.services.reconcile import (
    AlgorithmMismatch,
    AttachmentConflict,
    AttachmentWriteLocked,
    CountChanged,
    ReconcileError,
    ReopenRequired,
)
from app.services.runner import run_job
POLICY_SNAPSHOT = rc.POLICY_SNAPSHOT
POLICY_LATEST = rc.POLICY_LATEST


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )


def create_done_job(app, client, name="t1", seed="/mock/catalog/alpha"):
    login(client)
    client.post("/jobs", data={"name": name, "seed_path": seed}, follow_redirects=True)
    with app.app_context():
        job_id = get_db().execute("SELECT id FROM crawl_jobs LIMIT 1").fetchone()["id"]
        run_job(job_id)
    return job_id


def post_ledger(client, job_id, code, required=True, label=""):
    return client.post(
        f"/jobs/{job_id}/reconcile/ledger",
        data={"code": code, "required": "1" if required else "0", "label": label},
        follow_redirects=True,
    )


def post_attachment(client, job_id, page_name, operator="op1"):
    return client.post(
        f"/jobs/{job_id}/reconcile/attachments",
        data={"page_name": page_name, "operator": operator},
        follow_redirects=True,
    )


def lock_and_open(app, job_id, policy):
    with app.app_context():
        rc.lock_day_policy(policy)
        sess = rc.open_session(job_id, policy)
        return sess["id"]


def build_complete_attachments(app, job_id):
    """按目录声明把应附份数一次性补齐：A-001×1、A-002×2、A-003×2。"""
    with app.app_context():
        for code, copies in (("A-001", 1), ("A-002", 2), ("A-003", 2)):
            for _ in range(copies):
                rc.add_attachment(job_id, code, "op1")


# ---------------------------------------------------------------------------
# 第 1 段：份数不对时停在未齐（少一份或多一份都列出编码）
# ---------------------------------------------------------------------------
class TestSegment1Copies:
    def test_missing_one_copy_stays_incomplete(self, app, client):
        job_id = create_done_job(app, client)
        for code in ("A-001", "A-002", "A-003"):
            post_ledger(client, job_id, code, required=True)
        # A-001 声明 1 份（齐）；A-002 声明 2 份只建 1 份（少一份）；A-003 不建
        with app.app_context():
            rc.add_attachment(job_id, "A-001", "op1")
            rc.add_attachment(job_id, "A-002", "op1")
            card = rc.card_state(job_id)
        assert card["is_complete"] is False
        missing_codes = {m["code"] for m in card["missing"]}
        assert missing_codes == {"A-002", "A-003"}
        a2 = next(m for m in card["missing"] if m["code"] == "A-002")
        assert (a2["expected"], a2["actual"]) == (2, 1)

    def test_extra_copy_also_incomplete(self, app, client):
        job_id = create_done_job(app, client)
        post_ledger(client, job_id, "A-001", required=True)
        with app.app_context():
            rc.add_attachment(job_id, "A-001", "op1")
            rc.add_attachment(job_id, "A-001", "op1")  # 声明 1 份，实建 2 份
            card = rc.card_state(job_id)
        assert card["is_complete"] is False
        m = next(m for m in card["missing"] if m["code"] == "A-001")
        assert (m["expected"], m["actual"]) == (1, 2)

    def test_submit_with_wrong_copies_blocked_from_complete(self, app, client):
        job_id = create_done_job(app, client)
        post_ledger(client, job_id, "A-001", required=True)
        with app.app_context():
            # 一份都不建
            sid = lock_and_open(app, job_id, POLICY_SNAPSHOT)
            out = rc.submit_session(job_id, sid, POLICY_SNAPSHOT)
        assert out["complete"] is False
        assert out["result"]["missing"][0]["code"] == "A-001"
        # 会话仍打开，卡片停在未齐
        with app.app_context():
            assert rc.get_open_session(job_id) is not None
            assert rc.card_state(job_id)["is_complete"] is False


# ---------------------------------------------------------------------------
# 第 2 段：有多附时不能已齐（可不附行误建附页 -> 多附）
# ---------------------------------------------------------------------------
class TestSegment2Extra:
    def test_optional_row_with_page_is_extra_and_blocks_completion(self, app, client):
        job_id = create_done_job(app, client)
        post_ledger(client, job_id, "A-001", required=True)
        post_ledger(client, job_id, "B-909", required=False)  # 可不附
        with app.app_context():
            rc.add_attachment(job_id, "A-001", "op1")  # 应附齐
            rc.add_attachment(job_id, "B-909", "op1")  # 可不附却建了附页 -> 多附
            card = rc.card_state(job_id)
            assert card["is_complete"] is False
            assert {x["code"] for x in card["extra"]} == {"B-909"}
            sid = lock_and_open(app, job_id, POLICY_SNAPSHOT)
            out = rc.submit_session(job_id, sid, POLICY_SNAPSHOT)
        assert out["complete"] is False
        assert {x["code"] for x in out["result"]["extra"]} == {"B-909"}

    def test_undeclared_page_is_extra_too(self, app, client):
        job_id = create_done_job(app, client)
        post_ledger(client, job_id, "A-001", required=True)
        with app.app_context():
            rc.add_attachment(job_id, "A-001", "op1")
            rc.add_attachment(job_id, "X-000", "op1")  # 账册上不存在的编码
            card = rc.card_state(job_id)
        assert {x["code"] for x in card["extra"]} == {"X-000"}
        assert card["is_complete"] is False

    def test_optional_row_without_page_is_fine(self, app, client):
        job_id = create_done_job(app, client)
        post_ledger(client, job_id, "A-001", required=True)
        post_ledger(client, job_id, "B-909", required=False)  # 可不附且不建页
        with app.app_context():
            rc.add_attachment(job_id, "A-001", "op1")
            sid = lock_and_open(app, job_id, POLICY_SNAPSHOT)
            out = rc.submit_session(job_id, sid, POLICY_SNAPSHOT)
        assert out["complete"] is True


# ---------------------------------------------------------------------------
# 第 3 段：条数变化按口径失败（snapshot 整笔失败写「条数变了」）或必须重开（latest）
# ---------------------------------------------------------------------------
class TestSegment3CountChange:
    def _happy_base(self, app, client):
        job_id = create_done_job(app, client)
        for code in ("A-001", "A-002", "A-003"):
            post_ledger(client, job_id, code, required=True)
        build_complete_attachments(app, job_id)
        return job_id

    def test_snapshot_policy_count_change_fails_whole_submit(self, app, client):
        job_id = self._happy_base(app, client)
        with app.app_context():
            rc.lock_day_policy(POLICY_SNAPSHOT)
            sess = rc.open_session(job_id, POLICY_SNAPSHOT)
            # 核齐过程中又有应附行写入
            rc.add_ledger_row(job_id, "A-002", True, "核齐中追加")
            with pytest.raises(CountChanged) as ei:
                rc.submit_session(job_id, sess["id"], POLICY_SNAPSHOT)
            assert "条数变了" in str(ei.value)
            # 没有静默成功：仍是未齐、会话仍打开
            assert rc.get_open_session(job_id) is not None
            assert rc.card_state(job_id)["is_complete"] is False

    def test_latest_policy_count_change_requires_reopen(self, app, client):
        job_id = self._happy_base(app, client)
        with app.app_context():
            rc.lock_day_policy(POLICY_LATEST)
            sess = rc.open_session(job_id, POLICY_LATEST)
            rc.add_attachment(job_id, "A-001", "op2")  # 打开核齐后再写入
            with pytest.raises(ReopenRequired) as ei:
                rc.submit_session(job_id, sess["id"], POLICY_LATEST)
            assert "重新打开核齐" in str(ei.value)
            assert rc.get_open_session(job_id) is not None

    def test_latest_policy_reopen_then_submit_succeeds(self, app, client):
        job_id = self._happy_base(app, client)
        with app.app_context():
            rc.lock_day_policy(POLICY_LATEST)
            sess = rc.open_session(job_id, POLICY_LATEST)
            rc.add_ledger_row(job_id, "A-002", True, "追加应附行")
            with pytest.raises(ReopenRequired):
                rc.submit_session(job_id, sess["id"], POLICY_LATEST)
            # 关闭旧会话并重新打开核齐 -> 重新拍快照后可提交
            rc.close_session(job_id, sess["id"])
            new_sess = rc.open_session(job_id, POLICY_LATEST)
            out = rc.submit_session(job_id, new_sess["id"], POLICY_LATEST)
            assert out["complete"] is True
            assert rc.card_state(job_id)["is_complete"] is True

    def test_snapshot_policy_happy_path_completes(self, app, client):
        job_id = self._happy_base(app, client)
        with app.app_context():
            sid = lock_and_open(app, job_id, POLICY_SNAPSHOT)
            out = rc.submit_session(job_id, sid, POLICY_SNAPSHOT)
        assert out["complete"] is True
        with app.app_context():
            card = rc.card_state(job_id)
            assert card["is_complete"] is True
            assert card["policy_name"] == rc.POLICY_NAMES[POLICY_SNAPSHOT]


# ---------------------------------------------------------------------------
# 第 4 段：核齐算法不一致会被挡住（当场提示「算法不对」）
# ---------------------------------------------------------------------------
class TestSegment4AlgorithmMismatch:
    def test_submit_algorithm_differs_from_day_lock_is_blocked(self, app, client):
        job_id = create_done_job(app, client)
        post_ledger(client, job_id, "A-001", required=True)
        with app.app_context():
            rc.add_attachment(job_id, "A-001", "op1")
            rc.lock_day_policy(POLICY_SNAPSHOT)  # 当日写死 snapshot
            sess = rc.open_session(job_id, POLICY_SNAPSHOT)
            with pytest.raises(AlgorithmMismatch) as ei:
                rc.submit_session(job_id, sess["id"], POLICY_LATEST)  # 却按 latest 提交
            assert "算法不对" in str(ei.value)
            # 被挡住，未产生已齐
            assert rc.card_state(job_id)["is_complete"] is False

    def test_day_policy_cannot_change_once_locked(self, app):
        with app.app_context():
            rc.lock_day_policy(POLICY_SNAPSHOT)
            with pytest.raises(ReconcileError) as ei:
                rc.lock_day_policy(POLICY_LATEST)
            assert "不能改口" in str(ei.value)
            # 相同口径重复锁定幂等
            again = rc.lock_day_policy(POLICY_SNAPSHOT)
            assert again["policy"] == POLICY_SNAPSHOT

    def test_unknown_algorithm_rejected(self, app, client):
        job_id = create_done_job(app, client)
        with app.app_context():
            rc.lock_day_policy(POLICY_SNAPSHOT)
            sess = rc.open_session(job_id, POLICY_SNAPSHOT)
            with pytest.raises(AlgorithmMismatch):
                rc.submit_session(job_id, sess["id"], "bogus-algorithm")

    def test_http_route_flashes_algorithm_mismatch(self, app, client):
        job_id = create_done_job(app, client)
        post_ledger(client, job_id, "A-001", required=True)
        with app.app_context():
            rc.lock_day_policy(POLICY_SNAPSHOT)
            sess = rc.open_session(job_id, POLICY_SNAPSHOT)
        resp = client.post(
            f"/jobs/{job_id}/reconcile/sessions/{sess['id']}/submit",
            data={"algorithm": POLICY_LATEST},
            follow_redirects=True,
        )
        text = resp.get_data(as_text=True)
        assert "算法不对" in text


# ---------------------------------------------------------------------------
# 第 5 段：并发建附页只有一笔成功（另一笔冲突结束）
# ---------------------------------------------------------------------------
class TestSegment5Concurrency:
    def test_concurrent_same_code_only_one_wins(self, app, client):
        job_id = create_done_job(app, client)

        # 锁内栅栏：先到者持锁后等在门内，后到者先撞 BEGIN IMMEDIATE，
        # 两线程在真实写锁上确定性相遇（生产环境不配置该钩子）。
        entered = threading.Event()
        release = threading.Event()

        def gate():
            entered.set()
            if not release.wait(timeout=5):  # pragma: no cover - 仅防死锁
                raise RuntimeError("lock gate timeout")

        app.config["ATTACHMENT_LOCK_GATE"] = gate
        results: list[Exception | dict] = []

        def first_holder():
            with app.app_context():
                try:
                    results.append(rc.add_attachment(job_id, "A-001", "op-a"))
                except Exception as exc:  # noqa: BLE001 - 需汇总线程内异常
                    results.append(exc)

        def contender():
            # 等先到者确实进了锁，再去抢同一把写锁
            if not entered.wait(timeout=5):  # pragma: no cover - 仅防死锁
                results.append(RuntimeError("first holder never entered lock"))
                return
            with app.app_context():
                try:
                    results.append(rc.add_attachment(job_id, "A-001", "op-b"))
                except Exception as exc:  # noqa: BLE001 - 需汇总线程内异常
                    results.append(exc)
                finally:
                    release.set()

        t1 = threading.Thread(target=first_holder)
        t2 = threading.Thread(target=contender)
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)
        assert not t1.is_alive() and not t2.is_alive()

        oks = [r for r in results if not isinstance(r, Exception)]
        conflicts = [r for r in results if isinstance(r, AttachmentConflict)]
        assert len(oks) == 1
        assert len(conflicts) == 1
        with app.app_context():
            rows = get_db().execute(
                "SELECT * FROM attachment_pages WHERE job_id = ? AND page_name = 'A-001'",
                (job_id,),
            ).fetchall()
            assert len(rows) == 1  # 只留一笔写入

    def test_sequential_creates_produce_multiple_copies(self, app, client):
        job_id = create_done_job(app, client)
        with app.app_context():
            first = rc.add_attachment(job_id, "A-002", "op1")
            second = rc.add_attachment(job_id, "A-002", "op2")
            assert (first["seq"], second["seq"]) == (1, 2)
            n = get_db().execute(
                "SELECT COUNT(*) AS c FROM attachment_pages "
                "WHERE job_id = ? AND page_name = 'A-002'",
                (job_id,),
            ).fetchone()["c"]
            assert n == 2


# ---------------------------------------------------------------------------
# 第 6 段：采集未收束时附页写不进去（附页库锁写）
# ---------------------------------------------------------------------------
class TestSegment6WriteLock:
    def test_attachment_blocked_while_job_not_done(self, app, client):
        login(client)
        client.post(
            "/jobs",
            data={"name": "draft", "seed_path": "/mock/catalog/alpha"},
            follow_redirects=True,
        )
        with app.app_context():
            job_id = get_db().execute("SELECT id FROM crawl_jobs LIMIT 1").fetchone()["id"]
            with pytest.raises(AttachmentWriteLocked) as ei:
                rc.add_attachment(job_id, "A-001", "op1")
            assert "采集未收束" in str(ei.value)
            assert get_db().execute("SELECT COUNT(*) AS c FROM attachment_pages").fetchone()["c"] == 0

    def test_http_route_flashes_lock_and_writes_nothing(self, app, client):
        login(client)
        client.post(
            "/jobs",
            data={"name": "draft", "seed_path": "/mock/catalog/alpha"},
            follow_redirects=True,
        )
        with app.app_context():
            job_id = get_db().execute("SELECT id FROM crawl_jobs LIMIT 1").fetchone()["id"]
        resp = post_attachment(client, job_id, "A-001")
        text = resp.get_data(as_text=True)
        assert "采集未收束" in text and "锁写" in text
        with app.app_context():
            assert get_db().execute("SELECT COUNT(*) AS c FROM attachment_pages").fetchone()["c"] == 0

    def test_attachment_allowed_after_collection_converges(self, app, client):
        job_id = create_done_job(app, client)
        resp = post_attachment(client, job_id, "A-001", "op1")
        assert "附页已入附页库" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# 第 7 段：页面上能分清四种状态和口径名字 + 已齐后再写入回到未齐
# ---------------------------------------------------------------------------
class TestSegment7PageAndReversion:
    def test_page_renders_four_states_and_policy_name(self, app, client):
        job_id = create_done_job(app, client)
        post_ledger(client, job_id, "A-001", required=True)
        post_ledger(client, job_id, "B-909", required=False)
        with app.app_context():
            rc.lock_day_policy(POLICY_SNAPSHOT)
            rc.add_attachment(job_id, "A-001", "op1")
            rc.add_attachment(job_id, "B-909", "op1")  # 造成多附
        page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
        for heading in ("应附", "多附", "未齐", "已齐"):
            assert heading in page
        assert rc.POLICY_NAMES[POLICY_SNAPSHOT] in page
        assert "B-909" in page and "多附 1 份" in page

    def test_completed_card_reverts_to_incomplete_after_new_required_row(self, app, client):
        job_id = create_done_job(app, client)
        for code in ("A-001", "A-002", "A-003"):
            post_ledger(client, job_id, code, required=True)
        build_complete_attachments(app, job_id)
        with app.app_context():
            sid = lock_and_open(app, job_id, POLICY_SNAPSHOT)
            assert rc.submit_session(job_id, sid, POLICY_SNAPSHOT)["complete"] is True
            assert rc.card_state(job_id)["is_complete"] is True
            # 已齐之后再写入应附行 -> 自行回到未齐，旧已齐不能继续当完成
            rc.add_ledger_row(job_id, "A-001", True, "已齐后追加")
            card = rc.card_state(job_id)
        assert card["is_complete"] is False
        assert card["state"] == "incomplete"
        # 旧的 submitted 会话仍在历史里，但不再代表完成
        page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
        assert "尚未核齐" in page

    def test_declarations_seeded_with_copies_and_template(self, app, client):
        job_id = create_done_job(app, client)
        with app.app_context():
            decls = get_db().execute(
                "SELECT * FROM catalog_declarations WHERE job_id = ? ORDER BY code",
                (job_id,),
            ).fetchall()
        codes = {d["code"]: d for d in decls}
        assert set(codes) == {"A-001", "A-002", "A-003"}
        assert codes["A-001"]["required_copies"] == 1
        assert codes["A-002"]["required_copies"] == 2
        assert codes["A-001"]["template_name"].endswith("标准附页模板")
