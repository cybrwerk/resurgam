"""Two more dependency-free SVG charts for the Recovery Evidence
Management Summary, added for a C-level reader -- raised directly by
the user: "not enough diagrams for C-level", "more in Farbe und bunt".
Same reasoning as `timeline_svg.py`: plain string formatting, no
charting library, one aggregation feeds both the JSON endpoint and the
PDF template.

Deliberately dumb: both functions take already-labeled, already-ordered
data from the caller (`reporting/recovery_evidence.py`) rather than
deciding "which KPIs, what order, what labels" themselves -- that
business logic belongs where `ManagementSummaryOut`/`ProtectionLevelOut`
are actually assembled.
"""

from resurgam_api.schemas.reporting import ProtectionLevelWorkloadOut

_WIDTH = 640


def _rate_color(rate: float) -> str:
    if rate >= 0.9:
        return "#2e7d32"
    if rate >= 0.7:
        return "#b58a00"
    return "#c62828"


def render_kpi_bars_svg(kpis: list[tuple[str, float]]) -> str:
    """`kpis` is a list of (label, rate 0.0-1.0) pairs, in display order.
    Caller filters out any `None` rate (e.g. restore-test pass with zero
    restore tests run) before calling -- there is nothing meaningful to
    draw a bar for.
    """
    bar_height = 22
    gap = 14
    label_width = 190
    value_width = 50
    bar_max_width = _WIDTH - label_width - value_width
    height = len(kpis) * (bar_height + gap) + gap if kpis else bar_height + 2 * gap

    if not kpis:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_WIDTH} {height}" width="100%" height="{height}"></svg>'

    parts = []
    y = gap
    for label, rate in kpis:
        color = _rate_color(rate)
        bar_width = max(2.0, bar_max_width * max(0.0, min(1.0, rate)))
        text_y = y + bar_height * 0.7
        parts.append(f'<text x="0" y="{text_y:.1f}" font-size="10" fill="#333">{label}</text>')
        parts.append(
            f'<rect x="{label_width}" y="{y}" width="{bar_max_width}" height="{bar_height}" fill="#eceff1" rx="3" />'
        )
        parts.append(
            f'<rect x="{label_width}" y="{y}" width="{bar_width:.1f}" height="{bar_height}" fill="{color}" rx="3" />'
        )
        parts.append(
            f'<text x="{label_width + bar_max_width + 8}" y="{text_y:.1f}" font-size="10" fill="#333">{rate * 100:.0f}%</text>'
        )
        y += bar_height + gap

    body = "".join(parts)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_WIDTH} {height}" width="100%" height="{height}">{body}</svg>'


_TIER_COLOR = {
    "backup_only": "#c62828",
    "backup_verified": "#b58a00",
    "backup_verified_restore_tested": "#1565c0",
    "backup_verified_lab_tested": "#2e7d32",
}
_TIER_SHORT_LABEL = {
    "backup_only": "Backup only",
    "backup_verified": "Verified",
    "backup_verified_restore_tested": "Restore-tested",
    "backup_verified_lab_tested": "Lab-tested",
}
# Fixed draw order (worst to best), independent of dict insertion order,
# so the bar always reads left-to-right as "least protected -> most
# protected" regardless of which tiers are actually present.
_TIER_ORDER = ["backup_only", "backup_verified", "backup_verified_restore_tested", "backup_verified_lab_tested"]


def render_protection_level_distribution_svg(workloads: list[ProtectionLevelWorkloadOut]) -> str:
    """One glance at how many workloads sit at each Protection Level
    tier -- a single stacked bar (worst tier on the left) plus a legend
    with counts, not a per-workload table.
    """
    bar_height = 28
    legend_y = bar_height + 18
    height = legend_y + 24

    if not workloads:
        text = '<text x="0" y="16" font-size="10" fill="#777">No protected workloads in this period.</text>'
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_WIDTH} 24" width="100%" height="24">{text}</svg>'
        )

    counts = {tier: 0 for tier in _TIER_ORDER}
    for w in workloads:
        counts[w.tier] = counts.get(w.tier, 0) + 1
    total = len(workloads)

    segments = []
    x = 0.0
    for tier in _TIER_ORDER:
        count = counts[tier]
        if count == 0:
            continue
        seg_width = _WIDTH * count / total
        segments.append(
            f'<rect x="{x:.1f}" y="0" width="{seg_width:.1f}" height="{bar_height}" fill="{_TIER_COLOR[tier]}" />'
        )
        x += seg_width

    legend = []
    lx = 0.0
    for tier in _TIER_ORDER:
        count = counts[tier]
        if count == 0:
            continue
        label = f"{_TIER_SHORT_LABEL[tier]}: {count}"
        legend.append(f'<rect x="{lx:.1f}" y="{legend_y}" width="10" height="10" fill="{_TIER_COLOR[tier]}" />')
        legend.append(f'<text x="{lx + 14:.1f}" y="{legend_y + 9}" font-size="9" fill="#333">{label}</text>')
        lx += 9 * len(label) + 34

    body = "".join(segments) + "".join(legend)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_WIDTH} {height}" width="100%" height="{height}">{body}</svg>'
