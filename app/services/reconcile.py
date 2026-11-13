"""目录声明 / 账册 / 附页库 / 核齐 领域服务。

口径（写死在后台，只能选一种、当日不能改口）：

- ``snapshot`` 快照口径（打开核齐瞬间快照）：核齐时附页库与账册条数以
  「打开核齐瞬间」的快照为准；提交瞬间条数若已变化，整笔失败并写明「条数变了」。
- ``latest`` 提交瞬间最新口径：以「提交瞬间的最新条数」为准；打开核齐之后若
  又有应附行写入，必须重新打开核齐，不许静默成功。

两种口径在核齐过程中出现条数变化时都不会静默成功，区别只在失败方式与比对基线。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date

from flask import current_app

from app.db import get_db

# ---------------------------------------------------------------------------
# 口径常量：算法选项写死在后台。提交时传入的 algorithm 与此不一致 -> 当场挡住。
# ---------------------------------------------------------------------------
POLICY_SNAPSHOT = "snapshot"
POLICY_LATEST = "latest"

POLICY_NAMES = {
    POLICY_SNAPSHOT: "打开核齐瞬间快照口径",
    POLICY_LATEST: "提交瞬间最新条数口径",
}
POLICIES = tuple(POLICY_NAMES.keys())


class ReconcileError(Exception):
    """核齐领域错误，message 即要展示给操作员的中文原因。"""


class AttachmentWriteLocked(ReconcileError):
    """采集未收束，附页库锁写。"""


class AttachmentConflict(ReconcileError):
    """并发给同一编码建附页，只有一笔能写入。"""


class CountChanged(ReconcileError):
    """快照口径下提交瞬间条数与打开瞬间不一致。"""


class ReopenRequired(ReconcileError):
    """最新口径下核齐会话期间条数发生变化，要求重新打开核齐。"""


class AlgorithmMismatch(ReconcileError):
    """提交算法与说明里写死的当日口径不一致。"""


# ---------------------------------------------------------------------------
# 基础读取
# ---------------------------------------------------------------------------
def _today() -> str:
    return date.today().isoformat()


def get_day_policy(day: str | None = None) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM reconcile_policy_locks WHERE lock_date = ?",
        (day or _today(),),
    ).fetchone()


def lock_day_policy(policy: str, day: str | None = None) -> sqlite3.Row:
    """锁定当日口径。只能选一种，当日不能改口（重复提交相同口径幂等）。"""
    if policy not in POLICIES:
        raise ReconcileError(f"未知核齐口径: {policy}")
    day = day or _today()
    db = get_db()
    existing = db.execute(
        "SELECT * FROM reconcile_policy_locks WHERE lock_date = ?", (day,)
    ).fetchone()
    if existing is not None:
        if existing["policy"] != policy:
            raise ReconcileError(
                f"当日口径已锁定为「{POLICY_NAMES[existing['policy']]}」，不能改口"
            )
        return existing
    db.execute(
        "INSERT INTO reconcile_policy_locks (lock_date, policy) VALUES (?, ?)",
        (day, policy),
    )
    db.commit()
    return db.execute(
        "SELECT * FROM reconcile_policy_locks WHERE lock_date = ?", (day,)
    ).fetchone()


def seed_declarations_for_job(job_id: int, seed_path: str) -> None:
    """任务创建后按 mock 目录种子生成目录声明（应附份数与附页模板名）。"""
    from app.services.mock_catalog import CATALOGS

    db = get_db()
    items = CATALOGS.get(seed_path, [])
    for idx, it in enumerate(items, start=1):
        db.execute(
            """
            INSERT OR IGNORE INTO catalog_declarations
                (job_id, code, template_name, required_copies)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, it["sku"], f"{it['sku']}-标准附页模板", 1 if idx == 1 else 2),
        )
    db.commit()


