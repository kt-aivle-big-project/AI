"""Application configuration loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import dotenv_values

from app.domain.schemas import (
    AgentRetrievalMode,
    OptimizationBackend,
    OutboundFulfillmentMode,
    PlanningMode,
    canonicalize_planning_mode,
    normalize_warehouse_id,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _project_relative_path(value: Path | None) -> Path | None:
    """Resolve relative application paths against the project root, not the process CWD."""

    if value is None:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


class Settings(BaseSettings):
    """Central validated configuration for LLM, data files, tracing, and optimization."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "LARO v13.27 BE-Centered Structured Plan Bridge"
    app_env: str = "local"
    node_console_trace: bool = Field(default=True, alias="NODE_CONSOLE_TRACE")

    # Compatibility layer for the unmodified Spring BE OptimizationClient.
    # The Java client calls POST /optimize and POST /reoptimize with numeric
    # node/edge/robot/task IDs and camelCase JSON fields.
    be_compat_enabled: bool = Field(default=True, alias="BE_COMPAT_ENABLED")
    be_compat_robot_speed_distance_per_second: float = Field(
        default=1.0,
        alias="BE_COMPAT_ROBOT_SPEED_DISTANCE_PER_SECOND",
        gt=0,
        description=(
            "Distance units travelled per second for the legacy estimatedTime field."
        ),
    )
    be_compat_min_battery_pct: float = Field(
        default=30.0, alias="BE_COMPAT_MIN_BATTERY_PCT", ge=0, le=100
    )
    be_compat_graph_cache_ttl_seconds: int = Field(
        default=86400, alias="BE_COMPAT_GRAPH_CACHE_TTL_SECONDS", ge=60
    )
    be_compat_neo4j_projection: bool = Field(
        default=True, alias="BE_COMPAT_NEO4J_PROJECTION"
    )
    be_compat_graph_source: str = Field(
        default="auto",
        alias="BE_COMPAT_GRAPH_SOURCE",
        description=(
            "auto prefers the unmodified Spring public warehouse_node/warehouse_edge "
            "tables and falls back to the normalized laro_contract graph written by /optimize."
        ),
    )
    be_compat_graph_cache_mode: str = Field(
        default="metadata",
        alias="BE_COMPAT_GRAPH_CACHE_MODE",
        description="off, metadata, or full. metadata avoids duplicating the full static graph in Redis.",
    )
    be_compat_runtime_source: str = Field(
        default="request_then_redis",
        alias="BE_COMPAT_RUNTIME_SOURCE",
        description=(
            "request_only uses Spring's /reoptimize body; request_then_redis fills an empty "
            "robot list from Spring Redis; redis_only always reads the Spring Redis namespace."
        ),
    )
    be_compat_contract_schema_enabled: bool = Field(
        default=True, alias="BE_COMPAT_CONTRACT_SCHEMA_ENABLED"
    )
    be_compat_debug_runtime_api_enabled: bool = Field(
        default=False, alias="BE_COMPAT_DEBUG_RUNTIME_API_ENABLED"
    )
    be_compat_default_edge_status: str = Field(
        default="OPEN", alias="BE_COMPAT_DEFAULT_EDGE_STATUS"
    )

    default_planning_mode: PlanningMode = Field(
        default="llm_router",
        alias="DEFAULT_PLANNING_MODE",
        description="Server default for Rule/Agent routing. llm_router uses one tool-free input router.",
    )
    allow_request_planning_mode_override: bool = Field(
        default=False,
        alias="ALLOW_REQUEST_PLANNING_MODE_OVERRIDE",
        description="Allow an API request to override DEFAULT_PLANNING_MODE for that run.",
    )
    workload_rule_max_operation_count: int = Field(
        default=8,
        alias="WORKLOAD_RULE_MAX_OPERATION_COUNT",
        ge=1,
        description="Low-load operation ceiling that can stay on the deterministic Rule fast path.",
    )
    workload_agent_min_operation_count: int = Field(
        default=16,
        alias="WORKLOAD_AGENT_MIN_OPERATION_COUNT",
        ge=2,
        description="Operation count that deterministically requires Agent workload composition.",
    )
    workload_rule_max_operations_per_robot: float = Field(
        default=2.0,
        alias="WORKLOAD_RULE_MAX_OPERATIONS_PER_ROBOT",
        gt=0,
    )
    workload_agent_min_operations_per_robot: float = Field(
        default=3.0,
        alias="WORKLOAD_AGENT_MIN_OPERATIONS_PER_ROBOT",
        gt=0,
    )
    agent_retrieval_mode: AgentRetrievalMode = Field(
        default="parallel_plan",
        alias="AGENT_RETRIEVAL_MODE",
        description="parallel_plan uses one LLM retrieval plan and deterministic dependency waves.",
    )
    parallel_retrieval_max_workers: int = Field(
        default=4,
        alias="PARALLEL_RETRIEVAL_MAX_WORKERS",
        ge=1,
        le=16,
    )
    agent_optional_retrieval_planner: str = Field(
        default="auto",
        alias="AGENT_OPTIONAL_RETRIEVAL_PLANNER",
        description=(
            "auto skips the optional retrieval-planning LLM when canonical keys and "
            "the deterministic base DAG are sufficient; always invokes it; off never invokes it."
        ),
    )
    outbound_fulfillment_mode: OutboundFulfillmentMode = Field(
        default="goods_to_person",
        alias="OUTBOUND_FULFILLMENT_MODE",
        description=(
            "goods_to_person routes pure outbound waves through the integrated "
            "handling-unit compiler inside the main LangGraph workflow; "
            "legacy_order_tasks keeps the historical order-row task model."
        ),
    )

    g2p_distinct_robot_per_handling_unit: bool = Field(
        default=False,
        alias="G2P_DISTINCT_ROBOT_PER_HANDLING_UNIT",
        description=(
            "Legacy compatibility switch. New plans leave handling-unit cycles in the "
            "shared solver candidate space so cuOpt chooses both fleet size and assignment."
        ),
    )
    g2p_max_cycles_per_robot_per_wave: int = Field(
        default=1,
        alias="G2P_MAX_CYCLES_PER_ROBOT_PER_WAVE",
        ge=1,
        le=16,
    )
    force_agent_structured_input_router_llm: bool = Field(
        default=False,
        alias="FORCE_AGENT_STRUCTURED_INPUT_ROUTER_LLM",
        description=(
            "When false, trusted structured events in force_agent mode use the "
            "deterministic normalizer and lock Agent without an unnecessary router LLM call."
        ),
    )



    # Deferred Rule/Agent evaluation capture.  The live workflow executes only
    # the router-selected primary branch; a frozen request/context bundle is
    # persisted and can be compared later through the debug API.
    planning_evaluation_mode: str = Field(
        default="off",
        alias="PLANNING_EVALUATION_MODE",
        description="off or capture_only. Comparison is explicitly triggered later.",
    )
    planning_evaluation_persist: bool = Field(
        default=True, alias="PLANNING_EVALUATION_PERSIST"
    )
    planning_evaluation_output_dir: Path = Field(
        default=Path("runtime_outputs/evaluations"),
        alias="PLANNING_EVALUATION_OUTPUT_DIR",
    )
    planning_evaluation_compare_backend: str = Field(
        default="ortools", alias="PLANNING_EVALUATION_COMPARE_BACKEND"
    )
    planning_evaluation_compare_depth: str = Field(
        default="mapf", alias="PLANNING_EVALUATION_COMPARE_DEPTH"
    )
    planning_evaluation_compare_timeout_seconds: int = Field(
        default=240, alias="PLANNING_EVALUATION_COMPARE_TIMEOUT_SECONDS", ge=10, le=1800
    )
    planning_evaluation_redact_secrets: bool = Field(
        default=True, alias="PLANNING_EVALUATION_REDACT_SECRETS"
    )

    debug_scenario_api_enabled: bool = Field(
        default=False,
        alias="DEBUG_SCENARIO_API_ENABLED",
        description=(
            "Enable local/test-only endpoints that clone one Redis simulation "
            "runtime namespace for deterministic API scenario execution."
        ),
    )

    # Terminal relocation applied after rolling-horizon reassignment.  Used
    # robots expose the terminal leg to the solver; old-plan robots that receive
    # no new business work receive an execution-only PARK/CHARGE goal before MAPF.
    idle_robot_relocation_enabled: bool = Field(
        default=True, alias="IDLE_ROBOT_RELOCATION_ENABLED"
    )
    robot_opportunistic_charge_threshold_pct: float = Field(
        default=45.0, alias="ROBOT_OPPORTUNISTIC_CHARGE_THRESHOLD_PCT", ge=0, le=100
    )
    robot_default_terminal_policy: str = Field(
        default="PARK", alias="ROBOT_DEFAULT_TERMINAL_POLICY"
    )
    terminal_relocation_service_ms: int = Field(
        default=500, alias="TERMINAL_RELOCATION_SERVICE_MS", ge=1
    )

    hitl_execution_mode: str = Field(
        default="terminal",
        alias="HITL_EXECUTION_MODE",
        description=(
            "terminal stores a JSON checkpoint, returns an awaiting_* status, "
            "and resumes through the HITL response API."
        ),
    )
    hitl_store_dir: Path | None = Field(
        default=None,
        alias="HITL_STORE_DIR",
        description="Optional directory for file-backed HITL checkpoints.",
    )

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5-mini", alias="OPENAI_MODEL")
    openai_timeout_seconds: float = Field(default=60.0, alias="OPENAI_TIMEOUT_SECONDS", gt=0)
    openai_max_retries: int = Field(default=2, alias="OPENAI_MAX_RETRIES", ge=0, le=10)
    llm_cuopt_context_max_bytes: int = Field(
        default=750_000,
        alias="LLM_CUOPT_CONTEXT_MAX_BYTES",
        ge=10_000,
        description=(
            "Hard UTF-8 payload ceiling for the compact LLM cuOpt formulation "
            "context. Oversized requests fail locally instead of consuming an "
            "OpenAI TPM retry."
        ),
    )

    langsmith_tracing: bool = Field(default=False, alias="LANGSMITH_TRACING")
    langsmith_api_key: str | None = Field(default=None, alias="LANGSMITH_API_KEY")
    langsmith_project: str = Field(default="laro-evaluation-rolling-horizon-v13-21", alias="LANGSMITH_PROJECT")
    langsmith_endpoint: str = Field(default="https://api.smith.langchain.com", alias="LANGSMITH_ENDPOINT")

    allow_api_runtime_snapshot: bool = Field(
        default=True,
        alias="ALLOW_API_RUNTIME_SNAPSHOT",
        description=(
            "Allow a browser simulator to supply request-scoped robot states. "
            "Live Redis telemetry remains authoritative when no snapshot is supplied."
        ),
    )
    default_warehouse_id: str = Field(default="WH-001", alias="DEFAULT_WAREHOUSE_ID")
    warehouse_data_root: Path = Field(
        default=Path("data/warehouses"),
        alias="WAREHOUSE_DATA_ROOT",
        description=(
            "Optional JSON root containing one subdirectory per warehouse_id. "
            "The configured DATA_DIR remains the backward-compatible default warehouse."
        ),
    )
    data_dir: Path = Field(default=Path("data"), alias="DATA_DIR")
    output_dir: Path = Field(default=Path("runtime_outputs"), alias="OUTPUT_DIR")
    warehouse_repository_backend: str = Field(
        default="json",
        alias="WAREHOUSE_REPOSITORY_BACKEND",
        description="json uses fixtures; embedded uses local files; live uses LARO native tables; be_shared uses existing Spring BE tables plus laro_ext.",
    )
    local_db_dir: Path = Field(default=Path(".laro_local"), alias="LOCAL_DB_DIR")
    local_postgres_path: Path | None = Field(default=None, alias="LOCAL_POSTGRES_PATH")
    local_redis_path: Path | None = Field(default=None, alias="LOCAL_REDIS_PATH")
    local_neo4j_path: Path | None = Field(default=None, alias="LOCAL_NEO4J_PATH")
    runtime_simulation_id: str = Field(default="SIM001", alias="RUNTIME_SIMULATION_ID")
    postgres_dsn: str = Field(
        default="postgresql://laro:laro@localhost:5432/laro",
        alias="POSTGRES_DSN",
    )
    postgres_pool_min_size: int = Field(default=1, alias="POSTGRES_POOL_MIN_SIZE", ge=1, le=20)
    postgres_pool_max_size: int = Field(default=8, alias="POSTGRES_POOL_MAX_SIZE", ge=1, le=100)
    postgres_connect_timeout_seconds: int = Field(
        default=5, alias="POSTGRES_CONNECT_TIMEOUT_SECONDS", ge=1, le=60
    )
    postgres_pool_open_timeout_seconds: float = Field(
        default=10.0, alias="POSTGRES_POOL_OPEN_TIMEOUT_SECONDS", ge=1.0, le=120.0
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    redis_key_prefix: str = Field(default="laro", alias="REDIS_KEY_PREFIX")
    infrastructure_strict_startup: bool = Field(
        default=False, alias="INFRASTRUCTURE_STRICT_STARTUP"
    )
    map_repository_backend: str = Field(default="json", alias="MAP_REPOSITORY_BACKEND")
    neo4j_uri: str = Field(default="neo4j://localhost:7687", alias="NEO4J_URI")
    neo4j_username: str = Field(default="neo4j", alias="NEO4J_USERNAME")
    neo4j_password: str | None = Field(default=None, alias="NEO4J_PASSWORD")
    neo4j_database: str = Field(default="neo4j", alias="NEO4J_DATABASE")
    optimization_backend: OptimizationBackend = Field(default="ortools", alias="OPTIMIZATION_BACKEND")
    cuopt_api_url: str = Field(default="http://localhost:5000/cuopt/request", alias="CUOPT_API_URL")
    cuopt_solution_url_template: str | None = Field(default=None, alias="CUOPT_SOLUTION_URL_TEMPLATE")
    cuopt_health_url: str | None = Field(default=None, alias="CUOPT_HEALTH_URL")
    cuopt_transport: str = Field(default="http", alias="CUOPT_TRANSPORT")
    cuopt_payload_format: str = Field(default="native", alias="CUOPT_PAYLOAD_FORMAT")
    # Direct NVIDIA Build/API Catalog authentication.  This key is never
    # shared with self-hosted/private HTTP transports.
    nvidia_api_key: str | None = Field(default=None, alias="NVIDIA_API_KEY")

    # Optional self-hosted/private HTTP gateway authentication.  The explicit
    # HTTP prefix prevents this credential from being mistaken for a Build key.
    cuopt_http_auth_mode: str = Field(default="none", alias="CUOPT_HTTP_AUTH_MODE")
    cuopt_http_api_key: str | None = Field(default=None, alias="CUOPT_HTTP_API_KEY")
    cuopt_http_api_key_header: str = Field(
        default="Authorization", alias="CUOPT_HTTP_API_KEY_HEADER"
    )
    cuopt_action: str = Field(default="cuOpt_OptimizedRouting", alias="CUOPT_ACTION")
    cuopt_client_version: str = Field(default="custom", alias="CUOPT_CLIENT_VERSION")
    cuopt_asset_api_url: str = Field(
        default="https://api.nvcf.nvidia.com/v2/nvcf/assets",
        alias="CUOPT_ASSET_API_URL",
    )
    cuopt_inline_limit_bytes: int = Field(
        default=200_000, alias="CUOPT_INLINE_LIMIT_BYTES", ge=1_000, le=10_000_000
    )
    cuopt_delete_asset_after_solve: bool = Field(
        default=True, alias="CUOPT_DELETE_ASSET_AFTER_SOLVE"
    )
    cuopt_client_sak: str | None = Field(default=None, alias="CUOPT_CLIENT_SAK")
    nvidia_identity_federation_api_key: str | None = Field(
        default=None, alias="NVIDIA_IDENTITY_FEDERATION_API_KEY"
    )
    cuopt_function_id: str | None = Field(default=None, alias="CUOPT_FUNCTION_ID")
    cuopt_client_id: str | None = Field(default=None, alias="CUOPT_CLIENT_ID")
    cuopt_client_secret: str | None = Field(default=None, alias="CUOPT_CLIENT_SECRET")
    cuopt_verify_ssl: bool = Field(default=True, alias="CUOPT_VERIFY_SSL")
    cuopt_poll_interval_seconds: float = Field(default=1.0, alias="CUOPT_POLL_INTERVAL_SECONDS", gt=0)
    cuopt_max_poll_attempts: int = Field(default=30, alias="CUOPT_MAX_POLL_ATTEMPTS", ge=1, le=600)
    cuopt_time_limit_seconds: int = Field(default=5, alias="CUOPT_TIME_LIMIT_SECONDS", ge=1, le=300)
    cuopt_skip_first_trips: bool = Field(default=False, alias="CUOPT_SKIP_FIRST_TRIPS")
    cuopt_drop_return_trips: bool = Field(default=True, alias="CUOPT_DROP_RETURN_TRIPS")
    frontend_explanation_mode: str = Field(default="llm", alias="FRONTEND_EXPLANATION_MODE")
    frontend_explanation_language: str = Field(default="ko", alias="FRONTEND_EXPLANATION_LANGUAGE")
    ortools_time_limit_seconds: int = Field(default=5, alias="ORTOOLS_TIME_LIMIT_SECONDS", ge=1, le=300)
    global_solver_wait_threshold_ms: int = Field(default=15000, alias="GLOBAL_SOLVER_WAIT_THRESHOLD_MS", ge=0)
    mapf_max_wait_ms: int = Field(default=60000, alias="MAPF_MAX_WAIT_MS", ge=0)
    pickup_service_time_ms: int = Field(
        default=1000,
        alias="PICKUP_SERVICE_TIME_MS",
        ge=0,
        description="Base time needed to grip/lift one handling unit at a pickup node.",
    )
    pickup_service_time_per_unit_ms: int = Field(
        default=0,
        alias="PICKUP_SERVICE_TIME_PER_UNIT_MS",
        ge=0,
        description="Additional pickup handling time per requested inventory unit.",
    )
    drop_service_time_ms: int = Field(
        default=1000,
        alias="DROP_SERVICE_TIME_MS",
        ge=0,
        description="Base time needed to place/release one handling unit at a drop node.",
    )
    drop_service_time_per_unit_ms: int = Field(
        default=0,
        alias="DROP_SERVICE_TIME_PER_UNIT_MS",
        ge=0,
        description="Additional drop handling time per requested inventory unit.",
    )

    robot_min_battery_pct: float = Field(default=30.0, alias="ROBOT_MIN_BATTERY_PCT", ge=0, le=100)
    map_meters_per_coordinate_unit: float = Field(
        default=2.5,
        alias="MAP_METERS_PER_COORDINATE_UNIT",
        gt=0,
        description=(
            "Calibration used only when an edge lacks explicit distance_m. "
            "2.5 preserves the legacy 2500ms-per-coordinate-unit timing at 1m/s."
        ),
    )
    robot_nominal_speed_mps: float = Field(
        default=1.0, alias="ROBOT_NOMINAL_SPEED_MPS", gt=0
    )
    minimum_edge_travel_time_ms: int = Field(
        default=500, alias="MINIMUM_EDGE_TRAVEL_TIME_MS", ge=1
    )
    edge_cost_unit_ms: int = Field(
        default=2500,
        alias="EDGE_COST_UNIT_MS",
        ge=100,
        description="Deprecated legacy fallback retained for old fixture compatibility.",
    )
    traffic_safety_headway_ms: int = Field(default=500, alias="TRAFFIC_SAFETY_HEADWAY_MS", ge=0)

    # Goods-to-person handling-unit cycle timings.
    handling_unit_pickup_service_ms: int = Field(
        default=1200, alias="HANDLING_UNIT_PICKUP_SERVICE_MS", ge=0
    )
    handling_unit_return_service_ms: int = Field(
        default=900, alias="HANDLING_UNIT_RETURN_SERVICE_MS", ge=0
    )
    outbound_station_receive_ms: int = Field(
        default=500, alias="OUTBOUND_STATION_RECEIVE_MS", ge=0
    )
    outbound_station_release_ms: int = Field(
        default=300, alias="OUTBOUND_STATION_RELEASE_MS", ge=0
    )
    outbound_station_base_service_ms: int = Field(
        default=1000, alias="OUTBOUND_STATION_BASE_SERVICE_MS", ge=0
    )
    outbound_station_per_order_service_ms: int = Field(
        default=350, alias="OUTBOUND_STATION_PER_ORDER_SERVICE_MS", ge=0
    )
    outbound_station_per_unit_service_ms: int = Field(
        default=0, alias="OUTBOUND_STATION_PER_UNIT_SERVICE_MS", ge=0,
        description="Legacy additive service time; v13.20 station throughput uses ticks.",
    )
    simulation_tick_ms: int = Field(default=100, alias="SIM_TICK_MS", ge=1)
    outbound_station_items_per_tick: int = Field(
        default=1, alias="OUTBOUND_STATION_ITEMS_PER_TICK", ge=1
    )
    empty_tote_buffer_service_ms: int = Field(
        default=500, alias="EMPTY_TOTE_BUFFER_SERVICE_MS", ge=0
    )


    @field_validator("default_warehouse_id", mode="before")
    @classmethod
    def normalize_default_warehouse_id(cls, value: object) -> str:
        return normalize_warehouse_id(value)

    @field_validator("outbound_fulfillment_mode", mode="before")
    @classmethod
    def normalize_outbound_fulfillment_mode(cls, value: object) -> OutboundFulfillmentMode:
        text = str(value or "goods_to_person").strip().casefold().replace("-", "_")
        aliases = {
            "g2p": "goods_to_person",
            "goods_to_person": "goods_to_person",
            "legacy": "legacy_order_tasks",
            "legacy_order_tasks": "legacy_order_tasks",
        }
        if text not in aliases:
            raise ValueError(
                "OUTBOUND_FULFILLMENT_MODE must be goods_to_person or legacy_order_tasks."
            )
        return aliases[text]  # type: ignore[return-value]

    @field_validator("agent_retrieval_mode", mode="before")
    @classmethod
    def normalize_agent_retrieval_mode(cls, value: object) -> AgentRetrievalMode:
        text = str(value or "parallel_plan").strip().casefold().replace("-", "_")
        if text not in {"parallel_plan", "stepwise"}:
            raise ValueError("AGENT_RETRIEVAL_MODE must be parallel_plan or stepwise.")
        return text  # type: ignore[return-value]

    @field_validator("agent_optional_retrieval_planner", mode="before")
    @classmethod
    def normalize_agent_optional_retrieval_planner(cls, value: object) -> str:
        text = str(value or "auto").strip().casefold().replace("-", "_")
        aliases = {
            "auto": "auto",
            "always": "always",
            "always_llm": "always",
            "off": "off",
            "disabled": "off",
            "deterministic": "off",
        }
        if text not in aliases:
            raise ValueError(
                "AGENT_OPTIONAL_RETRIEVAL_PLANNER must be auto, always, or off."
            )
        return aliases[text]

    @field_validator(
        "local_postgres_path",
        "local_redis_path",
        "local_neo4j_path",
        "hitl_store_dir",
        mode="before",
    )
    @classmethod
    def normalize_optional_path(cls, value: object) -> object:
        """Treat blank optional path environment variables as truly unset.

        ``Path("")`` becomes ``Path('.')`` before an ``after`` validator runs,
        which previously made SQLite try to open the project directory as a
        database file.  Normalize blanks before Pydantic constructs ``Path``.
        """

        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "data_dir",
        "warehouse_data_root",
        "output_dir",
        "planning_evaluation_output_dir",
        "local_db_dir",
        "local_postgres_path",
        "local_redis_path",
        "local_neo4j_path",
        "hitl_store_dir",
        mode="after",
    )
    @classmethod
    def resolve_application_paths(cls, value: Path | None) -> Path | None:
        return _project_relative_path(value)


    @field_validator("be_compat_graph_source", mode="before")
    @classmethod
    def normalize_be_compat_graph_source(cls, value: object) -> str:
        text = str(value or "auto").strip().casefold()
        if text not in {"auto", "spring_db", "contract", "request_snapshot"}:
            raise ValueError(
                "BE_COMPAT_GRAPH_SOURCE must be auto, spring_db, contract, or request_snapshot."
            )
        return text

    @field_validator("be_compat_graph_cache_mode", mode="before")
    @classmethod
    def normalize_be_compat_graph_cache_mode(cls, value: object) -> str:
        text = str(value or "metadata").strip().casefold()
        if text not in {"off", "metadata", "full"}:
            raise ValueError("BE_COMPAT_GRAPH_CACHE_MODE must be off, metadata, or full.")
        return text

    @field_validator("be_compat_runtime_source", mode="before")
    @classmethod
    def normalize_be_compat_runtime_source(cls, value: object) -> str:
        text = str(value or "request_then_redis").strip().casefold()
        if text not in {"request_only", "request_then_redis", "redis_only"}:
            raise ValueError(
                "BE_COMPAT_RUNTIME_SOURCE must be request_only, request_then_redis, or redis_only."
            )
        return text

    @field_validator("be_compat_default_edge_status", mode="before")
    @classmethod
    def normalize_be_compat_default_edge_status(cls, value: object) -> str:
        text = str(value or "OPEN").strip().upper()
        if text not in {"OPEN", "CONGESTED", "BLOCKED", "CLOSED", "MAINTENANCE"}:
            raise ValueError("BE_COMPAT_DEFAULT_EDGE_STATUS is invalid.")
        return text

    @field_validator("warehouse_repository_backend", mode="before")
    @classmethod
    def normalize_warehouse_repository_backend(cls, value: object) -> str:
        text = str(value or "json").strip().casefold()
        if text not in {"json", "embedded", "live", "be_shared"}:
            raise ValueError("WAREHOUSE_REPOSITORY_BACKEND must be json, embedded, live, or be_shared.")
        return text

    @field_validator("map_repository_backend", mode="before")
    @classmethod
    def normalize_map_repository_backend(cls, value: object) -> str:
        text = str(value or "json").strip().casefold()
        if text not in {"json", "neo4j"}:
            raise ValueError("MAP_REPOSITORY_BACKEND must be json or neo4j.")
        return text

    @field_validator("default_planning_mode", mode="before")
    @classmethod
    def normalize_default_planning_mode(cls, value: object) -> PlanningMode:
        """Accept canonical v13.12 names and documented legacy aliases."""

        return canonicalize_planning_mode(value)


    @field_validator("planning_evaluation_mode", mode="before")
    @classmethod
    def normalize_planning_evaluation_mode(cls, value: object) -> str:
        text = str(value or "off").strip().casefold()
        if text not in {"off", "capture_only"}:
            raise ValueError("PLANNING_EVALUATION_MODE must be off or capture_only.")
        return text

    @field_validator("planning_evaluation_compare_backend", mode="before")
    @classmethod
    def normalize_evaluation_backend(cls, value: object) -> str:
        text = str(value or "ortools").strip().casefold()
        if text not in {"ortools", "cuopt_payload_only", "cuopt"}:
            raise ValueError("PLANNING_EVALUATION_COMPARE_BACKEND is invalid.")
        return text

    @field_validator("planning_evaluation_compare_depth", mode="before")
    @classmethod
    def normalize_evaluation_depth(cls, value: object) -> str:
        text = str(value or "mapf").strip().casefold()
        if text not in {"formulation", "payload", "solve", "mapf"}:
            raise ValueError("PLANNING_EVALUATION_COMPARE_DEPTH is invalid.")
        return text

    @field_validator("robot_default_terminal_policy", mode="before")
    @classmethod
    def normalize_terminal_policy(cls, value: object) -> str:
        text = str(value or "PARK").strip().upper()
        if text not in {"STAY", "PARK", "CHARGE"}:
            raise ValueError("ROBOT_DEFAULT_TERMINAL_POLICY must be STAY, PARK, or CHARGE.")
        return text

    @field_validator("hitl_execution_mode", mode="before")
    @classmethod
    def normalize_hitl_execution_mode(cls, value: object) -> str:
        """The PoC uses explicit terminal checkpoints instead of a hanging HTTP request."""

        text = str(value or "terminal").strip().casefold()
        if text != "terminal":
            raise ValueError("HITL_EXECUTION_MODE currently supports only terminal.")
        return text

    @field_validator("cuopt_transport", mode="before")
    @classmethod
    def normalize_cuopt_transport(cls, value: object) -> str:
        """Normalize documented transport aliases and reject unknown values."""

        text = str(value or "http").strip().casefold().replace("-", "_")
        aliases = {
            "http": "http",
            "self_hosted": "http",
            "selfhosted": "http",
            "nim": "http",
            "managed": "managed",
            "managed_thin_client": "managed",
            "thin_client": "managed",
            "nvidia_api": "nvidia_api",
            "build_api": "nvidia_api",
            "api_catalog": "nvidia_api",
            "build": "nvidia_api",
        }
        if text not in aliases:
            raise ValueError("CUOPT_TRANSPORT must be http, managed, or nvidia_api.")
        return aliases[text]

    @field_validator("cuopt_payload_format", mode="before")
    @classmethod
    def normalize_cuopt_payload_format(cls, value: object) -> str:
        """Accept only the native NVIDIA request or the internal debug payload."""

        text = str(value or "native").strip().casefold()
        if text not in {"native", "internal"}:
            raise ValueError("CUOPT_PAYLOAD_FORMAT must be native or internal.")
        return text

    @field_validator("cuopt_http_auth_mode", mode="before")
    @classmethod
    def normalize_cuopt_http_auth_mode(cls, value: object) -> str:
        """Normalize supported HTTP authentication modes."""

        text = str(value or "none").strip().casefold().replace("_", "-")
        aliases = {"none": "none", "bearer": "bearer", "x-api-key": "x-api-key", "api-key": "x-api-key", "header": "header"}
        if text not in aliases:
            raise ValueError("CUOPT_HTTP_AUTH_MODE must be none, bearer, x-api-key, or header.")
        return aliases[text]

    @field_validator("frontend_explanation_mode", mode="before")
    @classmethod
    def normalize_frontend_explanation_mode(cls, value: object) -> str:
        """Normalize the front-end explanation strategy."""

        text = str(value or "llm").strip().casefold()
        if text not in {"llm", "deterministic", "off"}:
            raise ValueError("FRONTEND_EXPLANATION_MODE must be llm, deterministic, or off.")
        return text

    @field_validator(
        "openai_api_key",
        "langsmith_api_key",
        "cuopt_http_api_key",
        "nvidia_api_key",
        "cuopt_client_sak",
        "nvidia_identity_federation_api_key",
        "cuopt_function_id",
        "cuopt_client_id",
        "cuopt_client_secret",
        "neo4j_password",
        mode="before",
    )
    @classmethod
    def normalize_secret(cls, value: object) -> object:
        """Treat empty and example secret values as unconfigured."""

        if value is None:
            return None
        text = str(value).strip()
        placeholders = {
            "sk-your-openai-api-key",
            "your-openai-api-key",
            "your-langsmith-api-key",
            "your-cuopt-http-api-key",
            "your-nvidia-api-key",
            "nvapi-your-nvidia-api-key",
            "your-cuopt-client-sak",
            "your-nvidia-identity-federation-api-key",
            "your-function-id",
            "your-client-id",
            "your-client-secret",
        }
        if not text or text.casefold() in placeholders:
            return None
        return text



    @property
    def local_postgres_db_path(self) -> Path:
        return self.local_postgres_path or (self.local_db_dir / "postgres.sqlite3")

    @property
    def local_redis_db_path(self) -> Path:
        return self.local_redis_path or (self.local_db_dir / "redis.sqlite3")

    @property
    def local_neo4j_db_path(self) -> Path:
        return self.local_neo4j_path or (self.local_db_dir / "neo4j.sqlite3")

    # Explicit aliases used by diagnostics and tests.  They make it clear that
    # blank optional overrides have been normalized and the returned value is
    # the final project-rooted SQLite path.
    @property
    def resolved_local_postgres_path(self) -> Path:
        return self.local_postgres_db_path

    @property
    def resolved_local_redis_path(self) -> Path:
        return self.local_redis_db_path

    @property
    def resolved_local_neo4j_path(self) -> Path:
        return self.local_neo4j_db_path

    @property
    def nvidia_build_api_key(self) -> str | None:
        """Return only the Build/API Catalog credential.

        A self-hosted/private HTTP credential must never authorize the public
        NVIDIA endpoint.  Keeping this contract strict also prevents local
        ``.env`` files from turning a missing-key unit test into a live API call.
        """

        return self.nvidia_api_key

    @property
    def cuopt_nvidia_api_configured(self) -> bool:
        """Return whether the direct Build API transport has its required key."""

        return bool(self.nvidia_build_api_key and self.cuopt_api_url)

    @property
    def effective_cuopt_client_sak(self) -> str | None:
        """Return the configured NVIDIA Identity Federation API key, if any."""

        return self.cuopt_client_sak or self.nvidia_identity_federation_api_key

    @property
    def cuopt_managed_credentials_configured(self) -> bool:
        """Return whether either current SAK or legacy client credentials are complete."""

        current = bool(self.effective_cuopt_client_sak and self.cuopt_function_id)
        legacy = bool(self.cuopt_client_id and self.cuopt_client_secret)
        return current or legacy

    @property
    def langsmith_enabled(self) -> bool:
        """Return whether LangSmith tracing is enabled."""

        return self.langsmith_tracing


