"""Performance-report filesystem service."""

from dataclasses import dataclass
from pathlib import Path
from threading import Event

from .common import ServiceError, ServiceErrorCode, ServiceResult


@dataclass(frozen=True)
class PerformanceReport:
    name: str
    path: str
    modified_time: float


@dataclass(frozen=True)
class PerformanceReportContent:
    report: PerformanceReport
    markdown: str


class DiagnosticsService:
    def __init__(self, report_dir: str | Path):
        self.report_dir = Path(report_dir).expanduser()

    def list_reports(self, *, cancel_event: Event | None = None):
        root = self.report_dir
        try:
            reports = []
            for path in root.glob("performance-*/report.md"):
                if cancel_event is not None and cancel_event.is_set():
                    return ServiceResult(
                        error=ServiceError(
                            ServiceErrorCode.CANCELLED,
                            "Report listing cancelled.",
                        )
                    )
                if path.is_file():
                    reports.append(
                        PerformanceReport(
                            path.parent.name,
                            str(path),
                            path.stat().st_mtime,
                        )
                    )
            reports = sorted(
                reports,
                key=lambda report: report.modified_time,
                reverse=True,
            )
            return ServiceResult(tuple(reports))
        except OSError as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.FILESYSTEM,
                    "Could not list performance reports.",
                    str(exc),
                )
            )

    def read_report(
        self,
        report: PerformanceReport,
        *,
        cancel_event: Event | None = None,
    ):
        if cancel_event is not None and cancel_event.is_set():
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.CANCELLED,
                    "Report loading cancelled.",
                )
            )
        try:
            path = Path(report.path).expanduser().resolve()
            root = self.report_dir.resolve()
            if root not in path.parents:
                return ServiceResult(
                    error=ServiceError(
                        ServiceErrorCode.VALIDATION,
                        "The selected report is outside the report folder.",
                    )
                )
            markdown = path.read_text(encoding="utf-8")
            return ServiceResult(
                PerformanceReportContent(report, markdown)
            )
        except OSError as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.FILESYSTEM,
                    "Could not read the performance report.",
                    str(exc),
                )
            )
