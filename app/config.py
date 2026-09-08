from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    openai_api_key: str
    openai_model: str = "gpt-4o"
    github_token: str
    github_owner: str
    github_repo: str
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    jira_project_key: str

    # Optional Jira status name mapping (internal stage -> workflow status name).
    # Leave a var unset or empty to skip that transition gracefully (R-11).
    jira_status_in_progress: str = "In Progress"
    jira_status_awaiting_approval: str = "Awaiting Approval"
    jira_status_in_review: str = "In Review"
    jira_status_blocked: str = "Blocked"
    jira_status_done: str = "Done"

    # Jira polling interval for the scheduled intake job (phase 1.6).
    jira_poll_interval_minutes: int = 30

    # Stuck-ticket threshold: tickets In Progress longer than this get a
    # history-aware Jira comment (phase 2.5).
    stuck_threshold_minutes: int = 120

    # Optional fallback names per stage when the primary name has no matching transition.
    # Set as JSON in .env, e.g.:
    #   JIRA_STATUS_FALLBACKS={"in_progress":"Start Progress","done":"Resolved"}
    jira_status_fallbacks: dict[str, str] = {}


settings = Settings()