def resolve_settings_env_file(env_file: str | Path | None = None) -> Path | None:
    """Resolve one explicit/PID-scoped settings file without parsing it manually."""

    selected = env_file or os.getenv("LARO_ENV_FILE")
    if selected:
        path = Path(selected).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path = path.resolve()
        if not path.exists():
            raise FileNotFoundError(f"Settings environment file not found: {path}")
        return path
    default = PROJECT_ROOT / ".env"
    return default.resolve() if default.exists() else None


def create_settings(
    env_file: str | Path | None = None,
    **overrides: object,
) -> Settings:
    """Create one validated settings snapshot.

    An explicitly selected file (argument or ``LARO_ENV_FILE``) is authoritative
    for its non-empty values.  Parsing is delegated to ``python-dotenv`` through
    Pydantic's dependency rather than a project-specific parser.  Empty sample
    secrets are omitted so an already exported credential is not erased.
    """

    explicit_selection = env_file is not None or bool(os.getenv("LARO_ENV_FILE"))
    path = resolve_settings_env_file(env_file)
    if explicit_selection and path is not None:
        file_values = {
            str(key): value
            for key, value in dotenv_values(path).items()
            if value not in {None, ""}
        }
        file_values.update(overrides)
        return Settings(_env_file=None, **file_values)
    return Settings(
        _env_file=str(path) if path is not None else None,
        **overrides,
    )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings snapshot selected by ``LARO_ENV_FILE``."""

    return create_settings()
