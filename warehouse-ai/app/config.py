from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini"
    report_with_llm: bool = True

    # Destructive developer utilities require this value to be set explicitly.
    # The application itself does not branch on it.
    app_env: str = ""
    warehouse_timezone: str = ""

    database_url: str = ""
    neo4j_uri: str = ""
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    redis_url: str = ""

    optimizer_backend: Literal["auto", "local", "cuopt"] = "auto"
    routing_backend: Literal["internal", "mapf"] = "internal"
    cuopt_url: str = ""
    cuopt_client_sak: str = ""
    # Primary API Catalog key. The P16.4 CUOPT_CLIENT_SAK name remains as a fallback alias.
    cuopt_api_key: str = ""
    cuopt_rest_url: str = "https://optimize.api.nvidia.com/v1/nvidia/cuopt"
    cuopt_status_url: str = "https://optimize.api.nvidia.com/v1/status/{request_id}"
    # Makes a copied legacy OPTIMIZER_BACKEND=local .env key-only compatible.
    cuopt_auto_enable: bool = True
    cuopt_poll_timeout_seconds: float = 30.0
    cuopt_poll_interval_seconds: float = 1.0
    cuopt_solver_time_limit_seconds: int = 10
    mapf_url: str = ""
    robot_gateway_url: str = ""
    cuopt_fallback_to_local: bool = True
    mapf_fallback_to_internal: bool = True

    request_timeout_seconds: float = 30.0
    robot_gateway_max_attempts: int = 3
    robot_gateway_retry_backoff_seconds: float = 0.2
    freeze_horizon_seconds: int = 15
    max_replan_count: int = 3
    time_step_seconds: int = 5
    max_mapf_time_steps: int = 720
    min_robot_battery: float = 20.0
    battery_safety_margin_percent: float = 0.5
    energy_per_distance: float = 0.05
    charge_target_battery: float = 80.0
    charge_rate_percent_per_minute: float = 5.0
    opportunity_charging_enabled: bool = True
    opportunity_charge_target_battery: float = 95.0
    opportunity_charge_min_idle_minutes: float = 15.0
    opportunity_charge_min_gain_percent: float = 2.0

    def missing_for_connections(self) -> list[str]:
        required = {
            "DATABASE_URL": self.database_url,
            "NEO4J_URI": self.neo4j_uri,
            "NEO4J_PASSWORD": self.neo4j_password,
            "REDIS_URL": self.redis_url,
        }
        return [name for name, value in required.items() if not value]

    def missing_for_planning(self) -> list[str]:
        required = {
            "OPENAI_API_KEY": self.openai_api_key,
            "DATABASE_URL": self.database_url,
            "NEO4J_URI": self.neo4j_uri,
            "NEO4J_PASSWORD": self.neo4j_password,
            "REDIS_URL": self.redis_url,
        }
        # cuOpt REST is optional because CPU fallback is always available.
        # In auto mode a CUOPT_API_KEY automatically enables NVIDIA managed cuOpt.
        if (
            self.optimizer_backend == "cuopt"
            and not self.cuopt_url
            and not self.cuopt_client_sak
            and not self.cuopt_api_key
            and not self.cuopt_fallback_to_local
        ):
            required["CUOPT_API_KEY_OR_CUOPT_URL"] = ""
        if self.routing_backend == "mapf":
            required["MAPF_URL"] = self.mapf_url
        return [name for name, value in required.items() if not value]


@lru_cache
def get_settings() -> Settings:
    return Settings()
