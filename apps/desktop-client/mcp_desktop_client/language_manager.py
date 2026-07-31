from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QLibraryInfo,
    QLocale,
    QObject,
    QSettings,
    QTranslator,
    Signal,
)

from .i18n import tr


LANGUAGE_CODES = {"system", "en_US", "zh_CN"}


class LanguageManager(QObject):
    language_changed = Signal(str)

    SETTINGS_ORGANIZATION = "CodingToolsMCP"
    SETTINGS_APPLICATION = "DesktopClient"
    SETTINGS_KEY = "ui/language"

    def __init__(
        self, parent: QObject | None = None, *, settings: QSettings | None = None
    ) -> None:
        super().__init__(parent)
        self._settings = (
            settings
            if settings is not None
            else QSettings(self.SETTINGS_ORGANIZATION, self.SETTINGS_APPLICATION)
        )
        self._app_translator = QTranslator(self)
        self._qt_translator = QTranslator(self)
        self._configured_language = str(
            self._settings.value(self.SETTINGS_KEY, "system")
        )
        if self._configured_language not in LANGUAGE_CODES:
            self._configured_language = "system"
        self._effective_language = "en_US"
        self._apply_language(
            self._configured_language, persist=False, emit_signal=False
        )

    @property
    def configured_language(self) -> str:
        return self._configured_language

    @property
    def effective_language(self) -> str:
        return self._effective_language

    def set_language(self, language: str) -> None:
        if language not in LANGUAGE_CODES:
            language = "system"
        if language == self._configured_language:
            return
        self._apply_language(language, persist=True, emit_signal=True)

    def language_options(self) -> list[tuple[str, str]]:
        return [
            ("system", tr("LanguageManager", "System default")),
            ("en_US", tr("LanguageManager", "English")),
            ("zh_CN", tr("LanguageManager", "Simplified Chinese")),
        ]

    def _apply_language(
        self, language: str, *, persist: bool, emit_signal: bool
    ) -> None:
        application = QCoreApplication.instance()
        if application is None:
            raise RuntimeError("LanguageManager requires a QCoreApplication instance.")

        application.removeTranslator(self._app_translator)
        application.removeTranslator(self._qt_translator)

        effective = self._resolve_language(language)
        if effective == "zh_CN":
            qt_translation_path = QLibraryInfo.path(
                QLibraryInfo.LibraryPath.TranslationsPath
            )
            if self._qt_translator.load("qtbase_zh_CN", qt_translation_path):
                application.installTranslator(self._qt_translator)

            app_catalog = Path(__file__).resolve().parent / "locales" / "app_zh_CN.qm"
            if self._app_translator.load(str(app_catalog)):
                application.installTranslator(self._app_translator)
            else:
                application.removeTranslator(self._qt_translator)
                effective = "en_US"

        QLocale.setDefault(QLocale(effective))
        self._configured_language = language
        self._effective_language = effective
        if persist:
            self._settings.setValue(self.SETTINGS_KEY, language)
            self._settings.sync()
        if emit_signal:
            self.language_changed.emit(effective)

    def _resolve_language(self, configured: str) -> str:
        if configured != "system":
            return configured
        system_language = QLocale.system().name()
        if system_language.startswith(("zh_CN", "zh_SG")):
            return "zh_CN"
        return "en_US"
