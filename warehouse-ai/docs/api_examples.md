# API 예시

## 첫 계획

```http
POST /v1/planning/commands
Content-Type: application/json
```

```json
{
  "warehouse_id": 1,
  "text": "로봇 3대로 이동거리 최소화 계획을 만들어줘",
  "requested_execution_mode": "PLAN_ONLY"
}
```

응답의 `conversation_id`를 후속 요청에 전달한다.

## 후속 override

```json
{
  "warehouse_id": 1,
  "conversation_id": "conversation-id",
  "text": "이번에는 2대로 완료시간 우선으로 바꿔줘"
}
```

## 가상 시나리오

```json
{
  "warehouse_id": 1,
  "conversation_id": "conversation-id",
  "text": "R-02 고장을 가정해서 시뮬레이션해줘"
}
```

## 대화 조회

```http
GET /v1/conversations/{conversation_id}
GET /v1/conversations/{conversation_id}/commands?limit=50&offset=0
```

## Clarification 응답

```http
POST /v1/clarifications/{clarification_id}/responses
```

```json
{
  "selected_value": "SIMULATE_ONLY",
  "conversation_id": "conversation-id"
}
```
# PHASE 11 What-if

```http
POST /v1/scenario-comparisons
Content-Type: application/json

{
  "warehouse_id": 1,
  "conversation_id": "conversation-1",
  "text": "로봇 2대와 3대를 비교하고 거리 기준으로 추천해줘"
}
```

# PHASE 12 이벤트 재계획 승인

```http
POST /v1/execution/events
Content-Type: application/json

{
  "event_id": "event-robot-failed-1",
  "warehouse_id": 1,
  "robot_id": "R-02",
  "event_type": "ROBOT_FAILED",
  "execution_context": "REAL",
  "payload": {}
}
```

응답의 `replan_request_id`가 `APPROVAL_REQUIRED`이면 다음처럼 승인한다.

```http
POST /v1/event-replans/{request_id}/approve
Content-Type: application/json

{"actor_id": "operator-1", "reason": "현장 안전 확인 완료"}
```

