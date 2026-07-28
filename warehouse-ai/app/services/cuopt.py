from typing import Any

import httpx

from app.models import CuOptPlan


class CuOptHttpOptimizer:
    """팀에서 운영할 cuOpt HTTP 어댑터 클라이언트입니다."""

    def __init__(self, base_url: str, timeout: float):
        if not base_url:
            raise RuntimeError("CUOPT_URL이 설정되지 않았습니다.")
        self.url = base_url.rstrip("/") + "/optimize"
        self.timeout = timeout

    def optimize(self, problem: dict[str, Any]) -> CuOptPlan:
        response = httpx.post(self.url, json=problem, timeout=self.timeout)
        response.raise_for_status()
        return CuOptPlan.model_validate(response.json())


# 기존 import 경로를 사용하는 코드와의 하위 호환성입니다.
CuOptClient = CuOptHttpOptimizer
