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


settings = Settings()
