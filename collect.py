"""
AI Credit Radar — 일간 수집기

무료 소스만으로 AI 크레딧 스트레스를 매일 재현 가능하게 산출한다.

  [1] 실측 5Y CDS    : cds_seed.json (수기 큐레이션, 보도 기준)   ← 정본이지만 저빈도
  [2] Credit Stress  : 주가 기반 CSI 0~100 (yfinance)             ← 매일 갱신
  [3] 매크로 컨텍스트: ICE BofA BBB OAS (FRED, 키 불필요)
  [4] 후보 감지      : Google News RSS에서 새 CDS 보도 포착

CSI는 CDS의 대체물이 아니라 "같은 방향을 매일 읽는 계기판"이다. 라벨을 분리해 표기한다.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
import yfinance as yf

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(BASE, "docs", "data.json")
SEED_PATH = os.path.join(BASE, "cds_seed.json")
KST = timezone(timedelta(hours=9))

# ── 추적 대상 ──────────────────────────────────────────────────────────
# traded : 단일종목 CDS가 실제로 거래되는 기업
# proxy  : 비상장·무공모채라 CDS가 없고, 신용위험이 전가된 상장사로만 읽히는 기업
ENTITIES = [
    {
        "key": "oracle", "name": "Oracle", "type": "traded",
        "basket": {"ORCL": 1.0},
        "note": "AI 부채 공포의 대리지표. IG 지수 최대 비금융 발행사.",
    },
    {
        "key": "nvidia", "name": "Nvidia", "type": "traded",
        "basket": {"NVDA": 1.0},
        "note": "대차대조표가 아닌 벤더 파이낸싱·보증이 스프레드를 움직인다.",
    },
    {
        "key": "openai", "name": "OpenAI", "type": "proxy",
        "basket": {"CRWV": 0.6, "ORCL": 0.4},
        "note": "단일종목 CDS 없음. 컴퓨트 파트너(CoreWeave·Oracle) 크레딧으로 전가.",
    },
    {
        "key": "anthropic", "name": "Anthropic", "type": "proxy",
        "basket": {"GOOGL": 0.7, "AMZN": 0.3},
        "note": "단일종목 CDS 없음. 리스 백스톱 제공자(Alphabet·Amazon) 크레딧으로 전가.",
    },
]

TICKERS = sorted({t for e in ENTITIES for t in e["basket"]})

# CSI 가중치 — 합 1.0
W_DRAWDOWN, W_VOL, W_MOMENTUM, W_MACRO = 0.35, 0.30, 0.20, 0.15
DD_FULL_SCALE = 0.35   # -35% 낙폭에서 해당 축 만점
MOM_FULL_SCALE = 0.25  # 60일 -25%에서 만점

FRED_BBB_OAS = "BAMLC0A4CBBB"
HISTORY_DAYS = 260


# ── 유틸 ───────────────────────────────────────────────────────────────
def clip01(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    return max(0.0, min(1.0, float(x)))


def save_json(path: str, obj) -> bool:
    """원자적 저장. 내용이 실질적으로 같으면 쓰지 않고 False를 돌려준다."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
            old.pop("generated_at", None)
            cmp_new = json.loads(new)
            cmp_new.pop("generated_at", None)
            if json.dumps(old, ensure_ascii=False, sort_keys=True) == \
               json.dumps(cmp_new, ensure_ascii=False, sort_keys=True):
                return False
        except (json.JSONDecodeError, OSError):
            pass
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return True


# ── [3] 매크로: BBB OAS ────────────────────────────────────────────────
def fetch_bbb_oas() -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={FRED_BBB_OAS}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    s = pd.to_numeric(df["value"], errors="coerce").dropna()
    s.index = df.loc[s.index, "date"]
    return s.astype(float)


# ── [2] 주가 기반 CSI ──────────────────────────────────────────────────
def fetch_prices(tickers: list[str]) -> pd.DataFrame:
    raw = yf.download(tickers, period="2y", interval="1d",
                      auto_adjust=True, progress=False, threads=False)
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    if not isinstance(close, pd.DataFrame):
        close = close.to_frame()
    if len(tickers) == 1:
        close.columns = tickers
    return close.dropna(how="all")


def ticker_stress(px: pd.Series, macro_pct: float) -> pd.Series:
    """티커별 일간 스트레스 0~100 시계열."""
    px = px.dropna()
    roll_max = px.rolling(252, min_periods=60).max()
    dd = (roll_max - px) / roll_max

    ret = px.pct_change()
    vol20 = ret.rolling(20).std() * (252 ** 0.5)
    vol_pct = vol20.rolling(504, min_periods=120).rank(pct=True)

    ret60 = px.pct_change(60)

    s = (
        W_DRAWDOWN * (dd / DD_FULL_SCALE).clip(0, 1)
        + W_VOL * vol_pct.clip(0, 1)
        + W_MOMENTUM * (-ret60 / MOM_FULL_SCALE).clip(0, 1)
        + W_MACRO * clip01(macro_pct)
    ) * 100
    return s.dropna()


