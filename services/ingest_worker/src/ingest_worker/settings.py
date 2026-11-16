"""Ingest worker configuration (12-factor, environment variables)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from oee_shared import ValidationLimits


class IngestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "postgresql://oee_app@localhost:5432/oee?sslmode=prefer"

    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_topic: str = "oee/#"
    mqtt_client_id: str = "oee-ingest-worker"
    mqtt_tls: bool = False
    mqtt_ca_file: str | None = None
    mqtt_keepalive: int = 30

    batch_max_size: int = Field(default=1000, ge=1, le=50_000)
    batch_max_wait_ms: int = Field(default=250, ge=10, le=60_000)
    queue_max_messages: int = Field(default=20_000, ge=100)

    backoff_initial_s: float = 1.0
    backoff_max_s: float = 60.0

    site_timezone: str = "UTC"
    max_cycle_time_s: float = 86_400.0
    max_future_skew_s: float = 300.0

    heartbeat_file: str = "/tmp/oee_ingest_heartbeat"  # noqa: S108 - container-local liveness file
    heartbeat_max_age_s: float = 60.0

    @property
    def limits(self) -> ValidationLimits:
        return ValidationLimits(
            site_timezone=self.site_timezone,
            max_cycle_time_s=self.max_cycle_time_s,
            max_future_skew_s=self.max_future_skew_s,
        )
