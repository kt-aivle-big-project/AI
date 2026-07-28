# AI Agent 역할 경계

- Supervisor Agent: 자연어 의도, 실행 모드, 계획 범위와 도구 선택
- Verification Agent: deterministic evidence를 종합하되 오류를 PASS로 변경 불가
- Scenario Comparison Service: 명시된 조건만 구조화하고 정량 비교는 코드로 수행
- Event Impact Analyzer: 활성 계획과 Snapshot에서 영향 ID와 재계획 범위 계산
- Optimizer/Routing/Simulation: 수치 계산과 충돌 검증의 유일한 근거

What-if 추천 ID와 이벤트 영향 범위는 LLM이 생성하지 않는다. LLM은 이미
계산된 근거를 설명하는 문장만 작성할 수 있다. PHASE 12 이후 UI, 인증,
클라우드 배포는 이 구현 범위에 포함되지 않는다.

