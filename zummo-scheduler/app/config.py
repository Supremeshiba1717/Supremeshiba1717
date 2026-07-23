"""Central configuration, loaded from environment variables / .env.

All secrets and tunable knobs live here so nothing is hardcoded elsewhere.
Everything is read once at import time into a single `settings` object.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Database ---
    database_url: str = "sqlite:///./zummo.db"

    # --- Twilio ---
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    twilio_test_mode: bool = True

    # --- Manager ---
    manager_name: str = "Steve"
    manager_phone_number: str = ""

    # --- LLM ---
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-4-5-20251001"

    # --- ConnecTeam ---
    connecteam_api_key: str = ""
    connecteam_base_url: str = ""

    # --- Cycle timing ---
    cycle_kickoff_day_of_week: int = 1  # 0=Mon ... 6=Sun
    cycle_kickoff_hour: int = 20
    cycle_kickoff_minute: int = 0
    timezone: str = "America/New_York"

    reminder_offset_hours: int = 24
    response_cutoff_hours: int = 48
    nonresponder_cooldown_hours: int = 6

    # --- Shift offers (revision loop) ---
    default_offer_count: int = 1
    offer_timeout_hours: int = 2
    max_offer_attempts: int = 5

    # --- App / hosting ---
    public_base_url: str = ""

    @property
    def use_connecteam(self) -> bool:
        """True only when real ConnecTeam creds are present; otherwise stub."""
        return bool(self.connecteam_api_key and self.connecteam_base_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
