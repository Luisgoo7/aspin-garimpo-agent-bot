import re

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Endpoints oficiais api_publica_* do DataJud (CNJ). Foco: SP + Justiça Federal + RJ.
DEFAULT_DATAJUD_TRIBUNALS = ",".join((
    "api_publica_tjsp", "api_publica_trf3", "api_publica_trf1", "api_publica_trf2",
    "api_publica_trf4", "api_publica_trf5", "api_publica_trf6", "api_publica_tjrj",
    "api_publica_tjmg",
))


DATABASE_URL_NORMALIZED = [False]


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
    datajud_tribunals: str = DEFAULT_DATAJUD_TRIBUNALS
    datajud_max_pages: int = Field(default=5, ge=1, le=100)
    datajud_time_budget_seconds: int = Field(default=300, ge=30, le=3600)
    datajud_timeout_seconds: float = Field(default=60, ge=5, le=300)
    max_claims_per_cycle: int = Field(default=2000, ge=1, le=100000)
    backfill_time_budget_seconds: float = Field(default=120, ge=5, le=1800)
    datajud_request_delay: float = Field(default=1.5, ge=0, le=30)
    datajud_initial_lookback_days: int = Field(default=30, ge=1, le=120)
    datajud_max_expedition_age_days: int = Field(default=180, ge=7, le=3650)
    djen_tribunals: str = ""
    max_document_age_days: int = Field(default=30, ge=1, le=365)
    simulate_failure: str = ""
    enable_datajud: bool = True
    enable_djen: bool = True
    enable_tjsp_probe: bool = True
    poll_interval_minutes: int = Field(default=60, ge=5)
    status_interval_minutes: int = Field(default=360, ge=5)
    # "failures": o canal só recebe status quando o coletor falha; "always": resumo periódico.
    status_telegram: str = "failures"
    # Entrega: só as N melhores, nestes horários locais.
    digest_hours: str = "8,12,18"
    digest_timezone: str = "America/Sao_Paulo"
    digest_top_n: int = Field(default=3, ge=1, le=20)
    digest_pool_size: int = Field(default=50, ge=1, le=1000)
    # Planilha Google (Apps Script Web App) que recebe as oportunidades aprovadas.
    sheets_webhook_url: str = ""
    sheets_token: str = ""
    # IDs numéricos do Telegram autorizados a aprovar/descartar (vazio = qualquer membro do canal).
    telegram_approvers: str = ""
    sheet_failure_streak_to_alert: int = Field(default=3, ge=1, le=100)
    collector_max_gap_minutes: int = Field(default=65, ge=10, le=1440)
    daemon_poll_seconds: int = Field(default=25, ge=1, le=50)
    daemon_queue_check_seconds: int = Field(default=120, ge=10, le=3600)
    ai_max_chars: int = Field(default=6000, ge=1000, le=30000)
    failure_streak_to_alert: int = Field(default=3, ge=1, le=48)
    digest_max_delay_minutes: int = Field(default=180, ge=10, le=720)
    initial_lookback_days: int = Field(default=7, ge=1, le=30)
    incremental_lookback_minutes: int = Field(default=120, ge=1)
    min_confidence: float = Field(default=0.78, ge=0, le=1)
    min_opportunity_amount: float = Field(default=100000, ge=0)
    aspin_commission_rate: float = Field(default=0.05, ge=0, le=1)
    datajud_page_size: int = Field(default=100, ge=1, le=1000)
    max_classify_per_cycle: int = Field(default=25, ge=1, le=1000)
    djen_page_size: int = Field(default=50, ge=1, le=100)
    djen_max_pages: int = Field(default=10, ge=1, le=200)
    djen_max_documents: int = Field(default=2500, ge=1, le=20000)
    djen_max_requests: int = Field(default=80, ge=1, le=2000)
    djen_request_delay: float = Field(default=0.5, ge=0)
    djen_timeout_seconds: float = Field(default=30, ge=1, le=120)
    max_processing_attempts: int = Field(default=5, ge=1, le=20)
    cycle_reserve_seconds: int = Field(default=240, ge=30, le=1800)
    ai_time_budget_seconds: int = Field(default=600, ge=30, le=7200)
    cycle_timeout_seconds: int = Field(default=1500, ge=60, le=7200)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("database_url", mode="before")
    @classmethod
    def _strip_whitespace_inside_uri(cls, value):
        # Espaço/quebra de linha nunca é válido dentro de uma URI PostgreSQL (seria %20).
        # Acontece ao colar a URI quebrada em várias linhas no secret.
        text = str(value or "")
        cleaned = re.sub(r"\s+", "", text)
        if cleaned != text.strip():
            DATABASE_URL_NORMALIZED[0] = True
        return cleaned

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
