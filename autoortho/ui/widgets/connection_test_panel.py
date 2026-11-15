"""The connection test as the settings and the setup wizard show it."""

import threading

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho import connection_test
else:
    import connection_test


class _TestThread(QThread):
    progress = Signal(str)
    done = Signal(object)

    def __init__(self, run, parent=None):
        super().__init__(parent)
        self._run = run
        self.cancel_event = threading.Event()

    def run(self):
        try:
            result = self._run(self.progress.emit, self.cancel_event)
        except Exception as exc:  # shown to the user, never raised
            result = exc
        self.done.emit(result)


def describe(result):
    if result is None:
        return (
            "Not tested yet. The test downloads about 30 MB of imagery to "
            "measure your connection (about half a minute)."
        )
    age = result.age_days()
    when = (
        "today" if age < 1 else
        "yesterday" if age < 2 else
        f"{int(age)} days ago"
    )
    return f"{result.summary} (tested {when})"


class ConnectionTestPanel(QWidget):
    """A button, a status line, and the last result."""

    measured = Signal(object)

    def __init__(self, maptype=lambda: "BI", max_connections=lambda: 160,
                 runner=None, parent=None):
        super().__init__(parent)
        self._maptype = maptype
        self._max_connections = max_connections
        self._runner = runner or connection_test.run
        self._thread = None
        self._blocked_reason = ""
        self.result = connection_test.load_result()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.run_button = QPushButton()
        self.run_button.setAccessibleName("Test my connection")
        self.run_button.clicked.connect(self.start)
        row.addWidget(self.run_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("textRole", "secondary")
        layout.addWidget(self.status_label)
        self._refresh()

    @property
    def running(self):
        return self._thread is not None and self._thread.isRunning()

    def set_blocked(self, reason=""):
        """Disable the test (for example while streaming shares the line)."""
        self._blocked_reason = reason
        self._refresh()

    def _refresh(self):
        if self.running:
            self.run_button.setText("Testing…")
            self.run_button.setEnabled(False)
            return
        self.run_button.setText(
            "Test my connection" if self.result is None else "Test again"
        )
        self.run_button.setEnabled(not self._blocked_reason)
        self.run_button.setToolTip(self._blocked_reason)
        text = describe(self.result)
        if self._blocked_reason:
            text += f" {self._blocked_reason}"
        self.status_label.setText(text)

    def start(self):
        if self.running or self._blocked_reason:
            return
        maptype = self._maptype()
        connections = int(self._max_connections())

        def run(progress, cancel):
            return self._runner(
                maptype, progress, cancel, max_connections=connections
            )

        self._thread = _TestThread(run, self)
        self._thread.progress.connect(self.status_label.setText)
        self._thread.done.connect(self._finished)
        self._refresh()
        self.status_label.setText("Opening the connection…")
        self._thread.start()

    def _finished(self, result):
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.wait(2000)
        if isinstance(result, Exception):
            self._refresh()
            self.status_label.setText(f"The test failed: {result}")
            return
        self.result = result
        self._refresh()
        self.measured.emit(result)

    def stop(self):
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.cancel_event.set()
            thread.wait(15000)
