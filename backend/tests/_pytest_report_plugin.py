"""临时 pytest 插件：把失败详情写进文件。

原因：backend/conftest.py 的 `pytest_sessionfinish` 直接 `os._exit(exitstatus)`，
pytest 的失败汇总（terminal summary）根本来不及打印。这里在 logreport 阶段
（一定早于 sessionfinish）就把失败细节落盘，便于排查。用完即删。
"""
from __future__ import annotations

from pathlib import Path

_OUT = Path(__file__).resolve().parent / "_pytest_failures.txt"


def pytest_sessionstart(session):
    _OUT.write_text("", encoding="utf-8")


def pytest_runtest_logreport(report):
    if report.failed:
        with _OUT.open("a", encoding="utf-8") as f:
            f.write(f"===== {report.when}: {report.nodeid} =====\n")
            f.write(str(report.longrepr))
            f.write("\n\n")
