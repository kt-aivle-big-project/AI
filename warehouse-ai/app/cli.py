import argparse
import json
from pathlib import Path
from typing import Sequence

import uvicorn

from app.config import get_settings
from app.execution import handle_robot_event
from app.map_loader import upload_map
from app.models import NaturalLanguageCommand, RobotEvent
from app.planning import run_planning
from app.services.container import get_services


EXECUTION_MODES = ["AUTO", "PLAN_ONLY", "SIMULATE_ONLY", "EXECUTE"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Warehouse Planning Supervisor")
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("health", help="PostgreSQL, Neo4j, Redis 연결 확인")

    plan = subparsers.add_parser("plan", help="자연어 명령 실행")
    plan.add_argument("--warehouse-id", type=int, required=True)
    plan.add_argument("--command", required=True)
    plan.add_argument("--mode", choices=EXECUTION_MODES, default="SIMULATE_ONLY")

    event = subparsers.add_parser("event", help="로봇 이벤트 JSON 처리")
    event.add_argument("--file", required=True)
    event.add_argument("--auto-replan", action="store_true")

    map_parser = subparsers.add_parser("upload-map", help="초기 지도를 Neo4j에 등록")
    map_parser.add_argument("--warehouse-id", type=int, required=True)
    map_parser.add_argument("--warehouse-name", required=True)
    map_parser.add_argument("--nodes", required=True)
    map_parser.add_argument("--edges", required=True)

    api = subparsers.add_parser("api", help="FastAPI 개발 서버 실행")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--reload", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.action == "health":
        missing = get_settings().missing_for_connections()
        if missing:
            print(json.dumps({"status": "not_configured", "missing": missing}, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps(get_services().healthcheck(), ensure_ascii=False, indent=2))
        return 0

    if args.action == "plan":
        result = run_planning(
            NaturalLanguageCommand(
                warehouse_id=args.warehouse_id,
                text=args.command,
                requested_execution_mode=args.mode,
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.action == "event":
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        result = handle_robot_event(
            RobotEvent.model_validate(payload),
            auto_replan=args.auto_replan,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.action == "upload-map":
        result = upload_map(
            args.warehouse_id,
            args.warehouse_name,
            args.nodes,
            args.edges,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.action == "api":
        uvicorn.run(
            "app.api:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
        return 0

    return 1
