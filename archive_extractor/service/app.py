"""仅监听本机回环地址的 Flask 管理界面。"""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from archive_extractor.config.loader import DEFAULT_RULE_DIR
from archive_extractor.core.arbiter import Arbiter, ArbitrationError
from archive_extractor.core.archiver import Archiver
from archive_extractor.core.deduplicator import Deduplicator
from archive_extractor.core.task_runner import TaskRunner
from archive_extractor.core.tracer import Tracer
from archive_extractor.storage.file_store import FileStore
from archive_extractor.storage.sqlite_client import SQLiteClient
from archive_extractor.utils.logger import get_logger

LOGGER = get_logger("archive_extractor.web")
LOCAL_ADDRESSES = {"127.0.0.1", "::1", "localhost"}


def create_app(
    db_path: Optional[Path | str] = None,
    data_dir: Optional[Path | str] = None,
) -> Flask:
    """应用工厂；所有资源均从本地加载。"""
    base = Path(data_dir or "data").expanduser().resolve()
    db = SQLiteClient(db_path or base / "archive.db")
    store = FileStore(base)
    archiver = Archiver(db, store)
    tracer = Tracer(db, store)
    arbiter = Arbiter(db, store)
    app = Flask(__name__)
    app.secret_key = os.urandom(24)
    app.config.update(JSON_AS_ASCII=False, MAX_CONTENT_LENGTH=2 * 1024 * 1024)
    app.extensions.update(
        db=db, store=store, archiver=archiver, tracer=tracer, arbiter=arbiter,
        auth=threading.Lock(), running=set(),
    )

    @app.before_request
    def local_only():
        if request.remote_addr not in LOCAL_ADDRESSES:
            abort(403, "仅允许本机回环地址访问")

    @app.after_request
    def security_headers(response):
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.route("/")
    def index():
        return redirect(url_for("tasks_page"))

    @app.route("/tasks", methods=["GET", "POST"])
    def tasks_page():
        if request.method == "POST":
            _create_task(app, db)
            return redirect(url_for("tasks_page"))
        return render_template("task_list.html", tasks=db.list_tasks(100))

    @app.post("/tasks/<int:task_id>/<action>")
    def task_action(task_id: int, action: str):
        task = db.get_task(task_id)
        if not task:
            abort(404)
        runner = _runner(app, task)
        if action in {"start", "resume"}:
            db.update_task(task_id, status="pending")
            _launch(app, task_id)
        elif action == "pause":
            runner.pause(task_id)
        elif action == "cancel":
            runner.cancel(task_id)
        elif action == "retry":
            count = runner.retry(task_id)
            flash(f"已重置 {count} 个失败文件，点击“继续”重新执行。", "info")
        elif action == "delete":
            try:
                runner.delete(task_id)
            except RuntimeError as exc:
                flash(str(exc), "error")
        else:
            abort(404)
        return redirect(url_for("tasks_page"))

    @app.route("/results")
    def results_page():
        filters = {
            "q": request.args.get("q", "").strip(),
            "form_type": request.args.get("form_type", "").strip(),
            "error_level": request.args.get("error_level", "").strip(),
            "task_id": request.args.get("task_id", "").strip(),
        }
        page = max(1, request.args.get("page", 1, type=int))
        page_size = 15
        rows = db.list_results(filters, page=page, page_size=page_size)
        for row in rows:
            try:
                data = json.loads(row["data_json"])
            except ValueError:
                data = {}
            row["preview"] = "；".join(
                f"{key}={value}" for key, value in list(data.items())[:3]
            )
        total = db.count_results(filters)
        pages = max(1, math.ceil(total / page_size))
        return render_template(
            "result_list.html", results=rows, filters=filters, total=total,
            page=page, pages=pages, tasks=db.list_tasks(100),
        )

    @app.route("/results/<int:result_id>")
    def result_detail(result_id: int):
        chain = tracer.chain(result_id)
        if not chain:
            abort(404)
        try:
            data = json.loads(chain["result"]["data_json"])
        except ValueError:
            data = {}
        return render_template(
            "trace_detail.html", chain=chain, data=data,
            data_pretty=json.dumps(data, ensure_ascii=False, indent=2),
        )

    @app.post("/results/<int:result_id>/restore")
    def result_restore(result_id: int):
        if Deduplicator(db).restore(result_id):
            flash("重复记录已恢复为有效状态。", "info")
        else:
            flash("记录不存在或无需恢复。", "error")
        return redirect(url_for("results_page"))

    @app.route("/snapshot/<int:result_id>")
    def snapshot_preview(result_id: int):
        info = tracer.snapshot_info(result_id)
        if not info:
            abort(404)
        return send_file(info["path"], mimetype="text/html")

    @app.route("/exports/<int:task_id>/<fmt>")
    def export_file(task_id: int, fmt: str):
        if not db.get_task(task_id):
            abort(404)
        if fmt not in {"csv", "json", "xlsx"}:
            abort(400)
        try:
            result = archiver.export(task_id, fmt)
        except ValueError as exc:
            abort(400, str(exc))
        return send_file(result["path"], as_attachment=True,
                         download_name=Path(result["path"]).name)

    @app.route("/exports/<int:task_id>/errors.csv")
    def export_errors(task_id: int):
        if not db.get_task(task_id):
            abort(404)
        path = store.new_export_path("csv", f"task_{task_id}_errors")
        archiver.export_error_list(task_id, path)
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.route("/arbitration")
    def arbitration_list_page():
        task_id = request.args.get("task_id", "").strip()
        batches = db.list_arbitration_batches(
            int(task_id) if task_id.isdigit() else None
        )
        return render_template(
            "arbitration_list.html", batches=batches,
            tasks=db.list_tasks(100), selected_task=task_id,
        )

    @app.route("/arbitration/<batch_ref>")
    def arbitration_detail_page(batch_ref: str):
        try:
            detail = arbiter.batch_detail(batch_ref)
        except ArbitrationError:
            abort(404)
        return render_template("arbitration_detail.html", detail=detail)

    @app.post("/arbitration/<batch_ref>/rollback")
    def arbitration_rollback(batch_ref: str):
        try:
            result = arbiter.rollback_batch(batch_ref)
            flash(
                f"仲裁批次 {result['batch_no']} 已撤销，"
                f"恢复 {result['journal_entries']} 项变更。", "info"
            )
        except ArbitrationError as exc:
            flash(str(exc), "error")
        return redirect(url_for("arbitration_list_page"))

    @app.route("/arbitration/<int:task_id>/<fmt>")
    def arbitration_export_file(task_id: int, fmt: str):
        if not db.get_task(task_id):
            abort(404)
        if fmt not in {"csv", "json", "xlsx"}:
            abort(400)
        try:
            result = archiver.export_arbitration(task_id, fmt)
        except ValueError as exc:
            abort(400, str(exc))
        return send_file(result["path"], as_attachment=True,
                         download_name=Path(result["path"]).name)

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("message.html", title="未找到",
                               message="请求的页面或记录不存在。"), 404

    @app.errorhandler(403)
    def forbidden(error):
        return render_template("message.html", title="访问受限",
                               message=str(error)), 403

    return app


