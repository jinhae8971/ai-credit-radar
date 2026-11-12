# AI Credit Radar

엔비디아 · 오라클 · 코어위브 · OpenAI · 앤트로픽의 **신용위험(CDS 스프레드)** 을 매일 추적해
GitHub Pages 대시보드로 보여주고, 전략봇(@spytltgld_strategy_bot) 채널로 텔레그램 브리프를 보냅니다.

- 대시보드: `https://jinhae8971.github.io/ai-credit-radar`
- 수집: 매일 **06:07 KST** + 07:37 KST 2차(멱등) — `.github/workflows/radar.yml`
- 발송: korea-etf-calmar **Brief Hub**가 `docs/hub/latest.json`을 읽어 화~토 아침에 별도 메시지로 발송
  (실측 관측치·기준선이 바뀐 날, 경보선 돌파 시, 매주 화요일에는 트렌드 차트 PNG를 함께 첨부)

## 왜 두 종류의 숫자가 있나

5년 CDS 호가는 무료 실시간 피드가 없습니다(ICE·LSEG·Bloomberg 유료). 그래서 계층을 나눴습니다.

| 계층 | 소스 | 갱신 | 성격 |
|---|---|---|---|
| 실측 5Y CDS | `cds_seed.json` (보도 기준 큐레이션) | 수기 | 정본, 저빈도 |
| 크레딧 스트레스 지수(CSI) | yfinance 주가 + FRED BBB OAS | 매일 자동 | 대리지표, 고빈도 |
| 신규 수치 후보 | Google News RSS (7일) | 매일 자동 | 시드 갱신 트리거 |
| 실측 트렌드·기준선 | `cds_seed.json` 오라클·코어위브 + `_baselines` | 시드 push 즉시 | 경보선 돌파 감시 |

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
| OpenAI | 대리지표 | CRWV 60% + ORCL 40% | 단일종목 CDS 없음 → 컴퓨트 파트너 크레딧으로 전가 (실측 CDS = 코어위브) |
| Anthropic | 대리지표 | GOOGL 70% + AMZN 30% | 단일종목 CDS 없음 → 리스 백스톱 제공자 크레딧으로 전가 |

## 구성

```
collect.py                 수집 + CSI + 실측 트렌드(measured) 산출 → docs/data.json
chart.py                   오라클·코어위브 실측 CDS 트렌드 PNG → docs/hub/cds_measured.png
notify.py                  브리프 본문 → docs/hub/latest.json (Brief Hub payload)
cds_seed.json              실측 CDS 관측치 + 기준선(_baselines) (여기만 고치면 전체 반영)
docs/index.html            GitHub Pages 대시보드 (외부 JS 의존성 0)
tests/test_core.py         워크플로우 게이트
.github/workflows/radar.yml
setup_github.ps1           Windows 최초 배포 (구 방식)
```

## 배포

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_github.ps1
```

배포 후 **Settings → Pages → Source: Deploy from a branch → main / `/docs`** 로 지정하세요.

### 발송 경로

이 레포에는 봇 토큰이 없습니다. 발송은 토큰을 가진 korea-etf-calmar의 Brief Hub(`brief-hub.yml`)가
실행 때마다 이 레포의 `docs/hub/latest.json`·`docs/hub/cds_measured.png`를 Contents API로 받아 보냅니다.

Variables(선택): `PAGES_URL` — 대시보드 주소. 미설정 시 기본값 사용.

## 운영

- 새 CDS 보도를 보면 `cds_seed.json`에 `["2026-11-20", 310.0, "출처"]` 한 줄 추가 후 push → 워크플로우가 바로 돌아 대시보드·차트·브리프에 반영.
- 기준선은 `cds_seed.json`의 `_baselines`에서 bp만 바꾸면 됩니다. `kind: "alert"`는 경보선(돌파 시 브리프에 🔴 표시), `kind: "ref"`는 참고선.
- 현재 기준선: 오라클 198bp(2008 금융위기 최고치)·300bp(경보선) / 코어위브 1,000bp(디스트레스)·1,289bp(경보선, 11/5 고점).
- 가격 데이터가 4일 이상 밀리면 브리프 상단에 경고가 붙습니다. 수집이 멈추면 Brief Hub가 "오늘 데이터가 아직 없습니다" 지연 알림을 보냅니다.
- 주가는 yfinance 우선, 막히면 api.nasdaq.com 일봉으로 자동 폴백합니다.

## 한계

- 실측 CDS는 보도 시차가 있고 소스마다 1~10bp 차이가 납니다(예: 7/27 오라클 203 vs 215).
- CSI는 주가 파생이라 크레딧 고유 정보(등급 조치·발행 계획)를 즉시 반영하지 못합니다.
- 투자 판단의 책임은 이용자에게 있습니다.


## 데이터 품질 보강 (2026-09-09)

CSI의 매크로 항은 각 가격 관측일 이전의 OAS 백분위를 사용합니다. 최신 매크로 값으로 과거 전체를 다시 이동시키지 않습니다. 이는 관측일 정합성을 개선하지만 FRED 빈티지/공표 시각까지 재현하는 백테스트는 아닙니다. 계산 방식 변경 후 첫 갱신은 이전 버전 점수와 직접 비교하지 마세요. 매크로 수집 실패나 구성 종목 누락은 기존 파일을 보존하고 실패 처리합니다. 가격가 4일 이상 오래되면 화면과 브리프에서 최신 판단을 보류합니다.


## 실측 CDS 트렌드 (2026-11-13)

오라클·코어위브 실측 5Y CDS를 추세선과 기준선으로 봅니다. 대시보드 상단 섹션과 텔레그램 첨부 PNG가 같은 데이터를 씁니다.

- 점 = 보도 기준 관측치. 관측 간격이 30일을 넘는 구간은 옅은 점선으로 그려 보간이 아님을 표시합니다.
- 세로 점선 = 오늘. 마지막 관측과의 거리가 곧 데이터 신선도입니다(수기 큐레이션이라 며칠 묵을 수 있음).
- 색 표시는 경보선 돌파에만 씁니다. CSI 과열은 굵은 글씨로만 표시합니다.
