"""Logging streams that accept any character.

On Windows, files opened without an encoding and the console use the
legacy code page (e.g. cp1252). A log message with a character it lacks,
such as "→", then fails in ``Handler.emit``: Python prints a "Logging
error" traceback and the line is lost from that handler.
"""

import logging.handlers

LOG_FILE_ENCODING = "utf-8"


def rotating_file_handler(filename, max_bytes, backup_count):
    """A RotatingFileHandler that writes UTF-8, whatever the platform."""
    return logging.handlers.RotatingFileHandler(
        filename,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding=LOG_FILE_ENCODING,
    )


def tolerant_console(stream):
    """Make ``stream`` print characters its encoding lacks as ``\\uXXXX``
    instead of raising. Returns the stream (unchanged if it can't be
    reconfigured, e.g. a redirected or replaced stream)."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            pass
    return stream
