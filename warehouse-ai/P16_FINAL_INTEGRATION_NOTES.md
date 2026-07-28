# P16 Final Integration

P16은 P11~P15 기능을 통합한 최종 개발 버전입니다.

## 핵심 수정

### 최종 라우팅 거리 기반 배터리 보정

Optimizer의 예상 경로보다 Prioritized Time A* 최종 경로가 길어질 수 있습니다.
P15의 실제 사례는 예상거리 30.84, 최종거리 34.44였지만 배터리 소모는 예상거리
기준 1.542%로 계산되고 있었습니다.

P16은 라우팅 후 다음을 수행합니다.

1. 로봇별 최종 route distance 계산
2. `route_distance × energy_per_distance`로 실제 예상 소비량 재계산
3. 기존 CHARGE 작업의 충전량과 목표 배터리 보정
4. 충전 시간이 time step 경계를 넘어 바뀌면 경로와 예약 재계산
5. CHARGE 작업이 없어서 안전기준을 맞출 수 없으면 Verification 차단
6. Simulation도 동일한 최종 경로 기반 에너지 사용

근거 필드:

```text
route_energy_reconciliation.energy_source = ROUTING_FINAL_DISTANCE
simulation.metrics.battery_by_robot.*.energy_source = ROUTING_FINAL_DISTANCE
```

### 사용자용 COMPACT / 개발자용 FULL

`NaturalLanguageCommand.response_view`를 추가했습니다.

- `AUTO`: STANDARD/SUMMARY는 COMPACT, DEBUG는 FULL
- `COMPACT`: 핵심 결과만 반환
- `FULL`: 기존 전체 응답 반환

내부 계획 결과와 감사 저장 데이터는 축소하지 않습니다.

### 최종 자동 검사

```powershell
python -m scripts.run_p16_release_checks
```

P15 다중 로봇 4개 시나리오, 응답 뷰, compile, 문서 구성을 검사합니다.

## 하위 호환성

- `run_planning()`은 기존 전체 응답을 그대로 반환합니다.
- API에서 `response_view=FULL`을 사용하면 기존 필드를 유지합니다.
- 기존 공개 smoke test는 FULL을 명시하도록 수정했습니다.
- DEBUG 요청의 AUTO 동작은 FULL이므로 기존 Swagger 디버깅 흐름을 유지합니다.

## 최종 운영 전 확인

- `.env` secret 미포함
- PostgreSQL migration 적용
- Neo4j charger cost 및 active node 확인
- Redis namespace 확인
- Robot Gateway URL 확인
- `pytest`, P16 release checks, 공개 API smoke test 실행
