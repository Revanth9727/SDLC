from pathlib import Path

from app.tools.test_runner import TestResult, ephemeral_test_environment, run_tests


def test_deterministic_three_way_classification():
    failure = TestResult(passed=False, returncode=1,
                         output="FAILED test_total.py::test_total - assert 4 == 5")
    collection = TestResult(passed=False, returncode=2,
                            output="ERROR collecting tests/test_api.py\nModuleNotFoundError: No module named 'service'")
    missing = TestResult(passed=False, returncode=1, output=(
        "environment variable DB_PASSWORD is required\n"
        "Missing env var API_SECRET"
    ))

    assert failure.verification_outcome == "FAIL"
    assert collection.verification_outcome == "UNVERIFIABLE"
    assert missing.verification_outcome == "UNVERIFIABLE"
    assert missing.required_credentials == ["API_SECRET", "DB_PASSWORD"]


def test_executed_failure_signatures_do_not_override_runtime_failure():
    result = TestResult(
        passed=False,
        returncode=1,
        output=(
            "F [100%]\n"
            "FAILED test_service.py::test_message - AssertionError: ImportError; "
            "KeyError: 'API_KEY'; unauthorized; connection refused\n"
            "1 failed in 0.01s\n"
        ),
    )

    assert result.verification_outcome == "FAIL"


def test_ephemeral_credentials_are_injected_then_discarded_and_redacted(tmp_path: Path):
    (tmp_path / "test_credentials.py").write_text(
        "import os\n\n"
        "def test_credentials():\n"
        "    if 'DB_PASSWORD' not in os.environ:\n"
        "        raise RuntimeError('environment variable DB_PASSWORD is required')\n"
        "    if os.environ['DB_PASSWORD'] != 'correct':\n"
        "        raise RuntimeError('authentication failed: ' + os.environ['DB_PASSWORD'])\n",
        encoding="utf-8",
    )

    missing = run_tests(tmp_path)
    assert missing.verification_outcome == "UNVERIFIABLE"
    assert missing.required_credentials == ["DB_PASSWORD"]

    with ephemeral_test_environment({"DB_PASSWORD": "correct"}):
        passed = run_tests(tmp_path)
    assert passed.verification_outcome == "PASS"

    with ephemeral_test_environment({"DB_PASSWORD": "wrong-secret-value"}):
        wrong = run_tests(tmp_path)
    assert wrong.verification_outcome == "UNVERIFIABLE"
    assert "wrong-secret-value" not in wrong.output
    assert "<redacted test output>" in wrong.output

    after = run_tests(tmp_path)
    assert after.verification_outcome == "UNVERIFIABLE"
    assert after.required_credentials == ["DB_PASSWORD"]
