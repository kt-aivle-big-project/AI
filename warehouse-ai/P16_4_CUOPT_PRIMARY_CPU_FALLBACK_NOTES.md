> **대체됨:** 이 방식은 P16.5 REST 직접 호출 방식으로 대체되었습니다. P16.5에서는 thin client와 Function ID가 필요하지 않습니다.

# P16.4 cuOpt Primary + CPU Fallback

## 사용자 설정

가장 간단한 설정은 `.env`에 아래 한 줄을 추가하는 것입니다.

```env
CUOPT_API_KEY=...
```

`CUOPT_CLIENT_SAK`도 동일하게 지원합니다. 이전 P16.3.4 `.env`에 `OPTIMIZER_BACKEND=local`이 남아 있어도 `CUOPT_AUTO_ENABLE=true` 기본값으로 cuOpt가 우선 실행됩니다.

`CUOPT_FUNCTION_ID`는 선택값입니다. cuOpt 함수가 여러 개인 NVIDIA 계정에서만 필요합니다.

## 실행 정책

- API key/SAK 있음: managed cuOpt 우선
- `CUOPT_AUTO_ENABLE=false` + `OPTIMIZER_BACKEND=local`: CPU 강제
- SAK 없음: CPU optimizer
- cuOpt 패키지/인증/네트워크/타임아웃/응답/해 오류: CPU fallback
- `CUOPT_FALLBACK_TO_LOCAL=false`: cuOpt 실패를 그대로 반환
- 기존 `CUOPT_URL` custom HTTP adapter도 유지

## 계약

cuOpt는 로봇별 작업 배정과 작업 방문 순서를 계산합니다. 기존 `LocalOptimizer`는 cuOpt 배정을 고정한 상태에서 창고 전용 배터리, 충전, 시간 창, 세부 source/target 및 downstream 계약을 정규화합니다. 이후 routing/simulation/verification은 변경하지 않습니다.

## 응답 확인

`optimizer_execution.used_provider`가 `CUOPT`, `CUOPT_HTTP`, 또는 `CPU`로 기록됩니다.
