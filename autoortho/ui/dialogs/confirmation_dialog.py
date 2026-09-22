"""Reusable explicit confirmation dialog."""

from dataclasses import dataclass

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout


@dataclass(frozen=True)
class ConfirmationResult:
    accepted: bool


class ConfirmationDialog(QDialog):
    def __init__(self, title, message, *, destructive=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        label = QLabel(message)
        label.setWordWrap(True)
        layout.addWidget(label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes
            | QDialogButtonBox.StandardButton.Cancel
        )
        yes = buttons.button(QDialogButtonBox.StandardButton.Yes)
        yes.setText("Confirm")
        yes.setProperty(
            "role",
            "destructive" if destructive else "primary",
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_value(self):
        return ConfirmationResult(
            self.result() == QDialog.DialogCode.Accepted
        )
