import pytest

from app.security import validate_env_vars, validate_env_vars_detailed


def test_validate_env_vars_detailed_valid_lines():
    result = validate_env_vars_detailed("API_BASE_URL=http://127.0.0.1:8000\nTOKEN=secret=value\nEMPTY=")

    assert result.ok is True
    assert result.env_vars == {
        "API_BASE_URL": "http://127.0.0.1:8000",
        "TOKEN": "secret=value",
        "EMPTY": "",
    }
    assert [line.state for line in result.lines] == ["valid", "valid", "valid"]


def test_validate_env_vars_detailed_reports_line_errors():
    result = validate_env_vars_detailed("GOOD=value\nBAD LINE\n1BAD=value")

    assert result.ok is False
    assert [issue.line for issue in result.issues] == [2, 3]
    assert "缺少" in result.issues[0].message
    assert "名称无效" in result.issues[1].message


def test_validate_env_vars_detailed_reports_control_character():
    result = validate_env_vars_detailed("BAD=value\x01")

    assert result.ok is False
    assert result.issues[0].line == 1
    assert "非法控制字符" in result.issues[0].message


def test_validate_env_vars_detailed_reports_size_limit():
    result = validate_env_vars_detailed("A=" + "x" * 9000)

    assert result.ok is False
    assert result.issues[0].line == 0
    assert "8 KiB" in result.issues[0].message


def test_validate_env_vars_raises_first_error():
    with pytest.raises(ValueError, match="第 2 行环境变量缺少 ="):
        validate_env_vars("GOOD=value\nBAD LINE")
