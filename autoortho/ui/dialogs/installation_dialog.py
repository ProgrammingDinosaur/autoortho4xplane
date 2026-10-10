"""Scenery installation review dialog."""

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.readiness import format_bytes
else:
    from ui.readiness import format_bytes


@dataclass(frozen=True)
class InstallationReview:
    name: str
    version: str
    download_bytes: int
    temporary_bytes: int
    final_bytes: int
    destination: str


class InstallationDialog(QDialog):
    def __init__(self, review: InstallationReview, parent=None):
        super().__init__(parent)
        self.review = review
        self.setWindowTitle("Install Scenery")
        layout = QVBoxLayout(self)
        heading = QLabel(f"Install {review.name}?")
        heading.setProperty("textRole", "sectionTitle")
        layout.addWidget(heading)
        form = QFormLayout()
        form.addRow("Version", QLabel(review.version))
        form.addRow(
            "Download size",
            QLabel(format_bytes(review.download_bytes)),
        )
        form.addRow(
            "Temporary space",
            QLabel(format_bytes(review.temporary_bytes)),
        )
        form.addRow(
            "Final space",
            QLabel(format_bytes(review.final_bytes)),
        )
        destination = QLabel(review.destination)
        destination.setWordWrap(True)
        form.addRow("Destination", destination)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        install = buttons.button(QDialogButtonBox.StandardButton.Ok)
        install.setText("Install")
        install.setProperty("role", "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
