"""目录声明 / 账册 / 附页库 / 核齐 路由。"""

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.db import get_db
from app.routes.auth import login_required
from app.services import reconcile as rc

bp = Blueprint("reconcile", __name__, url_prefix="/jobs/<int:job_id>/reconcile")


def _detail_url(job_id: int) -> str:
    return url_for("jobs.detail", job_id=job_id)


@bp.post("/policy")
@login_required
def lock_policy(job_id: int):
    policy = request.form.get("policy", "").strip()
    try:
        row = rc.lock_day_policy(policy)
        flash(f"当日核齐口径已锁定：{rc.POLICY_NAMES[row['policy']]}（当日不能改口）", "ok")
    except rc.ReconcileError as e:
        flash(str(e), "error")
    return redirect(_detail_url(job_id))


@bp.post("/declarations")
@login_required
def upsert_declaration(job_id: int):
    try:
        rc.upsert_declaration(
            job_id,
            request.form.get("code", ""),
            request.form.get("template_name", ""),
            int(request.form.get("required_copies", "1")),
        )
        flash("目录声明已保存（编码 / 模板名 / 应附份数）", "ok")
    except (rc.ReconcileError, ValueError) as e:
        flash(f"目录声明保存失败：{e}", "error")
    return redirect(_detail_url(job_id))


@bp.post("/ledger")
@login_required
def add_ledger(job_id: int):
    code = request.form.get("code", "")
    required = request.form.get("required", "1") == "1"
    label = request.form.get("label", "")
    try:
        rc.add_ledger_row(job_id, code, required, label)
        flash(f"账册行已写入：{code}（{'应附' if required else '可不附'}）", "ok")
    except rc.ReconcileError as e:
        flash(str(e), "error")
    return redirect(_detail_url(job_id))


@bp.post("/attachments")
@login_required
def add_attachment(job_id: int):
    page_name = request.form.get("page_name", "")
    operator = request.form.get("operator", "")
    try:
        out = rc.add_attachment(job_id, page_name, operator)
        flash(
            f"附页已入附页库：{out['page_name']} 第 {out['seq']} 份"
            f"（操作员 {out['operator']}）",
            "ok",
        )
    except rc.ReconcileError as e:
        flash(str(e), "error")
    return redirect(_detail_url(job_id))


@bp.post("/sessions/open")
@login_required
def open_session(job_id: int):
    policy = request.form.get("policy") or None
    try:
        sess = rc.open_session(job_id, policy)
        flash(
            f"已打开核齐（口径：{rc.POLICY_NAMES[sess['policy']]}），"
            "条数快照已固定",
            "ok",
        )
    except rc.ReconcileError as e:
        flash(str(e), "error")
    return redirect(_detail_url(job_id))


@bp.post("/sessions/<int:session_id>/submit")
@login_required
def submit_session(job_id: int, session_id: int):
    algorithm = request.form.get("algorithm", "").strip()
    try:
        out = rc.submit_session(job_id, session_id, algorithm)
        if out["complete"]:
            flash("核齐通过：应附份数全部一致、无多附，卡片标记为已齐", "ok")
        else:
            result = out["result"]
            problems = []
            for m in result["missing"]:
                problems.append(
                    f"编码 {m['code']} 未齐（应附 {m['expected']} 份，实有 {m['actual']} 份）"
                )
            for x in result["extra"] + result["undeclared"]:
                problems.append(f"编码 {x['code']} 多附 {x['actual']} 份")
            flash("停在未齐：" + "；".join(problems), "error")
    except rc.ReconcileError as e:
        flash(str(e), "error")
    return redirect(_detail_url(job_id))


@bp.post("/sessions/<int:session_id>/close")
@login_required
def close_session(job_id: int, session_id: int):
    try:
        rc.close_session(job_id, session_id)
        flash("核齐会话已关闭，如需核齐请重新打开", "ok")
    except rc.ReconcileError as e:
        flash(str(e), "error")
    return redirect(_detail_url(job_id))
