from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import redis
from dotenv import load_dotenv


load_dotenv()

SEED_PATH = Path("data/redis/redis_seed.json")


def restore_key(
    client: redis.Redis,
    item: dict[str, Any],
) -> None:
    key = str(item["key"])
    key_type = str(item["type"])
    value = item["value"]

    # 전체 Redis를 비우지 않고 복원 대상 키만 교체한다.
    client.delete(key)

    if key_type == "string":
        client.set(key, value)

    elif key_type == "hash":
        if value:
            client.hset(key, mapping=value)

    elif key_type == "set":
        if value:
            client.sadd(key, *value)

    elif key_type == "list":
        if value:
            client.rpush(key, *value)

    elif key_type == "zset":
        if value:
            mapping = {
                row["member"]: float(row["score"])
                for row in value
            }
            client.zadd(key, mapping)

    else:
        raise RuntimeError(
            f"지원하지 않는 Redis 타입입니다: key={key}, type={key_type}"
        )


def main() -> None:
    redis_url = os.getenv("REDIS_URL")

    if not redis_url:
        raise RuntimeError("REDIS_URL이 .env에 없습니다.")

    if not SEED_PATH.exists():
        raise FileNotFoundError(
            f"Redis Seed 파일이 없습니다: {SEED_PATH}"
        )

    client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
    )

    client.ping()

    payload = json.loads(
        SEED_PATH.read_text(encoding="utf-8")
    )

    keys = payload.get("keys", [])

    for item in keys:
        restore_key(client, item)

    print(f"Redis seed import complete: {SEED_PATH}")
    print(f"Imported key count: {len(keys)}")

    for item in keys:
        print(f"- {item['key']}")


if __name__ == "__main__":
    main()
