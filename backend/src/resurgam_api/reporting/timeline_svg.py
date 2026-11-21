"""A small, dependency-free SVG period timeline for the Recovery Evidence
Management Summary -- plain string formatting, no charting library, same
reasoning WeasyPrint was chosen over a headless-Chromium stack for PDF
rendering. One aggregation feeds both renderers: the JSON endpoint and the
PDF template both embed this same markup as-is (`| safe` in Jinja,
`dangerouslySetInnerHTML` in React) since it is entirely server-built from
numeric aggregates, never from customer-editable input.
"""

from datetime import date

from resurgam_api.schemas.reporting import BackupHistoryEntryOut, RestoreTestEvidenceEntryOut

_WIDTH = 640
_HEIGHT = 80
_MARGIN = 20
_VERIFY_COLOR = {"ok": "#2e7d32", "failed": "#c62828"}
_DEFAULT_COLOR = "#9e9e9e"


def render_period_timeline_svg(
    date_from: date,
    date_to: date,
    backup_history: list[BackupHistoryEntryOut],
    restore_test_evidence: list[RestoreTestEvidenceEntryOut],
) -> str:
    total_days = max((date_to - date_from).days, 1)
    plot_width = _WIDTH - 2 * _MARGIN

    def x_for(d: date) -> float:
        offset_days = (d - date_from).days
        return _MARGIN + max(0.0, min(1.0, offset_days / total_days)) * plot_width

    circles = [
        f'<circle cx="{x_for(e.snapshot_time.date()):.1f}" cy="28" r="3" fill="{_VERIFY_COLOR.get(e.verify_status, _DEFAULT_COLOR)}" />'
        for e in backup_history
    ]
    triangles = []
    for e in restore_test_evidence:
        if e.tested_at is None:
            continue
        cx = x_for(e.tested_at.date())
        color = "#2e7d32" if e.member_status == "passed" else "#c62828"
        triangles.append(f'<polygon points="{cx:.1f},52 {cx - 4:.1f},60 {cx + 4:.1f},60" fill="{color}" />')

    axis = f'<line x1="{_MARGIN}" y1="66" x2="{_WIDTH - _MARGIN}" y2="66" stroke="#ccc" stroke-width="1" />'
    label_from = f'<text x="{_MARGIN}" y="78" font-size="8" fill="#666">{date_from.isoformat()}</text>'
    label_to = (
        f'<text x="{_WIDTH - _MARGIN}" y="78" font-size="8" fill="#666" text-anchor="end">{date_to.isoformat()}</text>'
    )
    legend = (
        f'<text x="{_MARGIN}" y="14" font-size="8" fill="#666">'
        '<tspan fill="#2e7d32">&#9679;</tspan> verified&#160;&#160;'
        '<tspan fill="#c62828">&#9679;</tspan> verify failed&#160;&#160;'
        '<tspan fill="#2e7d32">&#9650;</tspan> restore test passed&#160;&#160;'
        '<tspan fill="#c62828">&#9650;</tspan> restore test failed'
        "</text>"
    )

    body = "".join(circles) + "".join(triangles) + axis + label_from + label_to + legend
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_WIDTH} {_HEIGHT}" width="100%" height="{_HEIGHT}">{body}</svg>'
