"""Thread-safe bounded logging handler for Qt views."""

from collections import deque
import logging
import sys

from PySide6.QtCore import QObject, Signal


class _LogSignals(QObject):
    appended = Signal(str, int)


class QTextEditLogger(logging.Handler):
    def __init__(self, text_edit, max_lines=1000):
        super().__init__()
        self.text_edit = text_edit
        self.max_lines = max_lines
        self.entries = deque(maxlen=max_lines)
        self.minimum_level = logging.DEBUG
        self.search_text = ""
        self.paused = False
        self.model = None
        self._signals = _LogSignals()
        self._signals.appended.connect(self._append_text)

    def emit(self, record):
        try:
            self._signals.appended.emit(
                self.format(record),
                int(record.levelno),
            )
        except Exception as exc:
            print(f"QTextEditLogger error: {exc}", file=sys.stderr)

    def _append_text(self, message, level):
        self.entries.append((int(level), message))
        if self.model is not None:
            self.model.append_entry((int(level), message))
        if self.paused or not self._matches(level, message):
            return
        self.text_edit.append(message)
        scrollbar = self.text_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def attach_model(self, model):
        self.model = model
        if self.entries:
            model.append_entries(self.entries)

    def _matches(self, level, message):
        return (
            int(level) >= self.minimum_level
            and self.search_text.lower() in message.lower()
        )

    def set_filter(self, minimum_level=logging.DEBUG, search_text=""):
        self.minimum_level = int(minimum_level)
        self.search_text = str(search_text or "")
        self.render()

    def set_paused(self, paused):
        self.paused = bool(paused)
        if not self.paused:
            self.render()

    def render(self):
        if self.paused:
            return
        self.text_edit.clear()
        for level, message in self.entries:
            if self._matches(level, message):
                self.text_edit.append(message)
