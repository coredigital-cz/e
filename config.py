"""
config.py — Hybrid King Settings
==================================
Reads from .env file via pydantic-settings.

IMPORTANT: pydantic-settings incarca .env DOAR in obiectul `settings`,
NU si in os.environ. Dar main.py, groq_rotator.py si orchestrator.py
(MARKET) folosesc os.getenv() direct. De-aceea incarcam .env explicit
aici, o singura data, ca toate variabilele sa fie vizibile peste tot.
"""
from __future__ import annotations

from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# override=False: nu suprascrie variabile deja setate manual
# (ex: MARKET / DATABASE_URL, pe care main.py le seteaza inainte
# de acest import).
load_dotenv(override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./hybridking.db"

    # ── Groq AI ───────────────────────────────────────────
    # O singura cheie (legacy) SAU lista separata cu virgula:
    groq_api_key: str = ""
    groq_api_keys: str = ""   # Ex: "gsk_key1,gsk_key2,gsk_key3"

    # ── Vercel deployment ─────────────────────────────────
    vercel_token: str = ""
    vercel_team_id: str = ""          # leave empty if personal account

    # ── WhatsApp / WAHA (NOT used — manual mode) ─────────
    waha_api_url: str = "http://localhost:3000"
    daily_limit: int = 35

    # ── City default ──────────────────────────────────────
    default_city: str = "Praha"

    @field_validator("groq_api_key", "vercel_token", mode="before")
    @classmethod
    def strip_quotes(cls, v: str) -> str:
        return str(v).strip().strip('"').strip("'")

    @property
    def vercel_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.vercel_token}",
            "Content-Type": "application/json",
        }


settings = Settings()
