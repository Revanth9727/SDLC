from pathlib import Path

import pytest

from app.agents.executor import ExecutorAgent
from app.agents.state import SubtaskState
from app.tools.test_runner import run_tests

from app.tools.test_preflight import (
    clear_preflight,
    credential_prompt,
    mark_prompt_offered,
    predict_test_credentials,
    remember_preflight,
)


def test_preflight_scans_only_setup_files_and_batches_likely_secrets(tmp_path: Path):
    (tmp_path / ".env.example").write_text("DB_PASSWORD=\nLOG_LEVEL=info\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("password: ${REDIS_AUTH_TOKEN}\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text("os.environ['PAYMENTS_API_KEY']\n", encoding="utf-8")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "test.yml").write_text("token: ${{ secrets.CI_SECRET }}\n", encoding="utf-8")
    (tmp_path / "application.py").write_text("os.getenv('MISSED_PRIVATE_KEY')\n", encoding="utf-8")

    assert predict_test_credentials(tmp_path) == [
        "CI_SECRET", "DB_PASSWORD", "PAYMENTS_API_KEY", "REDIS_AUTH_TOKEN"
    ]


def test_predictions_are_optional_and_not_repeated_after_runtime_surprise():
    subtask_id = "preflight-test"
    clear_preflight(subtask_id)
    remember_preflight(subtask_id, ["DB_PASSWORD", "OPTIONAL_API_TOKEN"])

    first = credential_prompt(subtask_id, ["DB_PASSWORD"])
    assert first == [
        {"name": "DB_PASSWORD", "required": True, "source": "runtime"},
        {"name": "OPTIONAL_API_TOKEN", "required": False, "source": "preflight"},
    ]

    mark_prompt_offered(subtask_id)
    remember_preflight(subtask_id, ["DB_PASSWORD", "OPTIONAL_API_TOKEN"])
    assert credential_prompt(subtask_id, ["SURPRISE_SECRET"]) == [
        {"name": "SURPRISE_SECRET", "required": True, "source": "runtime"}
    ]
    clear_preflight(subtask_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("test_body", "expected"),
    [
        ("def test_ok():\n    assert True\n", "PASS"),
        ("def test_failure():\n    assert 1 == 2\n", "FAIL"),
    ],
)
async def test_preflight_predictions_never_determine_runtime_verdict(tmp_path: Path, test_body: str, expected: str):
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\nDATABASE_PASSWORD=\n", encoding="utf-8")
    (tmp_path / "test_runtime.py").write_text(test_body, encoding="utf-8")
    state = SubtaskState(
        ticket_id="00000000-0000-0000-0000-000000000001",
        subtask_id="00000000-0000-0000-0000-000000000002",
        subtask_type="bug",
        description="verification regression",
        repo="owner/repo",
    )
    agent = ExecutorAgent(
        llm=object(), repo_tool=object(), code_search=object(), test_runner=run_tests,
    )

    result = await agent._run_tests(state, tmp_path)

    assert predict_test_credentials(tmp_path) == ["DATABASE_PASSWORD", "OPENAI_API_KEY"]
    assert result.verification_outcome == expected
    assert result.required_credentials == []
    assert credential_prompt(state.subtask_id, []) == []
