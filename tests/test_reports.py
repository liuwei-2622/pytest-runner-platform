from pathlib import Path

from app.reports import parse_junit_report


def test_parse_junit_report_counts_outcomes(tmp_path: Path):
    path = tmp_path / "junit.xml"
    path.write_text(
        """
        <testsuite time="1.5">
          <testcase classname="tests.test_demo" name="test_pass" time="0.1" />
          <testcase classname="tests.test_demo" name="test_fail" time="0.2" file="tests/test_demo.py" line="10">
            <failure message="failed message">assert 1 == 2</failure>
          </testcase>
          <testcase classname="tests.test_demo" name="test_error" time="0.3">
            <error message="error message">Traceback</error>
          </testcase>
          <testcase classname="tests.test_demo" name="test_skip" time="0.4">
            <skipped message="skip reason" />
          </testcase>
        </testsuite>
        """,
        encoding="utf-8",
    )

    report = parse_junit_report(path)

    assert report.exists is True
    assert report.total == 4
    assert report.passed == 1
    assert report.failed == 1
    assert report.errors == 1
    assert report.skipped == 1
    assert report.time_seconds == 1.5
    assert [case.outcome for case in report.failed_cases] == ["failed", "error"]
    assert report.skipped_cases[0].message == "skip reason"


def test_parse_junit_report_supports_testsuites_root(tmp_path: Path):
    path = tmp_path / "junit.xml"
    path.write_text(
        """
        <testsuites>
          <testsuite>
            <testcase classname="a" name="one" time="0.2" />
          </testsuite>
          <testsuite>
            <testcase classname="b" name="two" time="0.3" />
          </testsuite>
        </testsuites>
        """,
        encoding="utf-8",
    )

    report = parse_junit_report(path)

    assert report.total == 2
    assert report.passed == 2
    assert report.time_seconds == 0.5


def test_parse_junit_report_handles_missing_and_malformed_files(tmp_path: Path):
    missing = parse_junit_report(tmp_path / "missing.xml")
    assert missing.exists is False

    malformed_path = tmp_path / "bad.xml"
    malformed_path.write_text("<testsuite>", encoding="utf-8")
    malformed = parse_junit_report(malformed_path)
    assert malformed.exists is True
    assert "JUnit XML 解析失败" in malformed.error_message
