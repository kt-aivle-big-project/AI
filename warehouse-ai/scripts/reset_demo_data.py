"""Reset one warehouse's mutable demo state in an explicit dev environment.

This utility is deliberately separate from the operational RESET API.  It is
for repeatable local demonstrations and must never be used in production.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

# ``python scripts/reset_demo_data.py`` sets sys.path to ``scripts``.  Add the
# project root so the documented direct invocation resolves the app package.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.repositories.postgres import PostgresRepository
from app.repositories.redis_store import RedisRepository


ALLOWED_ENVIRONMENTS = {"dev", "development", "local", "test"}


def assert_development_environment(app_env: str) -> str:
    normalized = app_env.strip().lower()
    if normalized not in ALLOWED_ENVIRONMENTS:
        raise RuntimeError(
            "demo data reset은 APP_ENV가 dev/development/local/test일 때만 허용됩니다. "
            "production 및 미설정 환경에서는 실행할 수 없습니다."
        )
    return normalized


def reset_postgres(
    repository: PostgresRepository,
    warehouse_id: int,
    warehouse_timezone: str = "Asia/Seoul",
) -> dict[str, Any]:
    """Reset mutable demo rows and keep all command/simulation audit history."""

    timezone = ZoneInfo(warehouse_timezone or "Asia/Seoul")
    local_now = datetime.now(UTC).astimezone(timezone)

    def next_local(hour: int, minute: int = 0) -> datetime:
        candidate = local_now.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    inbound_arrival_at = next_local(7)
    inbound_available_at = inbound_arrival_at + timedelta(minutes=10)
    a_required_at = next_local(1, 30)
    with repository.engine.begin() as connection:
        simulation_ids = [
            str(row["simulation_id"])
            for row in connection.execute(
                text(
                    """
                    SELECT simulation_id
                    FROM simulation_session
                    WHERE warehouse_id = :warehouse_id
                    ORDER BY simulation_id
                    """
                ),
                {"warehouse_id": warehouse_id},
            ).mappings()
        ]
        works = [
            dict(row)
            for row in connection.execute(
                text(
                    """
                    WITH reset_targets AS (
                        SELECT
                            work_id,
                            row_number() OVER (
                                ORDER BY priority ASC, work_id ASC
                            ) AS ordinal
                        FROM works
                        WHERE warehouse_id = :warehouse_id
                    )
                    UPDATE works AS target
                    SET status = 'NEW',
                        assigned_robot_id = NULL,
                        scheduled_start = CURRENT_TIMESTAMP
                            + (reset_targets.ordinal * INTERVAL '10 minutes'),
                        scheduled_end = CURRENT_TIMESTAMP
                            + (reset_targets.ordinal * INTERVAL '10 minutes')
                            + INTERVAL '1 hour',
                        version = COALESCE(target.version, 0) + 1
                    FROM reset_targets
                    WHERE target.work_id = reset_targets.work_id
                    RETURNING target.work_id, target.scheduled_start, target.scheduled_end
                    """
                ),
                {"warehouse_id": warehouse_id},
            ).mappings()
        ]
        robots = [
            dict(row)
            for row in connection.execute(
                text(
                    """
                    UPDATE robot
                    SET status = 'IDLE',
                        current_load = 0,
                        version = COALESCE(version, 0) + 1
                    WHERE warehouse_id = :warehouse_id
                    RETURNING robot_id, status, current_load
                    """
                ),
                {"warehouse_id": warehouse_id},
            ).mappings()
        ]
        inventory_reset = {"lots": 0, "inbounds": 0, "outbounds": 0, "works": 0}
        migration_applied = bool(
            connection.execute(
                text("SELECT to_regclass('inventory_movement') IS NOT NULL")
            ).scalar()
        )
        if migration_applied:
            inventory_reset["lots"] = int(
                connection.execute(
                    text(
                        """
                        UPDATE warehouse_items
                        SET quantity = CASE item_id
                                WHEN 'A' THEN 40 WHEN 'B' THEN 20
                                WHEN 'C' THEN 60 WHEN 'D' THEN 15
                                WHEN 'E' THEN 120 WHEN 'F' THEN 30
                                ELSE quantity
                            END,
                            reserved_quantity = 0,
                            status = 'AVAILABLE',
                            available_at = CURRENT_TIMESTAMP - INTERVAL '1 hour',
                            version = COALESCE(version, 0) + 1
                        WHERE warehouse_id = :warehouse_id
                          AND warehouse_item_id LIKE :demo_prefix
                        """
                    ),
                    {
                        "warehouse_id": warehouse_id,
                        "demo_prefix": f"DEMO-INV-{warehouse_id}-%",
                    },
                ).rowcount
                or 0
            )
            inventory_reset["inbounds"] = int(
                connection.execute(
                    text(
                        """
                        UPDATE inbound_order_line
                        SET status = 'INSPECTING',
                            actual_arrival_at = NULL,
                            actual_available_at = NULL,
                            expected_arrival_at = :arrival_at,
                            expected_available_at = :available_at,
                            warehouse_item_id = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE warehouse_id = :warehouse_id
                          AND inbound_id LIKE :demo_prefix
                        """
                    ),
                    {
                        "warehouse_id": warehouse_id,
                        "demo_prefix": f"DEMO-IN-{warehouse_id}-%",
                        "arrival_at": inbound_arrival_at,
                        "available_at": inbound_available_at,
                    },
                ).rowcount
                or 0
            )
            inventory_reset["outbounds"] = int(
                connection.execute(
                    text(
                        """
                        UPDATE outbound_order_line
                        SET status = 'OPEN',
                            required_by = CASE item_id
                                WHEN 'A' THEN :a_required_at
                                WHEN 'F' THEN :f_required_at
                                ELSE required_by
                            END,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE warehouse_id = :warehouse_id
                          AND outbound_id LIKE :demo_prefix
                        """
                    ),
                    {
                        "warehouse_id": warehouse_id,
                        "demo_prefix": f"DEMO-OUT-{warehouse_id}-%",
                        "a_required_at": a_required_at,
                        "f_required_at": inbound_arrival_at,
                    },
                ).rowcount
                or 0
            )
            inventory_reset["works"] = int(
                connection.execute(
                    text(
                        """
                        UPDATE works
                        SET status = 'NEW', assigned_robot_id = NULL,
                            scheduled_end = CASE item_id
                                WHEN 'A' THEN :a_required_at
                                WHEN 'F' THEN :f_required_at
                                ELSE scheduled_end
                            END,
                            scheduled_start = CASE item_id
                                WHEN 'A' THEN :a_required_at - INTERVAL '5 minutes'
                                WHEN 'F' THEN :f_required_at - INTERVAL '5 minutes'
                                ELSE scheduled_start
                            END,
                            required_at = CASE item_id
                                WHEN 'A' THEN :a_required_at
                                WHEN 'F' THEN :f_required_at
                                ELSE required_at
                            END,
                            version = COALESCE(version, 0) + 1
                        WHERE warehouse_id = :warehouse_id
                          AND work_id LIKE :demo_prefix
                        """
                    ),
                    {
                        "warehouse_id": warehouse_id,
                        "demo_prefix": f"DEMO-W-OUT-{warehouse_id}-%",
                        "a_required_at": a_required_at,
                        "f_required_at": inbound_arrival_at,
                    },
                ).rowcount
                or 0
            )
    return {
        "work_rows": works,
        "robot_rows": robots,
        "simulation_ids": simulation_ids,
        "inventory_reset": inventory_reset,
    }


def reset_redis(
    repository: RedisRepository,
    warehouse_id: int,
    *,
    robot_ids: list[str],
    simulation_ids: list[str],
) -> dict[str, Any]:
    prefix = repository._prefix(warehouse_id)
    indexed_simulations = {
        str(value) for value in repository.client.smembers(f"{prefix}:simulations")
    }
    all_simulations = sorted(indexed_simulations.union(simulation_ids))
    removed_simulations: list[dict[str, Any]] = []
    for simulation_id in all_simulations:
        removed_simulations.append(
            repository.remove_simulation_state(warehouse_id, simulation_id)
        )

    task_ids = set(repository.client.smembers(f"{prefix}:tasks:executing"))
    task_ids.update(repository.client.smembers(f"{prefix}:tasks:planned"))
    active_version = repository.client.get(f"{prefix}:active_plan_version")
    keys = [
        f"{prefix}:robots",
        f"{prefix}:tasks:executing",
        f"{prefix}:tasks:planned",
        f"{prefix}:active_plan_version",
        f"{prefix}:inventory:reservations",
        f"{prefix}:inventory:reservation_idempotency",
    ]
    keys.extend(f"{prefix}:robot:{robot_id}" for robot_id in robot_ids)
    keys.extend(f"{prefix}:task:{task_id}" for task_id in sorted(task_ids))
    if active_version:
        keys.append(f"{prefix}:plan:{active_version}")
    deleted_live_key_count = int(repository.client.delete(*keys)) if keys else 0
    return {
        "simulation_ids": all_simulations,
        "simulation_key_groups_removed": len(removed_simulations),
        "live_keys_requested": sorted(set(keys)),
        "live_keys_deleted": deleted_live_key_count,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="개발 환경의 창고 demo data를 초기 상태로 되돌립니다."
    )
    parser.add_argument("--warehouse-id", type=int, required=True)
    parser.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="상태 변경을 명시적으로 승인합니다.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = get_settings()
    assert_development_environment(settings.app_env)
    if not settings.database_url or not settings.redis_url:
        raise RuntimeError("DATABASE_URL과 REDIS_URL이 모두 필요합니다.")

    postgres = PostgresRepository(settings.database_url)
    redis = RedisRepository(settings.redis_url)
    postgres.healthcheck()
    redis.healthcheck()

    sql_result = reset_postgres(
        postgres,
        args.warehouse_id,
        settings.warehouse_timezone or "Asia/Seoul",
    )
    redis_result = reset_redis(
        redis,
        args.warehouse_id,
        robot_ids=[str(row["robot_id"]) for row in sql_result["robot_rows"]],
        simulation_ids=sql_result["simulation_ids"],
    )
    print(
        json.dumps(
            {
                "status": "RESET_COMPLETED",
                "environment": settings.app_env,
                "warehouse_id": args.warehouse_id,
                "postgres": {
                    "work_count": len(sql_result["work_rows"]),
                    "robot_count": len(sql_result["robot_rows"]),
                    "works": sql_result["work_rows"],
                    "robots": sql_result["robot_rows"],
                    "inventory": sql_result["inventory_reset"],
                    "preserved_audit_history": True,
                },
                "redis": redis_result,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
