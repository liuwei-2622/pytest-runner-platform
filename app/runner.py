from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from .config import MAX_CONCURRENT_RUNS, RUN_TIMEOUT_SECONDS
from .models import TestRun, utc_now
from .projects import ProjectConfig, get_project
from .storage import get_run, update_run

_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)


def build_pytest_command(run: TestRun, project: ProjectConfig) -> list[str]:
    options = run.options
    command = [
        project.python_executable,
        "-m",
        "pytest",
        run.resolved_test_path,
        *project.default_args,
    ]

    if project.report_mode == "platform":
        command.extend(
            [
                f"--html={run.html_report_path}",
                "--self-contained-html",
                f"--junitxml={run.junit_report_path}",
                f"--alluredir={run.allure_results_path}",
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

    return command


def _write_bytes(path: str, data: bytes) -> None:
    Path(path).write_bytes(data)


def _append_bytes(path: str, data: bytes) -> None:
    if data:
        with Path(path).open("ab") as file:
            file.write(data)


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
        )

        env = os.environ.copy()
        env.update(project.default_env)
        env.update(run.options.env_vars)

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(project.working_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=RUN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            process.terminate()
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                stdout, stderr = await process.communicate()
            timeout_message = f"\nRun timed out after {RUN_TIMEOUT_SECONDS} seconds.\n".encode()
            _write_bytes(run.stdout_path, stdout)
            _write_bytes(run.stderr_path, stderr + timeout_message)
            update_run(
                run_id,
                status="timeout",
                finished_at=utc_now(),
                return_code=process.returncode,
                error_message=f"运行超过 {RUN_TIMEOUT_SECONDS} 秒后超时",
            )
            return

        _write_bytes(run.stdout_path, stdout)
        _write_bytes(run.stderr_path, stderr)

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
