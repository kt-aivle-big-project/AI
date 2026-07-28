from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import redis
from dotenv import load_dotenv


load_dotenv()

WAREHOUSE_ID = 2
OUTPUT_PATH = Path("data/redis/redis_seed.json")

ALLOWED_KEYS = {
    f"wh:{WAREHOUSE_ID}:robots",
}

ALLOWED_PREFIXES = (
    f"wh:{WAREHOUSE_ID}:robot:",
)


def export_key(client: redis.Redis, key: str) -> dict[str, Any]:
    key_type = client.type(key)

    if key_type == "string":
        value = client.get(key)

    elif key_type == "hash":
        value = client.hgetall(key)

    elif key_type == "set":
        value = sorted(client.smembers(key))

    elif key_type == "list":
        value = client.lrange(key, 0, -1)

    elif key_type == "zset":
        value = [
            {"member": member, "score": score}
            for member, score in client.zrange(key, 0, -1, withscores=True)
        ]

    else:
        raise RuntimeError(
            f"지원하지 않는 Redis 타입입니다: key={key}, type={key_type}"
        )

    return {
        "key": key,
        "type": key_type,
        "value": value,
    }


def main() -> None:
    redis_url = os.getenv("REDIS_URL")

    if not redis_url:
        raise RuntimeError("REDIS_URL이 .env에 없습니다.")

    client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
    )

    client.ping()

    selected_keys: list[str] = []

    for key in client.scan_iter(match=f"wh:{WAREHOUSE_ID}:*"):
        if key in ALLOWED_KEYS or key.startswith(ALLOWED_PREFIXES):
            selected_keys.append(key)

    selected_keys.sort()

    payload = {
        "format_version": 1,
        "warehouse_id": WAREHOUSE_ID,
        "description": (
            "LARO AI 팀 공유용 Redis 초기 상태. "
            "과거 계획, 이벤트, 예약, 시뮬레이션 이력은 포함하지 않습니다."
        ),
        "keys": [
            export_key(client, key)
            for key in selected_keys
        ],
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Redis seed export complete: {OUTPUT_PATH}")
    print(f"Exported key count: {len(selected_keys)}")

    for key in selected_keys:
        print(f"- {key}")


if __name__ == "__main__":
    main()
