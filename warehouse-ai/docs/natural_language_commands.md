# 자연어 명령

명령은 `POST /v1/planning/commands`의 `text`로 전달한다. 명확한 한국어 표현은 결정적 파서가 우선 처리하며, LLM이 없거나 실패해도 기본 명령은 동작한다. 명령에 없는 로봇·작업·노드 ID는 생성하지 않는다. 추출한 ID는 Snapshot 이후 실제 존재 여부를 별도로 검증한다.

## 지원 범위

- 조회: 로봇, 재고, 작업, 지도, 활성 계획, 시뮬레이션 이력, 재계획/검증/초기화 이력, 저장된 evidence
- 계획: 전체/선택 작업, 긴급 삽입, 로봇·노드·간선 제외, 로봇 수 제한, 고정 배정, 실행 중 작업 보호
- 최적화: 거리, 완료시간, tardiness, 에너지, 로봇 수, 계획 변경, 거리·시간 균형, 명시적 사용자 가중치
- 가정: 로봇 고장/저전력, 노드·간선 폐쇄, 충전소 불가, 긴급 주문, 작업 지연, 재고 부족
- 실행 모드: `PLAN_ONLY`, `SIMULATE_ONLY`, `EXECUTE`

가정 명령은 기본적으로 `SIMULATE_ONLY`다. “처리해줘”, “돌려줘”, “적용해봐”처럼 실행 의미가 불분명한 표현은 `EXECUTE`로 승격하지 않는다.

비교 명령은 `SCENARIO_COMPARISON`, `comparison_requested=true`, `requires_future_feature=true`로만 분류한다. PHASE 11의 실제 다중 시나리오 비교는 구현하지 않았다.

## ID 정규화

- `R-2`, `로봇 2번`, `로봇02` → `R-02`
- `W-3`, `작업 3번` → `W-003`

정규화는 존재를 의미하지 않는다. `verified_*_ids`와 `invalid_*_ids`는 Snapshot 결과로 결정된다.

## 최적화 기본값

명시적 기준이 없으면 다음 값을 유지한다.

```json
{
  "total_distance": 1.0,
  "makespan": 1.0,
  "tardiness": 5.0,
  "energy": 1.0,
  "robot_activation": 0.5,
  "plan_change": 2.0
}
```

## 최적화 프로필과 직접 가중치 우선순위

- 완료시간 최소화: `MINIMIZE_MAKESPAN`
- 이동거리 최소화: `MINIMIZE_DISTANCE`
- 납기 지연 최소화: `MINIMIZE_TARDINESS`
- 에너지 사용 최소화: `MINIMIZE_ENERGY`
- 사용 로봇 수 최소화: `MINIMIZE_ROBOTS`
- 기존 계획 변경 최소화: `MINIMIZE_PLAN_CHANGE`

명시적인 프로필은 대응 목적함수의 기본 가중치를 5배로 높인다. 사용자가
숫자 `optimization_weights`를 직접 지정하면 직접 지정한 값이 프로필 기본값보다
우선한다. 이 경우 프로필은 사용자의 목표를 표시하고, 실제 계산에는 직접 지정한
가중치를 사용한다. 후속 명령에서 새 프로필을 지정하면 이전 프로필과 가중치를
함께 교체하며, 로봇 수 같은 다른 대화 조건은 그대로 상속한다.


## P11 가상 배터리·자동 충전 명령

다음과 같이 로봇별 가정 배터리, 고정 배정, 최소 잔량 유지, 목적지 노드를 한 문장에 지정할 수 있다.

```text
R2-03의 배터리가 현재 21%라고 가정하고 E상품 30 BOX를 R2-03에 고정 배정해.
최소 배터리를 유지하지 못하면 active CHARGER 노드 중 비용이 가장 낮은 충전소에서
필요한 만큼 충전한 뒤 출고 노드 2146으로 이동해. 실제 Redis 배터리는 변경하지 말고
시뮬레이션만 해.
```

결정적 파서는 이를 다음 조건으로 구조화한다.

- `hypothetical_events[].parameters.battery_percent = 21`
- `target_node_ids = [2146]`, `target_node_type = OUTBOUND`
- `hard_constraints`에 `MINIMUM_REQUIRED_CHARGE` 추가
- 가정 배터리는 Optimization Snapshot 복사본에만 적용하며 실제 Redis 상태를 쓰지 않음
- 충전이 필요하면 충전소 이동 소비량과 이후 작업 소비량을 포함해 최소 충전량 계산
- 비용이 설정된 active CHARGER가 있으면 최저 비용 우선, 없으면 거리 fallback과 경고 기록
- 가정 미적용, 최소 잔량 미달, 필요한 CHARGE 누락 시 Verification은 PASS할 수 없음
