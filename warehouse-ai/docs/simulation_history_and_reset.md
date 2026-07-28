# 시뮬레이션 이력과 초기화

## 저장 구조

`simulation_run`은 시뮬레이션을 실행할 때마다 결과를 한 행씩 추가하는 실행 이력이다. 같은 `simulation_id`로 재계획하거나 다시 실행해도 이전 행을 갱신하거나 삭제하지 않는다. 이를 통해 과거 입력, 결과, 체크포인트를 감사 자료로 보존한다.

`simulation_session`은 한 시뮬레이션의 현재 상태를 나타낸다.

- `base_state`: 가상 실행을 시작하기 전의 초기 Snapshot
- `current_state`: timeline 재생 후의 현재 가상 상태
- `checkpoint`: 현재 진행 위치
- `status`: `ACTIVE`, `COMPLETED`, `FAILED`, `RESET`, `RESET_PENDING`, `RESET_FAILED`
- `generation`: 세션 세대 번호. 기존 `simulation_id`는 초기화 후 재사용하지 않는다.

기존 `simulation_run` 중 `simulation_id`와 `current_state`가 있는 행은 마이그레이션 시 시뮬레이션별 최신 행 하나를 사용해 세션으로 backfill한다. 당시 별도의 초기 상태가 저장되지 않았으므로 backfill된 세션은 `base_state`와 `current_state`에 같은 값을 사용한다.

## Redis 키

시뮬레이션의 가상 상태만 다음 키에 저장한다.

```text
sim:{simulation_id}:inventory
sim:{simulation_id}:robots
sim:{simulation_id}:works
sim:{simulation_id}:events
```

창고별 활성 시뮬레이션 ID는 다음 Set에 등록한다.

```text
wh:{warehouse_id}:simulations
```

초기화는 PostgreSQL `simulation_session`을 기준 저장소로 사용한다. Redis Set은 빠른 조회와 정리용이며 유일한 기준이 아니다. `KEYS`나 `SCAN` 없이 대상 시뮬레이션의 네 키를 정확히 지정해 삭제하고 Set에서 해당 ID만 제거한다.

## 단일 시뮬레이션 초기화

```http
POST /v1/simulations/{simulation_id}/reset
Content-Type: application/json

{
  "actor_id": "user-01",
  "reason": "지도와 작업 조건을 수정한 후 다시 실행하기 위해 초기화"
}
```

처리 순서는 `RESET_PENDING` 저장, Redis 가상 상태 삭제, `RESET` 확정이다. 이미 초기화된 세션은 오류 없이 `ALREADY_RESET`을 반환한다. 존재하지 않는 세션은 HTTP 404를 반환하되 실패한 명령과 단계 로그는 보존한다.

## 창고 전체 시뮬레이션 초기화

```http
POST /v1/warehouses/1/simulations/reset-all
Content-Type: application/json

{
  "actor_id": "user-01",
  "reason": "새로운 창고 설정으로 전체 시뮬레이션을 다시 실행"
}
```

해당 창고의 `RESET`이 아닌 세션을 PostgreSQL에서 조회하고 세션별로 독립 처리한다. 일부 Redis 삭제나 DB 확정이 실패하면 성공한 세션은 유지하면서 전체 결과를 `PARTIAL_SUCCESS`로 기록한다. 실패한 세션은 `RESET_FAILED`로 남겨 재시도할 수 있다. 대상이 없으면 `NO_ACTIVE_SIMULATIONS`를 반환하며 정상 명령 이력으로 저장한다.

## 초기화 감사 이력

초기화 요청은 기존 감사 기능을 그대로 사용한다.

- `command_history`: RESET 명령의 `PROCESSING`, `SUCCESS`, `FAILED` 상태
- `planning_stage_log`: 요청, 검증, 상태 캡처, Redis 삭제, 완료 또는 실패 단계
- `simulation_reset_audit`: 대상, 사용자, 사유, 전후 요약, 부분 실패 내역

사유와 로그 세부정보에는 기존 민감정보 제거 정책을 적용한다. 감사 로그 저장 실패는 경고로 남기되, 핵심 세션 상태 변경 실패는 성공으로 처리하지 않는다.

## 초기화해도 유지되는 데이터

다음 데이터는 수정하거나 삭제하지 않는다.

- 모든 `simulation_run` 실행 이력
- `command_history`, `planning_stage_log`, `simulation_reset_audit`
- PostgreSQL의 `warehouse_items`, `works`, `robot`, `work_event`, `inventory_reservation`
- Redis의 실제 로봇, 실제 작업, 활성 계획 및 계획 버전 키
- Neo4j의 `Warehouse`, `Zone`, `MapNode`, `CONNECTED_TO`

초기화로 제거되는 데이터는 대상 `simulation_id`의 Redis 가상 상태 네 키와 창고별 시뮬레이션 Set 멤버뿐이다. `simulation_session` 행은 삭제하지 않고 RESET 상태로 보존한다.

## 조회 API

```text
GET /v1/simulations
GET /v1/simulations/{simulation_id}
GET /v1/simulations/{simulation_id}/state
GET /v1/simulations/{simulation_id}/runs
GET /v1/simulations/{simulation_id}/logs
GET /v1/warehouses/{warehouse_id}/simulation-reset-logs
```

목록 API는 `warehouse_id`, `status`, `date_from`, `date_to`, `limit`, `offset` 필터를 지원한다. 기본 `limit`은 50, 최댓값은 200이다. 상세 API는 큰 상태 JSON 대신 요약을 반환하며 전체 상태는 `/state`에서 별도로 조회한다. 실행 이력과 초기화 로그는 최신순 페이지네이션을 지원한다.

## 마이그레이션

서비스 배포 전에 PostgreSQL에 다음 파일을 적용한다.

```powershell
$psqlUrl = $env:DATABASE_URL -replace '^postgresql\+psycopg://', 'postgresql://'
psql $psqlUrl -f migrations/004_simulation_history_and_reset.sql
```

Docker PostgreSQL을 사용하는 경우 접속 사용자와 데이터베이스 이름에 맞춰 `docker exec ... psql -f ...` 형식으로 실행한다. 운영 적용 전 백업하고 스테이징에서 backfill 결과와 제거되는 unique index를 먼저 확인한다.

## Swagger 확인 순서

1. `POST /v1/planning/commands`로 `SIMULATE_ONLY`를 실행한다.
2. 응답의 `simulation_id`를 기록한다.
3. `GET /v1/simulations/{simulation_id}`와 `/state`, `/runs`, `/logs`를 확인한다.
4. `POST /v1/simulations/{simulation_id}/reset`을 실행한다.
5. 상세 조회에서 상태가 `RESET`이고 실행·감사 이력이 남아 있는지 확인한다.
6. 새 시뮬레이션들을 만든 뒤 창고 단위 `reset-all`과 reset 로그 조회를 확인한다.
