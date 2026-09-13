from pydantic import Field
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
    app_encryption_key: str = ""
    workspace_root: str = ""
    repo_cache_dir: str = ""
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
    ticket_token_budget: int = Field(default=100000, gt=0)
    llm_est_cost_per_1k_tokens: float = Field(default=0.01, gt=0)
    ticket_call_budget: int = Field(default=40, gt=0)
    ticket_cost_budget_usd: float = Field(default=2.00, gt=0)
    ticket_time_budget_seconds: int = Field(default=7200, gt=0)
    human_gate_reminder_minutes: int = Field(default=60, ge=0)
    human_gate_expiry_minutes: int = Field(default=1440, ge=1)

    max_agent_retries: int = Field(default=2, ge=0, le=5)
    test_timeout_seconds: int = Field(default=120, ge=1, le=600)
    max_edit_file_chars: int = 60000
    workspace_min_free_mb: int = 100
    repo_index_max_file_bytes: int = Field(default=1_000_000, ge=1024)
    repo_index_excluded_dirs: list[str] = [
        "node_modules", "vendor", "dist", "build", "target", "generated", "coverage", ".git",
    ]
    code_intelligence_max_steps: int = Field(default=18, ge=4, le=50)
    git_author_name: str = "SDLC Agent"
    git_author_email: str = "sdlc-agent@users.noreply.github.com"
    jira_approval_account_ids: list[str] = []
    jira_bot_account_id: str = ""

    # --- Webhook secrets (R-37, §5b) ---
    # Shared secrets to verify inbound webhooks are genuine (Phase 5.5).
    # Empty string means webhook verification is skipped (dev / not-yet-configured).
    github_webhook_secret: str = ""
    jira_webhook_secret: str = ""

    # --- Memory / solution reuse (memory.md, R-29) ---
    memory_top_k: int = Field(default=3, ge=1, le=10)
    memory_search_threshold: float = Field(default=0.75, ge=0, le=1)
    # The "hardcoded STRONG bar" (memory.md §8a) — a match at or above this
    # skips Diagnosis + Step-Planner. Kept as a named setting for testability,
    # not because it's meant to be casually tuned.
    memory_reuse_similarity_threshold: float = Field(default=0.9, ge=0, le=1)

    # --- LLM response cache (R-36, architecture.md §7d) ---
    llm_cache_enabled: bool = True
    llm_cache_semantic_threshold: float = Field(default=0.92, ge=0, le=1)
    # Shared caching of ticket-scoped prompts is opt-in to prevent context leakage.
    llm_cache_ticket_prompts: bool = False

    # --- Runtime / deployment ---
    log_level: str = "INFO"
    ui_basic_auth_username: str = ""
    ui_basic_auth_password: str = ""
    external_retry_attempts: int = Field(default=3, ge=1, le=8)
    external_retry_base_seconds: float = Field(default=0.5, ge=0, le=30)


settings = Settings()
