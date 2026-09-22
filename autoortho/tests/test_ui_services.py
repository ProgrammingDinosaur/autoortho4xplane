import os
import sys
from types import SimpleNamespace

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

from ui.services.diagnostics_service import DiagnosticsService
from ui.services.storage_service import StorageService


def test_installation_capacity_is_structured(monkeypatch):
    free = {
        "/downloads": 8_000,
        "/scenery": 20_000,
    }
    monkeypatch.setattr(
        "ui.services.storage_service.free_space_bytes",
        lambda path: free[path],
    )

    result = StorageService().check_installation_capacity(
        "/downloads",
        "/scenery",
        temporary_required=7_000,
        final_required=18_000,
    )

    assert result.success
    assert result.value.sufficient
    assert result.value.download_free_bytes == 8_000


def test_diagnostics_rejects_report_outside_report_folder(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("not a report", encoding="utf-8")
    service = DiagnosticsService(reports)

    result = service.read_report(
        SimpleNamespace(path=str(outside))
    )

    assert not result.success
    assert result.error.code.value == "validation"


def test_diagnostics_lists_and_reads_reports(tmp_path):
    report = tmp_path / "performance-test" / "report.md"
    report.parent.mkdir()
    report.write_text("# Healthy", encoding="utf-8")
    service = DiagnosticsService(tmp_path)

    listing = service.list_reports()
    content = service.read_report(listing.value[0])

    assert listing.success
    assert listing.value[0].name == "performance-test"
    assert content.success
    assert content.value.markdown == "# Healthy"
