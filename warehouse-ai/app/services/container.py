from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.config import get_settings
from app.repositories import (
    Neo4jRepository,
    PlanningPostgresRepository,
    RedisRepository,
    create_postgres_repository,
)


@dataclass
class ServiceContainer:
    postgres: PlanningPostgresRepository
    neo4j: Neo4jRepository
    redis: RedisRepository

    def healthcheck(self) -> dict[str, Any]:
        dependencies: dict[str, Any] = {}
        for name, repository in (
            ("postgres", self.postgres),
            ("neo4j", self.neo4j),
            ("redis", self.redis),
        ):
            try:
                dependencies[name] = repository.healthcheck()
            except Exception as exc:
                dependencies[name] = {
                    "ok": False,
                    "error_type": exc.__class__.__name__,
                }
        return dependencies


@lru_cache
def get_services() -> ServiceContainer:
    settings = get_settings()
    missing = [
        name
        for name, value in {
            "DATABASE_URL": settings.database_url,
            "NEO4J_URI": settings.neo4j_uri,
            "NEO4J_PASSWORD": settings.neo4j_password,
            "REDIS_URL": settings.redis_url,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("연결 정보가 없습니다: " + ", ".join(missing))
    return ServiceContainer(
        postgres=create_postgres_repository(
            settings.database_url,
            settings.postgres_schema_profile,
        ),
        neo4j=Neo4jRepository(
            settings.neo4j_uri,
            settings.neo4j_user,
            settings.neo4j_password,
            settings.neo4j_database,
        ),
        redis=RedisRepository(settings.redis_url),
    )


def reset_services() -> None:
    if get_services.cache_info().currsize:
        try:
            get_services().neo4j.close()
        finally:
            get_services.cache_clear()
