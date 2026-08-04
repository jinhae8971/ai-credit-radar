"""
AI Credit Radar — 전략비서 일간 브리프

docs/data.json 을 읽어 단일 메시지로 압축해 텔레그램으로 보낸다.
상세(전체 시계열·출처·뉴스 후보)는 GitHub Pages 대시보드로 분리한다.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE, "docs", "data.json")
KST = timezone(timedelta(hours=9))
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

PAGES_URL = os.environ.get("PAGES_URL", "https://jinhae8971.github.io/ai-credit-radar")

# CSI 밴드
BANDS = [(75, "🔴", "과열"), (60, "🟠", "확대"), (40, "🟡", "관망"), (0, "🟢", "안정")]


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


def band(csi: float) -> tuple[str, str]:
    for lo, icon, label in BANDS:
        if csi >= lo:
            return icon, label
    return "🟢", "안정"


def arrow(cur: float, prev: float | None) -> str:
    if prev is None:
        return "  ―"
    d = cur - prev
    if d >= 0.5:
        return f"▲{d:.0f}"
    if d <= -0.5:
        return f"▼{abs(d):.0f}"
    return " ―"


def build_message(d: dict) -> str:
    now = datetime.now(KST)
    head = f"🛡️ <b>AI 크레딧 레이더</b> · {now:%m/%d}({WEEKDAY_KR[now.weekday()]})"

    stale = ""
    try:
        gap = (now.date() - datetime.strptime(d["asof"], "%Y-%m-%d").date()).days
        if gap >= 4:
            stale = f"\n⚠️ 가격 데이터 {gap}일 경과 — 휴장/수집 지연 확인 필요"
    except (ValueError, KeyError):
        pass

    lines = [head, f"<i>기준 {d['asof']} 종가</i>{stale}", "", "▸ <b>크레딧 스트레스 지수</b> (0-100)"]

    rising = 0
    for e in d["entities"]:
        icon, label = band(e["csi"])
        tag = "" if e["type"] == "traded" else "*"
        if e["csi"] > (e["csi_prev"] or e["csi"]):
            rising += 1
        name = f"{e['name']}{tag}"
        lines.append(
            f"{icon} <code>{name:<11}{e['csi']:>5.1f} {arrow(e['csi'], e['csi_prev']):>3}</code> "
            f"<i>{label}</i>"
        )

    lines += ["", "▸ <b>실측 5Y CDS</b> (보도 기준)"]
    for e in d["entities"]:
        c = e["cds"]
        if c.get("last") is None:
            continue
        mark = "" if e["type"] == "traded" else " (대리)"
        lines.append(f"· {e['name']}{mark} <b>{c['last']:.0f}bp</b> <i>@{c['last_date']}</i>")

    m = d.get("macro", {})
    if m.get("bbb_oas_bp") is not None:
        pct = m.get("percentile_2y", 0) * 100
        lines += ["", f"▸ <b>시장 전체</b>  BBB OAS {m['bbb_oas_bp']:.0f}bp "
                      f"(2년 백분위 {pct:.0f}%)"]
        lines.append("· 지수는 잠잠 — 개별 AI 이름에 국한된 스트레스"
                     if pct < 50 else "· 지수까지 확대 — 시스템 리스크 전이 주의")

    # 오늘의 판단
    n = len(d["entities"]) or 1
    if rising >= 3:
        verdict = f"확대 국면 — {n}축 중 {rising}축 상승"
    elif rising == 0:
        verdict = f"진정 국면 — {n}축 모두 하락"
    else:
        verdict = f"혼조 — {n}축 중 {rising}축 상승"
    lines += ["", f"▸ <b>판단</b>  {verdict}"]

    hits = [c for c in d.get("candidates", []) if c.get("bp_hint")]
    if hits:
        top = hits[0]
        lines.append(f"📰 신규 수치 감지: {top['entity']} {top['bp_hint']:.0f}bp — 시드 갱신 검토")

    lines += ["", "* 표시는 단일종목 CDS 부재 → 보증 제공자 크레딧 대리치",
              f'📊 <a href="{PAGES_URL}">대시보드 열기</a>']
    return "\n".join(lines)


def send(msg: str, token: str, chat_id: str) -> None:
    if not token or not chat_id:
        print("[telegram] 자격증명 없음 - 발송 생략")
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
    if not os.path.exists(DATA_PATH):
        print("[error] docs/data.json 없음 — collect.py 를 먼저 실행하세요")
        return 1
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    cfg = load_config()
    send(build_message(d), cfg["telegram_token"], cfg["telegram_chat_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
