import os
import time


def test_passes():
    assert 1 + 1 == 2


def test_fails():
    assert "pytest" == "platform"


def test_slow():
    time.sleep(1)
    assert True


def test_env_var():
    assert os.getenv("PYTEST_PLATFORM_ENV") == "enabled"
