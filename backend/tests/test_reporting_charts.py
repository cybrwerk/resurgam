"""Unit tests for the two new dependency-free SVG charts added for a
C-level reader (`reporting/charts.py`) -- valid SVG output, no crash on
empty data, correct proportions for the stacked protection-level bar.
"""

from resurgam_api.reporting.charts import render_kpi_bars_svg, render_protection_level_distribution_svg
from resurgam_api.schemas.reporting import ProtectionLevelWorkloadOut


def test_kpi_bars_renders_valid_svg_with_data():
    svg = render_kpi_bars_svg([("Backup success", 1.0), ("Verification pass", 0.5), ("Policy adherence", 0.2)])
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert svg.count("<rect") == 6  # 2 rects (track + fill) per bar


def test_kpi_bars_handles_empty_list_without_crashing():
    svg = render_kpi_bars_svg([])
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")


def test_kpi_bars_colors_by_threshold():
    svg = render_kpi_bars_svg([("High", 0.95), ("Mid", 0.75), ("Low", 0.3)])
    assert "#2e7d32" in svg  # green for >=90%
    assert "#b58a00" in svg  # amber for >=70%
    assert "#c62828" in svg  # red for <70%


def _workload(tier: str) -> ProtectionLevelWorkloadOut:
    return ProtectionLevelWorkloadOut(workload_label="vm-100", tier=tier)


def test_protection_level_distribution_handles_empty_workloads():
    svg = render_protection_level_distribution_svg([])
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    assert "No protected workloads" in svg


def test_protection_level_distribution_renders_one_segment_per_present_tier():
    workloads = [_workload("backup_only"), _workload("backup_only"), _workload("backup_verified_lab_tested")]
    svg = render_protection_level_distribution_svg(workloads)
    assert svg.count("<rect") == 4  # 2 distinct tiers present, each drawn as a bar segment + a legend swatch
    assert "backup_only: 2" not in svg  # label text uses the short display label, not the raw tier key
    assert "Backup only: 2" in svg
    assert "Lab-tested: 1" in svg


def test_protection_level_distribution_omits_tiers_with_zero_workloads():
    workloads = [_workload("backup_verified")]
    svg = render_protection_level_distribution_svg(workloads)
    assert "Backup only" not in svg
    assert "Verified: 1" in svg
