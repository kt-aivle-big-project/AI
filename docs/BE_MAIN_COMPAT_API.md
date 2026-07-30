# BE-main 무수정 FastAPI 호환 API v2

## 1. 목적

팀의 기존 Spring `BE-main` 소스와 DTO를 수정하지 않고 현재 `OptimizationClient`가 호출하는 계약을 LARO FastAPI가 그대로 제공합니다.

```text
POST /optimize
POST /reoptimize
```

Spring의 기존 주소도 유지합니다.

```yaml
# local profile
fastapi:
  base-url: http://localhost:8000

# Spring Docker profile
fastapi:
  base-url: http://host.docker.internal:8000
```

프론트는 기존처럼 Spring BE를 호출하며, Spring이 내부적으로 LARO를 호출합니다.

---

## 2. 최초 최적화 `POST /optimize`

### 요청

```http
POST http://localhost:8000/optimize
Content-Type: application/json
```

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
    {"nodeId": 2, "x": 1.0, "y": 0.0},
    {"nodeId": 3, "x": 2.0, "y": 0.0},
    {"nodeId": 4, "x": 3.0, "y": 0.0}
  ],
  "edges": [
    {
      "edgeId": 11,
      "fromNodeId": 1,
      "toNodeId": 2,
      "distance": 1.0,
      "directionType": "BOTH"
    },
    {
      "edgeId": 12,
      "fromNodeId": 2,
      "toNodeId": 3,
      "distance": 1.0,
      "directionType": "A_TO_B"
    },
    {
      "edgeId": 13,
      "fromNodeId": 3,
      "toNodeId": 4,
      "distance": 1.0,
      "directionType": "BOTH"
    }
  ]
}
```

### 필드

| 필드 | 의미 |
|---|---|
| `warehouseId` | Spring PostgreSQL의 창고 PK |
| `robots[].robotId` | Spring Robot PK |
| `currentNodeId` | 현재 Node PK |
| `targetNodeId` | 최초 목적 Node PK. `null`이면 현재 위치 유지 |
| `batteryLevel` | 현재 DTO 호환용 메타데이터 |
| `nodes` | Spring이 구성한 창고 Node 집합 |
| `edges` | Spring이 구성한 창고 Edge 집합 |
| `distance` | 거리 또는 비용의 기준 단위 |
| `directionType` | `BOTH`, `A_TO_B`, `B_TO_A` |

중복 Node/Edge ID, 존재하지 않는 Node를 참조하는 Edge 또는 Robot 위치는 HTTP 422로 거절합니다.

### 응답

```json
{
  "requestId": "OPT-W1-7E6C3D0C589E4B7A",
  "status": "success",
  "routes": [
    {
      "robotId": 101,
      "nodePath": [1, 2, 3, 4],
      "totalDistance": 3.0,
      "estimatedTime": 3.0
    }
  ]
}
```

`estimatedTime`의 단위는 초입니다.

```text
estimatedTime
= totalDistance / BE_COMPAT_ROBOT_SPEED_DISTANCE_PER_SECOND
```

기본 속도는 `1.0 distance-unit/second`입니다.

### 그래프 저장·선택

v2는 정적 그래프를 PostgreSQL·Redis 양쪽에 무조건 복제하지 않습니다.

```text
BE_COMPAT_GRAPH_SOURCE=auto

1. Spring과 LARO가 공유하는 public.warehouse_node / warehouse_edge 조회
2. 요청 Graph Hash와 Spring Graph Hash가 같으면 Spring Graph 사용
3. Spring Graph가 없거나 요청과 다르면 Spring 테이블은 건드리지 않고
   laro_contract.route_node / route_edge에 호환 Graph 저장
