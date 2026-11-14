"""Up to three notices for Home: what needs the user, with buttons.

A notice appears only when there is something to say, and goes away when it
is handled or dismissed. Most severe first.
"""

from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.theme import repolish
else:
    from ui.theme import repolish

MAX_NOTICES = 3
_RANK = {"error": 0, "warning": 1, "info": 2}
_SYMBOL = {"error": "×", "warning": "!", "info": "○"}


@dataclass(frozen=True)
class Notice:
    id: str
    level: str          # "error", "warning" or "info"
    text: str
    actions: tuple = field(default_factory=tuple)   # ((action_id, label), ...)
    dismissable: bool = True


class NoticeBar(QWidget):
    action_triggered = Signal(str, str)     # notice id, action id
    dismissed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Notices")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self.notices = ()
        self.setVisible(False)

    def set_notices(self, notices):
        ordered = sorted(notices, key=lambda n: _RANK.get(n.level, 3))
        self.notices = tuple(ordered[:MAX_NOTICES])
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for notice in self.notices:
            self._layout.addWidget(self._frame(notice))
        self.setVisible(bool(self.notices))

    def _frame(self, notice):
        frame = QFrame()
        frame.setObjectName("noticeFrame")
        frame.setProperty("state", {"error": "error", "warning": "warning"}.get(
            notice.level, "info"
        ))
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 6, 6, 6)
        label = QLabel(f"{_SYMBOL.get(notice.level, '•')}  {notice.text}")
        label.setWordWrap(True)
        label.setAccessibleName(f"{notice.level}: {notice.text}")
        layout.addWidget(label, 1)
        for action_id, text in notice.actions:
            button = QPushButton(text)
            button.clicked.connect(
                lambda _c=False, n=notice.id, a=action_id: self.action_triggered.emit(n, a)
            )
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        if notice.dismissable:
            close = QPushButton("×")
            close.setFixedWidth(28)
            close.setToolTip("Dismiss")
            close.setAccessibleName(f"Dismiss: {notice.text}")
            close.clicked.connect(
                lambda _c=False, n=notice.id: self.dismissed.emit(n)
            )
            layout.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        repolish(frame)
        return frame
