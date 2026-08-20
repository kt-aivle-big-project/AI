# Spring BE 최적화 호환 API

기존 Spring BE의 `OptimizationClient`가 호출하는 두 계약만 제공합니다.

```text
POST /optimize
POST /reoptimize
```

프론트는 Spring BE를 호출하고, Spring BE가 내부적으로 이 API를 호출합니다.

## `POST /optimize`

Spring이 전달한 창고 그래프와 로봇의 현재·목표 노드로 최초 경로를 계산합니다.

```json
{
  "warehouseId": 1,
  "robots": [
    {
      "robotId": 101,
      "currentNodeId": 1,
      "targetNodeId": 4,
      "batteryLevel": 82.0
    }
  ],
  "nodes": [
    {"nodeId": 1, "x": 0.0, "y": 0.0},
    {"nodeId": 4, "x": 3.0, "y": 0.0}
  ],
  "edges": [
    {
      "edgeId": 11,
      "fromNodeId": 1,
      "toNodeId": 4,
      "distance": 3.0,
      "directionType": "BOTH"
    }
  ]
}
```

응답은 Spring DTO가 읽는 `requestId`, `status`, `routes`만 포함합니다.

## `POST /reoptimize`

실행 중 남은 작업을 현재 가용 로봇에 다시 배정합니다.

```json
{
  "simulationRunId": 77,
  "warehouseId": 1,
  "reason": "LOW_BATTERY",
  "triggerRobotId": 101,
  "blockedEdgeIds": [],
  "robots": [
    {
      "robotId": 102,
      "currentNodeId": 2,
      "batteryLevel": 80.0,
      "status": "IDLE"
    }
  ],
  "remainingTasks": [
    {
      "taskId": 5001,
      "assignedRobotId": null,
      "startNodeId": 2,
      "endNodeId": 4,
      "taskType": "OUTBOUND",
      "status": "PENDING"
    }
  ]
}
```

가용 후보에서는 `ERROR`, `OFFLINE`, 최소 배터리 미만 로봇과 고장·저배터리
트리거 로봇을 제외합니다. 기본 최소 배터리는
`BE_COMPAT_MIN_BATTERY_PCT=30`입니다.

## 오류

| HTTP | 의미 |
|---|---|
| 404 | `BE_COMPAT_ENABLED=false` |
| 409 | 그래프 또는 도달 가능한 배정이 없음 |
| 422 | 요청 필드·참조가 유효하지 않음 |
| 503 | 저장소 또는 최적화 경계 오류 |

과거 호환 진단·Runtime Bootstrap API는 운영 호출자가 없어 제거했습니다.
상태 확인은 `GET /health`, 상세 계획·재계획은 BE 중심
`/api/v1/simulation-runs/{id}/...` 계약을 사용합니다.