# ── [4] 새 CDS 보도 후보 감지 ─────────────────────────────────────────
def fetch_news_candidates(names: list[str], limit: int = 6) -> list[dict]:
    out: list[dict] = []
    for name in names:
        q = urllib.parse.quote(f'"credit default swap" {name}')
        url = f"https://news.google.com/rss/search?q={q}+when:7d&hl=en-US&gl=US&ceid=US:en"
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "ai-credit-radar/1.0"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                bp = re.search(r"(\d{2,4}(?:\.\d+)?)\s*basis points", title, re.I)
                out.append({
                    "entity": name,
                    "title": title[:180],
                    "link": (item.findtext("link") or "").strip(),
                    "published": (item.findtext("pubDate") or "").strip(),
                    "bp_hint": float(bp.group(1)) if bp else None,
                })
                if len([o for o in out if o["entity"] == name]) >= limit:
                    break
        except Exception as e:  # 뉴스는 부가 기능 — 실패해도 파이프라인은 계속
            print(f"[news] {name} 수집 실패: {e}")
    # RSS 정렬 흔들림이 매 실행 커밋을 만들지 않도록 결정론적으로 고정
    seen, uniq = set(), []
    for o in out:
        k = o["link"] or o["title"]
        if k not in seen:
            seen.add(k)
            uniq.append(o)
    uniq.sort(key=lambda o: (o["entity"], o["title"]))
    return uniq


# ── 메인 ───────────────────────────────────────────────────────────────
def build() -> dict:
    with open(SEED_PATH, "r", encoding="utf-8") as f:
        seed = json.load(f)

    # 매크로
    macro_pct, macro_last, macro_series = 0.0, None, []
    try:
        oas = fetch_bbb_oas()
        macro_pct = float(oas.tail(504).rank(pct=True).iloc[-1])
        macro_last = round(float(oas.iloc[-1]) * 100, 1)  # % → bp
        macro_series = [[d.strftime("%Y-%m-%d"), round(v * 100, 1)]
                        for d, v in oas.tail(HISTORY_DAYS).items()]
        print(f"[macro] BBB OAS {macro_last}bp (2y 백분위 {macro_pct:.0%})")
    except Exception as e:
        print(f"[macro] 실패 — 매크로 축 0 처리: {e}")

    # 가격 → 티커 스트레스
    px = fetch_prices(TICKERS)
    asof = px.index[-1].strftime("%Y-%m-%d")
    stress = {t: ticker_stress(px[t], macro_pct) for t in TICKERS if t in px.columns}

    entities = []
    for e in ENTITIES:
        parts = {t: w for t, w in e["basket"].items() if t in stress and len(stress[t]) > 2}
        if not parts:
            print(f"[warn] {e['name']} 산출 불가 — 스킵")
            continue
        wsum = sum(parts.values())
        combined = sum(stress[t] * (w / wsum) for t, w in parts.items()).dropna()
        hist = [[d.strftime("%Y-%m-%d"), round(float(v), 1)]
                for d, v in combined.tail(HISTORY_DAYS).items()]

        cds_raw = [r for r in seed.get(e["key"], []) if isinstance(r, list) and len(r) == 3]
        cds_raw.sort(key=lambda r: r[0])
        cds = {
            "series": [[d, float(v)] for d, v, _ in cds_raw],
            "last": float(cds_raw[-1][1]) if cds_raw else None,
            "last_date": cds_raw[-1][0] if cds_raw else None,
            "source": cds_raw[-1][2] if cds_raw else None,
            "ytd_low": min(v for _, v, _ in cds_raw) if cds_raw else None,
            "ytd_high": max(v for _, v, _ in cds_raw) if cds_raw else None,
        }

        entities.append({
            "key": e["key"], "name": e["name"], "type": e["type"], "note": e["note"],
            "basket": e["basket"],
            "csi": round(float(combined.iloc[-1]), 1),
            "csi_prev": round(float(combined.iloc[-2]), 1),
            "csi_5d": round(float(combined.iloc[-6]), 1) if len(combined) > 6 else None,
            "csi_hist": hist,
            "cds": cds,
        })

    return {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "asof": asof,
        "macro": {"bbb_oas_bp": macro_last, "percentile_2y": round(macro_pct, 3),
                  "series": macro_series},
        "entities": entities,
        "candidates": fetch_news_candidates([e["name"] for e in ENTITIES]),
        "method": {
            "csi": "0.35×낙폭(252d) + 0.30×20일변동성 백분위 + 0.20×60일 모멘텀 + 0.15×BBB OAS 백분위",
            "disclaimer": "CSI는 CDS 호가가 아니라 주가·매크로 기반 대리지표다. 실측 CDS는 보도 기준 큐레이션 값이다.",
        },
    }


def main() -> int:
    data = build()
    changed = save_json(OUT_PATH, data)
    print(f"[done] asof={data['asof']} entities={len(data['entities'])} "
          f"changed={changed}")
    for e in data["entities"]:
        print(f"  {e['name']:<10} CSI {e['csi']:>5.1f} (전일 {e['csi_prev']:>5.1f})  "
              f"CDS {e['cds']['last']}bp @{e['cds']['last_date']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
