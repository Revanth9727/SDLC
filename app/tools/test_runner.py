"""Fixed, time-bounded pytest command; no model-authored shell commands."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import re
from contextlib import contextmanager
from contextvars import ContextVar

from typing import Iterator, Literal

from pydantic import BaseModel, computed_field
from app.config import settings

# pytest exit codes (https://docs.pytest.org/en/stable/reference/exitcodes.html):
# 0 all passed, 1 real test failures, 5 NO TESTS COLLECTED — never a failure.
TestOutcome = Literal["passed", "failed", "no_tests_collected", "timed_out"]
VerificationOutcome = Literal["PASS", "FAIL", "UNVERIFIABLE"]
NO_TESTS_COLLECTED = 5
_TEST_ENV: ContextVar[dict[str, str]] = ContextVar("ephemeral_test_environment", default={})
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_MISSING_PATTERNS = (
    re.compile(r"(?:environment variable|env var)\s+['\"]?([A-Z_][A-Z0-9_]*)['\"]?\s+(?:is\s+)?(?:not set|missing|required)", re.I),
    re.compile(r"(?:missing|required)\s+(?:environment variable|env var)\s+['\"]?([A-Z_][A-Z0-9_]*)", re.I),
    re.compile(r"KeyError:\s*['\"]([A-Z_][A-Z0-9_]*)['\"]"),
)
_UNVERIFIABLE_SIGNATURES = (
    "could not connect to server", "connection refused", "connection timed out",
    "name or service not known", "temporary failure in name resolution",
    "authentication failed", "password authentication failed", "access denied",
    "unauthorized", "forbidden", "invalid credentials", "login failed",
    "modulenotfounderror", "importerror", "error collecting", "collection error",
    "errors during collection", "could not import", "no tests ran",
)
_RUNTIME_BLOCKER_SIGNATURES = (
    "could not connect to server", "connection refused", "connection timed out",
    "name or service not known", "temporary failure in name resolution",
    "authentication failed", "password authentication failed", "access denied",
    "unauthorized", "forbidden", "invalid credentials", "login failed",
)
_EXECUTED_TEST_SUMMARY = re.compile(
    r"(?:^|[\s=,])(\d+)\s+(?:failed|passed|xfailed|xpassed)(?=\s|,|=|$)",
    re.I | re.M,
)
_FAILED_TEST_NODE = re.compile(r"(?m)^FAILED\s+\S+::\S+")
_ASSERTION_FAILURE = re.compile(r"(?im)(?:\bAssertionError\b|^\s*>\s*assert\b|^\s*E\s+assert\b)")


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

    @computed_field
    @property
    def verification_outcome(self) -> VerificationOutcome:
        if self.passed and self.returncode == 0 and not self.timed_out:
            return "PASS"
        if self.timed_out:
            return "UNVERIFIABLE"
        lowered = self.output.lower()
        # Completed pytest outcomes are authoritative. Diagnostic words inside a
        # traceback from an executed test must never turn a real failure into an
        # environment/collection failure.
        if _tests_executed(self.output) and _assertion_failed(self.output):
            return "FAIL"
        # A suite can enter a test but still be unable to perform verification
        # because its required environment/service is unavailable. Those runtime
        # blockers remain UNVERIFIABLE, but only absent assertion-failure evidence.
        if (_tests_executed(self.output)
                and not self.required_credentials
                and not any(signature in lowered for signature in _RUNTIME_BLOCKER_SIGNATURES)):
            return "FAIL"
        if (self.returncode in {2, 3, 4, NO_TESTS_COLLECTED}
                or self.required_credentials
                or any(signature in lowered for signature in _UNVERIFIABLE_SIGNATURES)):
            return "UNVERIFIABLE"
        return "FAIL"

    @computed_field
    @property
    def required_credentials(self) -> list[str]:
        return _required_names(self.output)

    @computed_field
    @property
    def reason(self) -> str:
        if self.verification_outcome == "PASS":
            return "Tests ran and passed"
        if self.required_credentials:
            return "Missing test credentials: " + ", ".join(self.required_credentials)
        if self.timed_out:
            return "Test environment timed out before verification completed"
        first = next((line.strip() for line in self.output.splitlines() if line.strip()), "No diagnostic output")
        prefix = "Test environment could not run the suite" if self.verification_outcome == "UNVERIFIABLE" else "Tests ran and failed"
        return f"{prefix}: {first[:500]}"

    @computed_field
    @property
    def facts(self) -> dict[str, object]:
        return {
            "command": "python -m pytest -q",
            "exit_code": self.returncode,
            "timed_out": self.timed_out,
            "tests_passed": self.verification_outcome == "PASS",
            "environment_ready": self.verification_outcome != "UNVERIFIABLE",
            "required_credentials": self.required_credentials,
        }


@contextmanager
def ephemeral_test_environment(credentials: dict[str, str]) -> Iterator[None]:
    """Expose validated values only to test subprocesses in this request context."""
    clean = {name: value for name, value in credentials.items()
             if _ENV_NAME.fullmatch(name) and isinstance(value, str) and value}
    token = _TEST_ENV.set(clean)
    try:
        yield
    finally:
        _TEST_ENV.reset(token)


def run_tests(checkout: Path) -> TestResult:
    # Do not hand provider/Jira/GitHub credentials to repository test code.
    env = {"PATH": os.defpath, "PYTHONIOENCODING": "utf-8", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    credentials = _TEST_ENV.get()
    env.update(credentials)
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
    output = tail.decode('utf-8', errors='replace')
    if credentials:
        output = _credential_safe_output(output, process.returncode, timed_out)
    return TestResult(passed=process.returncode == 0 and not timed_out,
                      returncode=process.returncode, output=output, timed_out=timed_out)


def _required_names(output: str) -> list[str]:
    names = {match.group(1).upper() for pattern in _MISSING_PATTERNS
             for match in pattern.finditer(output)}
    return sorted(name for name in names if _ENV_NAME.fullmatch(name))


def _tests_executed(output: str) -> bool:
    """Return whether pytest reported at least one completed test outcome."""
    return any(int(match.group(1)) > 0 for match in _EXECUTED_TEST_SUMMARY.finditer(output)) or bool(
        _FAILED_TEST_NODE.search(output)
    )


def _assertion_failed(output: str) -> bool:
    """Identify pytest assertion-phase evidence, independent of traceback wording."""
    return bool(_ASSERTION_FAILURE.search(output))


def _credential_safe_output(raw: str, returncode: int, timed_out: bool) -> str:
    """Discard all output from a process that received secrets; retain safe facts only."""
    required = _required_names(raw)
    lowered = raw.lower()
    if required:
        facts = "; ".join(f"environment variable {name} is required" for name in required)
        return "<redacted test output>; " + facts
    if timed_out:
        return "<redacted test output>; test environment timed out"
    if any(marker in lowered for marker in (
        "authentication failed", "password authentication failed", "access denied",
        "unauthorized", "forbidden", "invalid credentials", "login failed",
    )):
        return "<redacted test output>; authentication failed"
    if any(marker in lowered for marker in _UNVERIFIABLE_SIGNATURES):
        return "<redacted test output>; test environment unavailable"
    return ("<redacted test output>; tests passed" if returncode == 0
            else "<redacted test output>; tests ran and failed")
