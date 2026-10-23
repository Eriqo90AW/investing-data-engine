from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FINANCIALS_", extra="ignore")

    database_url: str = "postgresql://financials:financials@postgres:5432/financials"
    document_dir: Path = Path("/data/documents")
    sec_contact_email: str | None = Field(default=None, validation_alias=AliasChoices("FINANCIALS_SEC_CONTACT_EMAIL", "SEC_CONTACT_EMAIL"))
    ninerouter_api_key: str | None = Field(default=None, validation_alias=AliasChoices("FINANCIALS_NINEROUTER_API_KEY", "NINEROUTER_API_KEY"))
    ninerouter_base_url: str = "https://api.ercloud.site/v1"
    ninerouter_model: str = "go-free"
    extraction_timeout_seconds: int = 900
    ocr_tessdata_dir: Path | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


