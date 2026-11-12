"""
AI Credit Radar — 실측 CDS 트렌드 차트 (텔레그램 첨부용 PNG)

docs/data.json 의 measured(오라클·코어위브) 블록을 읽어 패널별 추세선과
cds_seed.json `_baselines` 기준선(참고선·경보선)을 그린다 → docs/hub/cds_measured.png

- 점 = 보도 기준 관측치. 관측 간격이 30일을 넘는 구간은 옅은 점선(보간이 아님을 표시)
- 회색 점선 = 참고선, 주황 파선 = 경보선(돌파 시 브리프 경보)
- 세로 점선 = 오늘. 마지막 관측과의 거리가 곧 데이터 신선도
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE, "docs", "data.json")
OUT_PATH = os.path.join(BASE, "docs", "hub", "cds_measured.png")
KST = timezone(timedelta(hours=9))

INK, INK2, INK3 = "#131A22", "#4A555F", "#7B858E"
RULE, PAPER, CARD = "#C6CDD4", "#E4E7EA", "#F8F9FA"
ALERT_C = "#C4562B"
PAL = {"oracle": "#A8322A", "coreweave": "#42566F"}
GAP_DAYS = 30
KR_FONTS = ["Noto Sans CJK KR", "Noto Sans KR", "NanumGothic", "Malgun Gothic", "Apple SD Gothic Neo"]


def pick_font() -> str | None:
    names = {f.name for f in font_manager.fontManager.ttflist}
    return next((n for n in KR_FONTS if n in names), None)


def _d(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def render(d: dict, out_path: str = OUT_PATH, today: datetime | None = None) -> bool:
    measured = [m for m in (d.get("measured") or []) if m.get("series")]
    if not measured:
        print("[chart] 실측 데이터 없음 — 생략")
        return False
    today = (today or datetime.now(KST)).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)

    font = pick_font()
    ko = font is not None
    plt.rcParams.update({
        "font.family": font or "DejaVu Sans", "axes.unicode_minus": False,
        "axes.edgecolor": RULE, "axes.labelcolor": INK2, "xtick.color": INK3, "ytick.color": INK3,
    })

    n = len(measured)
    fig, axes = plt.subplots(n, 1, figsize=(8, 3.55 * n + 0.75), dpi=150, facecolor=CARD)
    axes = [axes] if n == 1 else list(axes)

    for ax, m in zip(axes, measured):
        col = PAL.get(m["key"], INK)
        xs = [_d(r[0]) for r in m["series"]]
        ys = [float(r[1]) for r in m["series"]]
        ax.set_facecolor(CARD)

        # 추세선 — 관측 공백이 길면 옅은 점선
        for i in range(1, len(xs)):
            far = (xs[i] - xs[i - 1]).days > GAP_DAYS
            ax.plot(xs[i - 1:i + 1], ys[i - 1:i + 1], color=col, zorder=3,
                    lw=1.3 if far else 2.2, alpha=.5 if far else 1,
                    ls=(0, (3, 3)) if far else "-", solid_capstyle="round")
        ax.scatter(xs, ys, s=20, color=col, zorder=4, edgecolors=CARD, linewidths=.8)
        ax.scatter([xs[-1]], [ys[-1]], s=64, color=col, zorder=5, edgecolors=CARD, linewidths=1.4)

        # 축 범위 — 기준선이 모두 보이도록
        lines = m.get("baselines") or []
        lo = min(ys + [b["bp"] for b in lines])
        hi = max(ys + [b["bp"] for b in lines])
        pad = (hi - lo) * .14 or 10
        ax.set_ylim(max(0, lo - pad), hi + pad)
        x0 = xs[0] - timedelta(days=7)
        x1 = max(today, xs[-1]) + timedelta(days=12)
        ax.set_xlim(x0, x1)

        # 기준선
        for b in lines:
            alert = b.get("kind") == "alert"
            c = ALERT_C if alert else INK3
            ax.axhline(b["bp"], color=c, zorder=2, lw=1.3 if alert else 1.0,
                       ls=(0, (6, 4)) if alert else (0, (1.5, 3)))
            if ko:
                txt = f"{b['label']} {b['bp']:,.0f}"
            else:
                txt = f"{'alert' if alert else 'ref'} {b['bp']:,.0f}"
            ax.text(x0 + timedelta(days=2), b["bp"], txt, va="bottom", ha="left", fontsize=8.3,
                    color=c, zorder=6, fontweight="bold" if alert else "normal",
                    bbox=dict(fc=CARD, ec="none", pad=1.0, alpha=.85))

        # 오늘
        ax.axvline(today, color=INK3, lw=.9, ls=":", zorder=1)
        ax.text(today, ax.get_ylim()[1], " 오늘" if ko else " today", va="top", ha="left",
                fontsize=8, color=INK3)

        # 마지막 값 — 상단에 붙으면 아래쪽에 표기해 '오늘'·경보선 라벨과 겹치지 않게
        y_lo, y_hi = ax.get_ylim()
        near_top = (ys[-1] - y_lo) / ((y_hi - y_lo) or 1) > .82
        ax.annotate(f"{ys[-1]:,.0f}bp", (xs[-1], ys[-1]), xytext=(8, -16 if near_top else 7),
                    textcoords="offset points", fontsize=10.5, fontweight="bold", color=col, zorder=7)

        # 제목 + 상태
        name = m["name"] if ko else m.get("name_en", m["key"])
        ax.set_title(f"{name} 5Y CDS" if ko else f"{name} 5Y CDS (bp)", loc="left",
                     fontsize=12.5, fontweight="bold", color=INK, pad=20)
        last_md = f"{m['last_date'][5:7]}/{m['last_date'][8:10]}"
        if m.get("alert_bp") is None:
            status, sc = (f"최신 {ys[-1]:,.0f}bp · {last_md} 보도" if ko else f"last {ys[-1]:,.0f}bp · {last_md}"), INK2
        elif m.get("alert"):
            status = (f"최신 {ys[-1]:,.0f}bp · {last_md} 보도 · 경보선 {m['alert_bp']:,.0f} 돌파" if ko
                      else f"last {ys[-1]:,.0f}bp · {last_md} · above alert {m['alert_bp']:,.0f}")
            sc = ALERT_C
        else:
            status = (f"최신 {ys[-1]:,.0f}bp · {last_md} 보도 · 경보선 {m['alert_bp']:,.0f}까지 {m['to_alert']:,.0f}bp" if ko
                      else f"last {ys[-1]:,.0f}bp · {last_md} · {m['to_alert']:,.0f}bp to alert {m['alert_bp']:,.0f}")
            sc = INK2
        ax.text(0, 1.015, status, transform=ax.transAxes, fontsize=9, color=sc, va="bottom")

        # 눈금
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(FuncFormatter(
            lambda v, _: f"{mdates.num2date(v).month}월" if ko else mdates.num2date(v).strftime("%b")))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
        ax.tick_params(labelsize=8.5, length=0)
        ax.grid(axis="y", color=RULE, lw=.6, alpha=.6)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)

    foot = ("점 = 보도 기준 관측치 · 옅은 점선 = 관측 공백 30일 초과 구간 · 기준선 설정: cds_seed.json _baselines"
            if ko else "dots = reported observations · faint dashes = gaps over 30 days · baselines: cds_seed.json")
    fig.text(.012, .008, foot, fontsize=7.6, color=INK3, ha="left", va="bottom")
    fig.tight_layout(rect=(0, .025, 1, 1), h_pad=2.2)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp.png"
    fig.savefig(tmp, facecolor=CARD, metadata={"Software": None})
    plt.close(fig)
    if os.path.exists(out_path):
        with open(out_path, "rb") as a, open(tmp, "rb") as b:
            if a.read() == b.read():
                os.remove(tmp)
                print(f"[chart] 변경 없음 → {out_path}")
                return False
    os.replace(tmp, out_path)
    print(f"[chart] 저장 → {out_path} (font={font or 'fallback-en'})")
    return True


def main() -> int:
    if not os.path.exists(DATA_PATH):
        print("[error] docs/data.json 없음 — collect.py 를 먼저 실행하세요")
        return 1
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        render(json.load(f))
    return 0


if __name__ == "__main__":
    sys.exit(main())
