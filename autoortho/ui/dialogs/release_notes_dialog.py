"""Release notes for an available AutoOrtho update."""

import webbrowser

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTextBrowser,
    QVBoxLayout,
)


class ReleaseNotesDialog(QDialog):
    def __init__(self, title, notes, url, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{title} Release Notes")
        self.setMinimumSize(560, 420)
        self._url = url

        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setAccessibleName("Release notes")
        self.browser.setOpenExternalLinks(True)
        self.browser.setMarkdown(notes.strip() or "No release notes were published.")
        layout.addWidget(self.browser, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.open_button = buttons.addButton(
            "Open on GitHub",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.open_button.setEnabled(bool(url))
        self.open_button.clicked.connect(self._open_release)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _open_release(self):
        if self._url:
            webbrowser.open(self._url)
