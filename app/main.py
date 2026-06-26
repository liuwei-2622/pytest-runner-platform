from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, status as http_status
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import ALLOWED_WORKER_VALUES, BASE_DIR
from .projects import (
    ProjectConfig,
    default_project_id,
    delete_project,
    get_project,
    list_projects,
    upsert_project,
)
from .runner import execute_run
from .security import validate_env_vars, validate_options, validate_test_path
from .storage import artifact_path, create_run, get_run, list_runs, read_log_preview

app = FastAPI(title="pytest-runner-platform")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app/static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app/templates"))
WORKER_VALUES = ["disabled", "auto", "1", "2", "4", "8"]
WORKER_VALUES = [value for value in WORKER_VALUES if value in ALLOWED_WORKER_VALUES]


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


@app.get("/")
async def index(request: Request, project_id: str | None = None):
    projects = list_projects()
    selected_project_id = project_id or default_project_id()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "projects": projects,
            "selected_project_id": selected_project_id,
            "worker_values": WORKER_VALUES,
            "form": {"project_id": selected_project_id},
        },
    )


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
    }
    try:
        project = get_project(selected_project_id)
        if not project:
            raise ValueError("项目不存在")
        display_path, resolved_path = validate_test_path(project, test_path)
        options = validate_options(keyword, marker, verbosity, maxfail, workers)
        options.env_vars = validate_env_vars(env_vars_text)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "error": str(exc),
                "projects": projects,
                "selected_project_id": selected_project_id,
                "worker_values": WORKER_VALUES,
                "form": form,
            },
            status_code=http_status.HTTP_400_BAD_REQUEST,
        )

    run = create_run(project.id, project.name, display_path, resolved_path, options)
    asyncio.create_task(execute_run(run.id))
    return RedirectResponse(url=f"/runs/{run.id}", status_code=http_status.HTTP_303_SEE_OTHER)


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
        )
        upsert_project(project)
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
        )
        upsert_project(project)
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
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "projects.html",
            {"projects": list_projects(), "error": str(exc)},
            status_code=http_status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(url="/projects", status_code=http_status.HTTP_303_SEE_OTHER)


@app.get("/runs")
async def runs(request: Request):
    return templates.TemplateResponse(request, "runs.html", {"runs": list_runs()})


@app.get("/runs/{run_id}")
async def run_detail(request: Request, run_id: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    project = get_project(run.project_id)
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "project": project,
            "has_allure_results": _directory_has_files(run.allure_results_path),
            "has_allure_report": (Path(run.allure_report_path) / "index.html").exists(),
            "stdout_preview": read_log_preview(run.stdout_path),
            "stderr_preview": read_log_preview(run.stderr_path),
        },
    )


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
        "has_allure_results": _directory_has_files(run.allure_results_path),
        "has_allure_report": (Path(run.allure_report_path) / "index.html").exists(),
    }
