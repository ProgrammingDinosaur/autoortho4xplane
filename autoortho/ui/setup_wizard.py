"""Compatibility export for the first-run setup wizard."""

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.dialogs.setup_wizard import SetupWizard
else:
    from ui.dialogs.setup_wizard import SetupWizard

__all__ = ["SetupWizard"]
