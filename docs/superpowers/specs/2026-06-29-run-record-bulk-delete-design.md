# Run Record Bulk Delete Design

## Goal

Prevent long-running use of the pytest runner platform from filling the disk with accumulated run reports, logs, and metadata. Add a safe manual bulk-delete feature to the run history page.

## Scope

This design covers bulk deletion from the `/runs` page only.

In scope:

- Select multiple run records on the current run history page.
- Delete selected completed/error/cancelled run records.
- Delete each deleted run's report directory, including pytest HTML, JUnit XML, stdout/stderr logs, Allure results, and Allure HTML output.
- Skip `queued` and `running` records and report that they were skipped.
- Redirect back to the run history page with a clear result message.
- Add storage and route tests for deletion behavior.

Out of scope for this first version:

- Automatic retention by age or count.
- Deleting all records across all pages without explicit selection.
- Automatically cancelling active runs before deletion.
- Background cleanup jobs.

## User Experience

On the `/runs` page, the "全部运行" table gains a selection column:

- The header contains a checkbox to select or clear all visible rows on the current page.
- Each run row contains a checkbox named `run_ids` with the run ID as its value.
- A "删除选中记录" button appears near the table controls.
- Submitting the form shows a browser confirmation dialog explaining that the action deletes both records and report/log files.

After deletion, the user returns to `/runs` on the current page and sees a result message such as:

- `已删除 3 条运行记录。`
- `已删除 3 条运行记录，跳过 1 条运行中记录。`
- `未删除任何记录，跳过 2 条运行中记录。`

If no records are selected, the page should show a non-destructive message asking the user to select records first.

## Backend Behavior

Add a storage-level deletion function that accepts run IDs and returns a small result object with counts:

- `deleted`: records removed from SQLite and disk.
- `skipped_active`: records found but not deleted because their status is `queued` or `running`.
- `missing`: IDs not found in storage.

Deletion rules:

1. Load each run by ID.
2. If missing, increment `missing`.
3. If status is active (`queued` or `running`), increment `skipped_active` and leave all files and metadata intact.
4. Otherwise, delete the SQLite row and remove `run.report_dir` recursively.
5. Disk deletion should be best-effort but safe: missing report directories are not errors; unexpected filesystem errors should be surfaced as a user-visible failure instead of silently claiming success.

The function must not delete paths outside the run's report directory. Since run directories are created under the configured `REPORTS_DIR`, deletion should resolve `run.report_dir` and require it to be equal to or inside resolved `REPORTS_DIR` before removing it.

## Routes

Add a POST route:

```text
POST /runs/delete
```

Form fields:

- `run_ids`: repeated selected run IDs.
- `page`: current page, used for redirect.
- `page_size`: current page size, used for redirect.

Responses:

- On success or partial success, redirect to `/runs?page=<page>&page_size=<page_size>&message=<summary>`.
- On filesystem failure, return an HTTP error with a clear message.

The existing `/api/runs` and run-detail endpoints remain unchanged except that deleted runs naturally return 404 or disappear from lists.

## Templates

Update `app/templates/runs.html`:

- Wrap the table in a POST form targeting `/runs/delete`.
- Add hidden `page` and `page_size` inputs.
- Add selection checkboxes.
- Add a delete button and confirmation JavaScript.
- Render optional `message` and `error` query/context values at the top of the card.

Keep the existing pagination and history summary behavior. Deletion should not require JavaScript to submit, but JavaScript should provide the confirmation and current-page select-all convenience.

## Testing

Add automated coverage for:

- Storage deletion removes a completed run from SQLite and removes its report directory.
- Storage deletion skips active runs and leaves their report directories intact.
- Storage deletion counts missing IDs.
- The `/runs/delete` route redirects back to `/runs` with a success message.
- The `/runs` page renders bulk-delete controls.

Existing pagination, report, and run-status tests should continue to pass.

## Future Enhancements

If manual cleanup is not enough, add a separate retention feature later:

- Keep only the newest N runs.
- Delete runs older than N days.
- Show estimated disk usage before deletion.
- Schedule cleanup on application startup or via an explicit admin action.
