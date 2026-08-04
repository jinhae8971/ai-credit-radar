# AI Credit Radar

엔비디아 · 오라클 · OpenAI · 앤트로픽의 **신용위험(CDS 스프레드)** 을 매일 추적해
GitHub Pages 대시보드로 보여주고, 전략비서 채널로 텔레그램 브리프를 보냅니다.

- 대시보드: `https://jinhae8971.github.io/ai-credit-radar`
- 발송: 매일 **07:30 KST** (UTC cron `30 22 * * *`)

## 왜 두 종류의 숫자가 있나

5년 CDS 호가는 무료 실시간 피드가 없습니다(ICE·LSEG·Bloomberg 유료). 그래서 계층을 나눴습니다.

| 계층 | 소스 | 갱신 | 성격 |
|---|---|---|---|
| 실측 5Y CDS | `cds_seed.json` (보도 기준 큐레이션) | 수기 | 정본, 저빈도 |
| 크레딧 스트레스 지수(CSI) | yfinance 주가 + FRED BBB OAS | 매일 자동 | 대리지표, 고빈도 |
| 신규 수치 후보 | Google News RSS (7일) | 매일 자동 | 시드 갱신 트리거 |

CSI는 CDS 호가가 **아닙니다**. 방향을 매일 읽기 위한 계기판입니다.

```
CSI = 100 × ( 0.35×낙폭(252d)/0.35 + 0.30×20일변동성 백분위
            + 0.20×(-60일수익률)/0.25 + 0.15×BBB OAS 백분위 )
```

## 추적 대상

| 축 | 유형 | 구성 | 근거 |
|---|---|---|---|
| Oracle | CDS 거래 | ORCL 100% | IG 지수 최대 비금융 발행사, AI 부채 공포의 대리지표 |
| Nvidia | CDS 거래 | NVDA 100% | 벤더 파이낸싱·보증이 스프레드를 움직임 |
| OpenAI | 대리지표 | CRWV 60% + ORCL 40% | 단일종목 CDS 없음 → 컴퓨트 파트너 크레딧으로 전가 |
| Anthropic | 대리지표 | GOOGL 70% + AMZN 30% | 단일종목 CDS 없음 → 리스 백스톱 제공자 크레딧으로 전가 |

## 구성

```
collect.py                 수집 + CSI 산출 → docs/data.json
notify.py                  텔레그램 전략비서 브리프
cds_seed.json              실측 CDS 관측치 (여기만 고치면 전체 반영)
docs/index.html            GitHub Pages 대시보드 (외부 JS 의존성 0)
tests/test_core.py         워크플로우 게이트
.github/workflows/daily.yml
setup_github.ps1           Windows 최초 배포
```

## 배포

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_github.ps1
```

배포 후 **Settings → Pages → Source: Deploy from a branch → main / `/docs`** 로 지정하세요.

### 등록할 Secret

| 이름 | 발급처 |
|---|---|
| `TELEGRAM_TOKEN` | @BotFather |
| `TELEGRAM_CHAT_ID` | 전략비서 채널 id |

Variables(선택): `PAGES_URL` — 대시보드 주소. 미설정 시 기본값 사용.

## 운영

- 새 CDS 보도를 보면 `cds_seed.json`에 `["2026-08-11", 221.0, "출처"]` 한 줄 추가 후 push.
- 데이터만 갱신하고 발송은 건너뛰려면 `workflow_dispatch → skip_notify: true`.
- 가격 데이터가 4일 이상 밀리면 브리프 상단에 경고가 붙습니다.
- 실패 시 텔레그램으로 실행 링크가 옵니다(curl 기반 — pip 실패도 알림됨).

## 한계

- 실측 CDS는 보도 시차가 있고 소스마다 1~10bp 차이가 납니다(예: 7/27 오라클 203 vs 215).
- CSI는 주가 파생이라 크레딧 고유 정보(등급 조치·발행 계획)를 즉시 반영하지 못합니다.
- 투자 판단의 책임은 이용자에게 있습니다.
