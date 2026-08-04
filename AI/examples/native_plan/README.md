# Native plan examples

- `plan_request_structured.json`: OpenAI key 없이 `force_rule + ortools`로 통신·DB·MAPF를 확인하는 요청
- `plan_request_natural_language.json`: `DEFAULT_PLANNING_MODE=llm_router`와 `OPENAI_API_KEY`를 설정한 뒤 확인하는 요청

Endpoint:

```text
POST /api/v1/warehouses/WH-001/missions/plan
```
