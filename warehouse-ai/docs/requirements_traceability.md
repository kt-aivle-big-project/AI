# 최종 요구사항 추적표

| 요구사항 | 구현 위치 | 검증 |
|---|---|---|
| 자연어 명령 구조화 | `app/services/command_language.py`, LLM interpretation | `test_natural_language_commands.py` |
| Supervisor 실행 모드 결정 | `app/planning/nodes.py`, `app/prompts.py` | `test_supervisor.py`, `test_planning_modes.py` |
| 단일 로봇·단일 작업 LOCAL_REPLAN | P14 scope override | `test_p14_scope_and_dependency_validation.py` |
| PostgreSQL/Neo4j/Redis Snapshot | repositories, snapshot node | `test_postgres_snapshot_scope.py`, `test_pipeline.py` |
| 재고·LOT 가능성 검증 | inventory projection/reservation | `test_time_indexed_inventory.py` |
| 로봇 배정·목적함수 | `app/services/local_optimizer.py` | `test_local_optimizer.py`, `test_optimization_evidence.py` |
| 비용 기반 충전소 선택 | local optimizer, Neo4j charger cost | `test_p13_explainability_and_safety.py` |
| CHARGE 실제 로봇 명령 | `app/services/robot_adapter.py` | `test_p12_charge_execution.py` |
| CHARGE→PICK→DROP 의존성 | optimizer metadata, P14 validation | `test_p14_scope_and_dependency_validation.py` |
| 최종 경로 거리 기반 배터리 | `app/services/energy_reconciliation.py` | `test_p16_route_energy_reconciliation.py` |
| Node 충돌 WAIT | Prioritized Time A* | `test_p15_multi_robot_conflicts.py` |
| Edge swap 충돌 우회 | Prioritized Time A* | `test_p15_multi_robot_conflicts.py` |
| 충전소 단일 점유 | charger reservation | `test_p15_multi_robot_conflicts.py` |
| 긴급 작업 우선 | priority route ordering | `test_p15_multi_robot_conflicts.py` |
| 시뮬레이션 원본 Redis 미변경 | simulation namespace/snapshot | `test_p13_explainability_and_safety.py` |
| Verification 후 실행 | verification/execution precheck | `test_verification.py`, `test_pipeline.py` |
| COMPACT/FULL 응답 분리 | `app/services/response_view.py` | `test_p16_response_views.py` |
| 감사·명령·단계 이력 | audit/repositories | `test_audit.py`, `test_command_history_api.py` |
| What-if 비교 | scenario comparison service | `test_scenario_comparison.py` |
| 이벤트 기반 재계획 | event replan service | `test_event_replan.py` |

## 범위 밖 또는 외부 연동 필요

- 실제 AGV/AMR 제조사 프로토콜: Mock Gateway까지만 제공
- 창고 CAD/설계도 자동 그래프 변환: 미구현
- 실제 센서·PLC·WMS 실시간 스트림: API 계약만 제공
- 운영 수준 인증·권한·암호화·장애 복구: 별도 배포 프로젝트 필요
- 대규모 MAPF 성능 검증: 현재 deterministic local test와 개발용 지도 기준
