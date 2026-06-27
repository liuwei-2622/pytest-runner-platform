from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
from pathlib import Path

from .config import (
    BASE_DIR,
    COLLECT_TIMEOUT_SECONDS,
    MAX_COLLECT_OUTPUT_BYTES,
    MAX_CONCURRENT_RUNS,
    RUN_TIMEOUT_SECONDS,
)
from .models import RunOptions, RunProgress, TestRun, utc_now
from .projects import ProjectConfig, get_project
from .storage import get_run, update_run, update_run_progress

_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)


def quote_command_for_display(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def build_pytest_args(
    *,
    project: ProjectConfig,
    test_path: str,
    options: RunOptions,
    report_paths: dict[str, str] | None,
    collect_only: bool = False,
    include_progress_plugin: bool = True,
) -> list[str]:
    command = [project.python_executable, "-m", "pytest"]
    if include_progress_plugin:
        command.extend(["-p", "app.pytest_progress"])
    command.extend([test_path, *project.default_args])

    if collect_only:
        command.append("--collect-only")
    elif project.report_mode == "platform" and report_paths:
        command.extend(
            [
                f"--html={report_paths['html']}",
                "--self-contained-html",
                f"--junitxml={report_paths['junit']}",
                f"--alluredir={report_paths['allure']}",
            ]
        )

    if options.keyword:
        command.extend(["-k", options.keyword])
    if options.marker:
        command.extend(["-m", options.marker])
    if options.verbosity == "quiet":
        command.append("-q")
    elif options.verbosity == "verbose":
        command.append("-v")
    if options.maxfail is not None:
        command.append(f"--maxfail={options.maxfail}")
    if options.workers != "disabled":
        command.extend(["-n", options.workers])
    if options.last_failed:
        command.append("--lf")
    if options.failed_first:
        command.append("--ff")
    if options.tb != "auto":
        command.append(f"--tb={options.tb}")

    return command


def build_pytest_command(run: TestRun, project: ProjectConfig) -> list[str]:
    return build_pytest_args(
        project=project,
        test_path=run.resolved_test_path,
        options=run.options,
        report_paths={
            "html": run.html_report_path,
            "junit": run.junit_report_path,
            "allure": run.allure_results_path,
        },
    )


def build_preview_command(project: ProjectConfig, test_path: str, options: RunOptions) -> list[str]:
    return build_pytest_args(
        project=project,
        test_path=test_path,
        options=options,
        report_paths={
            "html": "<run-report-dir>/pytest.html",
            "junit": "<run-report-dir>/junit.xml",
            "allure": "<run-report-dir>/allure-results",
        },
    )


def _write_bytes(path: str, data: bytes) -> None:
    Path(path).write_bytes(data)


def _append_bytes(path: str, data: bytes) -> None:
    if data:
        with Path(path).open("ab") as file:
            file.write(data)


async def _pump_stream(stream: asyncio.StreamReader | None, path: str) -> None:
    if not stream:
        return
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as file:
        while chunk := await stream.read(8192):
            file.write(chunk)
            file.flush()


def _progress_path(run: TestRun) -> Path:
    return Path(run.report_dir) / "progress.json"


def _load_progress(path: Path) -> RunProgress | None:
    if not path.exists():
        return None
    try:
        return RunProgress.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return None


async def _watch_progress(run_id: str, path: Path, stop_event: asyncio.Event) -> None:
    last_snapshot = ""
    while not stop_event.is_set():
        last_snapshot = _sync_progress_snapshot(run_id, path, last_snapshot)
        await asyncio.sleep(0.5)
    _sync_progress_snapshot(run_id, path, last_snapshot)


def _sync_progress_snapshot(run_id: str, path: Path, last_snapshot: str) -> str:
    try:
        snapshot = path.read_text(encoding="utf-8")
    except OSError:
        return last_snapshot
    if snapshot == last_snapshot:
        return last_snapshot
    try:
        progress = RunProgress.from_dict(json.loads(snapshot))
    except (json.JSONDecodeError, TypeError):
        return last_snapshot
    update_run_progress(run_id, progress)
    return snapshot


def _base_pytest_env(project: ProjectConfig, options: RunOptions) -> dict[str, str]:
    env = os.environ.copy()
    env.update(project.default_env)
    env.update(options.env_vars)
    existing_pythonpath = env.get("PYTHONPATH")
    platform_path = str(BASE_DIR)
    env["PYTHONPATH"] = (
        f"{platform_path}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else platform_path
    )
    return env


def _pytest_env(project: ProjectConfig, run: TestRun) -> dict[str, str]:
    env = _base_pytest_env(project, run.options)
    env["PYTEST_RUNNER_PROGRESS_ENABLED"] = "1"
    env["PYTEST_RUNNER_PROGRESS_PATH"] = str(_progress_path(run))
    return env


def _trim_output(data: bytes) -> str:
    if len(data) > MAX_COLLECT_OUTPUT_BYTES:
        data = data[-MAX_COLLECT_OUTPUT_BYTES:]
    return data.decode("utf-8", errors="replace")


def _parse_collected_count(output: str) -> int | None:
    matches = re.findall(r"collected\s+(\d+)\s+items?", output)
    if not matches:
        return None
    return int(matches[-1])


async def collect_tests(project: ProjectConfig, resolved_path: Path, options: RunOptions) -> dict:
    command = build_pytest_args(
        project=project,
        test_path=str(resolved_path),
        options=options,
        report_paths=None,
        collect_only=True,
        include_progress_plugin=False,
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(project.working_dir),
        env=_base_pytest_env(project, options),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=COLLECT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        timed_out = True
        process.terminate()
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
        stderr += f"\nCollect timed out after {COLLECT_TIMEOUT_SECONDS} seconds.\n".encode()

    stdout_text = _trim_output(stdout)
    stderr_text = _trim_output(stderr)
    collected_count = _parse_collected_count(f"{stdout_text}\n{stderr_text}")
    error = f"收集测试超过 {COLLECT_TIMEOUT_SECONDS} 秒后超时" if timed_out else ""

    return {
        "ok": process.returncode == 0 and not timed_out,
        "return_code": process.returncode,
        "command": command,
        "display_command": quote_command_for_display(command),
        "collected_count": collected_count,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "timed_out": timed_out,
        "error": error,
    }


async def _generate_allure_report(run: TestRun) -> str:
    allure_cli = shutil.which("allure")
    results_path = Path(run.allure_results_path)
    if not allure_cli or not results_path.exists() or not any(results_path.iterdir()):
        return ""

    process = await asyncio.create_subprocess_exec(
        allure_cli,
        "generate",
        run.allure_results_path,
        "-o",
        run.allure_report_path,
        "--clean",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    _append_bytes(run.stdout_path, b"\n\n[allure generate stdout]\n" + stdout)
    _append_bytes(run.stderr_path, b"\n\n[allure generate stderr]\n" + stderr)

    if process.returncode != 0:
        return f"Allure HTML 报告生成失败，返回码: {process.returncode}"
    return ""


async def execute_run(run_id: str) -> None:
    async with _semaphore:
        run = get_run(run_id)
        if not run:
            return

        project = get_project(run.project_id)
        if not project:
            update_run(
                run_id,
                status="error",
                finished_at=utc_now(),
                error_message=f"项目配置不存在: {run.project_id}",
            )
            return

        command = build_pytest_command(run, project)
        run = update_run(
            run_id,
            status="running",
            started_at=utc_now(),
            command=command,
            progress=RunProgress(updated_at=utc_now()),
        )

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(project.working_dir),
            env=_pytest_env(project, run),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stop_progress = asyncio.Event()
        stdout_task = asyncio.create_task(_pump_stream(process.stdout, run.stdout_path))
        stderr_task = asyncio.create_task(_pump_stream(process.stderr, run.stderr_path))
        progress_task = asyncio.create_task(_watch_progress(run_id, _progress_path(run), stop_progress))

        try:
            await asyncio.wait_for(process.wait(), timeout=RUN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
            stop_progress.set()
            await progress_task
            _append_bytes(
                run.stderr_path,
                f"\nRun timed out after {RUN_TIMEOUT_SECONDS} seconds.\n".encode(),
            )
            update_run(
                run_id,
                status="timeout",
                finished_at=utc_now(),
                return_code=process.returncode,
                error_message=f"运行超过 {RUN_TIMEOUT_SECONDS} 秒后超时",
            )
            return

        await asyncio.gather(stdout_task, stderr_task)
        stop_progress.set()
        await progress_task

        if process.returncode == 0:
            status = "passed"
        elif process.returncode == 1:
            status = "failed"
        else:
            status = "error"

        allure_warning = await _generate_allure_report(run)

        update_run(
            run_id,
            status=status,
            finished_at=utc_now(),
            return_code=process.returncode,
            error_message=allure_warning,
        )
