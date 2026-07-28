# What-if 시나리오 비교

PHASE 11 비교는 모든 시나리오를 `SIMULATE_ONLY`로 실행한다. 각 시나리오는
독립 `NaturalLanguageCommand`, `PlanningState`, `simulation_id`, Redis
`sim:{simulation_id}:*` 상태를 사용한다. 실제 로봇 상태, 작업, 재고, 활성 계획,
Robot Gateway와 Neo4j 지도는 변경하지 않는다.

## 지원 입력

- 로봇 2대와 3대 비교
- 거리/납기/완료시간/에너지/로봇 수/계획 변경 우선 조건 비교
- 특정 로봇 포함/제외 비교
- Neo4j `CONNECTED_TO.edge_id` 통로의 정상/폐쇄 비교
- 기존 계획 유지와 새 계획 비교
- 명시한 로봇의 고장 전후 비교
- 구조화된 `ScenarioComparisonRequest.scenarios`

조건이 두 개보다 적거나 비교 기준이 없으면 `CLARIFICATION_REQUIRED`를
반환한다. 기본 상한은 4개, 시스템 절대 상한은 6개다. 같은 제약을 가진
시나리오는 하나로 정규화한다.

## 실제 계산 지표

`SimulationResult`, `CuOptPlan.metadata`, `CollisionFreePlan.metadata`에서만
로봇 수, 배정/미배정 작업, 거리, makespan, tardiness, energy, 충돌,
WAIT, 계획 변경 및 재계획 횟수를 읽는다. 기준값이 0이면 percentage는
`null`이다. LLM은 정량값이나 추천 시나리오 ID를 변경하지 않는다.

추천은 FAIL/Clarification을 제외한 후 미배정 작업, 충돌, 사용자가 명시한
목표, `scenario_id` 순으로 결정한다. 목표가 없으면 추천하지 않고 tradeoff만
반환한다.

## API

- `POST /v1/scenario-comparisons`
- `GET /v1/scenario-comparisons`
- `GET /v1/scenario-comparisons/{comparison_id}`
- `GET /v1/scenario-comparisons/{comparison_id}/scenarios/{scenario_id}`

동일 요청은 정규화된 요청 SHA-256 키로 idempotent 처리된다. 목록과 기본
상세 응답에는 대규모 후보/경로 evidence 대신 저장된 요약만 포함된다.

## 저장 및 복구

`migrations/007_scenario_comparisons.sql`은 append 성격의 비교와 시나리오
실행 요약을 저장한다. 롤백 전 두 테이블을 내보낸 다음 자식 테이블
`scenario_comparison_run`, 부모 `scenario_comparison` 순으로 삭제한다.

