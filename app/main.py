from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import zipfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, Form, HTTPException, Query, Request, status as http_status
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import ALLOWED_TB_VALUES, BASE_DIR, COLLECT_TIMEOUT_SECONDS
from .test_target_index import TestTargetIndexCache
from .history import build_history_summary
from .models import RunTemplate, utc_now
from .projects import (
    ProjectConfig,
    default_project_id,
    delete_project,
    get_project,
    list_projects,
    upsert_project,
)
from .reports import report_for_run
from .run_templates import delete_run_template, list_run_templates, save_run_template
from .runner import build_preview_command, collect_tests, execute_run, quote_command_for_display, stream_collect_tests
from .security import env_var_keys_from_text, validate_env_vars, validate_env_vars_detailed, validate_options, validate_test_path
from .storage import (
    artifact_path,
    count_runs,
    create_run,
    delete_runs,
    format_delete_runs_message,
    get_run,
    list_runs,
    read_log_preview,
    recover_stale_runs,
    update_run,
)

app = FastAPI(title="pytest-runner-platform")
_RUN_TASKS: dict[str, asyncio.Task] = {}


@app.on_event("startup")
async def recover_stale_runs_on_startup():
    recover_stale_runs()


@app.on_event("shutdown")
async def cancel_active_run_tasks_on_shutdown():
    tasks = [task for task in _RUN_TASKS.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _RUN_TASKS.clear()


def _schedule_run_task(run_id: str) -> asyncio.Task:
    task = asyncio.create_task(execute_run(run_id))
    _RUN_TASKS[run_id] = task

    def remove_task(done_task: asyncio.Task) -> None:
        _RUN_TASKS.pop(run_id, None)
        if not done_task.cancelled():
            done_task.exception()

    task.add_done_callback(remove_task)
    return task


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app/static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app/templates"))
test_target_index_cache = TestTargetIndexCache()
WORKER_VALUES = ["disabled", "auto", "1", "2", "4", "8"]
TB_VALUES = [value for value in ["auto", "long", "short", "line", "native", "no"] if value in ALLOWED_TB_VALUES]


def _project_form(project: ProjectConfig | None = None) -> dict:
    if not project:
        return {
            "report_mode": "platform",
            "python_executable": "",
            "working_directory": "",
            "root_path": "",
            "allowed_test_roots": "",
            "default_args": "",
            "default_env": "",
            "collect_timeout_seconds": COLLECT_TIMEOUT_SECONDS,
        }
    return {
        "id": project.id,
        "name": project.name,
        "root_path": project.root_path,
        "python_executable": project.python_executable,
        "working_directory": project.working_directory,
        "allowed_test_roots": "\n".join(project.allowed_test_roots),
        "default_args": "\n".join(project.default_args),
        "default_env": "\n".join(f"{key}={value}" for key, value in project.default_env.items()),
        "report_mode": project.report_mode,
        "collect_timeout_seconds": project.collect_timeout_seconds,
    }


def _split_lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _directory_has_files(path: str) -> bool:
    directory = Path(path)
    return directory.exists() and directory.is_dir() and any(item.is_file() for item in directory.rglob("*"))


def _safe_child_path(root: str, child: str) -> Path | None:
    root_path = Path(root).resolve()
    target = (root_path / child).resolve()
    if target != root_path and root_path not in target.parents:
        return None
    if not target.exists() or not target.is_file():
        return None
    return target


def _zip_directory(directory: str) -> bytes | None:
    root = Path(directory)
    if not root.exists() or not root.is_dir() or not any(item.is_file() for item in root.rglob("*")):
        return None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in root.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(root))
    return buffer.getvalue()


