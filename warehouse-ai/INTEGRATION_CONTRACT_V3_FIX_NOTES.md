# Integration Contract V3 Fix Notes

## 수정 내용

1. 사용자용 계획 결과와 실행 상태의 `verification.warning_findings`,
   `verification.user_visible_warnings`에서 cuOpt 제공자 내부 오류를 제거했습니다.
   상세 원문은 기존처럼 debug/evidence API에 유지됩니다.
2. 재고 검증의 `required_at` 및 긴급 재고 검토 시각을 창고 시간대로 변환한 뒤
   사용자 보고서에 표시하도록 수정했습니다.

## 영향 범위

- 계획, 최적화, 경로 탐색, 충돌 회피, 시뮬레이션, 충전 및 Gateway 실행 로직은 변경하지 않았습니다.
- 변경 범위는 연동용 공개 응답과 사용자 보고서의 표시 계층입니다.

## 검증

- 관련 테스트: 55 passed
- 전체 테스트: 796 passed
