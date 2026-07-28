# P16.2 Inbound Execution

## 해결한 문제

기존 INBOUND 명령은 재고 이벤트만 생성하고 실제 로봇 작업을 만들지 않았다. 따라서 C상품 50 BOX 입고 요청이 `task_count=0`, `robot_count=0`, `command_count=0`인데도 성공으로 끝날 수 있었다.

## 수정 내용

- `저장 노드 2088`을 INBOUND operation의 `storage_node_id`와 `target_node_ids`에 연결
- 명시된 입고 노드는 source로, 저장 노드는 destination으로 역할 분리
- 명시된 입고 노드가 없으면 active INBOUND 노드만 후보로 사용
- 활성 로봇 접근 거리와 입고→저장 경로를 합산해 입고 source를 결정론적으로 선택
- 비활성 INBOUND/STORAGE 노드는 후보에서 제외
- INBOUND operation을 `PICK`과 `DROP` atomic task로 변환
- 동일 운송 trip의 PICK/DROP을 같은 로봇에 고정
- RobotAdapter에서 `PICKUP → MOVE → DROPOFF` 명령 생성
- 로봇 적재량보다 수량이 크면 여러 trip으로 분할
- 기존 SQL inbound order가 work와 연결된 경우 중복 task 생성 방지

## 시간창

예시 기준:

- 계획 기준: 2026-07-24 07:15 KST
- 입고 시간창: 11:00~13:00 KST
- UTC: 2026-07-24 02:00~04:00Z

입고 차량 도착 시각과 HARD_WINDOW 시작 중 늦은 시각부터 작업을 배치하고, 저장 완료는 latest_finish 이전이어야 한다.

## 자동 검사

```powershell
python -m scripts.run_p16_2_inbound_checks
```

기대 결과:

```json
{
  "all_passed": true
}
```
