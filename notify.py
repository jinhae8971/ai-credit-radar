"""
AI Credit Radar — 전략봇 일간 브리프

docs/data.json 을 읽어 단일 메시지로 압축한다. 발송은 korea-etf-calmar 의 Brief Hub
(@spytltgld_strategy_bot)가 맡고, 여기서는 허브가 읽을 payload(docs/hub/latest.json)를 만든다.
상세(전체 시계열·출처·뉴스 후보·트렌드 차트)는 GitHub Pages 대시보드로 분리한다.

사용:
  python notify.py --hub-out docs/hub/latest.json   # 허브용 payload 작성 (발송 없음)
  python notify.py                                  # TELEGRAM_TOKEN 이 있으면 직접 발송, 없으면 출력만
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE, "docs", "data.json")
KST = timezone(timedelta(hours=9))
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

PAGES_URL = os.environ.get("PAGES_URL") or "https://jinhae8971.github.io/ai-credit-radar"

# 허브(korea-etf-calmar) 기준 경로 — brief-hub.yml 의 fetch 단계가 이 위치로 내려받는다
HUB_PHOTO_PATH = "ai_credit/data/cds_measured.png"
CHART_WEEKDAY = 1          # 화요일(미국 주간 첫 마감 반영일)에는 변화가 없어도 차트를 첨부

# CSI 밴드 — 라벨만 쓰고 색은 쓰지 않는다 (색 표시는 실측 CDS 경보선 돌파에만)
BANDS = [(75, "과열"), (60, "확대"), (40, "관망"), (0, "안정")]
ALERT = "🔴"


def load_config() -> dict:
    cfg = {
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }
    path = os.path.join(BASE, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config] 읽기 실패 - 환경변수만 사용: {e}")
            data = {}
        for k, v in data.items():
            key = k.lower()
            if key in cfg and not cfg[key]:
                cfg[key] = v
    return cfg


def band(csi: float) -> tuple[bool, str]:
    """(과열 여부, 라벨)."""
    for lo, label in BANDS:
        if csi >= lo:
            return lo == 75, label
    return False, "안정"


def arrow(cur: float, prev: float | None) -> str:
    if prev is None:
        return "  ―"
    d = cur - prev
    if abs(d) < 0.5:
        return " ―"
    fmt = ".1f" if abs(d) < 1 else ".0f"
    return f"{'▲' if d > 0 else '▼'}{abs(d):{fmt}}"


def mmdd(d: str | None) -> str:
    return f"{d[5:7]}/{d[8:10]}" if d and len(d) >= 10 else "?"


def bp(v: float) -> str:
    return f"{v:,.0f}bp"


def measured_line(m: dict) -> str:
    """오라클 298bp 11/06 (+3) · 경보선 300까지 2bp"""
    chg = f" ({m['chg']:+,.0f})" if m.get("chg") is not None else ""
    s = f"{m['name']} <b>{bp(m['last'])}</b> {mmdd(m['last_date'])}{chg}"
    if m.get("alert_bp") is not None:
        if m.get("alert"):
            s = f"{ALERT} {s} · <b>경보선 {bp(m['alert_bp'])} 돌파</b>"
        else:
            s += f" · 경보선 {m['alert_bp']:,.0f}까지 {m['to_alert']:,.0f}bp"
    return s


def build_message(d: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(KST)
    head = f"<b>AI 크레딧 레이더</b> · {now:%m/%d}({WEEKDAY_KR[now.weekday()]})"

    stale = ""
    try:
        gap = (now.date() - datetime.strptime(d["asof"], "%Y-%m-%d").date()).days
        if gap >= 4:
            stale = f"\n가격 데이터 {gap}일 경과 — 휴장/수집 지연 확인 필요"
    except (ValueError, KeyError):
        pass

    lines = [head, f"<i>기준 {d['asof']} 종가</i>{stale}"]

    # 1) 실측 CDS — 오라클·코어위브는 경보선과 함께, 나머지는 한 줄로
    measured = d.get("measured") or []
    others = []
    for e in d.get("entities", []):
        c = e.get("cds") or {}
        if c.get("last") is None or e["key"] in ("oracle", "openai"):   # 오라클·코어위브(OpenAI 대리)는 위에서 표시
            continue
        label = "알파벳(앤트로픽 대리)" if e["key"] == "anthropic" else ("엔비디아" if e["key"] == "nvidia" else e["name"])
        others.append(f"{label} {bp(c['last'])} {mmdd(c.get('last_date'))}")
    if measured or others:
        newest = max([m["last_date"] for m in measured] or [""])
        lines += ["", f"<b>실측 5Y CDS</b> · 보도 기준" + (f", 최신 {mmdd(newest)}" if newest else "")]
        lines += [measured_line(m) for m in measured]
        if others:
            lines.append(" · ".join(others))

    # 2) CSI — 매일 움직이는 대리지표
    rising = falling = 0
    lines += ["", "<b>크레딧 스트레스 지수</b> · 0-100, 주가 기반"]
    for e in d["entities"]:
        hot, label = band(e["csi"])
        tag = "" if e["type"] == "traded" else "*"
        if e["csi"] > (e["csi_prev"] or e["csi"]):
            rising += 1
        if e["csi_prev"] is not None and e["csi"] < e["csi_prev"]:
            falling += 1
        name = f"{e['name']}{tag}"
        lines.append(f"<code>{name:<11}{e['csi']:>5.1f} {arrow(e['csi'], e['csi_prev']):>4}</code> "
                     + (f"<b>{label}</b>" if hot else label))

    m = d.get("macro", {})
    if m.get("bbb_oas_bp") is not None:
        pct = m.get("percentile_2y", 0) * 100
        lines += ["", f"<b>시장 전체</b> BBB OAS {m['bbb_oas_bp']:.0f}bp (2년 백분위 {pct:.0f}%)"]
        lines.append("· 지수는 잠잠 — 개별 AI 이름에 국한된 스트레스"
                     if pct < 50 else "· 지수까지 확대 — 시스템 리스크 전이 주의")

    # 3) 오늘의 판단
    n = len(d["entities"]) or 1
    if rising >= 3:
        verdict = f"확대 국면 — {n}축 중 {rising}축 상승"
    elif falling == n:
        verdict = f"진정 국면 — {n}축 모두 하락"
    else:
        verdict = f"혼조 — {n}축 중 {rising}축 상승"
    if stale or d.get("quality_warnings"):
        verdict = "관측 지연/품질 확인 필요 — 최신 시장 판단 보류"
    breached = [x["name"] for x in measured if x.get("alert")]
    if breached:
        verdict += f" · 실측 경보: {', '.join(breached)}"
    lines += ["", f"<b>판단</b> {verdict}"]
    lines.append("· 지수 변동은 직전 거래일 대비, 실측 CDS는 보도 시점 값입니다")

    hits = [c for c in d.get("candidates", []) if c.get("bp_hint")]
    if hits:
        top = hits[0]
        lines.append(f"· 신규 보도 수치 감지: {top['entity']} {top['bp_hint']:,.0f}bp — 시드 갱신 검토")

    lines += ["", "* 단일종목 CDS 부재 → 보증 제공자 크레딧 대리치",
              f'<a href="{PAGES_URL}">대시보드 · 실측 트렌드 차트</a>']
    return "\n".join(lines)


def attach_chart(d: dict, now: datetime) -> tuple[bool, str]:
    """트렌드 차트를 오늘 브리프에 붙일지. 같은 그림이 매일 반복되지 않게 조건부로."""
    today = now.strftime("%Y-%m-%d")
    measured = d.get("measured") or []
    if not measured:
        return False, "실측 데이터 없음"
    if any(m.get("alert") for m in measured):
        return True, "경보선 돌파"
    if d.get("measured_updated") == today:
        return True, "실측 관측치·기준선 갱신"
    if now.weekday() == CHART_WEEKDAY:
        return True, "주간 정기"
    return False, "변화 없음"


def build_hub_payload(d: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(KST)
    attach, why = attach_chart(d, now)
    measured = d.get("measured") or []
    return {
        "as_of_kst": now.strftime("%Y-%m-%d"),
        "generated_at": d.get("generated_at"),
        "asof": d.get("asof"),
        "message": build_message(d, now),
        "photos": [HUB_PHOTO_PATH] if attach else [],
        "photo_caption": "오라클·코어위브 실측 5Y CDS 추세와 기준선 (보도 기준)" if attach else "",
        "photo_reason": why,
        "alert": any(m.get("alert") for m in measured)
                 or any(e.get("csi", 0) >= 75 for e in d.get("entities", [])),
        "measured_updated": d.get("measured_updated"),
        "dashboard": PAGES_URL,
    }


def write_json(path: str, obj: dict) -> bool:
    """내용이 같으면 쓰지 않는다 (generated_at 제외 비교)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
            if {k: v for k, v in old.items() if k != "generated_at"} == \
               {k: v for k, v in obj.items() if k != "generated_at"}:
                return False
        except (OSError, json.JSONDecodeError):
            pass
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return True


def send(msg: str, token: str, chat_id: str) -> None:
    if not token or not chat_id:
        print("[telegram] 자격증명 없음 - 발송 생략 (전략봇 발송은 Brief Hub 담당)")
        print(msg)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()
    print("[telegram] 발송 완료")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub-out", help="Brief Hub payload 경로 (지정 시 발송하지 않음)")
    a = ap.parse_args()
    if not os.path.exists(DATA_PATH):
        print("[error] docs/data.json 없음 — collect.py 를 먼저 실행하세요")
        return 1
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    if a.hub_out:
        p = build_hub_payload(d)
        changed = write_json(a.hub_out, p)
        print(f"[hub] payload {'갱신' if changed else '변경 없음'} → {a.hub_out} "
              f"(차트 첨부: {'예' if p['photos'] else '아니오'} — {p['photo_reason']})")
        print(p["message"])
        return 0
    cfg = load_config()
    send(build_message(d), cfg["telegram_token"], cfg["telegram_chat_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
