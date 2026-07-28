# P16.5.8 Opportunity Charging & Charger-Area Return

## 목적

P16.5.7은 장기 대기를 전용 PARKING/STAGING/HOLDING 노드로 제한했습니다. P16.5.8은 그 위에 장기 공백을 배터리와 충전소 운영 관점에서 계획합니다.

핵심 원칙:

```text
길을 막지 않는 것 = 하드 제약
장기 공백에 충전소·충전 대기 구역·전용 주차장 중 어디로 갈지 = 최적화/운영 정책
충전소까지 MOVE와 실제 CHARGE = 정식 작업
```

## 주요 변경

- 원래 업무 작업의 로봇·순서·시작 시각을 보존합니다.
- 기존 idle gap 안에만 `CHARGE` 작업을 삽입하므로 원래 작업을 늦추지 않습니다.
- active이며 도달 가능한 충전소만 후보로 사용합니다.
- 충전소까지 거리·시간·이동 에너지와 다음 작업까지 복귀 비용을 계산합니다.
- 기본 기회 충전 목표는 95%, 최소 충전 이득은 2%입니다.
- 동일 충전 슬롯의 시간 중복을 계획 단계에서 금지합니다.
- 충전 완료 후 `CHARGER` 슬롯에서 계속 기다리지 않고 연결된 `CHARGER_WAITING_AREA`로 빠집니다.
- 배터리가 충분해 충전하지 않아도 긴 공백이면 충전소 주변 대기 구역 또는 다른 whitelist idle node를 사용합니다.
- 일반 통로·교차로·서비스 노드에서의 장기 대기는 계속 금지합니다.
- 충돌 회피를 위한 짧은 1-step WAIT는 원인과 차단 자원을 기록한 경우에만 허용합니다.

## 창고 2 데모 노드

| 대기 노드 | 유형 | 연결 충전소 |
|---:|---|---:|
| 2160 | CHARGER_WAITING_AREA | 2150 |
| 2161 | CHARGER_WAITING_AREA | 2151 |
| 2162 | CHARGER_WAITING_AREA | 2152 |

## 설치 후 데이터 반영

```powershell
python -m scripts.seed_p16_5_8_charger_waiting_nodes --warehouse-id 2
```

이 스크립트는 기존 2160~2162를 충전소별 대기 구역으로 upsert하고 연결 간선을 생성합니다.

## Swagger 예제

```text
examples/p16_5_8_opportunity_charging_request.json
```

## 정상 확인값

```text
response_schema_version = p16.5.8
status = SIMULATION_SUCCESS
conflict_count = 0
idle_energy_planning.enabled = true
opportunity_charging.policy = LONG_IDLE_CHARGER_AREA_FIRST
charge_tasks의 action = CHARGE
충전 후 장기 WAIT node = 2160 / 2161 / 2162
CHARGER 슬롯에서 충전 종료 후 장기 WAIT 없음
```

## 설정

```dotenv
OPPORTUNITY_CHARGING_ENABLED=true
OPPORTUNITY_CHARGE_TARGET_BATTERY=95
OPPORTUNITY_CHARGE_MIN_IDLE_MINUTES=15
OPPORTUNITY_CHARGE_MIN_GAIN_PERCENT=2
```

## 검사

```powershell
python -m scripts.run_p16_5_8_final_checks
pytest -q tests/test_p16_5_8_opportunity_charging.py
```
