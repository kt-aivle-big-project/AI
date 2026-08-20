"""Repeated HTTP smoke test for the unmodified Spring BE compatibility API."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def without_request_id(value: dict) -> dict:
    result = dict(value)
    result.pop("requestId", None)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--warehouse-id", type=int, default=900001)
    parser.add_argument("--simulation-run-id", type=int, default=900001)
    args = parser.parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")

    root = Path(__file__).resolve().parents[1]
    example = root / "examples" / "be_compat"
    optimize_request = load(example / "optimize_request.json")
    reoptimize_request = load(example / "reoptimize_request.json")
    optimize_request["warehouseId"] = args.warehouse_id
    reoptimize_request["warehouseId"] = args.warehouse_id
    reoptimize_request["simulationRunId"] = args.simulation_run_id

    optimize_results: list[dict] = []
    reoptimize_results: list[dict] = []
    with httpx.Client(base_url=args.base_url, timeout=60.0) as client:
        health = client.get("/health")
        health.raise_for_status()
        health_body = health.json()

        for _ in range(args.repeat):
            optimize = client.post("/optimize", json=optimize_request)
            optimize.raise_for_status()
            optimize_body = optimize.json()
            if optimize_body.get("status") != "success" or not optimize_body.get("routes"):
                raise RuntimeError(f"Unexpected /optimize response: {optimize_body}")
            optimize_results.append(optimize_body)

            reoptimize = client.post("/reoptimize", json=reoptimize_request)
            reoptimize.raise_for_status()
            reoptimize_body = reoptimize.json()
            if reoptimize_body.get("status") not in {"success", "partial_success"}:
                raise RuntimeError(f"Unexpected /reoptimize response: {reoptimize_body}")
            reoptimize_results.append(reoptimize_body)

    optimize_canonical = {
        json.dumps(without_request_id(value), sort_keys=True)
        for value in optimize_results
    }
    reoptimize_canonical = {
        json.dumps(without_request_id(value), sort_keys=True)
        for value in reoptimize_results
    }
    if len(optimize_canonical) != 1 or len(reoptimize_canonical) != 1:
        raise RuntimeError("Repeated compatibility responses were not deterministic.")

    print(
        json.dumps(
            {
                "status": "PASS",
                "base_url": args.base_url,
                "repeat": args.repeat,
                "warehouse_id": args.warehouse_id,
                "simulation_run_id": args.simulation_run_id,
                "health": health_body,
                "optimize": optimize_results[-1],
                "reoptimize": reoptimize_results[-1],
                "deterministic": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
