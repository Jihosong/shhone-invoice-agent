"""환경설정 (.env) 로더."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ANTHROPIC_API_KEY: str = ""
    GOOGLE_CREDENTIALS_FILE: str = ""
    NCP_ACCESS_KEY: str = ""
    NCP_SECRET_KEY: str = ""
    NCP_SERVICE_ID: str = ""
    SMS_FROM: str = ""
    SMS_TO: str = ""
    DB_PATH: str = "data/agent.db"


settings = Settings()
