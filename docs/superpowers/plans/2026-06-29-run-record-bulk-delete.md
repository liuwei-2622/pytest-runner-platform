# Run Record Bulk Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe bulk deletion for run records so old reports and logs can be removed from disk.

**Architecture:** Implement deletion at the storage layer so SQLite metadata and report directories are cleaned together behind one interface. Add a FastAPI form route for `/runs/delete`, then expose it in the existing `/runs` template with current-page checkboxes and a confirmation prompt.

**Tech Stack:** Python 3, FastAPI, Jinja2, SQLite, pytest, FastAPI TestClient.

## Global Constraints

- Bulk deletion is only from the `/runs` page.
- Delete selected completed/error/cancelled run records.
- Delete each deleted run's `report_dir`, including pytest HTML, JUnit XML, stdout/stderr logs, Allure results, and Allure HTML output.
- Skip `queued` and `running` records and report that they were skipped.
- Redirect back to the run history page with a clear result message.
- Do not implement automatic retention, background cleanup, or automatic cancellation of active runs.
- Do not delete paths outside the resolved `REPORTS_DIR` tree.
- Missing report directories are not errors.
- Unexpected filesystem errors must be surfaced as user-visible failures instead of silently claiming success.

---

## File Structure

- Modify `app/storage.py`: add the storage-level deletion result dataclass, path-safety helper, result-message helper, and `delete_runs(run_ids: list[str]) -> DeleteRunsResult`.
- Modify `app/main.py`: import `delete_runs`, render optional messages on `/runs`, and add `POST /runs/delete`.
- Modify `app/templates/runs.html`: add bulk selection UI, delete form, result message rendering, and select-all/confirmation JavaScript.
- Modify `tests/test_storage.py`: add unit tests for completed deletion, active skip, missing counts, and unsafe path protection.
- Modify `tests/test_routes_pagination.py`: add route/template tests for bulk-delete controls and redirect behavior.

---

### Task 1: Storage-Level Run Deletion

