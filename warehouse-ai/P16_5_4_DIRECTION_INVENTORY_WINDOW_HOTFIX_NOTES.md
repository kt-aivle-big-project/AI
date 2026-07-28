# P16.5.4 Direction, Inventory and Noon-Window Hotfix

## Fixed

1. 복합 문장에서 이전 입고 문장이 다음 E/F 출고 문장을 오염시키던 고정 길이 context 판정을 문장 단위 판정으로 변경했습니다.
2. `오전 10시 30분부터 12시까지`의 생략된 종료 오전/오후를 정오로 해석합니다.
3. INBOUND PICK은 입고 도크에서 물품을 받는 작업이므로 기존 저장 재고 소비 검증에서 제외합니다.
4. OUTBOUND PICK의 FEFO 할당이 현재 lot + FUTURE_INBOUND로 수량을 충족하면 단순 현재 재고 비교로 실패시키지 않습니다.
5. 재고 부족으로 최적화 대상에서 제외된 출고 작업도 최종 verification evidence에 남깁니다.

## Response contract

- API version: `2.5.4`
- Response schema: `p16.5.4`
- cuOpt REST 계약과 CPU fallback 계약은 P16.5.3과 동일합니다.

## Verification

```powershell
python -m scripts.run_p16_5_4_final_checks
python -m scripts.run_p16_release_checks
pytest -q tests/test_p16_5_4_inventory_direction_hotfix.py
```

Swagger 예시는 `examples/p16_5_4_complex_daily_request.json`을 사용합니다.
