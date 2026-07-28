# P16.5.7 Idle Whitelist Safety Hotfix

## 목적

P16.5.6은 STORAGE/INBOUND/OUTBOUND에서 장시간 대기하던 로봇을 일반 ROUTE 노드로 이동시켜 `ROUTE_FAILED`를 해소했습니다. 그러나 실제 Swagger 결과에서는 2001, 2002, 2043 같은 일반 이동 경로에서 약 2시간 동안 대기해 다른 로봇이 우회했습니다.

P16.5.7은 **길을 막지 않는 것을 하드 제약**으로 전환합니다.

## 하드 제약

- `NO_IDLE_ON_TRANSIT_NODE`
- `NO_IDLE_ON_INTERSECTION`
- `NO_IDLE_ON_SERVICE_NODE`
- `NO_IDLE_ON_ARTICULATION_NODE`
- `NO_IDLE_ON_CONGESTION_NODE`
- `NO_IDLE_ON_CHARGER_SLOT_AFTER_CHARGE`
- `IDLE_ONLY_ON_WHITELISTED_NODE`

일일 일정 명령에는 위 제약이 기본 적용됩니다.

## 대기 허용 노드

장기 대기는 다음 타입 또는 `idle_allowed=true`가 설정된 노드에서만 허용합니다.

- `PARKING`
- `STAGING`
- `HOLDING`
- `CHARGER_WAITING_AREA`
- `ROBOT_PARKING`

일반 `ROUTE`, `INTERSECTION`, `STORAGE`, `INBOUND`, `OUTBOUND`, `CHARGER`는 장기 대기 후보가 아닙니다.

## 명시적 공백 작업

라우팅 메타데이터에 다음 작업이 생성됩니다.

- `MOVE_TO_IDLE_NODE`
- `WAIT_AT_IDLE_NODE`

확인 필드:

```json
{
  "idle_action_task_count": 12,
  "idle_action_tasks": [],
  "idle_policy": {
    "strict": true,
    "violation_count": 0
  }
}
```

## 초기 대기 처리

첫 작업이 몇 시간 뒤인 로봇도 초기 위치에서 그대로 기다리지 않습니다.

```text
Snapshot 위치
→ 전용 PARKING 이동
→ PARKING에서 대기
→ 작업 시간에 출발
```

같은 초기 노드에 여러 로봇이 있으면 출발 시각을 순차화합니다.

## 전용 대기 노드가 없을 때

일일 계획은 다음 오류로 승인되지 않습니다.

```text
IDLE_NODE_NOT_CONFIGURED
```

임의의 통로 노드를 안전한 대기 장소로 추정하지 않습니다.

## 창고 2 데모 노드

- 2160: PARKING P1, 연결 노드 2078
- 2161: PARKING P2, 연결 노드 2079
- 2162: PARKING P3, 연결 노드 2080

설정 방법:

```powershell
python -m scripts.seed_p16_5_7_idle_nodes --warehouse-id 2
```

또는 Neo4j Aura Query에서 다음 파일을 실행합니다.

```text
migrations/011_p16_5_7_idle_parking_nodes.cypher
```

## 검증 명령

```powershell
python -m scripts.run_p16_5_7_final_checks
pytest -q tests/test_p16_5_7_idle_whitelist.py
```

## 이번 버전에 포함하지 않은 기능

P16.5.7은 대기 위치 안전성 기반을 확정하는 버전입니다. 다음 기능은 후속 버전에 포함합니다.

- 긴 공백의 충전소 복귀 비용 비교
- 기회 충전
- 적정 충전량 최적화
- 충전기 슬롯 및 대기열 최적화
- cuOpt 입력에 idle/charge 방문 작업 직접 포함
