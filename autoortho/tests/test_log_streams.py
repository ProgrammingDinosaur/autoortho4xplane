import io
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.log_streams import rotating_file_handler, tolerant_console  # noqa: E402

ARROW_MESSAGE = "Governor: raise prefetch lead time 60→90"


def _logger(name, handler):
    logger = logging.getLogger(name)
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger


def test_log_file_keeps_characters_the_locale_lacks(tmp_path, monkeypatch):
    # Windows opens files in the legacy code page by default; this one has no arrow.
    monkeypatch.setattr(logging, "raiseExceptions", True)
    path = tmp_path / "autoortho.log"
    handler = rotating_file_handler(str(path), max_bytes=1_000_000, backup_count=1)
    errors = []
    handler.handleError = lambda record: errors.append(record)
    try:
        _logger("test.log_streams.file", handler).info(ARROW_MESSAGE)
    finally:
        handler.close()
    assert errors == []
    assert path.read_text(encoding="utf-8").strip() == ARROW_MESSAGE


def test_console_without_the_character_prints_an_escape():
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252")
    handler = logging.StreamHandler(tolerant_console(console))
    errors = []
    handler.handleError = lambda record: errors.append(record)
    _logger("test.log_streams.console", handler).info(ARROW_MESSAGE)
    console.flush()
    assert errors == []
    assert raw.getvalue().decode("cp1252").strip() == "Governor: raise prefetch lead time 60\\u219290"


def test_console_that_cannot_be_reconfigured_is_returned_as_is():
    stream = io.StringIO()
    assert tolerant_console(stream) is stream
    assert tolerant_console(None) is None
