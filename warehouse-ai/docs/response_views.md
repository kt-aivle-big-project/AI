# P16 API 응답 뷰

`POST /v1/planning/commands`는 `response_view`로 응답 크기를 선택합니다.
내부 계획과 감사 저장 데이터는 항상 전체 구조를 유지하며, 이 옵션은 공개 API로
반환되는 JSON만 바꿉니다.

## AUTO

기본값입니다.

- `report_detail_level=SUMMARY` 또는 `STANDARD`: `COMPACT`
- `report_detail_level=DEBUG`: `FULL`

일반 화면에서는 AUTO를 사용하고, 개발자 진단이 필요할 때 DEBUG를 사용합니다.

```json
{
  "warehouse_id": 2,
  "text": "E상품 30 BOX를 출고 노드 2146으로 이동하는 계획을 시뮬레이션해줘.",
  "requested_execution_mode": "SIMULATE_ONLY",
  "report_detail_level": "STANDARD",
  "response_view": "AUTO"
}
```

## COMPACT

사용자 화면과 일반 API 소비자를 위한 결과입니다. 다음 정보만 유지합니다.

- 상태, 사용자용 설명, command/plan/simulation 식별자
- Verification 결과
- 작업 배정과 순서
- 최종 거리·시간·충돌·배터리
- 충전소 선택과 충전량
- 실행 의존성 검증
- 충돌 해결 요약
- 재고 가능 여부
- 실제 Gateway 전송 여부

다음 대용량 내부 정보는 제거합니다.

- 전체 LLM 해석 객체
- 모든 최적화 후보 점수
- 모든 경로 waypoint와 reservation
- 전체 Robot Command 배열
- 전체 trace
- 중복된 optimization/simulation/data 사본

## FULL

기존 전체 응답 계약입니다. 테스트, 원인 분석, 감사 확인에 사용합니다.

```json
{
  "warehouse_id": 2,
  "text": "상세 근거와 전체 경로를 포함해 시뮬레이션해줘.",
  "requested_execution_mode": "SIMULATE_ONLY",
  "report_detail_level": "DEBUG",
  "response_view": "FULL"
}
```

`FULL`에는 `response_schema_version`과 `response_view=FULL`이 추가되며 기존 필드는
그대로 유지됩니다.

## 상세 근거 재조회

COMPACT 응답의 `details.evidence_api`에 계획 근거 조회 경로가 포함됩니다.

```text
GET /v1/commands/{command_id}/plan-evidence
GET /v1/commands/{command_id}/stages
GET /v1/simulations/{simulation_id}/logs
```

## P16.3.3 range compression

`FULL` 응답은 내부 충돌 검사용 time-expanded 경로를 변경하지 않고, 외부 표시에서만 연속 `WAIT`와 `CHARGE` waypoint/timeline 이벤트를 범위로 압축합니다. 압축 행은 `time_step`, `end_time_step`, `duration_steps`, `duration_seconds`를 포함합니다.
