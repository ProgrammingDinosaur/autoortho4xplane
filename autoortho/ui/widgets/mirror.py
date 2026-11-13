"""Keep a second control in step with the control that owns a setting.

The Essentials page shows a few settings that also have a control on their
full settings page. The full control stays the owner: saving, Revert and the
change tracking read it. The mirror copies its value both ways:

* the user changes the mirror -> the owner is set normally, so the owner's
  own handlers (labels, validation, change tracking) run;
* the owner changes (edits, Revert, presets) -> the mirror follows silently.
"""

from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSlider,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
)


def _kind(widget):
    if isinstance(widget, QLineEdit):
        return "text"
    if isinstance(widget, QComboBox):
        return "index"
    if isinstance(widget, QAbstractButton):
        return "checked"
    if isinstance(widget, (QAbstractSlider, QAbstractSpinBox)):
        return "value"
    raise TypeError(f"Cannot mirror {type(widget).__name__}")


def _signal(widget, kind):
    return {
        "text": lambda: widget.textChanged,
        "index": lambda: widget.currentIndexChanged,
        "checked": lambda: widget.toggled,
        "value": lambda: widget.valueChanged,
    }[kind]()


def _get(widget, kind):
    if kind == "text":
        return widget.text()
    if kind == "index":
        return widget.currentIndex()
    if kind == "checked":
        return widget.isChecked()
    return widget.value()


def _set(widget, kind, value):
    if kind == "text":
        widget.setText(value)
    elif kind == "index":
        widget.setCurrentIndex(value)
    elif kind == "checked":
        widget.setChecked(value)
    else:
        widget.setValue(value)


def mirror(owner, proxy, *, copy_items=True):
    """Bind ``proxy`` to ``owner`` both ways (see the module docstring)."""
    kind = _kind(owner)
    if _kind(proxy) != kind:
        raise TypeError("owner and proxy must be the same kind of control")
    if kind == "index" and copy_items and proxy.count() == 0:
        for index in range(owner.count()):
            proxy.addItem(owner.itemText(index), owner.itemData(index))
    if kind == "value":
        try:
            proxy.setRange(owner.minimum(), owner.maximum())
        except AttributeError:
            pass
    proxy.blockSignals(True)
    _set(proxy, kind, _get(owner, kind))
    proxy.blockSignals(False)

    def to_owner(*_args):
        value = _get(proxy, kind)
        if _get(owner, kind) != value:
            _set(owner, kind, value)

    def to_proxy(*_args):
        value = _get(owner, kind)
        if _get(proxy, kind) != value:
            proxy.blockSignals(True)
            _set(proxy, kind, value)
            proxy.blockSignals(False)

    _signal(proxy, kind).connect(to_owner)
    _signal(owner, kind).connect(to_proxy)
    return to_proxy
