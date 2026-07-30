"""Safely upsert backend_laro robot state into the existing Redis key model."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from app.config import get_settings
from app.repositories import BackendLaroPostgresAdapter, RedisRepository


def _redis_robot_mapping(robot: dict[str, object]) -> dict[str, str]:
    return {
        "robot_id": str(robot["robot_id"]),
        "node_id": str(robot["node_id"]),
        "battery": str(float(robot["battery"])),
        "status": str(robot["status"]),
        "last_event": "BACKEND_LARO_SEED",
        "updated_at": datetime.now(UTC).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "warehouse_id=1 backend_laro 로봇을 기존 Redis 키에 upsert합니다. "
            "기본 동작은 dry-run입니다."
        )
    )
    parser.add_argument("--warehouse-id", type=int, default=1)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="명시적으로 지정한 경우에만 Redis에 upsert합니다.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="변경 예정 내역만 확인합니다(기본값).",
    )
    args = parser.parse_args()

    if args.warehouse_id != 1:
        print("Refusing to seed: backend_laro seed target must be warehouse_id=1.")
        return 2

    settings = get_settings()
    missing = [
        name
        for name, value in {
            "DATABASE_URL": settings.database_url,
            "REDIS_URL": settings.redis_url,
        }.items()
        if not value
    ]
    if missing:
        print("Seed unavailable. Missing settings: " + ", ".join(missing))
        return 2

    try:
        postgres = BackendLaroPostgresAdapter(settings.database_url)
        robots = postgres.fetch_robots(args.warehouse_id)
    except Exception:
        print(
            "Seed failed while reading backend_laro robots. "
            "No Redis data was modified."
        )
        return 2

    robot_ids = sorted(str(robot["robot_id"]) for robot in robots)
    if not args.apply:
        print("Mode: DRY_RUN")
        print(f"Warehouse: {args.warehouse_id}")
        print(f"Robots to upsert: {len(robot_ids)}")
        print("Robot IDs: " + ", ".join(robot_ids))
        return 0

    redis = RedisRepository(settings.redis_url)
    try:
        prefix = redis._prefix(args.warehouse_id)
        pipeline = redis.client.pipeline(transaction=True)
        for robot in robots:
            robot_id = str(robot["robot_id"])
            pipeline.sadd(f"{prefix}:robots", robot_id)
            pipeline.hset(
                f"{prefix}:robot:{robot_id}",
                mapping=_redis_robot_mapping(robot),
            )
        pipeline.execute()
        print("Mode: APPLY")
        print(f"Warehouse: {args.warehouse_id}")
        print(f"Robots upserted: {len(robot_ids)}")
        print("Robot IDs: " + ", ".join(robot_ids))
        return 0
    except Exception:
        print(
            "Redis robot upsert failed. No delete or FLUSHDB operation was used."
        )
        return 2
    finally:
        redis.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
