"""Dialog wrapper for the model-backed dynamic zoom editor."""

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.dynamic_zoom_editor import DynamicZoomEditor
else:
    from ui.dynamic_zoom_editor import DynamicZoomEditor


class DynamicZoomDialog(QDialog):
    def __init__(self, parent=None, manager=None, current_max_zoom=16):
        super().__init__(parent)
        self.setWindowTitle("Dynamic Zoom Quality Steps")
        layout = QVBoxLayout(self)
        working_manager = manager
        if (
            manager is not None
            and not manager.is_empty()
            and not manager.has_base_step()
        ):
            lowest = min(
                manager.get_steps(),
                key=lambda step: step.altitude_ft,
            )
            if lowest.altitude_ft < 0:
                working_manager = manager.clone()
                working_manager.set_base_zoom(
                    lowest.zoom_level,
                    lowest.zoom_level_airports,
                )
        self.editor = DynamicZoomEditor(working_manager)
        self.steps_table = self.editor.table
        self.validation_label = self.editor.validation_label
        layout.addWidget(self.editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_manager(self):
        return self.editor.manager()

    def _remove_step(self, altitude):
        self.editor._remove_altitude(altitude)

    def _on_accept(self):
        if not self.editor.model.is_valid():
            self.editor._update_validation_message()
            return
        self.accept()

    @property
    def manager(self):
        return self.editor.manager()


QualityStepsDialog = DynamicZoomDialog
