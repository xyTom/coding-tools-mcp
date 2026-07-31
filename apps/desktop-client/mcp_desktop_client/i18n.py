from __future__ import annotations


def tr(
    context: str, source: str, disambiguation: str | None = None, n: int = -1
) -> str:
    """Translate a user-facing string without making non-UI modules depend on Qt."""

    try:
        from PySide6.QtCore import QCoreApplication
    except (ImportError, ModuleNotFoundError):
        return source
    return QCoreApplication.translate(context, source, disambiguation, n)
