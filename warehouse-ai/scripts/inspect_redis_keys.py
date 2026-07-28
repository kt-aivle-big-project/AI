from __future__ import annotations

import os
from collections import Counter

import redis
from dotenv import load_dotenv


load_dotenv()


def decode(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def get_item_count(client: redis.Redis, key: bytes, key_type: str) -> int | str:
    try:
        if key_type == "string":
            return client.strlen(key)
        if key_type == "hash":
            return client.hlen(key)
        if key_type == "list":
            return client.llen(key)
        if key_type == "set":
            return client.scard(key)
        if key_type == "zset":
            return client.zcard(key)
        if key_type == "stream":
            return client.xlen(key)
        return "-"
    except redis.RedisError:
        return "ERROR"


def main() -> None:
    redis_url = os.getenv("REDIS_URL")

    if not redis_url:
        raise RuntimeError(
            "REDIS_URL이 .env에 없습니다. "
            "예: REDIS_URL=redis://localhost:6379/0"
        )

    client = redis.Redis.from_url(redis_url)

    if not client.ping():
        raise RuntimeError("Redis PING에 실패했습니다.")

    rows: list[tuple[str, str, int, int | str]] = []
    type_counts: Counter[str] = Counter()

    for raw_key in client.scan_iter(match="*"):
        key_name = decode(raw_key)
        key_type = decode(client.type(raw_key))
        ttl = client.ttl(raw_key)
        count = get_item_count(client, raw_key, key_type)

        rows.append((key_name, key_type, ttl, count))
        type_counts[key_type] += 1

    rows.sort(key=lambda row: row[0])

    print("Redis connection: OK")
    print(f"Total keys: {len(rows)}")
    print()

    print(f"{'KEY':<70} {'TYPE':<10} {'TTL':>8} {'COUNT/SIZE':>12}")
    print("-" * 105)

    for key_name, key_type, ttl, count in rows:
        print(
            f"{key_name:<70} "
            f"{key_type:<10} "
            f"{ttl:>8} "
            f"{str(count):>12}"
        )

    print()
    print("Type summary:")
    for key_type, count in sorted(type_counts.items()):
        print(f"- {key_type}: {count}")


if __name__ == "__main__":
    main()
