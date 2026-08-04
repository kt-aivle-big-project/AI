# Spring BE Compatibility Mode v2

기존 팀 Spring `BE-main` 소스를 수정하지 않고 다음 계약을 제공합니다.

```text
POST /optimize
POST /reoptimize
```

v2에서는 Spring과 LARO가 같은 PostgreSQL·Redis·Neo4j 서버를 사용하며, 정적 Graph는 Spring public 테이블을 우선 읽습니다. 요청 Graph가 다를 때만 `laro_contract` Schema에 Fallback을 저장하고 Redis는 기본적으로 Graph Metadata만 Cache합니다.

빠른 시작:

```powershell
Copy-Item .env.docker.example .env.docker
.\scripts\start_be_compat_docker.ps1 -ResetData
python .\scripts\smoke_be_compat_api.py --repeat 5
```

오프라인 반복 검증:

```powershell
python .\scripts\validate_be_compat_v2_release.py --pytest-repeats 3
```

Swagger:

```text
http://localhost:8000/docs
```

문서:

- [입력·출력 API](docs/BE_MAIN_COMPAT_API.md)
- [PostgreSQL·Redis 공유 계약](docs/BE_SHARED_DB_CONTRACT_V2.md)
