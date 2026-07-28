# Clarification

해석 또는 Supervisor 판단에 필수 정보가 부족하면 그래프는 읽기 전용 Snapshot까지만 만든 뒤 `CLARIFICATION_REQUIRED`로 종료한다. 이 상태에서는 Optimizer, Routing, Simulation, 활성 계획 변경, Robot Gateway 호출이 발생하지 않는다.

응답에는 `clarification_id`, 질문, 누락/모호 필드, 선택지가 포함된다. 로봇·작업 선택지는 Snapshot에서 실제 확인된 값만 사용한다.

후속 응답:

```http
POST /v1/clarifications/{clarification_id}/responses
```

```json
{
  "selected_value": "MINIMIZE_TARDINESS",
  "text": "마감 지연을 줄여줘",
  "conversation_id": "..."
}
```

후속 명령은 새 `command_id`를 사용하고 원명령을 `parent_command_id`로 연결한다. 이미 해결된 요청의 중복 응답은 재실행하지 않고 `ALREADY_RESOLVED`를 반환한다. 다른 conversation의 응답은 409로 차단한다.

Stage log 이름은 `CLARIFICATION_REQUIRED`, `CLARIFICATION_RESPONSE_RECEIVED`, `CLARIFICATION_RESOLVED`, `CLARIFICATION_EXPIRED`다. EXPIRED는 저장 상태만 준비되어 있으며 자동 만료 스케줄러는 구현하지 않았다.