def _create_task(app: Flask, db: SQLiteClient) -> None:
    root_dir = request.form.get("root_dir", "").strip()
    name = request.form.get("name", "").strip() or "本地归档采集任务"
    rules_dir = request.form.get("rules_dir", "").strip()
    if not Path(root_dir).is_dir():
        flash("扫描目录不存在或不是目录。", "error")
        return
    if rules_dir and not Path(rules_dir).is_dir():
        flash("规则目录不存在。", "error")
        return
    concurrency = max(1, min(8, request.form.get("concurrency", 1, type=int)))
    runner = TaskRunner(db, app.extensions["store"].base_dir,
                        timeout=request.form.get("timeout", 10, type=float))
    task_id = runner.create_task(
        name, root_dir, rules_dir or str(DEFAULT_RULE_DIR),
        {"concurrency": concurrency},
    )
    flash(f"任务 #{task_id} 已创建，可手动启动。", "info")


def _runner(app: Flask, task: dict) -> TaskRunner:
    try:
        config = json.loads(task.get("config_json") or "{}")
    except ValueError:
        config = {}
    return TaskRunner(
        app.extensions["db"], app.extensions["store"].base_dir,
        timeout=float(config.get("timeout", 10)),
    )


def _launch(app: Flask, task_id: int) -> None:
    with app.extensions["auth"]:
        if task_id in app.extensions["running"]:
            flash("任务正在执行中。", "error")
            return
        task = app.extensions["db"].get_task(task_id)
        runner = _runner(app, task)
        app.extensions["running"].add(task_id)

    def run():
        try:
            runner.run(task_id)
        except Exception as exc:
            LOGGER.exception("后台任务 %s 失败", task_id)
            app.extensions["db"].update_task(
                task_id, status="failed", error_message=str(exc)
            )
        finally:
            with app.extensions["auth"]:
                app.extensions["running"].discard(task_id)

    thread = threading.Thread(target=run, name=f"task-{task_id}", daemon=True)
    thread.start()
