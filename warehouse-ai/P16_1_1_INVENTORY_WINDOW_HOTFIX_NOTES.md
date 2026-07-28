# P16.1.1 Inventory Window Hotfix

## 문제

시간창이 `09:00~11:00`으로 정상 파싱되어도 재고 사전검사가 `09:00` 정각만 검사했다. 기존 LOT가 `09:04:31`에 사용 가능해지는 B상품은 11시 이전에 출고 가능하지만 재고 부족으로 차단됐다.

또한 LLM structured output이 `required_at=시간창 시작`, `required_by=시간창 종료`로 반환하면 두 필드 불일치 검증 때문에 rule fallback이 발생했다.

## 수정

- 기준 시각 이후 사용 가능해지는 기존 PostgreSQL LOT를 `CURRENT_LOT_AVAILABLE` 재고 이벤트로 반영
- HARD_WINDOW에서는 전체 수량 확보 최초 시각이 시간창 안이면 그 시각으로 재고 재평가
- 시간창 내 전체 수량 확보가 불가능하면 `latest_finish` 시점의 최대 가용량과 실제 부족량 계산
- `required_at`, `required_by`가 다르면 늦은 시각을 완료 deadline으로 정규화
- 실제 PICK 작업 시작은 LOT `available_at`과 시간창 `earliest_start` 중 늦은 시각 사용

## 기대 결과

- B 20 BOX: `09:04:31` 사용 가능 → 해당 시각 이후 계획 진행
- A 30 BOX: 시간창 내 10 BOX만 가능 → 가용 10, 부족 20으로 표시
- A+B 요청: `PARTIAL_SUCCESS`, A 차단, B 독립 작업 진행
- 사용 가능 시각이 11시 이후면 기존처럼 `EMERGENCY_REVIEW_REQUIRED`

## 검사

```powershell
python -m scripts.run_p16_1_1_inventory_window_checks
```

전체 회귀 테스트: `584 passed`
