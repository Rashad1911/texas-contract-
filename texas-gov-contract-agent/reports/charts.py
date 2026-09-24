"""The one chart in the email: a simple donut of Top / Worth reviewing / Watchlist / Filtered."""
from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SEGMENTS = [("top", "Top", "#C2410C"), ("review", "Worth reviewing", "#D69E2E"),
            ("watch", "Watchlist", "#8A94A6"), ("filtered", "Filtered", "#D9DEE5")]


def donut_png(stats: dict, dpi: int = 160) -> bytes:
    """Returns PNG bytes. `stats` has keys top, review, watch, filtered."""
    values = [max(int(stats.get(k, 0) or 0), 0) for k, _, _ in SEGMENTS]
    total = sum(values)
    fig, ax = plt.subplots(figsize=(5.4, 2.6), dpi=dpi)
    fig.patch.set_facecolor("white")
    donut_ax = fig.add_axes([0.0, 0.02, 0.48, 0.96])
    ax.remove()
    if total == 0:
        donut_ax.pie([1], colors=["#E5E9EF"], startangle=90, wedgeprops={"width": 0.34, "edgecolor": "white"})
    else:
        shown = [(v, c) for v, (_, _, c) in zip(values, SEGMENTS)]
        donut_ax.pie([max(v, total * 0.012) if v else 0 for v, _ in shown], colors=[c for _, c in shown],
                     startangle=90, counterclock=False, wedgeprops={"width": 0.34, "edgecolor": "white", "linewidth": 2})
    donut_ax.text(0, 0.1, f"{total}", ha="center", va="center", fontsize=22, fontweight="bold", color="#14213D")
    donut_ax.text(0, -0.25, "found", ha="center", va="center", fontsize=10, color="#5B6575")
    donut_ax.set_aspect("equal")

    legend_ax = fig.add_axes([0.5, 0.0, 0.5, 1.0])
    legend_ax.axis("off")
    for i, ((key, label, color), value) in enumerate(zip(SEGMENTS, values)):
        y = 0.8 - i * 0.2
        legend_ax.add_patch(plt.Rectangle((0.02, y - 0.05), 0.07, 0.1, color=color, transform=legend_ax.transAxes))
        legend_ax.text(0.14, y, label, va="center", fontsize=11, color="#14213D", transform=legend_ax.transAxes)
        legend_ax.text(0.95, y, str(value), va="center", ha="right", fontsize=12, fontweight="bold",
                       color="#14213D", transform=legend_ax.transAxes)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
