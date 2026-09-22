"""Generic cancellable QThread adapter for typed services."""

from threading import Event

from PySide6.QtCore import QThread, Signal


class ServiceWorker(QThread):
    completed = Signal(object)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.cancel_event = Event()

    def cancel(self):
        self.cancel_event.set()
        self.requestInterruption()

    def run(self):
        try:
            result = self.operation(self.cancel_event)
        except Exception as exc:
            result = exc
        self.completed.emit(result)
