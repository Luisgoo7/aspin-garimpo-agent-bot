from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str = "development"
    log_level: str = "INFO"
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    database_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    groq_api_key: str = ""
    gemini_api_key: str = ""
    nvidia_api_key: str = ""
    datajud_api_key: str = ""
    datajud_tribunals: str = "api_publica_trf1"
    poll_interval_minutes: int = 60
    initial_lookback_days: int = 7
    incremental_lookback_minutes: int = 60
    min_confidence: float = 0.78
    datajud_page_size: int = 20
    max_classify_per_cycle: int = 60
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
