# P12 CHARGE 실행·경로 연속성 수정

## 기준

- 기준 소스: P11 battery charging fix
- 재현 명령: R2-03 배터리 21% 가정, E상품 30 BOX 고정 배정, 최소 배터리 20% 유지, 필요 충전 후 출고 노드 2146 이동, SIMULATE_ONLY

## 수정 내용

1. 같은 출고 운반 단위의 PICK/DROP 전체 경로를 미리 계산하여 너무 늦게 충전하지 않도록 수정
2. 가상 배터리 override와 `MINIMUM_REQUIRED_CHARGE` 조건에서는 충전소 도착 시점에도 최소 배터리를 유지하도록 제한
3. PICK을 완료해 화물을 적재한 DROP 작업은 충전 후 픽업 노드로 복귀하지 않고 충전소에서 목적지로 바로 이동
4. 라우터가 최적화기의 예상 종료 시각과 관계없이 `charge_duration_seconds` 전체를 충전소 점유 시간으로 예약
5. CHARGE waypoint를 RobotCommandAdapter가 실제 `CHARGE` 명령으로 변환하도록 통합 경로 보완
6. CHARGE 작업이 있는데 명령이 없거나 충전 시간이 다르면 Adapter validation 실패
7. Verification에 다음 차단 코드 추가
   - `BATTERY_BELOW_MINIMUM_AT_CHARGER`
   - `CHARGE_COMMAND_NOT_GENERATED`
   - `CHARGE_DURATION_NOT_ROUTED`

## 데이터 관련 제한

기존 지도 및 Neo4j CHARGER 데이터에 비교 가능한 실제 충전 비용값이 없는 경우 비용을 임의 생성하지 않는다. 이 경우 기존과 같이 `DISTANCE_FALLBACK_NO_COST_DATA` 정책과 `CHARGER_COST_DATA_MISSING` 경고를 사용한다.

비용 기반 선택을 검증하려면 각 CHARGER 노드에 동일 단위의 `charging_cost` 값을 입력해야 한다. 예시 형식은 다음과 같으며 값은 실제 운영 정책에 따라 정해야 한다.

```cypher
MATCH (n:MapNode {warehouse_id: 2, node_id: $node_id})
WHERE n.node_type = 'CHARGER'
SET n.charging_cost = $charging_cost
RETURN n.node_id, n.charging_cost;
```

## 검증 결과

- P12 전용 테스트 4개 추가
- 배터리·충전·RobotAdapter 관련 테스트 22개 통과
- 전체 회귀 테스트 551개 통과
- Python compileall 통과

전체 테스트는 현재 작업 컨테이너에 실제 LangGraph/OpenAI/Neo4j/Redis 패키지가 없어, import 및 순차 StateGraph 실행을 위한 임시 테스트 스텁을 외부 경로에서 사용했다. 스텁은 결과 ZIP에 포함하지 않았다. 실제 사용자 환경에서는 requirements.txt의 실제 패키지를 사용한다.
