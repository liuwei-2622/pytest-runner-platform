import os


def test_demo_passes():
    assert os.getenv("DEMO_PROJECT_ENV") == "ok"
