## Final-review storage safety fixes

- Changes:
  - Updated /Users/mac/Documents/pytest/app/storage.py so _safe_report_dir_for_delete() requires report directories to be strictly inside REPORTS_DIR; REPORTS_DIR itself is rejected.
  - Updated /Users/mac/Documents/pytest/app/storage.py delete_runs() to deduplicate submitted run IDs while preserving order.
  - Updated /Users/mac/Documents/pytest/app/storage.py delete_runs() to remove report directories before deleting SQLite rows, preserving rows when shutil.rmtree() fails. Missing report directories remain non-errors.
  - Added /Users/mac/Documents/pytest/tests/test_storage.py coverage for REPORTS_DIR root rejection, rmtree failure retry safety, and duplicate ID deduplication.

- Tests:
  - python3 -m pytest /Users/mac/Documents/pytest/tests/test_storage.py -k 'reports_root_as_report_dir or rmtree_fails or deduplicates' -v (3 passed)
  - python3 -m pytest /Users/mac/Documents/pytest/tests/test_storage.py -v (15 passed)

- Commit:
  - ac2b0d0 Fix run deletion safety issues

## Final re-review failure-path fixes

- Changes:
  - Updated /Users/mac/Documents/pytest/app/storage.py delete_runs() to call shutil.rmtree() directly and ignore only FileNotFoundError for missing report directories.
  - Preserved run metadata rows when shutil.rmtree() raises any other OSError so deletion can be retried after the filesystem issue is fixed.
  - Updated /Users/mac/Documents/pytest/app/main.py /runs/delete handling to catch sqlite3.Error along with filesystem/path validation failures and redirect back to /runs with a clear Chinese error message.
  - Added /Users/mac/Documents/pytest/tests/test_storage.py coverage for FileNotFoundError-as-missing-directory and non-FileNotFoundError retry safety.
  - Added /Users/mac/Documents/pytest/tests/test_routes_pagination.py coverage for sqlite3.OperationalError route redirects and rendered error text.

- Tests:
  - python3 -m pytest tests/test_storage.py::test_delete_runs_treats_file_not_found_rmtree_as_missing_report_dir tests/test_storage.py::test_delete_runs_keeps_row_and_report_dir_when_rmtree_fails tests/test_routes_pagination.py::test_runs_delete_database_error_redirects_with_error_message -v (3 passed)
  - python3 -m pytest tests/test_storage.py tests/test_routes_pagination.py -v (28 passed)

- Commit:
  - 2758730 Handle run delete failure paths safely

