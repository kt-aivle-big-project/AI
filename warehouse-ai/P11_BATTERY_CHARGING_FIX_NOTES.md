# P11 배터리·충전 수정 기록

기준 소스: `warehouse_langgraph_agent_vscode_p10_mock_batch_auto_execution_fix(2).zip`

## 수정한 문제

1. `R2-03의 배터리가 21%라고 가정`을 숫자형 `LOW_BATTERY` 이벤트로 파싱한다.
2. 가정값은 SQL/Redis Snapshot 원본이 아니라 Optimization Problem의 로봇 복사본에만 적용한다.
3. `출고 노드 2146`을 `target_node_ids=[2146]`으로 추출한다.
4. `MINIMUM_REQUIRED_CHARGE`일 때 충전소 우회 이동과 작업 소비량을 포함해 최소 충전량을 계산한다.
5. active CHARGER의 `charging_cost`가 있으면 최저 비용을 우선하고, 값이 전혀 없으면 거리 fallback과 경고를 남긴다.
6. CHARGE 작업에 충전량, 목표 배터리, 충전 시간, 후보 충전소, 비용, 선택 정책과 근거를 저장한다.
7. Verification이 가정 미적용, CHARGE 누락, 최소 배터리 미달, 비활성 충전소, 충전 계산 불일치, 비용 선택 오류, 목적지 누락을 차단한다.
8. 단일 E상품 명령에 기존 F상품 WORK가 내부 재고 검증 범위로 섞이지 않도록 분리한다.
9. `priority=NORMAL`, `insertion_policy=NORMAL`인 INSERT_TASK를 긴급 작업으로 표시하지 않는다.
10. Neo4j 지도 업로드·조회에서 선택적 `charging_cost` 속성을 보존한다.

## 비용 데이터에 대한 사실

P10의 기본 `examples/map_nodes.json`에는 충전소 비용 값이 없다. 임의 비용을 생성하지 않았다.
실제 비용 기반 선택을 사용하려면 CHARGER 노드에 동일한 단위의 `charging_cost` 숫자를 입력해야 한다.
비용이 없을 때는 `DISTANCE_FALLBACK_NO_COST_DATA` 정책과 `CHARGER_COST_DATA_MISSING` 경고가 기록된다.

## 검증 결과

- 실제 설치 패키지 없이 실행 가능한 순수 파서·충전·로봇 어댑터 테스트: 131 passed
- 임시 import/graph 호환 스텁을 사용한 P11 신규 시나리오 테스트: 4 passed
- 같은 스텁을 사용하고 OpenAI strict-schema 테스트 1개 파일을 제외한 전체 회귀 테스트: 540 passed
- `python -m compileall -q app tests`: 통과

현재 실행 환경에는 실제 `langgraph`, `openai`, `neo4j`, `redis`, `sqlalchemy` 패키지가 없어
실제 패키지 기반 전체 테스트는 실행하지 못했다. OpenAI SDK의 strict schema 변환 테스트 파일도
`openai` 패키지 부재로 제외했다. 스텁은 테스트 실행에만 사용했으며 결과 ZIP에는 포함하지 않았다.
