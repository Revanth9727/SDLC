"""Fixed, time-bounded pytest command; no model-authored shell commands."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from typing import Literal

from pydantic import BaseModel, computed_field
from app.config import settings

# pytest exit codes (https://docs.pytest.org/en/stable/reference/exitcodes.html):
# 0 all passed, 1 real test failures, 5 NO TESTS COLLECTED — never a failure.
TestOutcome = Literal["passed", "failed", "no_tests_collected", "timed_out"]
NO_TESTS_COLLECTED = 5


class TestResult(BaseModel):
    __test__ = False
    passed: bool
    returncode: int
    output: str
    timed_out: bool = False

    @computed_field
    @property
    def outcome(self) -> TestOutcome:
        if self.timed_out:
            return "timed_out"
        if self.returncode == NO_TESTS_COLLECTED:
            return "no_tests_collected"
        return "passed" if self.passed else "failed"


def run_tests(checkout: Path) -> TestResult:
    # Do not hand provider/Jira/GitHub credentials to repository test code.
    env = {"PATH": os.defpath, "PYTHONIOENCODING": "utf-8", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    process = subprocess.Popen([sys.executable, '-m', 'pytest', '-q'], cwd=checkout,
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    tail = bytearray()

    def drain():
        while chunk := process.stdout.read(4096):
            tail.extend(chunk)
            del tail[:-16000]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=settings.test_timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # Also reap lingering children of a completed pytest process.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        reader.join(timeout=5)
        process.stdout.close()
    return TestResult(passed=process.returncode == 0 and not timed_out,
                      returncode=process.returncode, output=tail.decode('utf-8', errors='replace'), timed_out=timed_out)
