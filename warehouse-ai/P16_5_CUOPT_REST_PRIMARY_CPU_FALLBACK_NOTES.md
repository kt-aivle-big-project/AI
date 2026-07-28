# P16.5 cuOpt REST Primary + CPU Fallback

## 변경 사항

- NVIDIA API Catalog managed cuOpt를 `httpx` HTTPS 요청으로 직접 호출합니다.
- 기본 제출 엔드포인트: `https://optimize.api.nvidia.com/v1/nvidia/cuopt`
- 기본 상태 엔드포인트: `https://optimize.api.nvidia.com/v1/status/{request_id}`
- 요청 본문은 `action=cuOpt_OptimizedRouting`, `data=<routing payload>`, `client_version=custom`입니다.
- `Authorization: Bearer <CUOPT_API_KEY>` 헤더를 사용합니다.
- HTTP 202 응답은 자동 polling합니다.
- `cuopt-thin-client`, `cuopt-lp`, NVIDIA Python index, CUDA, 로컬 GPU와 Function ID가 필요하지 않습니다.
- REST 실패 시 기존 deterministic CPU optimizer로 자동 전환합니다.
- 기존 `CUOPT_CLIENT_SAK`는 환경변수 호환 별칭으로 유지합니다.

## 최소 설정

```env
CUOPT_API_KEY=nvapi-YOUR_KEY
```

## 확인 필드

```json
{
  "used_provider": "CUOPT",
  "transport": "HTTPS_REST",
  "fallback_used": false
}
```

## 검사

```powershell
python -m scripts.run_p16_5_final_checks
python -m pytest -q
```
