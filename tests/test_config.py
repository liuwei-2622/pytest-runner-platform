from app.config import default_max_workers


def test_default_max_workers_is_cpu_based_and_conservative():
    assert default_max_workers(1) == 1
    assert default_max_workers(2) == 1
    assert default_max_workers(4) == 2
    assert default_max_workers(8) == 4
    assert default_max_workers(16) == 8
    assert default_max_workers(64) == 8


def test_default_max_workers_handles_unknown_cpu_count():
    assert default_max_workers(None) == 1
