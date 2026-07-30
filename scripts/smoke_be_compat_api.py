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
        contract = client.get("/compat/v2/contract")
        contract.raise_for_status()
        contract_body = contract.json()
        if not contract_body.get("ready"):
            raise RuntimeError(f"Compatibility contract is not ready: {contract_body}")

        runtime_bootstrap = client.put(
            f"/compat/v2/simulation-runs/{args.simulation_run_id}/runtime",
            json={
                "warehouseId": args.warehouse_id,
                "simTimeMs": 0,
                "replace": True,
                "robots": [
                    {
                        "robotId": 101,
                        "currentNodeId": 1,
                        "batteryLevel": 82.0,
                        "status": "IDLE",
                    },
                    {
                        "robotId": 102,
                        "currentNodeId": 4,
                        "batteryLevel": 25.0,
                        "status": "IDLE",
                    },
                ],
            },
        )
        runtime_bootstrap.raise_for_status()
        runtime_body = runtime_bootstrap.json()

        for _ in range(args.repeat):
            optimize = client.post("/optimize", json=optimize_request)
            optimize.raise_for_status()
            optimize_body = optimize.json()
            if optimize_body.get("status") != "success" or not optimize_body.get("routes"):
                raise RuntimeError(f"Unexpected /optimize response: {optimize_body}")
            optimize_results.append(optimize_body)

            graph = client.get(f"/compat/v1/warehouses/{args.warehouse_id}/graph")
            graph.raise_for_status()
            graph_body = graph.json()
            if not graph_body.get("available"):
                raise RuntimeError(f"Graph was not available: {graph_body}")

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
                "contract": contract_body,
                "runtime": runtime_body,
                "optimize": optimize_results[-1],
                "graph": graph_body,
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
