"""
AI Credit Radar — 일간 수집기

무료 소스만으로 AI 크레딧 스트레스를 매일 재현 가능하게 산출한다.

  [1] 실측 5Y CDS    : cds_seed.json (수기 큐레이션, 보도 기준)   ← 정본이지만 저빈도
  [2] Credit Stress  : 주가 기반 CSI 0~100 (yfinance → Nasdaq 폴백) ← 매일 갱신
  [3] 매크로 컨텍스트: ICE BofA BBB OAS (FRED, 키 불필요)
  [4] 후보 감지      : Google News RSS에서 새 CDS 보도 포착
  [5] 실측 트렌드    : 오라클·코어위브 실측 CDS 추세 + 기준선(cds_seed.json _baselines)

CSI는 CDS의 대체물이 아니라 "같은 방향을 매일 읽는 계기판"이다. 라벨을 분리해 표기한다.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone, timedelta

import pandas as pd
import requests

try:                      # yfinance가 없거나 막혀도 Nasdaq 폴백으로 돌아가게
    import yfinance as yf
except Exception:         # pragma: no cover
    yf = None

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(BASE, "docs", "data.json")
SEED_PATH = os.path.join(BASE, "cds_seed.json")
KST = timezone(timedelta(hours=9))

# ── 추적 대상 ──────────────────────────────────────────────────────────
# traded : 단일종목 CDS가 실제로 거래되는 기업
# proxy  : 비상장·무공모채라 CDS가 없고, 신용위험이 전가된 상장사로만 읽히는 기업
# cds_key: 실측 CDS를 다른 시드 시계열에서 빌려 올 때 (OpenAI → CoreWeave)
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
        "key": "openai", "name": "OpenAI", "type": "proxy", "cds_key": "coreweave",
        "basket": {"CRWV": 0.6, "ORCL": 0.4},
        "note": "단일종목 CDS 없음. 컴퓨트 파트너(CoreWeave·Oracle) 크레딧으로 전가 — 실측 CDS는 CoreWeave 값.",
    },
    {
        "key": "anthropic", "name": "Anthropic", "type": "proxy",
        "basket": {"GOOGL": 0.7, "AMZN": 0.3},
        "note": "단일종목 CDS 없음. 리스 백스톱 제공자(Alphabet·Amazon) 크레딧으로 전가.",
    },
]

TICKERS = sorted({t for e in ENTITIES for t in e["basket"]})

# 실측 CDS 트렌드 차트·경보 대상 (cds_seed.json 키와 _baselines 키가 같아야 한다)
MEASURED = [
    {"key": "oracle", "name": "오라클", "name_en": "Oracle"},
    {"key": "coreweave", "name": "코어위브", "name_en": "CoreWeave"},
]

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


def seed_rows(seed: dict, key: str) -> list[list]:
    """시드 시계열을 [date, bp, source] 정렬 목록으로. 형식이 깨진 행은 버린다."""
    rows = []
    for r in seed.get(key, []) or []:
        if not (isinstance(r, list) and len(r) == 3):
            continue
        try:
            datetime.strptime(str(r[0]), "%Y-%m-%d")
            rows.append([str(r[0]), float(r[1]), str(r[2])])
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r[0])
    return rows


# ── [3] 매크로: BBB OAS ────────────────────────────────────────────────
def fetch_bbb_oas() -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={FRED_BBB_OAS}"
    last_err: Exception | None = None
    for wait in (0, 6, 20):                      # FRED는 간헐적 503 — 짧게 재시도
        if wait:
            time.sleep(wait)
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            break
        except Exception as e:
            last_err = e
            print(f"[macro] FRED 재시도 대기 ({e})")
    else:
        raise RuntimeError(f"FRED 응답 실패: {last_err}")
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    s = pd.to_numeric(df["value"], errors="coerce").dropna()
    s.index = df.loc[s.index, "date"]
    return s.astype(float)


# ── [2] 주가 기반 CSI ──────────────────────────────────────────────────
def fetch_prices_yf(tickers: list[str]) -> pd.DataFrame:
    if yf is None:
        raise RuntimeError("yfinance 미설치")
    raw = yf.download(tickers, period="2y", interval="1d",
                      auto_adjust=True, progress=False, threads=False)
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    if not isinstance(close, pd.DataFrame):
        close = close.to_frame()
    if len(tickers) == 1:
        close.columns = tickers
    return close.dropna(how="all")


def fetch_close_nasdaq(ticker: str, days: int = 760) -> pd.Series:
    """api.nasdaq.com 일봉 종가 (Actions 러너에서 Yahoo가 막힐 때의 폴백, 하루 지연될 수 있음)."""
    today = datetime.now(KST).date()
    url = (f"https://api.nasdaq.com/api/quote/{ticker}/historical?assetclass=stocks"
           f"&fromdate={today - timedelta(days=days)}&limit=9999&todate={today}")
    r = requests.get(url, timeout=20, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ai-credit-radar/1.1",
        "Accept": "application/json"})
    r.raise_for_status()
    rows = ((r.json().get("data") or {}).get("tradesTable") or {}).get("rows") or []
    s = pd.Series({pd.to_datetime(x["date"], format="%m/%d/%Y"):
                   float(str(x["close"]).replace("$", "").replace(",", ""))
                   for x in rows if x.get("close")}, dtype=float)
    return s.sort_index()


def fetch_prices(tickers: list[str]) -> pd.DataFrame:
    """yfinance 우선, 비거나 막힌 티커만 Nasdaq으로 채운다."""
    try:
        close = fetch_prices_yf(tickers)
    except Exception as e:
        print(f"[price] yfinance 실패 — Nasdaq 폴백: {e}")
        close = pd.DataFrame()
    missing = [t for t in tickers if t not in close.columns or close[t].dropna().empty]
    for t in missing:
        try:
            s = fetch_close_nasdaq(t)
            if not s.empty:
                close = close.join(s.rename(t), how="outer") if not close.empty else s.rename(t).to_frame()
                print(f"[price] {t} Nasdaq 폴백 {len(s)}일")
        except Exception as e:
            print(f"[price] {t} Nasdaq 폴백 실패: {e}")
    return close.sort_index().dropna(how="all")


def ticker_stress(px: pd.Series, macro_pct: float | pd.Series) -> pd.Series:
    """티커별 일간 스트레스 0~100 시계열."""
    px = px.dropna()
    roll_max = px.rolling(252, min_periods=60).max()
    dd = (roll_max - px) / roll_max

    if isinstance(macro_pct, pd.Series):
        macro_pct = macro_pct.reindex(px.index, method="ffill").clip(0, 1)
    else:
        macro_pct = clip01(macro_pct)

    ret = px.pct_change()
    vol20 = ret.rolling(20).std() * (252 ** 0.5)
    vol_pct = vol20.rolling(504, min_periods=120).rank(pct=True)

    ret60 = px.pct_change(60)

    s = (
        W_DRAWDOWN * (dd / DD_FULL_SCALE).clip(0, 1)
        + W_VOL * vol_pct.clip(0, 1)
        + W_MOMENTUM * (-ret60 / MOM_FULL_SCALE).clip(0, 1)
        + W_MACRO * macro_pct
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


def load_cached_candidates(today: str) -> list[dict] | None:
    """같은 날 이미 수집한 뉴스가 있으면 재사용한다.

    Google News RSS는 호출마다 다른 세트를 돌려주므로, 재실행할 때마다
    data.json이 바뀌어 의미 없는 커밋이 쌓인다. 날짜 단위로 고정한다.
    """
    if not os.path.exists(OUT_PATH):
        return None
    try:
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            prev = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if prev.get("candidates_date") == today:
        print(f"[news] {today} 수집분 재사용 ({len(prev.get('candidates', []))}건)")
        return prev.get("candidates", [])
    return None


# ── [5] 실측 CDS 트렌드 + 기준선 ───────────────────────────────────────
def build_measured(seed: dict, today: date) -> list[dict]:
    """오라클·코어위브 실측 CDS 시계열과 기준선 판정.

    alert  : 최신 관측이 kind=alert 기준선 이상
    to_alert: 경보선까지 남은 bp (음수면 이미 돌파)
    age_days: 최신 관측이 며칠 전 보도인지 (수기 큐레이션이라 신선도를 함께 보여준다)
    """
    base = seed.get("_baselines", {}) if isinstance(seed.get("_baselines"), dict) else {}
    out = []
    for m in MEASURED:
        rows = seed_rows(seed, m["key"])
        if not rows:
            continue
        last, prev = rows[-1], (rows[-2] if len(rows) > 1 else None)
        lines = []
        for b in base.get(m["key"], []) or []:
            if isinstance(b, dict) and isinstance(b.get("bp"), (int, float)):
                lines.append({"bp": float(b["bp"]), "kind": b.get("kind", "ref"),
                              "label": str(b.get("label", "")), "source": str(b.get("source", "")),
                              "above": last[1] >= float(b["bp"])})
        lines.sort(key=lambda b: b["bp"])
        alert = next((b for b in reversed(lines) if b["kind"] == "alert"), None)
        hi = max(rows, key=lambda r: r[1])
        out.append({
            "key": m["key"], "name": m["name"], "name_en": m["name_en"],
            "series": rows,                                   # [date, bp, source]
            "last": last[1], "last_date": last[0], "source": last[2],
            "prev": prev[1] if prev else None, "prev_date": prev[0] if prev else None,
            "chg": round(last[1] - prev[1], 1) if prev else None,
            "high": hi[1], "high_date": hi[0],
            "baselines": lines,
            "alert_bp": alert["bp"] if alert else None,
            "alert": bool(alert and last[1] >= alert["bp"]),
            "to_alert": round(alert["bp"] - last[1], 1) if alert else None,
            "age_days": (today - datetime.strptime(last[0], "%Y-%m-%d").date()).days,
        })
    return out


def measured_signature(measured: list[dict]) -> str:
    """관측치·기준선이 바뀌었는지 판별용 지문 (age_days처럼 매일 바뀌는 값은 제외)."""
    core = [{"k": m["key"], "s": m["series"], "b": [(b["bp"], b["kind"]) for b in m["baselines"]]}
            for m in measured]
    return hashlib.sha1(json.dumps(core, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def previous_output() -> dict:
    try:
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ── 메인 ───────────────────────────────────────────────────────────────
def build() -> dict:
    with open(SEED_PATH, "r", encoding="utf-8") as f:
        seed = json.load(f)

    # 매크로
    macro_pct, macro_last, macro_series = None, None, []
    try:
        oas = fetch_bbb_oas()
        macro_history = oas.rolling(504, min_periods=120).rank(pct=True)
        macro_pct = float(macro_history.iloc[-1])
        macro_last = round(float(oas.iloc[-1]) * 100, 1)  # % → bp
        macro_series = [[d.strftime("%Y-%m-%d"), round(v * 100, 1)]
                        for d, v in oas.tail(HISTORY_DAYS).items()]
        print(f"[macro] BBB OAS {macro_last}bp (2y 백분위 {macro_pct:.0%})")
    except Exception as e:
        raise RuntimeError("BBB OAS 수집 실패 — 기존 산출물 보존") from e

    # 가격 → 티커 스트레스
    px = fetch_prices(TICKERS)
    asof = px.index[-1].strftime("%Y-%m-%d")
    stress = {t: ticker_stress(px[t], macro_history) for t in TICKERS if t in px.columns}

    entities = []
    for e in ENTITIES:
        parts = {t: w for t, w in e["basket"].items() if t in stress and len(stress[t]) > 2}
        if set(parts) != set(e["basket"]):
            raise RuntimeError(f"{e['name']} 구성 종목 누락 — 기존 산출물 보존")
        wsum = sum(parts.values())
        combined = sum(stress[t] * (w / wsum) for t, w in parts.items()).dropna()
        hist = [[d.strftime("%Y-%m-%d"), round(float(v), 1)]
                for d, v in combined.tail(HISTORY_DAYS).items()]

        cds_raw = seed_rows(seed, e.get("cds_key", e["key"]))
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
            "asof": combined.index[-1].strftime("%Y-%m-%d"),
            "csi": round(float(combined.iloc[-1]), 1),
            "csi_prev": round(float(combined.iloc[-2]), 1),
            "csi_5d": round(float(combined.iloc[-6]), 1) if len(combined) > 6 else None,
            "csi_hist": hist,
            "cds": cds,
        })

    today = datetime.now(KST).strftime("%Y-%m-%d")
    if not entities:
        raise RuntimeError("CSI 산출 불가 — 기존 산출물 보존")
    asof = min(e["asof"] for e in entities)
    quality = []
    if (datetime.now(KST).date() - datetime.strptime(asof, "%Y-%m-%d").date()).days >= 4:
        quality.append("가격 데이터 4일 이상 경과 — 최신 시장 판단 보류")
    if len(entities) != len(ENTITIES) or len({e["asof"] for e in entities}) != 1:
        quality.append("추적 대상 누락 또는 관측일 불일치 — 통합 판단 보류")
    candidates = load_cached_candidates(today)
    if candidates is None:
        candidates = fetch_news_candidates([e["name"] for e in ENTITIES] + ["CoreWeave"])

    measured = build_measured(seed, datetime.now(KST).date())
    sig = measured_signature(measured)
    prev = previous_output()
    # 시드(관측치·기준선)가 바뀐 날을 기록 — 브리프는 이 날 트렌드 차트를 첨부한다
    measured_updated = prev.get("measured_updated") if prev.get("measured_sig") == sig else today
    measured_updated = measured_updated or today

    return {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "asof": asof,
        "quality_warnings": quality,
        "method_version": "2-observation-aligned-macro",
        "measured": measured,
        "measured_sig": sig,
        "measured_updated": measured_updated,
        "macro": {"asof": oas.index[-1].strftime("%Y-%m-%d"), "bbb_oas_bp": macro_last, "percentile_2y": round(macro_pct, 3),
                  "series": macro_series},
        "entities": entities,
        "candidates_date": today,
        "candidates": candidates,
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
    for m in data["measured"]:
        state = "돌파" if m["alert"] else "%+.1fbp 남음" % (m["to_alert"] or 0)
        print(f"  [실측] {m['name_en']:<10} {m['last']:>7.1f}bp @{m['last_date']}  "
              f"경보선 {m['alert_bp']} ({state})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

