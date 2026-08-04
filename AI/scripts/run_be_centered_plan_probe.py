"""Call the numeric Spring simulation-run plan endpoint and preserve the response."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulation-run-id", type=int, required=True)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--request",
        type=Path,
        default=Path("examples/be_centered/fastapi_plan_request.json"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("runtime_outputs/be_centered_plan_probe"))
    args = parser.parse_args()
    payload = json.loads(args.request.read_text(encoding="utf-8"))
    url = f"{args.base_url.rstrip('/')}/api/v1/simulation-runs/{args.simulation_run_id}/missions/plan"
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=1200) as response:  # noqa: S310 - explicit integration URL
        body = json.loads(response.read().decode("utf-8"))
        status = response.status
    target = args.output_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target.mkdir(parents=True, exist_ok=True)
    (target / "request.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "response.json").write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    plan = ((body.get("result") or {}).get("plan") or {})
    summary = {
        "http_status": status,
        "status": (body.get("result") or {}).get("status"),
        "request_id": body.get("request_id"),
        "plan_id": plan.get("plan_id"),
        "plan_version": plan.get("plan_version"),
        "makespan_ms": plan.get("makespan_ms"),
        "robot_count": len(plan.get("robots") or []),
        "output_dir": str(target.resolve()),
    }
    (target / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if status == 200 and summary["status"] == "plan_validated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
