# 백엔드 연동 전달 체크리스트

## AI 담당 전달물

- [x] 사용자 결과 계약 `planning-ui.v1`
- [x] 시뮬레이션 계약 `simulation-view.v1`
- [x] 실행 상태 계약 `execution-status.v1`
- [x] 개발자 상세 계약 `planning-debug.v1`
- [x] Robot Gateway 계약 모델 `robot-command.v1`
- [x] PostgreSQL·Neo4j·Redis 필수 데이터 목록
- [x] 공통 PlanningSnapshot 구조
- [x] 공개 API 경로
- [x] 관련 단위 테스트

## 백엔드 담당 확인사항

- [ ] 회사 SQL 컬럼을 PlanningSnapshot 필드에 매핑
- [ ] PostgreSQL, Neo4j, Redis의 공통 ID 일치 확인
- [ ] 프론트에는 `/result`, `/view`, `/status`만 기본 제공
- [ ] `/debug`와 `/plan-evidence`는 관리자·개발자 권한으로 제한
- [ ] 실제 실행 API에는 실행 권한 확인 적용
- [ ] API 오류와 재시도 정책 연결
- [ ] Redis 초기화와 복구 절차 정의

## 프론트엔드 담당 확인사항

- [ ] 채팅 결과는 `planning-ui.v1` 사용
- [ ] 지도 애니메이션은 `simulation-view.v1` 사용
- [ ] 실행 화면은 `execution-status.v1` 사용
- [ ] FULL 응답의 내부 필드를 직접 참조하지 않음

## 시뮬레이션 담당 확인사항

- [ ] `time_step_seconds`를 기준으로 시간 재생
- [ ] route waypoint의 node_id를 지도 node_id와 연결
- [ ] 실제 운영 Redis와 시뮬레이션 상태를 분리

## 통합 검증 시나리오

1. 자연어 계획 요청
2. 사용자 결과 API 조회
3. 시뮬레이션 화면 API 조회
4. 계획 승인
5. Robot Gateway 전달
6. 명령 처리 결과 수신
7. 실행 상태 API 조회
8. START 실패 시 계획 복구와 재고 예약 해제 확인
