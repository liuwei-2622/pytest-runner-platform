from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
from pathlib import Path

from .config import (
    BASE_DIR,
    MAX_COLLECT_OUTPUT_BYTES,
    MAX_CONCURRENT_RUNS,
    REPORT_PLUGIN_MODE,
    RUN_TIMEOUT_SECONDS,
)
from .models import RunOptions, RunProgress, TestRun, utc_now
from .projects import ProjectConfig, get_project
from .storage import get_run, update_run, update_run_progress

_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
PROGRESS_PLUGIN = "pytest_runner_platform_progress.plugin"


def quote_command_for_display(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _process_group_kwargs() -> dict:
    return {"start_new_session": True} if os.name == "posix" else {}


async def _terminate_process_group(process: asyncio.subprocess.Process, grace_seconds: float = 5) -> None:
    if process.returncode is not None:
        return

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
            return
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await process.wait()
        return

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


def build_pytest_args(
    *,
    project: ProjectConfig,
    test_path: str,
    options: RunOptions,
    report_paths: dict[str, str] | None,
    collect_only: bool = False,
    include_progress_plugin: bool = True,
    report_plugins: dict[str, bool] | None = None,
) -> list[str]:
    command = [project.python_executable, "-m", "pytest"]
    if include_progress_plugin:
        command.extend(["-p", PROGRESS_PLUGIN])
    command.extend([test_path, *project.default_args])

    if collect_only:
        command.extend(["--collect-only", "-o", "addopts="])
    elif project.report_mode == "platform" and report_paths:
        plugins = report_plugins or {"html": True, "allure": True}
        if plugins.get("html", True):
            command.extend([f"--html={report_paths['html']}", "--self-contained-html"])
        command.append(f"--junitxml={report_paths['junit']}")
        if plugins.get("allure", True):
            command.append(f"--alluredir={report_paths['allure']}")

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


def _target_module_available(project: ProjectConfig, options: RunOptions, module_name: str) -> bool:
    command = [
        project.python_executable,
        "-c",
        "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)",
        module_name,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=str(project.working_dir),
            env=_base_pytest_env(project, options),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _report_plugin_flags(project: ProjectConfig, options: RunOptions) -> dict[str, bool]:
    if REPORT_PLUGIN_MODE == "strict":
        return {"html": True, "allure": True}
    if REPORT_PLUGIN_MODE == "builtin":
        return {"html": False, "allure": False}
    return {
        "html": _target_module_available(project, options, "pytest_html"),
        "allure": _target_module_available(project, options, "allure_pytest"),
    }


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
        report_plugins=_report_plugin_flags(project, run.options),
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
        report_plugins=_report_plugin_flags(project, options),
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


def _report_plugin_error_message(stderr_path: str) -> str:
    try:
        stderr = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if "unrecognized arguments" not in stderr:
        return ""
    if "--html" not in stderr and "--alluredir" not in stderr and "--self-contained-html" not in stderr:
        return ""
    return "报告插件参数不被目标 pytest 环境识别，请安装 pytest-html/allure-pytest，或设置 PYTEST_PLATFORM_REPORT_PLUGIN_MODE=auto/builtin"


async def _read_collect_output(kind: str, stream: asyncio.StreamReader | None, queue: asyncio.Queue, chunks: list[bytes]) -> None:
    if not stream:
        return
    while chunk := await stream.read(8192):
        chunks.append(chunk)
        await queue.put({"event": kind, "text": chunk.decode("utf-8", errors="replace")})


async def _cancel_unfinished_tasks(*tasks: asyncio.Task | None) -> None:
    pending = [task for task in tasks if task and not task.done()]
    for task in pending:
        task.cancel()
    active = [task for task in tasks if task]
    if active:
        await asyncio.gather(*active, return_exceptions=True)


async def _finish_run_tasks(
    stop_progress: asyncio.Event | None,
    stdout_task: asyncio.Task | None,
    stderr_task: asyncio.Task | None,
    progress_task: asyncio.Task | None,
) -> None:
    if stop_progress:
        stop_progress.set()
    tasks = [task for task in [stdout_task, stderr_task, progress_task] if task]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def collect_tests(project: ProjectConfig, resolved_path: Path, options: RunOptions) -> dict:
    command = build_pytest_args(
        project=project,
        test_path=str(resolved_path),
        options=options,
        report_paths=None,
        collect_only=True,
        include_progress_plugin=False,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(project.working_dir),
            env=_base_pytest_env(project, options),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_process_group_kwargs(),
        )
    except OSError as exc:
        error = f"收集测试启动失败: {type(exc).__name__}: {exc}"
        return {
            "ok": False,
            "return_code": None,
            "command": command,
            "display_command": quote_command_for_display(command),
            "collected_count": None,
            "stdout": "",
            "stderr": error,
            "timed_out": False,
            "error": error,
        }
    timeout_seconds = project.collect_timeout_seconds
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        timed_out = True
        await _terminate_process_group(process)
        stdout, stderr = await process.communicate()
        stderr += f"\nCollect timed out after {timeout_seconds} seconds.\n".encode()

    stdout_text = _trim_output(stdout)
    stderr_text = _trim_output(stderr)
    collected_count = _parse_collected_count(f"{stdout_text}\n{stderr_text}")
    error = f"收集测试超过 {timeout_seconds} 秒后超时" if timed_out else ""

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


async def stream_collect_tests(project: ProjectConfig, resolved_path: Path, options: RunOptions):
    command = build_pytest_args(
        project=project,
        test_path=str(resolved_path),
        options=options,
        report_paths=None,
        collect_only=True,
        include_progress_plugin=False,
    )
    yield {
        "event": "start",
        "command": command,
        "display_command": quote_command_for_display(command),
        "timeout_seconds": project.collect_timeout_seconds,
    }
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(project.working_dir),
            env=_base_pytest_env(project, options),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_process_group_kwargs(),
        )
    except OSError as exc:
        error = f"收集测试启动失败: {type(exc).__name__}: {exc}"
        yield {
            "event": "error",
            "ok": False,
            "return_code": None,
            "collected_count": None,
            "timed_out": False,
            "error": error,
            "stderr": error,
        }
        return

    queue: asyncio.Queue = asyncio.Queue()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_task = asyncio.create_task(_read_collect_output("stdout", process.stdout, queue, stdout_chunks))
    stderr_task = asyncio.create_task(_read_collect_output("stderr", process.stderr, queue, stderr_chunks))
    wait_task = asyncio.create_task(process.wait())
    tasks = {stdout_task, stderr_task, wait_task}
    timeout_seconds = project.collect_timeout_seconds
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    timed_out = False

    try:
        while tasks:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0 and not wait_task.done():
                timed_out = True
                await _terminate_process_group(process)
                break

            done, tasks = await asyncio.wait(
                tasks,
                timeout=max(0.05, min(0.2, remaining)),
                return_when=asyncio.FIRST_COMPLETED,
            )
            while not queue.empty():
                yield await queue.get()

            if not done and remaining <= 0 and not wait_task.done():
                timed_out = True
                await _terminate_process_group(process)
                break
    except asyncio.CancelledError:
        if process.returncode is None:
            await _terminate_process_group(process)
        await _cancel_unfinished_tasks(stdout_task, stderr_task, wait_task)
        raise
    finally:
        if process.returncode is None:
            await _cancel_unfinished_tasks(stdout_task, stderr_task, wait_task)
        else:
            await asyncio.gather(stdout_task, stderr_task, wait_task, return_exceptions=True)

    while not queue.empty():
        yield await queue.get()

    if timed_out:
        timeout_message = f"\nCollect timed out after {timeout_seconds} seconds.\n"
        stderr_chunks.append(timeout_message.encode())
        yield {"event": "stderr", "text": timeout_message}

    stdout_text = _trim_output(b"".join(stdout_chunks))
    stderr_text = _trim_output(b"".join(stderr_chunks))
    collected_count = _parse_collected_count(f"{stdout_text}\n{stderr_text}")
    error = f"收集测试超过 {timeout_seconds} 秒后超时" if timed_out else ""
    yield {
        "event": "complete",
        "ok": process.returncode == 0 and not timed_out,
        "return_code": process.returncode,
        "collected_count": collected_count,
        "timed_out": timed_out,
        "error": error,
        "timeout_seconds": timeout_seconds,
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
    process: asyncio.subprocess.Process | None = None
    stop_progress: asyncio.Event | None = None
    stdout_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    progress_task: asyncio.Task | None = None
    try:
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
                **_process_group_kwargs(),
            )

            stop_progress = asyncio.Event()
            stdout_task = asyncio.create_task(_pump_stream(process.stdout, run.stdout_path))
            stderr_task = asyncio.create_task(_pump_stream(process.stderr, run.stderr_path))
            progress_task = asyncio.create_task(_watch_progress(run_id, _progress_path(run), stop_progress))

            try:
                await asyncio.wait_for(process.wait(), timeout=RUN_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                await _terminate_process_group(process)
                await _finish_run_tasks(stop_progress, stdout_task, stderr_task, progress_task)
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

            report_plugin_error = _report_plugin_error_message(run.stderr_path) if status == "error" else ""
            allure_warning = await _generate_allure_report(run)

            update_run(
                run_id,
                status=status,
                finished_at=utc_now(),
                return_code=process.returncode,
                error_message=report_plugin_error or allure_warning,
            )
    except asyncio.CancelledError:
        if process and process.returncode is None:
            await _terminate_process_group(process)
        await _finish_run_tasks(stop_progress, stdout_task, stderr_task, progress_task)
        try:
            update_run(
                run_id,
                status="error",
                finished_at=utc_now(),
                return_code=process.returncode if process else None,
                error_message="运行被取消或服务关闭",
            )
        except KeyError:
            pass
        raise
    except Exception as exc:
        if process and process.returncode is None:
            await _terminate_process_group(process)
        await _finish_run_tasks(stop_progress, stdout_task, stderr_task, progress_task)
        try:
            update_run(
                run_id,
                status="error",
                finished_at=utc_now(),
                return_code=process.returncode if process else None,
                error_message=f"运行异常: {type(exc).__name__}: {exc}",
            )
        except KeyError:
            return
