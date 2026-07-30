# BE-main HTTP integration

The Spring backend and AI service now share two synchronous HTTP contracts:

| Spring client call | AI endpoint | Data source |
| --- | --- | --- |
| Initial optimization | `POST /optimize` | Robots, nodes, and edges in the request |
| Runtime reoptimization | `POST /reoptimize` | Runtime robots/tasks in the request plus the backend PostgreSQL map |

Both endpoints accept and return the camelCase fields declared by the Spring
records. The AI compatibility layer converts them to the internal optimizer
and time-expanded collision-free router, then returns `requestId`, `status`,
task assignments, and robot routes in the Spring response shape.

## Shared local environment

The AI settings accept the component-style variables already used by
`BE-main/compose.yaml`:

- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
- `NEO4J_USER`, `NEO4J_PASSWORD`
- `REDIS_PASSWORD`

When explicit `DATABASE_URL`, `NEO4J_URI`, and `REDIS_URL` values are absent,
the AI service builds local URLs from those variables. The PostgreSQL fallback
selects `backend_laro` unless `POSTGRES_SCHEMA_PROFILE` is explicitly set.

The Spring setting is configurable with `FASTAPI_BASE_URL`. Defaults are:

- Local Spring process: `http://localhost:8000`
- Spring container: `http://host.docker.internal:8000`

## Local startup order

```powershell
cd "C:\Users\User\Desktop\에이블 스쿨\빅프\BE-main\BE-main"
docker compose up -d postgres redis neo4j

cd "C:\Users\User\Desktop\에이블 스쿨\빅프\VS코드\AI\warehouse-ai"
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000

cd "C:\Users\User\Desktop\에이블 스쿨\빅프\BE-main\BE-main"
.\gradlew.bat bootRun
```

The AI backend adapter and compatibility endpoints only read backend-owned
PostgreSQL map/inventory/robot tables. They do not execute seed SQL or mutate
backend task, robot, inventory, Redis, or Neo4j state.