# ---------------------------------------------------------------------------
# 附页库写入：采集未收束锁写 + 并发同编码只留一笔
# ---------------------------------------------------------------------------
def add_attachment(job_id: int, page_name: str, operator: str) -> dict:
    """给某编码建一份附页。

    - 采集任务未到 done（采集未收束）-> 附页库锁写。
    - 两个操作员并发给同一编码建附页 -> 只留一笔，另一笔以冲突结束。
      用独立连接 + BEGIN IMMEDIATE 抢写锁，seq 在锁内计算，配合
      UNIQUE(job_id, page_name, seq) 兜底。
    """
    page_name = (page_name or "").strip()
    if not page_name:
        raise ReconcileError("附页名（编码）不能为空")
    operator = (operator or "").strip() or "未署名"

    flask_db = get_db()
    job = flask_db.execute(
        "SELECT id, status FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        raise ReconcileError("任务不存在")
    if job["status"] != "done":
        raise AttachmentWriteLocked("采集未收束，附页库锁写，暂不能建附页")

    conn = sqlite3.connect(
        current_app.config["DATABASE"],
        timeout=current_app.config.get("SQLITE_BUSY_TIMEOUT_MS", 2000) / 1000.0,
    )
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # 并发建附页：后到者不排队，立刻拿锁失败 -> 以冲突结束，只留先到的一笔。
        # 顺序不重叠的建页不会撞锁，可正常生成第 2、第 3 份。
        conn.execute("PRAGMA busy_timeout = 0")
        conn.isolation_level = None  # autocommit，手动 BEGIN IMMEDIATE
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise AttachmentConflict("附页库正被另一笔写入占用，本次以冲突结束") from exc
        try:
            # 测试钩子：在持有写锁、计算份序之前汇合，用于确定性复现并发相遇；
            # 生产配置不设置 ATTACHMENT_LOCK_GATE，此行为空。
            gate = current_app.config.get("ATTACHMENT_LOCK_GATE")
            if gate is not None:
                gate()
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM attachment_pages "
                "WHERE job_id = ? AND page_name = ?",
                (job_id, page_name),
            ).fetchone()
            seq = row[0] + 1
            try:
                conn.execute(
                    """
                    INSERT INTO attachment_pages (job_id, page_name, seq, operator)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job_id, page_name, seq, operator),
                )
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                raise AttachmentConflict(
                    f"编码 {page_name} 的附页已被另一操作员抢先写入，本次冲突未留笔"
                ) from exc
            conn.execute("COMMIT")
        except Exception:
            # 任何未预期错误都回滚，避免留下半笔事务
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {"page_name": page_name, "seq": seq, "operator": operator}


# ---------------------------------------------------------------------------
# 账册与声明维护
# ---------------------------------------------------------------------------
def add_ledger_row(job_id: int, code: str, required: bool, label: str = "") -> None:
    code = (code or "").strip()
    if not code:
        raise ReconcileError("账册行编码不能为空")
    db = get_db()
    if db.execute(
        "SELECT 1 FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone() is None:
        raise ReconcileError("任务不存在")
    db.execute(
        "INSERT INTO ledger_rows (job_id, code, label, required) VALUES (?, ?, ?, ?)",
        (job_id, code, label.strip(), 1 if required else 0),
    )
    db.commit()


def upsert_declaration(
    job_id: int, code: str, template_name: str, required_copies: int
) -> None:
    code = (code or "").strip()
    template_name = (template_name or "").strip()
    if not code or not template_name:
        raise ReconcileError("编码与附页模板名不能为空")
    if int(required_copies) < 1:
        raise ReconcileError("应附份数至少为 1")
    db = get_db()
    db.execute(
        """
        INSERT INTO catalog_declarations (job_id, code, template_name, required_copies)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(job_id, code) DO UPDATE SET
            template_name = excluded.template_name,
            required_copies = excluded.required_copies
        """,
        (job_id, code, template_name, int(required_copies)),
    )
    db.commit()


# ---------------------------------------------------------------------------
# 核齐算法
# ---------------------------------------------------------------------------
def _counts(db, job_id: int) -> tuple[dict[str, int], dict[str, int]]:
    """返回 (附页库各编码份数, 账册各编码行数)，均按实时条数统计。"""
    pages: dict[str, int] = {}
    for r in db.execute(
        "SELECT page_name, COUNT(*) AS c FROM attachment_pages "
        "WHERE job_id = ? GROUP BY page_name",
        (job_id,),
    ):
        pages[r["page_name"]] = r["c"]
    ledger: dict[str, int] = {}
    for r in db.execute(
        "SELECT code, COUNT(*) AS c FROM ledger_rows "
        "WHERE job_id = ? GROUP BY code",
        (job_id,),
    ):
        ledger[r["code"]] = r["c"]
    return pages, ledger


def live_counts(job_id: int) -> dict:
    pages, ledger = _counts(get_db(), job_id)
    return {
        "pages": pages,
        "ledger": ledger,
        "page_total": sum(pages.values()),
        "ledger_total": sum(ledger.values()),
    }


def _fingerprint(job_id: int) -> str:
    """当前账册行 + 附页库内容的指纹；已齐之后再写入应附行，指纹即变、回到未齐。"""
    db = get_db()
    ledger = db.execute(
        "SELECT code, label, required, created_at FROM ledger_rows "
        "WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    pages = db.execute(
        "SELECT page_name, seq, operator, created_at FROM attachment_pages "
        "WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    payload = {
        "ledger": [tuple(r) for r in ledger],
        "pages": [tuple(r) for r in pages],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def evaluate(job_id: int, snapshot: dict | None = None) -> dict:
    """核齐算法。

    snapshot 为 None（最新口径）时按实时条数评估；否则（快照口径）按打开核齐
    瞬间保存的条数快照评估。

    返回四类清单：应附 missing/mismatch、多附 extra、未声明附页 undeclared，
    以及是否已齐。多附存在时整题不得核齐。
    """
    db = get_db()
    declarations = {
        r["code"]: r
        for r in db.execute(
            "SELECT * FROM catalog_declarations WHERE job_id = ?", (job_id,)
        )
    }
    required_codes = {
        r["code"]
        for r in db.execute(
            "SELECT DISTINCT code FROM ledger_rows WHERE job_id = ? AND required = 1",
            (job_id,),
        )
    }
    optional_codes = {
        r["code"]
        for r in db.execute(
            "SELECT DISTINCT code FROM ledger_rows WHERE job_id = ? AND required = 0",
            (job_id,),
        )
    }

    if snapshot is None:
        page_counts, _ledger_counts = _counts(db, job_id)
    else:
        page_counts = dict(snapshot.get("pages", {}))

    missing: list[dict] = []      # 应附行缺附页或份数不符
    extra: list[dict] = []        # 可不附行误建附页 -> 多附
    undeclared: list[dict] = []   # 附页编码在目录里没有声明

    # 应附行：必须在附页库留下同名页，份数与声明一致；少一份或多一份都算未齐
    for code in sorted(required_codes):
        decl = declarations.get(code)
        actual = page_counts.get(code, 0)
        if decl is None:
            missing.append(
                {"code": code, "expected": None, "actual": actual,
                 "reason": "目录无该编码声明"}
            )
            continue
        expected = decl["required_copies"]
        if actual != expected:
            missing.append(
                {"code": code, "expected": expected, "actual": actual,
                 "reason": "应附份数不符（少一份或多一份均算未齐）"}
            )

    # 可不附行：误建了附页 -> 多附；多附存在时整题不得核齐
    for code in sorted(optional_codes):
        actual = page_counts.get(code, 0)
        if actual > 0:
            extra.append({"code": code, "actual": actual})

    # 附页库里出现了账册上根本不存在的编码页，也属于多附
    known = required_codes | optional_codes
    for code in sorted(page_counts):
        if code not in known:
            undeclared.append({"code": code, "actual": page_counts[code]})

    complete = not missing and not extra and not undeclared
    return {
        "complete": complete,
        "missing": missing,
        "extra": extra,
        "undeclared": undeclared,
        "required_codes": sorted(required_codes),
        "optional_codes": sorted(optional_codes),
    }


# ---------------------------------------------------------------------------
# 打开 / 提交 / 关闭核齐
# ---------------------------------------------------------------------------
def open_session(job_id: int, policy: str | None = None) -> sqlite3.Row:
    """打开核齐：锁定当日口径，并对当前条数拍快照。"""
    if policy is not None and policy not in POLICIES:
        raise ReconcileError(f"未知核齐口径: {policy}")
    db = get_db()
    if db.execute("SELECT 1 FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise ReconcileError("任务不存在")

    existing = db.execute(
        "SELECT * FROM reconcile_sessions WHERE job_id = ? AND status = 'open' "
        "ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if existing is not None:
        # 同一任务已有打开的核齐会话：幂等返回，但当日口径必须一致
        if policy is not None and policy != existing["policy"]:
            raise ReconcileError(
                f"已有打开的核齐会话使用「{POLICY_NAMES[existing['policy']]}」，"
                "请先关闭后再换口径重开"
            )
        return existing

    lock = lock_day_policy(policy) if policy else get_day_policy()
    if lock is None:
        raise ReconcileError("当日尚未锁定核齐口径，请先选择一种口径并锁定")
    chosen = lock["policy"]

    pages, ledger = _counts(db, job_id)
    snapshot = {"pages": pages, "ledger": ledger}
    fp = _fingerprint(job_id)
    cur = db.execute(
        """
        INSERT INTO reconcile_sessions (job_id, policy, status, snapshot_json, fingerprint)
        VALUES (?, ?, 'open', ?, ?)
        """,
        (job_id, chosen, json.dumps(snapshot, ensure_ascii=False), fp),
    )
    db.commit()
    return db.execute(
        "SELECT * FROM reconcile_sessions WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def submit_session(job_id: int, session_id: int, algorithm: str) -> dict:
    """提交核齐。算法与说明里写死的当日口径对不上 -> 当场挡住「算法不对」。"""
    db = get_db()
    sess = db.execute(
        "SELECT * FROM reconcile_sessions WHERE id = ? AND job_id = ?",
        (session_id, job_id),
    ).fetchone()
    if sess is None:
        raise ReconcileError("核齐会话不存在")
    if sess["status"] != "open":
        raise ReconcileError("该核齐会话已关闭，请重新打开核齐")

    day_lock = get_day_policy()
    if day_lock is None:
        raise ReconcileError("当日核齐口径未锁定")
    locked_policy = day_lock["policy"]

    # 当场挡住：提交算法必须与说明里写死（当日锁定）的选项一致
    if algorithm != locked_policy:
        raise AlgorithmMismatch(
            f"算法不对：提交算法「{POLICY_NAMES.get(algorithm, algorithm)}」与说明里"
            f"写死的当日口径「{POLICY_NAMES[locked_policy]}」不一致，已挡住提交"
        )
    if algorithm != sess["policy"]:
        raise AlgorithmMismatch(
            f"算法不对：提交算法与本核齐会话打开时的口径"
            f"「{POLICY_NAMES[sess['policy']]}」不一致，已挡住提交"
        )

    live_pages, live_ledger = _counts(db, job_id)
    snapshot = json.loads(sess["snapshot_json"])

    if locked_policy == POLICY_SNAPSHOT:
        # 快照口径：条数变了 -> 整笔失败，写明「条数变了」，绝不静默成功
        if live_pages != snapshot["pages"] or live_ledger != snapshot["ledger"]:
            raise CountChanged(
                "条数变了：附页库或账册条数已不同于打开核齐瞬间的快照，"
                "本笔核齐整笔失败（快照口径不接受静默成功）"
            )
        result = evaluate(job_id, snapshot=snapshot)
    else:
        # 最新口径：会话期间条数变化 -> 要求重新打开核齐
        if live_pages != snapshot["pages"] or live_ledger != snapshot["ledger"]:
            raise ReopenRequired(
                "打开核齐之后又有写入，条数已变化，请重新打开核齐后再提交"
                "（最新口径不接受静默成功）"
            )
        result = evaluate(job_id, snapshot=None)

    if not result["complete"]:
        # 份数不对停在未齐 / 有多附不能已齐：会话保持打开，便于补正后重提
        return {"complete": False, "result": result, "session_id": session_id}

    db.execute(
        "UPDATE reconcile_sessions SET status = 'submitted', "
        "closed_at = datetime('now'), fingerprint = ? WHERE id = ?",
        (_fingerprint(job_id), session_id),
    )
    db.commit()
    return {"complete": True, "result": result, "session_id": session_id}


def close_session(job_id: int, session_id: int) -> None:
    db = get_db()
    sess = db.execute(
        "SELECT * FROM reconcile_sessions WHERE id = ? AND job_id = ?",
        (session_id, job_id),
    ).fetchone()
    if sess is None:
        raise ReconcileError("核齐会话不存在")
    if sess["status"] == "open":
        db.execute(
            "UPDATE reconcile_sessions SET status = 'closed', "
            "closed_at = datetime('now') WHERE id = ?",
            (session_id,),
        )
        db.commit()


def get_open_session(job_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM reconcile_sessions WHERE job_id = ? AND status = 'open' "
        "ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# 卡片状态：应附 / 多附 / 未齐 / 已齐 分开展示
# ---------------------------------------------------------------------------
def card_state(job_id: int) -> dict:
    """计算任务卡片的四种状态。

    - 已齐：最近一次提交成功的核齐会话指纹与当前账册/附页指纹一致；
      已齐之后再写入应附行 -> 指纹变化 -> 自行回到未齐，旧已齐不能继续当完成。
    """
    db = get_db()
    eval_now = evaluate(job_id)
    latest_submitted = db.execute(
        "SELECT * FROM reconcile_sessions WHERE job_id = ? AND status = 'submitted' "
        "ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    current_fp = _fingerprint(job_id)
    is_complete = (
        latest_submitted is not None
        and latest_submitted["fingerprint"] == current_fp
        and eval_now["complete"]
    )

    pages, _ledger = _counts(db, job_id)
    attached_required = [
        code for code in eval_now["required_codes"] if pages.get(code, 0) > 0
    ]
    extra_all = list(eval_now["extra"]) + list(eval_now["undeclared"])

    day_lock = get_day_policy()
    open_sess = get_open_session(job_id)
    return {
        "state": "complete" if is_complete else "incomplete",
        "is_complete": is_complete,
        "required_codes": eval_now["required_codes"],
        "optional_codes": eval_now["optional_codes"],
        "attached_required": attached_required,
        "missing": eval_now["missing"],
        "extra": extra_all,
        "evaluation": eval_now,
        "page_counts": pages,
        "policy_name": POLICY_NAMES[day_lock["policy"]] if day_lock else None,
        "policy": day_lock["policy"] if day_lock else None,
        "open_session": open_sess,
        "latest_submitted": latest_submitted,
        "fingerprint": current_fp,
    }
