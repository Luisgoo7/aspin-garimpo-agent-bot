from pydantic import Field
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
    groq_model: str = "openai/gpt-oss-20b"
    gemini_model: str = "gemini-3.8-flash"
    nvidia_model: str = "openai/gpt-oss-20b"
    datajud_api_key: str = ""
    datajud_tribunals: str = "api_publica_trf1"
    enable_datajud: bool = True
    enable_tjsp_probe: bool = True
    poll_interval_minutes: int = Field(default=60, ge=5)
    status_interval_minutes: int = Field(default=120, ge=5)
    initial_lookback_days: int = Field(default=7, ge=1, le=30)
    incremental_lookback_minutes: int = Field(default=120, ge=1)
    min_confidence: float = Field(default=0.78, ge=0, le=1)
    min_opportunity_amount: float = Field(default=100000, ge=0)
    aspin_commission_rate: float = Field(default=0.05, ge=0, le=1)
    datajud_page_size: int = Field(default=50, ge=1, le=100)
    max_classify_per_cycle: int = Field(default=120, ge=1, le=1000)
    djen_page_size: int = Field(default=50, ge=1, le=100)
    djen_max_pages: int = Field(default=10, ge=1, le=200)
    djen_max_documents: int = Field(default=2500, ge=1, le=20000)
    djen_max_requests: int = Field(default=80, ge=1, le=2000)
    djen_request_delay: float = Field(default=0.5, ge=0)
    djen_timeout_seconds: float = Field(default=30, ge=1, le=120)
    max_processing_attempts: int = Field(default=5, ge=1, le=20)
    cycle_timeout_seconds: int = Field(default=1500, ge=60, le=7200)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def configuration_errors(self) -> list[str]:
        errors = []
        for key in ("database_url", "telegram_bot_token", "telegram_chat_id"):
            if not getattr(self, key).strip():
                errors.append(key.upper())
        if not any((self.groq_api_key, self.gemini_api_key, self.nvidia_api_key)):
            errors.append("GROQ_API_KEY ou GEMINI_API_KEY ou NVIDIA_API_KEY")
        if self.enable_datajud and not self.datajud_api_key:
            errors.append("DATAJUD_API_KEY")
        if self.enable_datajud and not self.datajud_tribunals.strip():
            errors.append("DATAJUD_TRIBUNALS")
        return errors


settings = Settings()