**Files:**
- Modify: `app/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: existing `ACTIVE_STATUSES`, `REPORTS_DIR`, `RUN_METADATA_DB`, `_connect()`, `ensure_storage()`, `get_run(run_id: str) -> TestRun | None`.
- Produces:
  - `@dataclass(frozen=True) class DeleteRunsResult` with fields `deleted: int`, `skipped_active: int`, `missing: int`.
  - `delete_runs(run_ids: list[str]) -> DeleteRunsResult`.
  - `format_delete_runs_message(result: DeleteRunsResult) -> str`.

- [ ] **Step 1: Add failing storage tests**

Append these tests to `tests/test_storage.py`:

```python
def test_delete_runs_removes_completed_run_from_sqlite_and_disk(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    report_dir = Path(run.report_dir)
    (report_dir / "stdout.log").write_text("log", encoding="utf-8")
    (report_dir / "allure-results").mkdir()
    (report_dir / "allure-results" / "result.json").write_text("{}", encoding="utf-8")
    storage.update_run(run.id, status="passed", return_code=0, finished_at=utc_now())

    result = storage.delete_runs([run.id])

    assert result.deleted == 1
    assert result.skipped_active == 0
    assert result.missing == 0
    assert storage.get_run(run.id) is None
    assert not report_dir.exists()
    assert storage.count_runs() == 0


def test_delete_runs_skips_active_runs_and_leaves_disk_intact(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    report_dir = Path(run.report_dir)
    (report_dir / "stdout.log").write_text("still running", encoding="utf-8")
    storage.update_run(run.id, status="running", started_at=utc_now())

    result = storage.delete_runs([run.id])

    assert result.deleted == 0
    assert result.skipped_active == 1
    assert result.missing == 0
    assert storage.get_run(run.id) is not None
    assert report_dir.exists()
    assert (report_dir / "stdout.log").read_text(encoding="utf-8") == "still running"


def test_delete_runs_counts_missing_ids(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    result = storage.delete_runs(["missing001"])

    assert result.deleted == 0
    assert result.skipped_active == 0
    assert result.missing == 1


def test_delete_runs_refuses_report_dir_outside_reports_root(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    outside_dir = tmp_path / "outside-report"
    outside_dir.mkdir()
    (outside_dir / "keep.txt").write_text("do not delete", encoding="utf-8")
    storage.update_run(run.id, status="passed", finished_at=utc_now(), report_dir=str(outside_dir))

    try:
        storage.delete_runs([run.id])
    except ValueError as exc:
        assert "outside reports directory" in str(exc)
    else:
        raise AssertionError("delete_runs should reject report_dir outside REPORTS_DIR")

    assert storage.get_run(run.id) is not None
    assert outside_dir.exists()
    assert (outside_dir / "keep.txt").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_storage.py::test_delete_runs_removes_completed_run_from_sqlite_and_disk \
  tests/test_storage.py::test_delete_runs_skips_active_runs_and_leaves_disk_intact \
  tests/test_storage.py::test_delete_runs_counts_missing_ids \
  tests/test_storage.py::test_delete_runs_refuses_report_dir_outside_reports_root -v
```

Expected: FAIL with `AttributeError: module 'app.storage' has no attribute 'delete_runs'`.

- [ ] **Step 3: Implement storage deletion**

In `app/storage.py`, add imports at the top:

```python
import shutil
from dataclasses import dataclass
```

Add this dataclass after `_initialized_storage`:

```python
@dataclass(frozen=True)
class DeleteRunsResult:
    deleted: int = 0
    skipped_active: int = 0
    missing: int = 0
```

Add these helpers after `count_runs()`:

```python
def _safe_report_dir_for_delete(report_dir: str) -> Path:
    reports_root = REPORTS_DIR.resolve()
    target = Path(report_dir).resolve()
    if target != reports_root and reports_root not in target.parents:
        raise ValueError(f"Refusing to delete report directory outside reports directory: {target}")
    return target


def format_delete_runs_message(result: DeleteRunsResult) -> str:
    parts: list[str] = []
    if result.deleted:
        parts.append(f"已删除 {result.deleted} 条运行记录")
    else:
        parts.append("未删除任何记录")
    if result.skipped_active:
        parts.append(f"跳过 {result.skipped_active} 条运行中记录")
    if result.missing:
        parts.append(f"忽略 {result.missing} 条不存在记录")
    return "，".join(parts) + "。"


def delete_runs(run_ids: list[str]) -> DeleteRunsResult:
    ensure_storage()
    deleted = 0
    skipped_active = 0
    missing = 0

    for run_id in run_ids:
        run = get_run(run_id)
        if not run:
            missing += 1
            continue
        if run.status in ACTIVE_STATUSES:
            skipped_active += 1
            continue

        report_dir = _safe_report_dir_for_delete(run.report_dir)
        with _lock, _connect() as conn:
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        if report_dir.exists():
            shutil.rmtree(report_dir)
        deleted += 1

    return DeleteRunsResult(deleted=deleted, skipped_active=skipped_active, missing=missing)
```

- [ ] **Step 4: Run storage deletion tests**

Run:

```bash
pytest tests/test_storage.py::test_delete_runs_removes_completed_run_from_sqlite_and_disk \
  tests/test_storage.py::test_delete_runs_skips_active_runs_and_leaves_disk_intact \
  tests/test_storage.py::test_delete_runs_counts_missing_ids \
  tests/test_storage.py::test_delete_runs_refuses_report_dir_outside_reports_root -v
```

Expected: PASS.

- [ ] **Step 5: Run all storage tests**

Run:

```bash
pytest tests/test_storage.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit storage deletion**

Run:

```bash
git add app/storage.py tests/test_storage.py
git commit -m "Add storage deletion for run records" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Delete Route and Run Page Context

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_routes_pagination.py`

**Interfaces:**
- Consumes from Task 1:
  - `delete_runs(run_ids: list[str]) -> DeleteRunsResult`
  - `format_delete_runs_message(result: DeleteRunsResult) -> str`
- Produces:
  - `POST /runs/delete` form route.
  - `/runs` template context includes `message` and `error` values.

- [ ] **Step 1: Add failing route tests**

Append these tests to `tests/test_routes_pagination.py`:

```python
def test_runs_delete_redirects_with_success_message_and_removes_disk(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = make_completed_run(tmp_path)
    report_dir = Path(run.report_dir)
    (report_dir / "stdout.log").write_text("old log", encoding="utf-8")

    response = TestClient(main.app).post(
        "/runs/delete",
        data={"run_ids": [run.id], "page": "1", "page_size": "25"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/runs?page=1&page_size=25&message=")
    assert storage.get_run(run.id) is None
    assert not report_dir.exists()


def test_runs_delete_without_selection_redirects_with_message(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    response = TestClient(main.app).post(
        "/runs/delete",
        data={"page": "2", "page_size": "10"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/runs?page=2&page_size=10&message=")
    assert storage.count_runs() == 0


def test_runs_page_renders_delete_message_from_query(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    response = TestClient(main.app).get("/runs?message=已删除%201%20条运行记录。")

    assert response.status_code == 200
    assert "已删除 1 条运行记录。" in response.text
```

- [ ] **Step 2: Run route tests to verify they fail**

Run:

```bash
pytest tests/test_routes_pagination.py::test_runs_delete_redirects_with_success_message_and_removes_disk \
  tests/test_routes_pagination.py::test_runs_delete_without_selection_redirects_with_message \
  tests/test_routes_pagination.py::test_runs_page_renders_delete_message_from_query -v
```

Expected: FAIL with `404 Not Found` for `/runs/delete` and missing query message rendering.

- [ ] **Step 3: Implement route and context**

In `app/main.py`, update the storage import:

```python
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
```

Update the `/runs` route signature and context:

```python
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
```

Add this route between `/runs` and `/api/runs`:

```python
@app.post("/runs/delete")
async def delete_selected_runs(
    run_ids: list[str] = Form(default=[]),
    page: int = Form(1),
    page_size: int = Form(25),
):
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    if not run_ids:
        message = "请选择要删除的运行记录。"
    else:
        try:
            result = delete_runs(run_ids)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除运行记录失败：{exc}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = format_delete_runs_message(result)
    return RedirectResponse(
        url=f"/runs?page={safe_page}&page_size={safe_page_size}&message={quote(message)}",
        status_code=http_status.HTTP_303_SEE_OTHER,
    )
```

Also add this import near the top of `app/main.py`:

```python
from urllib.parse import quote
```

- [ ] **Step 4: Run route tests**

Run:

```bash
pytest tests/test_routes_pagination.py::test_runs_delete_redirects_with_success_message_and_removes_disk \
  tests/test_routes_pagination.py::test_runs_delete_without_selection_redirects_with_message \
  tests/test_routes_pagination.py::test_runs_page_renders_delete_message_from_query -v
```

Expected: PASS.

- [ ] **Step 5: Commit route deletion**

Run:

```bash
git add app/main.py tests/test_routes_pagination.py
git commit -m "Add run record bulk delete route" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Run History Bulk Delete UI

**Files:**
- Modify: `app/templates/runs.html`
- Test: `tests/test_routes_pagination.py`

**Interfaces:**
- Consumes from Task 2:
  - `/runs` context values: `message`, `error`, `pagination`.
  - `POST /runs/delete` accepting `run_ids`, `page`, and `page_size`.
- Produces: visible bulk-delete controls on the run history page.

- [ ] **Step 1: Add failing template test**

Append this test to `tests/test_routes_pagination.py`:

```python
def test_runs_page_renders_bulk_delete_controls(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = make_completed_run(tmp_path)

    response = TestClient(main.app).get("/runs?page=1&page_size=25")

    assert response.status_code == 200
    assert 'form id="bulk-delete-form" method="post" action="/runs/delete"' in response.text
    assert 'input type="checkbox" id="select-all-runs"' in response.text
    assert f'input type="checkbox" name="run_ids" value="{run.id}"' in response.text
    assert 'button type="submit" class="link-button danger-button"' in response.text
    assert "删除选中记录" in response.text
    assert "确认删除选中的运行记录及其报告/日志文件吗？" in response.text
```

- [ ] **Step 2: Run template test to verify it fails**

Run:

```bash
pytest tests/test_routes_pagination.py::test_runs_page_renders_bulk_delete_controls -v
```

Expected: FAIL because the form, checkboxes, and confirmation text are not rendered yet.

- [ ] **Step 3: Implement template UI**

In `app/templates/runs.html`, after the section title block and before `<section class="history-summary">`, add:

```html
  {% if message %}
    <div class="alert status-ok">{{ message }}</div>
  {% endif %}
  {% if error %}
    <div class="alert">{{ error }}</div>
  {% endif %}
```

Replace the existing block from line 64 (`{% if runs %}`) through the closing `</table>` at line 104 with this form-wrapped table:

```html
  {% if runs %}
    <h2>全部运行</h2>
    <form id="bulk-delete-form" method="post" action="/runs/delete">
      <input type="hidden" name="page" value="{{ pagination.page }}">
      <input type="hidden" name="page_size" value="{{ pagination.page_size }}">
      <div class="actions">
        <button type="submit" class="link-button danger-button">删除选中记录</button>
      </div>
      <div class="pagination">
        {% if pagination.has_prev %}
          <a href="/runs?page={{ pagination.prev_page }}&page_size={{ pagination.page_size }}">上一页</a>
        {% else %}
          <span class="pagination-disabled">上一页</span>
        {% endif %}
        <span class="muted">第 {{ pagination.page }} / {{ pagination.total_pages }} 页，共 {{ pagination.total }} 条，每页 {{ pagination.page_size }} 条</span>
        {% if pagination.has_next %}
          <a href="/runs?page={{ pagination.next_page }}&page_size={{ pagination.page_size }}">下一页</a>
        {% else %}
          <span class="pagination-disabled">下一页</span>
        {% endif %}
      </div>
      <table>
        <thead>
          <tr>
            <th><input type="checkbox" id="select-all-runs" aria-label="选择当前页全部运行记录"></th>
            <th>ID</th>
            <th>项目</th>
            <th>状态</th>
            <th>路径</th>
            <th>创建时间</th>
            <th>耗时</th>
            <th>返回码</th>
          </tr>
        </thead>
        <tbody>
          {% for run in runs %}
            <tr>
              <td><input type="checkbox" name="run_ids" value="{{ run.id }}" aria-label="选择运行记录 {{ run.id }}"></td>
              <td><a href="/runs/{{ run.id }}"><code>{{ run.id }}</code></a></td>
              <td>{{ run.project_name }} <span class="muted">({{ run.project_id }})</span></td>
              <td><span class="badge badge-{{ run.status }}">{{ run.status }}</span></td>
              <td><code>{{ run.test_path }}</code></td>
              <td>{{ run.created_at }}</td>
              <td>{% if run.duration_seconds is not none %}{{ run.duration_seconds }}s{% else %}-{% endif %}</td>
              <td>{{ run.return_code if run.return_code is not none else '-' }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
```

Keep the existing second pagination block, then close the form immediately after that pagination block:

```html
    </form>
```

Before `{% endblock %}`, add this script:

```html
<script>
(() => {
  const form = document.getElementById("bulk-delete-form");
  const selectAll = document.getElementById("select-all-runs");
  if (!form || !selectAll) return;

  function runCheckboxes() {
    return Array.from(form.querySelectorAll('input[name="run_ids"]'));
  }

  selectAll.addEventListener("change", () => {
    for (const checkbox of runCheckboxes()) {
      checkbox.checked = selectAll.checked;
    }
  });

  form.addEventListener("submit", (event) => {
    const selectedCount = runCheckboxes().filter((checkbox) => checkbox.checked).length;
    if (selectedCount === 0) return;
    const ok = window.confirm("确认删除选中的运行记录及其报告/日志文件吗？运行中记录会被跳过。");
    if (!ok) event.preventDefault();
  });
})();
</script>
```

- [ ] **Step 4: Run template test**

Run:

```bash
pytest tests/test_routes_pagination.py::test_runs_page_renders_bulk_delete_controls -v
```

Expected: PASS.

- [ ] **Step 5: Run route pagination tests**

Run:

```bash
pytest tests/test_routes_pagination.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit UI**

Run:

```bash
git add app/templates/runs.html tests/test_routes_pagination.py
git commit -m "Add run history bulk delete UI" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Final Verification

**Files:**
- Modify: none expected unless verification finds a bug.
- Test: full test suite and app smoke test.

**Interfaces:**
- Consumes all tasks.
- Produces verified working feature.

- [ ] **Step 1: Run focused tests**

Run:

```bash
pytest tests/test_storage.py tests/test_routes_pagination.py -v
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
pytest
```

Expected: PASS. If the environment lacks FastAPI or another dependency, install dependencies first with:

```bash
python3 -m pip install -r requirements.txt
```

If proxy is needed, run:

```bash
https_proxy=http://127.0.0.1:7897 \
http_proxy=http://127.0.0.1:7897 \
all_proxy=socks5://127.0.0.1:7897 \
python3 -m pip install -r requirements.txt
```

- [ ] **Step 3: Start the app**

Run:

```bash
PYTHONPATH="/Users/mac/Documents/pytest" python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Expected log includes:

```text
Uvicorn running on http://127.0.0.1:8000
```

- [ ] **Step 4: Smoke test the run history page**

Run:

```bash
curl -sS http://127.0.0.1:8000/runs | grep -E "删除选中记录|select-all-runs|bulk-delete-form"
```

Expected output includes all three markers.

- [ ] **Step 5: Check git status**

Run:

```bash
git status --short
```

Expected: clean working tree after all task commits.

---

## Self-Review

- Spec coverage: storage deletion, active-run skipping, report directory cleanup, query-message redirect, template controls, and tests are all covered by Tasks 1-3. Final verification is covered by Task 4.
- Placeholder scan: no TBD/TODO/fill-in instructions remain; each code step includes exact snippets and commands.
- Type consistency: `DeleteRunsResult`, `delete_runs`, and `format_delete_runs_message` are defined in Task 1 and imported/used with the same names in Task 2.