4. Redis는 기본적으로 Graph Version·개수·Source 메타데이터만 Cache
5. Neo4j는 RouteNode/TRAVERSES 읽기용 Projection
```

신규 v2 설치에서는 구버전 `public.be_compat_graph_snapshots`를 생성하지 않습니다. 이미 존재하는 경우에만 Migration Fallback으로 읽습니다.

---

## 3. 재최적화 `POST /reoptimize`

### 요청

```http
POST http://localhost:8000/reoptimize
Content-Type: application/json
```

```json
{
  "simulationRunId": 77,
  "warehouseId": 1,
  "reason": "NEW_TASK_ADDED",
  "triggerRobotId": null,
  "blockedEdgeIds": [],
  "description": "새 작업 추가",
  "robots": [
    {
      "robotId": 101,
      "currentNodeId": 1,
      "batteryLevel": 82.0,
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

### `reason`

```text
ROBOT_TASK_COMPLETED
ROBOT_FAILURE
LOW_BATTERY
OBSTACLE_DETECTED
NEW_TASK_ADDED
MANUAL_REQUEST
```

### Runtime 선택 규칙

기본값은 다음입니다.

```dotenv
BE_COMPAT_RUNTIME_SOURCE=request_then_redis
```

```text
robots가 요청에 있으면
→ Spring이 조립한 요청 Robot 목록을 권위값으로 사용
→ Redis Edge Runtime의 차단 Edge만 추가 Overlay

robots가 비어 있으면
→ Spring Redis의 simulation:run:{runId}:robots와 RobotState 문서 조회
```

다른 모드:

```text
request_only
→ 요청 Body만 사용

redis_only
→ 요청 Robot 목록과 무관하게 Spring Redis 사용
```

후보에서 제외되는 Robot:

```text
status = ERROR 또는 OFFLINE
batteryLevel < BE_COMPAT_MIN_BATTERY_PCT
reason = ROBOT_FAILURE 또는 LOW_BATTERY이고 triggerRobotId와 동일
```

기본 최소 배터리:

```dotenv
BE_COMPAT_MIN_BATTERY_PCT=30
```

### Task 처리

```text
PENDING / ASSIGNED / IN_PROGRESS
→ 활성 Task

IN_PROGRESS의 기존 Robot이 계속 유효
→ 기존 배정 유지

기존 Robot 비가용
→ 다른 후보 Robot으로 재배정
```

현재 BE 무수정 계약에서는 정확한 `simTimeMs`, Step 경계, 적재 상태가 기본 Spring RobotState에 없으므로 재계획은 **현재 Node 기준 재배정**입니다.

### 차단 Edge

다음 두 집합을 합칩니다.

```text
request.blockedEdgeIds
Spring Redis Edge Runtime에서 status가 BLOCKED/CLOSED/MAINTENANCE인 Edge
```

### 응답

```json
{
  "requestId": "REOPT-S77-W1-43B5BA2D941E41",
  "status": "success",
  "assignments": [
    {"taskId": 5001, "robotId": 101}
  ],
  "routes": [
    {
      "robotId": 101,
      "nodePath": [1, 2, 3, 4],
      "totalDistance": 3.0,
      "estimatedTime": 3.0
    }
  ]
}
```

상태:

| 상태 | 의미 |
|---|---|
| `success` | 모든 활성 Task 배정 성공 |
| `partial_success` | 일부 Task만 도달 가능 |
| `no_eligible_robot` | 후보 Robot이 없어 허구 배정을 만들지 않음 |

---

## 4. 기존 Spring 공개 API와의 관계

프론트 또는 Postman은 기존 Spring BE를 호출할 수 있습니다.

### 최초 최적화

```http
POST http://localhost:8080/api/optimizations
```

Body는 `/optimize`와 동일하며 Spring이 내부적으로 LARO를 호출합니다.

### 재최적화

```http
POST http://localhost:8080/api/optimizations/simulation-runs/{simulationRunId}/reoptimize
```

프론트 입력은 기존 Spring 계약입니다.

```json
{
  "reason": "NEW_TASK_ADDED",
  "triggerRobotId": null,
  "blockedEdgeIds": [],
  "description": "새 작업 추가"
}
```

Spring이 PostgreSQL과 Redis에서 Robot·Task를 조회해 `/reoptimize` Body를 조립합니다.

---

## 5. 진단 API

### Graph 상태

```http
GET /compat/v1/warehouses/{warehouseId}/graph
```

```json
{
  "warehouseId": 1,
  "available": true,
  "graphVersion": "0d9c...",
  "nodeCount": 159,
  "edgeCount": 218,
  "source": "spring_db"
}
```

`source` 가능 값:

```text
spring_db
contract
legacy_postgres
redis
memory
```

### 공유 DB 계약

```http
GET /compat/v2/contract
```

확인 항목:

```text
laro_contract Schema 준비 여부
Spring public Graph 테이블 존재 여부
Graph Source 설정
Redis Cache 모드
Runtime Source 설정
```

### Spring Redis Runtime

```http
GET /compat/v2/simulation-runs/{simulationRunId}/runtime
```

모드:

```text
FULL
→ Run Meta와 Robot LARO Extension이 모두 존재

COMPATIBILITY
→ Spring 기본 RobotState만 존재

NOT_INITIALIZED
→ robots Set 또는 유효 Robot State 없음
```

### 로컬 테스트용 Runtime Bootstrap

```http
PUT /compat/v2/simulation-runs/{simulationRunId}/runtime
```

```json
{
  "warehouseId": 1,
  "simTimeMs": 3000,
  "replace": true,
  "robots": [
    {
      "robotId": 101,
      "currentNodeId": 1,
      "batteryLevel": 82.0,
      "status": "IDLE"
    }
  ]
}
```

운영에서는 끕니다.

```dotenv
BE_COMPAT_DEBUG_RUNTIME_API_ENABLED=false
```

---

## 6. 오류 응답

| HTTP | 상황 |
|---|---|
| 404 | 호환 또는 Debug Runtime API 비활성화 |
| 409 | Graph 미등록, 목적지 도달 불가, Path/Body Warehouse 불일치 |
| 422 | 필드 누락, 자료형 오류, 중복 ID, 알 수 없는 Node 참조 |
| 503 | PostgreSQL·Redis·Neo4j 등 외부 경계 오류 |

---

## 7. BE 무수정 조건의 기능 한계

기존 Spring 응답 DTO가 표현하는 값은 다음입니다.

```text
robotId
nodePath
totalDistance
estimatedTime
taskId → robotId assignment
```

따라서 다음 Native LARO 결과는 이 호환 응답으로 전달할 수 없습니다.

```text
MOVE / WAIT / SERVICE 절대 시간표
MAPF 충돌 WAIT
Station Reservation
Handling Unit RETURN / EMPTY_TOTE
Rolling-Horizon Handover Point
Rule/Agent Evaluation ID
```

이 기능은 LARO Native Mission API 코드에 남아 있으나, 이 호환 Docker Stack은 fresh DB에 Native 업무 스키마를 자동 Seed하지 않습니다. Native Mission API를 실제로 사용하려면 LARO standalone infrastructure를 별도로 준비하고, Spring에서 사용하려면 Spring DTO·Plan 저장·Playback 계약을 최소한 확장해야 합니다.
