from app.core.failures import describe_failure


def test_failure_description_names_operation_type_and_message(monkeypatch):
    monkeypatch.setattr("app.core.failures.settings.github_token", "secret-token")
    message = describe_failure(
        "Cloning repository owner/repo",
        RuntimeError("remote rejected secret-token"),
    )
    assert "Cloning repository owner/repo failed" in message
    assert "RuntimeError" in message
    assert "remote rejected" in message
    assert "secret-token" not in message
    assert "<redacted>" in message
