# P16.5.10 cuOpt 충전 방문 및 통합 목적함수

## 목표

P16.5.9까지 충전 작업은 기본 작업 일정이 만들어진 뒤 로컬 단계에서 삽입됐다.
P16.5.10은 선택된 충전 방문을 cuOpt/CPU 최적화 입력에 정식 작업으로 다시 넣어
로봇 배정, 방문 순서, 시간창, 이동비용과 함께 검토한다.

## 1. 두 단계 최적화

1. 1차 최적화: PICK/DROP 기본 배정과 순서 생성
2. 배터리·공백 분석: 필수 충전 및 기회 충전 후보 선택
3. 선택한 충전을 명시적 `CHARGE` AtomicTask로 변환
4. 충전소에서 다음 작업 위치까지 `MOVE` 선이동 작업 생성
5. 2차 최적화: PICK/DROP/CHARGE/MOVE를 함께 최적화
6. 로컬 공유 자원 스케줄러가 서비스 노드·충전기·대기 공간 용량 확정
7. Prioritized Time A*가 최종 충돌 없는 지도 경로 생성

`MOVE` 선이동 작업은 충전 완료 후 다음 작업 시작 시각까지 작업 노드 근처로
이동할 수 있게 한다. 이 작업이 없으면 로컬 정규화 단계가 다음 작업의 하드
시간창이 시작된 뒤 충전소에서 출발해 `HARD_WINDOW_VIOLATION`을 만들 수 있다.

## 2. cuOpt 입력에 포함되는 충전 정보

- 충전 노드
- 담당 로봇
- 충전 서비스시간
- 충전 작업 시간창
- 선행·후행 작업
- 충전 후 다음 작업 위치로 이동하는 명시적 MOVE
- 거리·이동 에너지·혼잡 노드·충전소 방문 비용을 반영한 composite cost matrix

응답 메타데이터:

```json
{
  "charge_visit_optimization_contract": {
    "version": "p16.5.10",
    "mode": "TWO_PASS_EXPLICIT_CHARGE_VISITS",
    "explicit_charge_task_count": 5,
    "explicit_relocation_task_count": 5
  }
}
```

## 3. 역할 분리

### cuOpt

- 로봇별 작업 배정
- 작업 방문 순서
- 시간창
- 명시적 충전 방문
- 충전 서비스시간
- 거리·에너지·혼잡·충전소 방문 composite cost

### 로컬 창고 스케줄러

- PICK–DROP 동일 로봇
- 작업 노드 서비스 용량
- 충전기 슬롯 용량
- 대기 공간 용량
- 정확한 충전량
- 경로 확정 후 배터리 재계산

### Prioritized Time A*

- 정점 충돌 방지
- 간선 교차 충돌 방지
- 시간별 노드·간선 예약
- 실제 이동 중 WAIT 위치 검증

## 4. 통합 목적함수

P16.5.10은 최종 경로와 공유 자원 예약이 확정된 뒤 별도의
`operational_objective`를 계산한다.

포함 항목:

- 총 이동거리
- 전체 완료시간
- 납기 지연
- 이동 에너지
- 충전 시간
- 충전소 대기시간
- 충전 방문 횟수
- 로봇 활성화 비용
- 작업계획 변경 비용
- 혼잡 노드 방문
- 공유 자원 점유시간
- 불필요한 충전소 왕복거리

하드 제약 위반은 목적함수의 큰 벌점으로 허용하지 않는다.
불가능한 후보는 최적화 후보에서 제거하며 응답에는 다음 정책으로 기록한다.

```text
INFEASIBLE_CANDIDATES_REMOVED_NOT_PENALIZED
```

## 5. 응답 확인 기준

```text
response_schema_version = p16.5.10
status = SIMULATION_SUCCESS
result.valid = true
result.optimizer_roles.explicit_charge_task_count > 0
result.objective.status = PASS
result.objective.metrics.charger_visit_count > 0
result.resources.valid = true
result.schedule_validation.resource_capacity_valid = true
result.collision_resolution.final_conflict_count = 0
errors = []
```

## 6. 실행

추가 Neo4j 시드는 필요하지 않다. P16.5.9에서 설정한 공유 자원 속성을 그대로
사용한다.

```powershell
python -m scripts.run_p16_5_10_final_checks
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --reload
```