def _safe_form_int(raw: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _pagination(page: int, page_size: int, total: int) -> dict:
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(max(page, 1), total_pages)
    return {
        "page": current_page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "offset": (current_page - 1) * page_size if total else 0,
        "has_prev": current_page > 1,
        "has_next": current_page < total_pages,
        "prev_page": current_page - 1 if current_page > 1 else None,
        "next_page": current_page + 1 if current_page < total_pages else None,
    }


def _page_items(items: list, pagination: dict) -> list:
    start = pagination["offset"]
    return items[start:start + pagination["page_size"]]


def _validate_run_form(
    project_id: str,
    test_path: str,
    keyword: str,
    marker: str,
    verbosity: str,
    maxfail: str,
    workers: str,
    env_vars_text: str,
    last_failed: bool | str = False,
    failed_first: bool | str = False,
    tb: str = "auto",
):
    selected_project_id = project_id or default_project_id()
    if not selected_project_id:
        raise ValueError("请先添加 pytest 项目")
    project = get_project(selected_project_id)
    if not project:
        raise ValueError("项目不存在")
    display_path, resolved_path = validate_test_path(project, test_path)
    options = validate_options(keyword, marker, verbosity, maxfail, workers, last_failed, failed_first, tb)
    options.env_vars = validate_env_vars(env_vars_text)
    return project, display_path, resolved_path, options


def _template_payload(template: RunTemplate) -> dict:
    data = template.to_dict()
    data["options"].pop("env_vars", None)
    return data


def _project_default_test_target(project: ProjectConfig) -> str:
    if not project.allowed_test_roots:
        return "."
    root = Path(project.root_path).expanduser().resolve()
    allowed_root = Path(project.allowed_test_roots[0]).expanduser().resolve()
    try:
        relative_path = allowed_root.relative_to(root)
    except ValueError:
        return "."
    return relative_path.as_posix()


def _project_default_targets(projects: list[ProjectConfig]) -> dict[str, str]:
    return {project.id: _project_default_test_target(project) for project in projects}


def _project_collect_timeouts(projects: list[ProjectConfig]) -> dict[str, int]:
    return {project.id: project.collect_timeout_seconds for project in projects}


def _project_default_env_text(project: ProjectConfig) -> str:
    return "\n".join(f"{key}={value}" for key, value in project.default_env.items())


def _project_default_envs(projects: list[ProjectConfig]) -> dict[str, str]:
    return {project.id: _project_default_env_text(project) for project in projects}


def _ndjson_event(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False) + "\n"


def _path_size(path: str) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _run_phase_text(run) -> str:
    if run.status == "queued":
        return "等待开始..."
    if run.status != "running":
        return "已结束"
    if run.progress.collected is None:
        if _path_size(run.stdout_path) or _path_size(run.stderr_path):
            return "pytest 启动/环境准备中（已有日志输出）"
        return "pytest 启动/收集中..."
    if run.progress.percent is None:
        return "执行测试中..."
    return f"{run.progress.percent}%"


def _index_context(projects: list[ProjectConfig], selected_project_id: str | None = None, form: dict | None = None) -> dict:
    selected_project = get_project(selected_project_id) if selected_project_id else None
    if not selected_project and projects:
        selected_project = projects[0]
    selected_project_id = selected_project.id if selected_project else None
    default_form = {
        "project_id": selected_project_id or "",
        "test_path": _project_default_test_target(selected_project) if selected_project else "",
        "env_vars_text": _project_default_env_text(selected_project) if selected_project else "",
    }
    if form:
        default_form.update(form)
    return {
        "projects": projects,
        "selected_project_id": selected_project_id,
        "project_default_targets": _project_default_targets(projects),
        "project_default_envs": _project_default_envs(projects),
        "project_collect_timeouts": _project_collect_timeouts(projects),
        "worker_values": WORKER_VALUES,
        "tb_values": TB_VALUES,
        "collect_timeout_seconds": selected_project.collect_timeout_seconds if selected_project else COLLECT_TIMEOUT_SECONDS,
        "form": default_form,
    }


@app.get("/")
async def index(request: Request, project_id: str | None = None):
    projects = list_projects()
    selected_project_id = project_id or default_project_id()
    return templates.TemplateResponse(request, "index.html", _index_context(projects, selected_project_id))


@app.post("/runs")
async def create_run_route(
    request: Request,
    project_id: str = Form(""),
    test_path: str = Form("."),
    keyword: str = Form(""),
    marker: str = Form(""),
    verbosity: str = Form("normal"),
    maxfail: str = Form(""),
    workers: str = Form("disabled"),
    env_vars_text: str = Form(""),
    last_failed: bool = Form(False),
    failed_first: bool = Form(False),
    tb: str = Form("auto"),
):
    projects = list_projects()
    selected_project_id = project_id or default_project_id()
    form = {
        "project_id": selected_project_id,
        "test_path": test_path,
        "keyword": keyword,
        "marker": marker,
        "verbosity": verbosity,
        "maxfail": maxfail,
        "workers": workers,
        "env_vars_text": env_vars_text,
        "last_failed": last_failed,
        "failed_first": failed_first,
        "tb": tb,
    }
    try:
        project, display_path, resolved_path, options = _validate_run_form(
            selected_project_id,
            test_path,
            keyword,
            marker,
            verbosity,
            maxfail,
            workers,
            env_vars_text,
            last_failed,
            failed_first,
            tb,
        )
    except ValueError as exc:
        context = _index_context(projects, selected_project_id, form)
        context["error"] = str(exc)
        return templates.TemplateResponse(
            request,
            "index.html",
            context,
            status_code=http_status.HTTP_400_BAD_REQUEST,
        )

    run = create_run(project.id, project.name, display_path, resolved_path, options)
    _schedule_run_task(run.id)
    return RedirectResponse(url=f"/runs/{run.id}", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/api/env-vars/validate")
async def env_vars_validate(env_vars_text: str = Form("")):
    return asdict(validate_env_vars_detailed(env_vars_text))


@app.get("/api/projects/{project_id}/test-targets")
async def test_targets(project_id: str, q: str = ""):
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "project_id": project.id,
        "suggestions": await test_target_index_cache.get_suggestions(project, q[:512]),
    }


@app.post("/api/command-preview")
async def command_preview(
    project_id: str = Form(""),
    test_path: str = Form("."),
    keyword: str = Form(""),
    marker: str = Form(""),
    verbosity: str = Form("normal"),
    maxfail: str = Form(""),
    workers: str = Form("disabled"),
    env_vars_text: str = Form(""),
    last_failed: bool = Form(False),
    failed_first: bool = Form(False),
    tb: str = Form("auto"),
):
    try:
        project, display_path, resolved_path, options = _validate_run_form(
            project_id,
            test_path,
            keyword,
            marker,
            verbosity,
            maxfail,
            workers,
            env_vars_text,
            last_failed,
            failed_first,
            tb,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    command = build_preview_command(project, str(resolved_path), options)
    return {
        "ok": True,
        "command": command,
        "display_command": quote_command_for_display(command),
        "test_path": display_path,
        "warnings": [],
    }


@app.post("/api/collect")
async def collect_route(
    project_id: str = Form(""),
    test_path: str = Form("."),
    keyword: str = Form(""),
    marker: str = Form(""),
    verbosity: str = Form("normal"),
    maxfail: str = Form(""),
    workers: str = Form("disabled"),
    env_vars_text: str = Form(""),
    last_failed: bool = Form(False),
    failed_first: bool = Form(False),
    tb: str = Form("auto"),
):
    try:
        project, _display_path, resolved_path, options = _validate_run_form(
            project_id,
            test_path,
            keyword,
            marker,
            verbosity,
            maxfail,
            workers,
            env_vars_text,
            last_failed,
            failed_first,
            tb,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "return_code": None,
            "collected_count": None,
            "command": [],
            "display_command": "",
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        }
    return await collect_tests(project, resolved_path, options)


@app.post("/api/collect/stream")
async def collect_stream_route(
    project_id: str = Form(""),
    test_path: str = Form("."),
    keyword: str = Form(""),
    marker: str = Form(""),
    verbosity: str = Form("normal"),
    maxfail: str = Form(""),
    workers: str = Form("disabled"),
    env_vars_text: str = Form(""),
    last_failed: bool = Form(False),
    failed_first: bool = Form(False),
    tb: str = Form("auto"),
):
    async def events():
        try:
            project, _display_path, resolved_path, options = _validate_run_form(
                project_id,
                test_path,
                keyword,
                marker,
                verbosity,
                maxfail,
                workers,
                env_vars_text,
                last_failed,
                failed_first,
                tb,
            )
        except ValueError as exc:
            yield _ndjson_event({"event": "error", "ok": False, "error": str(exc)})
            return

        async for event in stream_collect_tests(project, resolved_path, options):
            yield _ndjson_event(event)

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/api/run-templates")
async def run_templates(project_id: str | None = None):
    return {"templates": [_template_payload(template) for template in list_run_templates(project_id)]}


@app.post("/api/run-templates")
async def save_run_template_route(
    template_name: str = Form(""),
    project_id: str = Form(""),
    test_path: str = Form("."),
    keyword: str = Form(""),
    marker: str = Form(""),
    verbosity: str = Form("normal"),
    maxfail: str = Form(""),
    workers: str = Form("disabled"),
    env_vars_text: str = Form(""),
    last_failed: bool = Form(False),
    failed_first: bool = Form(False),
    tb: str = Form("auto"),
):
    try:
        project, display_path, _resolved_path, options = _validate_run_form(
            project_id,
            test_path,
            keyword,
            marker,
            verbosity,
            maxfail,
            workers,
            env_vars_text,
            last_failed,
            failed_first,
            tb,
        )
        options.env_vars = {}
        options.env_var_keys = env_var_keys_from_text(env_vars_text)
        now = utc_now()
        template = save_run_template(
            RunTemplate(
                id=uuid4().hex[:12],
                project_id=project.id,
                name=template_name,
                test_path=display_path,
                options=options,
                created_at=now,
                updated_at=now,
            )
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "template": _template_payload(template)}


@app.delete("/api/run-templates/{template_id}")
async def delete_run_template_route(template_id: str):
    if not delete_run_template(template_id):
        raise HTTPException(status_code=404, detail="Template not found")
    return {"ok": True}


@app.get("/projects")
async def projects(request: Request):
    return templates.TemplateResponse(request, "projects.html", {"projects": list_projects()})


@app.get("/projects/new")
async def new_project(request: Request):
    return templates.TemplateResponse(
        request,
        "project_form.html",
        {"form": _project_form(), "mode": "new", "error": ""},
    )


@app.post("/projects")
async def create_project_route(
    request: Request,
    id: str = Form(""),
    name: str = Form(""),
    root_path: str = Form(""),
    python_executable: str = Form(""),
    working_directory: str = Form(""),
    allowed_test_roots: str = Form(""),
    default_args: str = Form(""),
    default_env: str = Form(""),
    report_mode: str = Form("platform"),
    collect_timeout_seconds: int = Form(COLLECT_TIMEOUT_SECONDS),
):
    form = locals().copy()
    form.pop("request")
    try:
        env = validate_env_vars(default_env)
        project = ProjectConfig(
            id=id,
            name=name,
            root_path=root_path,
            python_executable=python_executable,
            working_directory=working_directory,
            allowed_test_roots=_split_lines(allowed_test_roots),
            default_args=_split_lines(default_args),
            default_env=env,
            report_mode=report_mode,
            collect_timeout_seconds=collect_timeout_seconds,
        )
        upsert_project(project)
        test_target_index_cache.invalidate(project.id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "project_form.html",
            {"form": form, "mode": "new", "error": str(exc)},
            status_code=http_status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(url="/projects", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/projects/{project_id}/edit")
async def edit_project(request: Request, project_id: str):
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return templates.TemplateResponse(
        request,
        "project_form.html",
        {"form": _project_form(project), "mode": "edit", "error": ""},
    )


@app.post("/projects/{project_id}")
async def update_project_route(
    request: Request,
    project_id: str,
    name: str = Form(""),
    root_path: str = Form(""),
    python_executable: str = Form(""),
    working_directory: str = Form(""),
    allowed_test_roots: str = Form(""),
    default_args: str = Form(""),
    default_env: str = Form(""),
    report_mode: str = Form("platform"),
    collect_timeout_seconds: int = Form(COLLECT_TIMEOUT_SECONDS),
):
    form = locals().copy()
    form.pop("request")
    form["id"] = project_id
    try:
        env = validate_env_vars(default_env)
        project = ProjectConfig(
            id=project_id,
            name=name,
            root_path=root_path,
            python_executable=python_executable,
            working_directory=working_directory,
            allowed_test_roots=_split_lines(allowed_test_roots),
            default_args=_split_lines(default_args),
            default_env=env,
            report_mode=report_mode,
            collect_timeout_seconds=collect_timeout_seconds,
        )
        upsert_project(project)
        test_target_index_cache.invalidate(project.id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "project_form.html",
            {"form": form, "mode": "edit", "error": str(exc)},
            status_code=http_status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(url="/projects", status_code=http_status.HTTP_303_SEE_OTHER)


@app.post("/projects/{project_id}/delete")
async def delete_project_route(request: Request, project_id: str):
    try:
        delete_project(project_id)
        test_target_index_cache.invalidate(project_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "projects.html",
            {"projects": list_projects(), "error": str(exc)},
            status_code=http_status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(url="/projects", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/runs")
async def runs(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    message: str = "",
    error: str = "",
):
    total = count_runs()
    pagination = _pagination(page, page_size, total)
    page_runs = list_runs(limit=pagination["page_size"], offset=pagination["offset"])
    all_runs = list_runs()
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "runs": page_runs,
            "history": build_history_summary(all_runs),
            "pagination": pagination,
            "message": message,
            "error": error,
        },
    )


@app.post("/runs/delete")
async def delete_selected_runs(
    run_ids: list[str] = Form(default=[]),
    page: str = Form("1"),
    page_size: str = Form("25"),
):
    safe_page = _safe_form_int(page, 1)
    safe_page_size = _safe_form_int(page_size, 25, maximum=100)
    if not run_ids:
        message = "请选择要删除的运行记录。"
    else:
        try:
            result = await asyncio.to_thread(delete_runs, run_ids)
        except (OSError, ValueError, sqlite3.Error) as exc:
            error = f"删除运行记录失败：{exc}"
            return RedirectResponse(
                url=f"/runs?page={safe_page}&page_size={safe_page_size}&error={quote(error)}",
                status_code=http_status.HTTP_303_SEE_OTHER,
            )
        message = format_delete_runs_message(result)
    return RedirectResponse(
        url=f"/runs?page={safe_page}&page_size={safe_page_size}&message={quote(message)}",
        status_code=http_status.HTTP_303_SEE_OTHER,
    )


@app.get("/api/runs")
async def runs_api(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    total = count_runs()
    pagination = _pagination(page, page_size, total)
    page_runs = list_runs(limit=pagination["page_size"], offset=pagination["offset"])
    all_runs = list_runs()
    return {
        "runs": [run.to_dict() for run in page_runs],
        "history": asdict(build_history_summary(all_runs)),
        "pagination": pagination,
    }


@app.get("/runs/{run_id}")
async def run_detail(
    request: Request,
    run_id: str,
    failed_page: int = Query(1, ge=1),
    failed_page_size: int = Query(25, ge=1, le=100),
    skipped_page: int = Query(1, ge=1),
    skipped_page_size: int = Query(20, ge=1, le=100),
):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    project = get_project(run.project_id)
    report = report_for_run(run)
    failed_pagination = _pagination(failed_page, failed_page_size, len(report.failed_cases))
    skipped_pagination = _pagination(skipped_page, skipped_page_size, len(report.skipped_cases))
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "project": project,
            "has_allure_results": _directory_has_files(run.allure_results_path),
            "has_allure_report": (Path(run.allure_report_path) / "index.html").exists(),
            "report": report,
            "failed_cases": _page_items(report.failed_cases, failed_pagination),
            "skipped_cases": _page_items(report.skipped_cases, skipped_pagination),
            "failed_pagination": failed_pagination,
            "skipped_pagination": skipped_pagination,
            "stdout_preview": read_log_preview(run.stdout_path),
            "stderr_preview": read_log_preview(run.stderr_path),
            "display_command": quote_command_for_display(run.command) if run.command else "",
            "phase_text": _run_phase_text(run),
        },
    )


@app.get("/api/runs/{run_id}/report")
async def run_report(
    run_id: str,
    failed_page: int = Query(1, ge=1),
    failed_page_size: int = Query(25, ge=1, le=100),
    skipped_page: int = Query(1, ge=1),
    skipped_page_size: int = Query(20, ge=1, le=100),
):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    report = report_for_run(run)
    failed_pagination = _pagination(failed_page, failed_page_size, len(report.failed_cases))
    skipped_pagination = _pagination(skipped_page, skipped_page_size, len(report.skipped_cases))
    return {
        "exists": report.exists,
        "total": report.total,
        "passed": report.passed,
        "failed": report.failed,
        "errors": report.errors,
        "skipped": report.skipped,
        "time_seconds": report.time_seconds,
        "error_message": report.error_message,
        "failed_cases": [asdict(case) for case in _page_items(report.failed_cases, failed_pagination)],
        "skipped_cases": [asdict(case) for case in _page_items(report.skipped_cases, skipped_pagination)],
        "failed_pagination": failed_pagination,
        "skipped_pagination": skipped_pagination,
    }


@app.get("/runs/{run_id}/reports/pytest.html")
async def pytest_html_report(run_id: str):
    path = artifact_path(run_id, "pytest.html")
    if not path:
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(path, media_type="text/html")


@app.get("/runs/{run_id}/reports/junit.xml")
async def junit_report(run_id: str):
    path = artifact_path(run_id, "junit.xml")
    if not path:
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(path, media_type="application/xml", filename="junit.xml")


@app.get("/runs/{run_id}/reports/allure")
async def allure_report_index(run_id: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    index_path = Path(run.allure_report_path) / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Allure report not found")
    return RedirectResponse(url=f"/runs/{run.id}/reports/allure/index.html")


@app.get("/runs/{run_id}/reports/allure/{path:path}")
async def allure_report_asset(run_id: str, path: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    target = _safe_child_path(run.allure_report_path, path)
    if not target:
        raise HTTPException(status_code=404, detail="Allure asset not found")
    return FileResponse(target)


@app.get("/runs/{run_id}/reports/allure-results.zip")
async def allure_results_zip(run_id: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    data = _zip_directory(run.allure_results_path)
    if data is None:
        raise HTTPException(status_code=404, detail="Allure results not found")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{run.id}-allure-results.zip"'},
    )


@app.get("/runs/{run_id}/logs/stdout")
async def stdout_log(run_id: str):
    path = artifact_path(run_id, "stdout.log")
    if not path:
        raise HTTPException(status_code=404, detail="Log not found")
    return FileResponse(path, media_type="text/plain", filename="stdout.log")


@app.get("/runs/{run_id}/logs/stderr")
async def stderr_log(run_id: str):
    path = artifact_path(run_id, "stderr.log")
    if not path:
        raise HTTPException(status_code=404, detail="Log not found")
    return FileResponse(path, media_type="text/plain", filename="stderr.log")


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in {"queued", "running"}:
        return {"ok": True, "status": run.status, "message": "运行已结束"}

    task = _RUN_TASKS.get(run_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    else:
        update_run(
            run_id,
            status="error",
            finished_at=utc_now(),
            return_code=-15,
            error_message="用户取消运行",
        )
    run = get_run(run_id)
    return {"ok": True, "status": run.status if run else "error", "message": "已取消运行"}


@app.get("/api/runs/{run_id}")
async def run_status(run_id: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {
        "id": run.id,
        "project_id": run.project_id,
        "project_name": run.project_name,
        "status": run.status,
        "return_code": run.return_code,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_seconds": run.duration_seconds,
        "progress": asdict(run.progress),
        "phase_text": _run_phase_text(run),
        "stdout_preview": read_log_preview(run.stdout_path),
        "stderr_preview": read_log_preview(run.stderr_path),
        "error_message": run.error_message,
        "is_active": run.status in {"queued", "running"},
        "has_allure_results": _directory_has_files(run.allure_results_path),
        "has_allure_report": (Path(run.allure_report_path) / "index.html").exists(),
    }
