from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=("settings_",),
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

    # Optional Jira status name preferences. Jira status categories are the
    # stable signal; these names only disambiguate when a category has multiple
    # valid transition targets.
    jira_status_in_progress: str = ""
    jira_status_awaiting_approval: str = ""
    jira_status_in_review: str = ""
    jira_status_blocked: str = ""
    jira_status_done: str = ""
    jira_status_cache_ttl_seconds: int = 300

    # Jira polling interval for the scheduled intake job (phase 1.6).
    jira_poll_interval_minutes: int = 30

    # Stuck-ticket threshold: active-category tickets older than this get a
    # history-aware Jira comment (phase 2.5).
    stuck_threshold_minutes: int = 120

    # Optional fallback names per stage when the primary name has no matching transition.
    # Set as JSON in .env, e.g.:
    #   JIRA_STATUS_FALLBACKS={"in_progress":"Start Progress","done":"Resolved"}
    jira_status_fallbacks: dict[str, str] = {}

    # --- Model tiering (R-33, §7d) ---
    # Big model for hard reasoning (Diagnosis, Orchestrator, Critic).
    # Cheap model for well-specified execution (Step-Planner, routing, parsing).
    model_strong: str = "gpt-4o"
    model_cheap: str = "gpt-4o-mini"
    openai_embed_model: str = "text-embedding-3-small"

    # --- Per-ticket budget (R-34, §7d) ---
    # Guard pauses and escalates when either ceiling is exceeded.
    ticket_call_budget: int = 40
    ticket_cost_budget_usd: float = 2.00

    # --- Webhook secrets (R-37, §5b) ---
    # Shared secrets to verify inbound webhooks are genuine (Phase 5.5).
    # Empty string means webhook verification is skipped (dev / not-yet-configured).
    github_webhook_secret: str = ""
    jira_webhook_secret: str = ""


settings = Settings()
