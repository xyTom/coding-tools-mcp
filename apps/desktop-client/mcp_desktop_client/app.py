from __future__ import annotations

import sys
from copy import deepcopy

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QComboBox,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .i18n import tr
from .language_manager import LanguageManager
from .models import MCP_ENDPOINT_PATH, WorkspaceProfile, build_profile
from .runtime import RuntimeManager
from .storage import load_profiles, log_dir_for_profile, save_profiles
from .theme import STYLESHEET


class RuntimeJob(QObject):
    finished = Signal(str, str, object, str)

    def __init__(self, runtime: RuntimeManager, profile: WorkspaceProfile, action: str) -> None:
        super().__init__()
        self.runtime = runtime
        self.profile = profile
        self.action = action

    def run(self) -> None:
        try:
            if self.action == "start":
                status = self.runtime.start(self.profile)
            else:
                status = self.runtime.stop(self.profile)
            self.finished.emit(self.profile.id, self.action, status, "")
        except Exception as exc:  # noqa: BLE001
            self.finished.emit(self.profile.id, self.action, None, str(exc))


class MainWindow(QMainWindow):
    TUNNEL_OPTIONS = [
        ("frp", "FRP (externally managed)"),
        ("cloudflare", "Cloudflare"),
    ]
    CLOUDFLARE_MODE_OPTIONS = [
        ("quick", "Quick tunnel"),
        ("named", "Fixed domain"),
    ]
    AUTH_OPTIONS = [
        ("oauth", "OAuth"),
        ("bearer", "Bearer Token"),
        ("noauth", "No authentication"),
    ]
    PERMISSION_MODE_OPTIONS = [
        ("trusted", "Trusted"),
        ("safe", "Safe"),
        ("dangerous", "Unrestricted"),
    ]

    def __init__(self, language_manager: LanguageManager) -> None:
        super().__init__()
        self.language_manager = language_manager
        self.setWindowTitle(tr("MainWindow", "Coding Tools MCP Desktop"))
        self.resize(1460, 920)
        self.runtime = RuntimeManager()
        self.profiles = load_profiles()
        self.current_profile: WorkspaceProfile | None = None
        self._runtime_thread: QThread | None = None
        self._runtime_job: RuntimeJob | None = None
        self._busy_profile_id: str | None = None
        self._busy_action: str | None = None
        self._busy_dots = 0
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(350)
        self._busy_timer.timeout.connect(self._tick_busy_indicator)
        self._build_ui()
        self.language_manager.language_changed.connect(self._on_language_changed)
        self._populate_workspace_list()
        if self.profiles:
            self.workspace_list.setCurrentRow(0)
        else:
            self._clear_panel()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(18)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(18, 18, 18, 18)
        sidebar_layout.setSpacing(14)

        self.sidebar_eyebrow = QLabel(tr("MainWindow", "Workspace console"))
        self.sidebar_eyebrow.setObjectName("Eyebrow")
        self.sidebar_title = QLabel(tr("MainWindow", "MCP Desktop Client"))
        self.sidebar_title.setObjectName("Title")
        self.sidebar_subtitle = QLabel(
            tr("MainWindow", "Manage public access, authentication, and local runtime state by workspace.")
        )
        self.sidebar_subtitle.setWordWrap(True)
        self.sidebar_subtitle.setStyleSheet("color:#667085; font-size:14px;")

        language_row = QHBoxLayout()
        self.language_label = QLabel(tr("MainWindow", "Language"))
        self.language_combo = QComboBox()
        self._populate_language_combo()
        self.language_combo.currentIndexChanged.connect(self._on_language_selected)
        language_row.addWidget(self.language_label)
        language_row.addWidget(self.language_combo, 1)

        actions = QHBoxLayout()
        self.add_button = QPushButton(tr("MainWindow", "Add workspace"))
        self.add_button.clicked.connect(self._add_workspace)
        self.delete_button = QPushButton(tr("MainWindow", "Delete"))
        self.delete_button.setProperty("secondary", True)
        self.delete_button.clicked.connect(self._delete_workspace)
        self.refresh_button = QPushButton(tr("MainWindow", "Refresh"))
        self.refresh_button.setProperty("secondary", True)
        self.refresh_button.clicked.connect(self._refresh_current)
        actions.addWidget(self.add_button)
        actions.addWidget(self.delete_button)
        actions.addWidget(self.refresh_button)

        self.workspace_list = QListWidget()
        self.workspace_list.currentRowChanged.connect(self._on_workspace_selected)

        sidebar_layout.addWidget(self.sidebar_eyebrow)
        sidebar_layout.addWidget(self.sidebar_title)
        sidebar_layout.addWidget(self.sidebar_subtitle)
        sidebar_layout.addLayout(language_row)
        sidebar_layout.addLayout(actions)
        sidebar_layout.addWidget(self.workspace_list, 1)

        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(22, 22, 22, 22)
        panel_layout.setSpacing(16)

        self.header_title = QLabel(tr("MainWindow", "Add a workspace to get started"))
        self.header_title.setObjectName("Title")
        self.header_title.setStyleSheet("font-size:24px;")
        self.header_meta = QLabel(
            tr("MainWindow", "Add a workspace on the left, then configure public access and authentication.")
        )
        self.header_meta.setStyleSheet("color:#667085; font-size:13px;")

        header_actions = QHBoxLayout()
        self.start_button = QPushButton(tr("MainWindow", "Start"))
        self.start_button.clicked.connect(self._start_runtime)
        self.stop_button = QPushButton(tr("MainWindow", "Stop"))
        self.stop_button.setProperty("secondary", True)
        self.stop_button.clicked.connect(self._stop_runtime)
        self.copy_button = QPushButton(tr("MainWindow", "Copy MCP URL"))
        self.copy_button.setProperty("secondary", True)
        self.copy_button.clicked.connect(self._copy_endpoint)
        self.copy_frp_button = QPushButton(tr("MainWindow", "Copy FRP snippet"))
        self.copy_frp_button.setProperty("secondary", True)
        self.copy_frp_button.clicked.connect(self._copy_frp_snippet)
        header_actions.addWidget(self.start_button)
        header_actions.addWidget(self.stop_button)
        header_actions.addWidget(self.copy_button)
        header_actions.addWidget(self.copy_frp_button)
        header_actions.addStretch(1)

        content = QGridLayout()
        content.setHorizontalSpacing(16)
        content.setVerticalSpacing(16)

        self.workspace_group = self._build_workspace_group()
        self.runtime_group = self._build_runtime_group()
        self.auth_group = self._build_auth_group()
        self.log_group = self._build_log_group()

        content.addWidget(self.workspace_group, 0, 0)
        content.addWidget(self.runtime_group, 0, 1)
        content.addWidget(self.auth_group, 1, 0)
        content.addWidget(self.log_group, 1, 1)
        content.setColumnStretch(0, 1)
        content.setColumnStretch(1, 1)

        panel_layout.addWidget(self.header_title)
        panel_layout.addWidget(self.header_meta)
        panel_layout.addLayout(header_actions)
        panel_layout.addLayout(content, 1)

        layout.addWidget(sidebar, 1)
        layout.addWidget(panel, 2)
        self.setCentralWidget(root)
        self._wire_live_updates()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API name
        if self._runtime_thread is not None and self._runtime_thread.isRunning():
            QMessageBox.information(
                self,
                tr("MainWindow", "Operation in progress"),
                tr("MainWindow", "Wait for the current start or stop operation to finish before closing."),
            )
            event.ignore()
            return
        super().closeEvent(event)

    def _build_workspace_group(self) -> QGroupBox:
        box = QGroupBox(tr("MainWindow", "Workspace and public access"))
        self.workspace_form = QFormLayout(box)

        self.name_edit = QLineEdit()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.tunnel_type = QComboBox()
        self._fill_combo(self.tunnel_type, self.TUNNEL_OPTIONS)
        self.tunnel_type.currentIndexChanged.connect(self._refresh_tunnel_fields)

        self.public_url_label = QLabel(tr("MainWindow", "Public URL"))
        self.public_url_edit = QLineEdit()
        self.public_url_edit.setPlaceholderText(
            tr("MainWindow", "Cloudflare assigns a public URL after startup")
        )
        self.cloudflare_mode_label = QLabel(tr("MainWindow", "Cloudflare mode"))
        self.cloudflare_mode = QComboBox()
        self._fill_combo(self.cloudflare_mode, self.CLOUDFLARE_MODE_OPTIONS)
        self.cloudflare_mode.currentIndexChanged.connect(self._refresh_tunnel_fields)
        self.cloudflare_token_label = QLabel(tr("MainWindow", "Tunnel Token"))
        self.cloudflare_token_edit = QLineEdit()
        self.cloudflare_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.cloudflare_token_edit.setPlaceholderText(
            tr("MainWindow", "Enter the Cloudflare Tunnel Token for fixed-domain mode")
        )

        self.frp_server_label = QLabel(tr("MainWindow", "FRP server domain"))
        self.frp_server_edit = QLineEdit()
        self.frp_server_edit.setPlaceholderText(tr("MainWindow", "Example: frp.example.com"))

        self.subdomain_label = QLabel(tr("MainWindow", "FRP subdomain"))
        self.subdomain_edit = QLineEdit()
        self.subdomain_edit.setPlaceholderText(tr("MainWindow", "Example: mcp"))

        self.workspace_form.addRow(tr("MainWindow", "Name"), self.name_edit)
        self.workspace_form.addRow(tr("MainWindow", "Workspace path"), self.path_edit)
        self.workspace_form.addRow(tr("MainWindow", "Tunnel type"), self.tunnel_type)
        self.workspace_form.addRow(self.cloudflare_mode_label, self.cloudflare_mode)
        self.workspace_form.addRow(self.public_url_label, self.public_url_edit)
        self.workspace_form.addRow(self.cloudflare_token_label, self.cloudflare_token_edit)
        self.workspace_form.addRow(self.frp_server_label, self.frp_server_edit)
        self.workspace_form.addRow(self.subdomain_label, self.subdomain_edit)

        self.endpoint_hint = QLabel(tr("MainWindow", "Current endpoint: -"))
        self.endpoint_hint.setWordWrap(True)
        self.endpoint_hint.setStyleSheet("color:#667085;")
        self.workspace_form.addRow(tr("MainWindow", "Current endpoint"), self.endpoint_hint)

        self.save_button = QPushButton(tr("MainWindow", "Save configuration"))
        self.save_button.clicked.connect(self._save_current)
        self.workspace_form.addRow(self.save_button)
        return box

    def _build_runtime_group(self) -> QGroupBox:
        box = QGroupBox(tr("MainWindow", "Runtime"))
        self.runtime_form = QFormLayout(box)

        self.local_port = QSpinBox()
        self.local_port.setMaximum(65535)
        self.local_port.setMinimum(1000)

        self.permission_mode = QComboBox()
        self._fill_combo(self.permission_mode, self.PERMISSION_MODE_OPTIONS)

        self.runtime_command = QLineEdit()
        self.runtime_command.setPlaceholderText(
            tr("MainWindow", "Optional, for example: coding-tools-mcp")
        )

        self.status_label = QLabel(tr("MainWindow", "Not started"))
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-weight:700; color:#b42318;")

        self.runtime_form.addRow(tr("MainWindow", "Local port"), self.local_port)
        self.runtime_form.addRow(tr("MainWindow", "Permission mode"), self.permission_mode)
        self.runtime_form.addRow(tr("MainWindow", "Custom command"), self.runtime_command)
        self.runtime_form.addRow(tr("MainWindow", "Status"), self.status_label)
        return box

    def _build_auth_group(self) -> QGroupBox:
        box = QGroupBox(tr("MainWindow", "Authentication and ChatGPT setup"))
        layout = QVBoxLayout(box)

        self.auth_form = QFormLayout()
        self.auth_type = QComboBox()
        self._fill_combo(self.auth_type, self.AUTH_OPTIONS)
        self.auth_type.currentIndexChanged.connect(self._refresh_auth_fields)

        self.oauth_password_label = QLabel(tr("MainWindow", "Authorization password"))
        self.oauth_password = QLineEdit()
        self.oauth_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.oauth_password.setPlaceholderText(
            tr("MainWindow", "Enter this password during the first ChatGPT authorization")
        )

        self.bearer_token_label = QLabel(tr("MainWindow", "Bearer Token"))
        self.bearer_token = QLineEdit()
        self.bearer_token.setEchoMode(QLineEdit.EchoMode.Password)

        self.auth_form.addRow(tr("MainWindow", "Authentication type"), self.auth_type)
        self.auth_form.addRow(self.oauth_password_label, self.oauth_password)
        self.auth_form.addRow(self.bearer_token_label, self.bearer_token)
        layout.addLayout(self.auth_form)

        self.oauth_actions = QWidget()
        oauth_actions_layout = QHBoxLayout(self.oauth_actions)
        oauth_actions_layout.setContentsMargins(0, 0, 0, 0)
        oauth_actions_layout.setSpacing(10)
        self.copy_oauth_password_button = QPushButton(tr("MainWindow", "Copy authorization password"))
        self.copy_oauth_password_button.setProperty("secondary", True)
        self.copy_oauth_password_button.clicked.connect(self._copy_oauth_password)
        oauth_actions_layout.addWidget(self.copy_oauth_password_button)
        oauth_actions_layout.addStretch(1)
        layout.addWidget(self.oauth_actions)

        self.bearer_actions = QWidget()
        bearer_actions_layout = QHBoxLayout(self.bearer_actions)
        bearer_actions_layout.setContentsMargins(0, 0, 0, 0)
        bearer_actions_layout.setSpacing(10)
        self.copy_bearer_button = QPushButton(tr("MainWindow", "Copy Bearer Token"))
        self.copy_bearer_button.setProperty("secondary", True)
        self.copy_bearer_button.clicked.connect(self._copy_bearer_token)
        bearer_actions_layout.addWidget(self.copy_bearer_button)
        bearer_actions_layout.addStretch(1)
        layout.addWidget(self.bearer_actions)

        self.auth_hint = QLabel(
            tr(
                "MainWindow",
                "In OAuth mode, the MCP client registers automatically. Use the authorization password "
                "during the first authorization.",
            )
        )
        self.auth_hint.setWordWrap(True)
        self.auth_hint.setStyleSheet("color:#667085;")
        layout.addWidget(self.auth_hint)
        return box

    def _build_log_group(self) -> QGroupBox:
        box = QGroupBox(tr("MainWindow", "Logs and URLs"))
        layout = QVBoxLayout(box)
        self.endpoint_label = QLabel(tr("MainWindow", "Public MCP URL: -"))
        self.local_label = QLabel(tr("MainWindow", "Local MCP URL: -"))
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMinimumHeight(220)
        layout.addWidget(self.endpoint_label)
        layout.addWidget(self.local_label)
        layout.addWidget(self.log_output)
        return box

    def _wire_live_updates(self) -> None:
        for widget in (
            self.name_edit,
            self.public_url_edit,
            self.cloudflare_token_edit,
            self.frp_server_edit,
            self.subdomain_edit,
            self.runtime_command,
            self.oauth_password,
            self.bearer_token,
        ):
            widget.textChanged.connect(self._refresh_connection_view)
        self.local_port.valueChanged.connect(self._refresh_connection_view)
        self.tunnel_type.currentIndexChanged.connect(self._refresh_connection_view)
        self.auth_type.currentIndexChanged.connect(self._refresh_connection_view)

    def _populate_language_combo(self) -> None:
        self.language_combo.blockSignals(True)
        self.language_combo.clear()
        for code, label in self.language_manager.language_options():
            self.language_combo.addItem(label, code)
        self._set_combo_value(self.language_combo, self.language_manager.configured_language)
        self.language_combo.blockSignals(False)

    def _on_language_selected(self, _index: int) -> None:
        language = self._combo_value(self.language_combo)
        self.language_manager.set_language(language)

    def _on_language_changed(self, _language: str) -> None:
        self._retranslate_ui()

    def _retranslate_ui(self) -> None:
        self.setWindowTitle(tr("MainWindow", "Coding Tools MCP Desktop"))
        self.sidebar_eyebrow.setText(tr("MainWindow", "Workspace console"))
        self.sidebar_title.setText(tr("MainWindow", "MCP Desktop Client"))
        self.sidebar_subtitle.setText(
            tr("MainWindow", "Manage public access, authentication, and local runtime state by workspace.")
        )
        self.language_label.setText(tr("MainWindow", "Language"))
        self._populate_language_combo()

        self.add_button.setText(tr("MainWindow", "Add workspace"))
        self.delete_button.setText(tr("MainWindow", "Delete"))
        self.refresh_button.setText(tr("MainWindow", "Refresh"))
        self.start_button.setText(tr("MainWindow", "Start"))
        self.stop_button.setText(tr("MainWindow", "Stop"))
        self.copy_button.setText(tr("MainWindow", "Copy MCP URL"))
        self.copy_frp_button.setText(tr("MainWindow", "Copy FRP snippet"))

        self.workspace_group.setTitle(tr("MainWindow", "Workspace and public access"))
        self.runtime_group.setTitle(tr("MainWindow", "Runtime"))
        self.auth_group.setTitle(tr("MainWindow", "Authentication and ChatGPT setup"))
        self.log_group.setTitle(tr("MainWindow", "Logs and URLs"))

        self._set_form_label(self.workspace_form, self.name_edit, tr("MainWindow", "Name"))
        self._set_form_label(self.workspace_form, self.path_edit, tr("MainWindow", "Workspace path"))
        self._set_form_label(self.workspace_form, self.tunnel_type, tr("MainWindow", "Tunnel type"))
        self.cloudflare_mode_label.setText(tr("MainWindow", "Cloudflare mode"))
        self.public_url_label.setText(tr("MainWindow", "Public URL"))
        self.cloudflare_token_label.setText(tr("MainWindow", "Tunnel Token"))
        self.frp_server_label.setText(tr("MainWindow", "FRP server domain"))
        self.subdomain_label.setText(tr("MainWindow", "FRP subdomain"))
        self._set_form_label(
            self.workspace_form,
            self.endpoint_hint,
            tr("MainWindow", "Current endpoint"),
        )
        self.save_button.setText(tr("MainWindow", "Save configuration"))

        self._set_form_label(self.runtime_form, self.local_port, tr("MainWindow", "Local port"))
        self._set_form_label(
            self.runtime_form,
            self.permission_mode,
            tr("MainWindow", "Permission mode"),
        )
        self._set_form_label(
            self.runtime_form,
            self.runtime_command,
            tr("MainWindow", "Custom command"),
        )
        self._set_form_label(self.runtime_form, self.status_label, tr("MainWindow", "Status"))

        self._set_form_label(self.auth_form, self.auth_type, tr("MainWindow", "Authentication type"))
        self.oauth_password_label.setText(tr("MainWindow", "Authorization password"))
        self.bearer_token_label.setText(tr("MainWindow", "Bearer Token"))
        self.copy_oauth_password_button.setText(tr("MainWindow", "Copy authorization password"))
        self.copy_bearer_button.setText(tr("MainWindow", "Copy Bearer Token"))

        self.runtime_command.setPlaceholderText(
            tr("MainWindow", "Optional, for example: coding-tools-mcp")
        )
        self.cloudflare_token_edit.setPlaceholderText(
            tr("MainWindow", "Enter the Cloudflare Tunnel Token for fixed-domain mode")
        )
        self.frp_server_edit.setPlaceholderText(tr("MainWindow", "Example: frp.example.com"))
        self.subdomain_edit.setPlaceholderText(tr("MainWindow", "Example: mcp"))
        self.oauth_password.setPlaceholderText(
            tr("MainWindow", "Enter this password during the first ChatGPT authorization")
        )

        self._retranslate_combo(self.tunnel_type, self.TUNNEL_OPTIONS)
        self._retranslate_combo(self.cloudflare_mode, self.CLOUDFLARE_MODE_OPTIONS)
        self._retranslate_combo(self.auth_type, self.AUTH_OPTIONS)
        self._retranslate_combo(self.permission_mode, self.PERMISSION_MODE_OPTIONS)

        if self.current_profile is None:
            self.header_title.setText(tr("MainWindow", "Add a workspace to get started"))
            self.header_meta.setText(
                tr(
                    "MainWindow",
                    "Add a workspace on the left, then configure public access and authentication.",
                )
            )
            self.status_label.setText(tr("MainWindow", "Not started"))
            self.log_output.setPlainText(tr("MainWindow", "No logs are available yet."))
        else:
            self._render_status(self.runtime.status(self.current_profile))
            self._load_logs(self.current_profile)
        for profile in self.profiles:
            self._refresh_workspace_item(profile.id)
        self._refresh_tunnel_fields()
        self._refresh_auth_fields()
        self._refresh_connection_view()

    def _populate_workspace_list(self) -> None:
        self.workspace_list.clear()
        for profile in self.profiles:
            item = QListWidgetItem(self._workspace_summary(profile))
            item.setData(Qt.ItemDataRole.UserRole, profile.id)
            self.workspace_list.addItem(item)

    def _on_workspace_selected(self, row: int) -> None:
        if row < 0 or row >= len(self.profiles):
            self.current_profile = None
            self._clear_panel()
            return
        self.current_profile = self.profiles[row]
        self._load_profile(self.current_profile)

    def _load_profile(self, profile: WorkspaceProfile) -> None:
        self.header_title.setText(profile.name)
        self.header_meta.setText(profile.path)
        self.name_edit.setText(profile.name)
        self.path_edit.setText(profile.path)
        self._set_combo_value(self.cloudflare_mode, profile.tunnel.cloudflare_mode)
        self.public_url_edit.setText(self._profile_public_url_for_edit(profile))
        self.cloudflare_token_edit.setText(profile.tunnel.cloudflare_token)
        self.frp_server_edit.setText(profile.tunnel.frp_server)
        self.subdomain_edit.setText(profile.tunnel.frp_subdomain)
        self._set_combo_value(self.tunnel_type, profile.tunnel.type)
        self.local_port.setValue(profile.runtime.local_port)
        self._set_combo_value(self.permission_mode, profile.runtime.permission_mode)
        self.runtime_command.setText(profile.runtime.runtime_command)
        self._set_combo_value(self.auth_type, profile.auth.type)
        self.oauth_password.setText(profile.auth.oauth_password)
        self.bearer_token.setText(profile.auth.bearer_token)
        self._set_panel_enabled(True)
        status = self.runtime.status(profile)
        self._render_status(status)
        self._load_logs(profile)
        self._refresh_tunnel_fields()
        self._refresh_auth_fields()
        self._refresh_connection_view()

    def _clear_panel(self) -> None:
        self.header_title.setText(tr("MainWindow", "Add a workspace to get started"))
        self.header_meta.setText(
            tr("MainWindow", "Add a workspace on the left, then configure public access and authentication.")
        )
        self.name_edit.clear()
        self.path_edit.clear()
        self.public_url_edit.clear()
        self.cloudflare_token_edit.clear()
        self.frp_server_edit.clear()
        self.subdomain_edit.clear()
        self.oauth_password.clear()
        self.bearer_token.clear()
        self.runtime_command.clear()
        self.local_port.setValue(28766)
        self._set_combo_value(self.tunnel_type, "frp")
        self._set_combo_value(self.cloudflare_mode, "quick")
        self._set_combo_value(self.permission_mode, "trusted")
        self._set_combo_value(self.auth_type, "oauth")
        self.status_label.setText(tr("MainWindow", "Not started"))
        self.status_label.setStyleSheet("font-weight:700; color:#b42318;")
        self.endpoint_label.setText(tr("MainWindow", "Public MCP URL: -"))
        self.local_label.setText(tr("MainWindow", "Local MCP URL: -"))
        self.endpoint_hint.setText(tr("MainWindow", "Current endpoint: -"))
        self.log_output.setPlainText(tr("MainWindow", "No logs are available yet."))
        self._refresh_tunnel_fields()
        self._refresh_auth_fields()
        self._set_panel_enabled(False)

    def _set_panel_enabled(self, enabled: bool) -> None:
        for widget in (
            self.workspace_group,
            self.runtime_group,
            self.auth_group,
            self.log_group,
            self.start_button,
            self.stop_button,
            self.copy_button,
            self.copy_frp_button,
            self.delete_button,
            self.refresh_button,
        ):
            widget.setEnabled(enabled)

    def _save_current(self) -> bool:
        profile = self._require_profile()
        draft = deepcopy(profile)
        self._update_profile_from_form(draft)

        status = self.runtime.status(profile)
        runtime_settings_changed = (
            draft.tunnel != profile.tunnel
            or draft.auth != profile.auth
            or draft.runtime != profile.runtime
        )
        if status.pid is not None and runtime_settings_changed:
            QMessageBox.warning(
                self,
                tr("MainWindow", "Stop the runtime first"),
                tr(
                    "MainWindow",
                    "The tunnel, authentication, or runtime configuration has changed. Stop the current runtime "
                    "before saving these settings.",
                ),
            )
            self._load_profile(profile)
            return False

        profile.name = draft.name
        profile.tunnel = draft.tunnel
        profile.auth = draft.auth
        profile.runtime = draft.runtime
        save_profiles(self.profiles)
        self._populate_workspace_list()
        self._restore_selection(profile.id)
        return True

    def _update_profile_from_form(self, profile: WorkspaceProfile) -> None:
        profile.name = self.name_edit.text().strip() or tr("MainWindow", "Workspace")
        profile.tunnel.type = self._combo_value(self.tunnel_type)
        profile.tunnel.cloudflare_mode = self._combo_value(self.cloudflare_mode)
        profile.tunnel.cloudflare_token = self.cloudflare_token_edit.text().strip()
        if profile.tunnel.type == "cloudflare" and profile.tunnel.cloudflare_mode == "named":
            profile.tunnel.public_url = self.public_url_edit.text().strip()
        else:
            profile.tunnel.public_url = ""
        profile.tunnel.frp_server = self.frp_server_edit.text().strip()
        profile.tunnel.frp_subdomain = self.subdomain_edit.text().strip()
        profile.runtime.local_port = self.local_port.value()
        profile.runtime.permission_mode = self._combo_value(self.permission_mode)
        profile.runtime.runtime_command = self.runtime_command.text().strip()
        profile.auth.type = self._combo_value(self.auth_type)
        profile.auth.oauth_password = self.oauth_password.text().strip()
        profile.auth.bearer_token = self.bearer_token.text().strip()

    def _add_workspace(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, tr("MainWindow", "Select workspace directory"))
        if not directory:
            return
        profile = build_profile(directory)
        self.profiles.append(profile)
        save_profiles(self.profiles)
        self._populate_workspace_list()
        self.workspace_list.setCurrentRow(len(self.profiles) - 1)

    def _delete_workspace(self) -> None:
        profile = self._require_profile()
        answer = QMessageBox.question(
            self,
            tr("MainWindow", "Delete workspace"),
            tr(
                "MainWindow",
                'Delete workspace "{name}"?\nThis removes it from the desktop client but does not delete the directory.',
            ).format(name=profile.name),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.runtime.stop(profile)
        current_index = self.workspace_list.currentRow()
        self.profiles = [item for item in self.profiles if item.id != profile.id]
        save_profiles(self.profiles)
        self.current_profile = None
        self._populate_workspace_list()
        if self.profiles:
            self.workspace_list.setCurrentRow(min(current_index, len(self.profiles) - 1))
        else:
            self._clear_panel()

    def _start_runtime(self) -> None:
        profile = self._require_profile()
        if not self._save_current():
            return
        self._set_runtime_busy(True, "starting")
        if not self._run_runtime_job(profile, "start"):
            self._set_runtime_busy(False)

    def _stop_runtime(self) -> None:
        profile = self._require_profile()
        self._set_runtime_busy(True, "stopping")
        if not self._run_runtime_job(profile, "stop"):
            self._set_runtime_busy(False)

    def _copy_endpoint(self) -> None:
        if not self._save_current():
            return
        profile = self._require_profile()
        endpoint = self.runtime.resolved_endpoint(profile) or self._draft_endpoint()
        QApplication.clipboard().setText(endpoint)
        self.statusBar().showMessage(tr("MainWindow", "MCP URL copied to the clipboard"), 3000)

    def _copy_frp_snippet(self) -> None:
        if not self._save_current():
            return
        profile = self._require_profile()
        QApplication.clipboard().setText(profile.frp_proxy_snippet())
        self.statusBar().showMessage(tr("MainWindow", "FRP proxy snippet copied to the clipboard"), 3000)

    def _copy_oauth_password(self) -> None:
        if not self._save_current():
            return
        QApplication.clipboard().setText(self.oauth_password.text().strip())
        self.statusBar().showMessage(tr("MainWindow", "Authorization password copied to the clipboard"), 3000)

    def _copy_bearer_token(self) -> None:
        if not self._save_current():
            return
        QApplication.clipboard().setText(self.bearer_token.text().strip())
        self.statusBar().showMessage(tr("MainWindow", "Bearer Token copied to the clipboard"), 3000)

    def _refresh_current(self) -> None:
        if self.current_profile is None:
            return
        self._load_profile(self.current_profile)

    def _refresh_tunnel_fields(self, *_args: object) -> None:
        tunnel_type = self._combo_value(self.tunnel_type)
        is_frp = tunnel_type == "frp"
        is_cloudflare = tunnel_type == "cloudflare"
        is_cloudflare_named = is_cloudflare and self._combo_value(self.cloudflare_mode) == "named"
        self._set_row_visible(self.cloudflare_mode_label, self.cloudflare_mode, is_cloudflare)
        self._set_row_visible(self.public_url_label, self.public_url_edit, is_cloudflare)
        self._set_row_visible(self.cloudflare_token_label, self.cloudflare_token_edit, is_cloudflare_named)
        self._set_row_visible(self.frp_server_label, self.frp_server_edit, is_frp)
        self._set_row_visible(self.subdomain_label, self.subdomain_edit, is_frp)
        self.public_url_edit.setReadOnly(is_cloudflare and not is_cloudflare_named)
        self.copy_frp_button.setEnabled(is_frp and self.current_profile is not None)
        if is_cloudflare_named:
            self.public_url_edit.setPlaceholderText(tr("MainWindow", "Example: https://mcp.example.com"))
        elif is_cloudflare:
            self.public_url_edit.setPlaceholderText(
                tr("MainWindow", "Cloudflare assigns a public URL after startup")
            )
            if self.current_profile is not None and not self.runtime.resolved_public_url(self.current_profile):
                self.public_url_edit.setText("")
        self._refresh_connection_view()

    def _refresh_auth_fields(self, *_args: object) -> None:
        auth_type = self._combo_value(self.auth_type)
        is_oauth = auth_type == "oauth"
        is_bearer = auth_type == "bearer"
        self._set_row_visible(self.oauth_password_label, self.oauth_password, is_oauth)
        self._set_row_visible(self.bearer_token_label, self.bearer_token, is_bearer)
        self.oauth_actions.setVisible(is_oauth)
        self.bearer_actions.setVisible(is_bearer)
        if is_oauth:
            self.auth_hint.setText(
                tr(
                    "MainWindow",
                    "The MCP client registers automatically. Use the authorization password during the first "
                    "authorization.",
                )
            )
        elif is_bearer:
            self.auth_hint.setText(
                tr("MainWindow", "In Bearer mode, configure this token in the calling client.")
            )
        else:
            self.auth_hint.setText(
                tr(
                    "MainWindow",
                    "Authentication is disabled. Use this only for local debugging; do not expose it directly "
                    "to the public internet.",
                )
            )
        self._refresh_connection_view()

    def _refresh_connection_view(self, *_args: object) -> None:
        endpoint = self._draft_endpoint()
        self.endpoint_label.setText(
            tr("MainWindow", "Public MCP URL: {endpoint}").format(endpoint=endpoint)
        )
        self.local_label.setText(
            tr(
                "MainWindow",
                "Local MCP URL: http://127.0.0.1:{port}/mcp",
            ).format(port=self.local_port.value())
        )
        self.endpoint_hint.setText(
            tr("MainWindow", "Current endpoint: {endpoint}").format(endpoint=endpoint)
        )

    def _load_logs(self, profile: WorkspaceProfile) -> None:
        log_dir = log_dir_for_profile(profile.id)
        output: list[str] = []
        for name in ("cloudflared.log", "stderr.log", "stdout.log"):
            path = log_dir / name
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="replace")
                output.append(f"[{name}]\n{text[-4000:]}")
        self.log_output.setPlainText(
            "\n\n".join(output) if output else tr("MainWindow", "No logs are available yet.")
        )

    def _state_text(self, state: str) -> str:
        state_map = {
            "running": tr("MainWindow", "Running"),
            "stopped": tr("MainWindow", "Stopped"),
            "starting": tr("MainWindow", "Starting"),
            "stopping": tr("MainWindow", "Stopping"),
            "error": tr("MainWindow", "Error"),
        }
        return state_map.get(state, state)

    def _render_status(self, status) -> None:
        state_text = self._state_text(status.state)
        details = [f"{state_text}  PID={status.pid or '-'}", status.local_message]
        if status.public_message:
            details.append(tr("MainWindow", "Public: {message}").format(message=status.public_message))
        self.status_label.setText("\n".join(details))
        color = "#067647" if status.state == "running" else "#b42318"
        self.status_label.setStyleSheet(f"font-weight:700; color:{color};")
        if self._busy_action is None:
            self.start_button.setEnabled(status.pid is None)
            self.stop_button.setEnabled(status.pid is not None)

    def _run_runtime_job(self, profile: WorkspaceProfile, action: str) -> bool:
        if self._runtime_thread is not None:
            return False
        self._runtime_thread = QThread(self)
        self._runtime_job = RuntimeJob(self.runtime, profile, action)
        self._runtime_job.moveToThread(self._runtime_thread)
        self._runtime_thread.started.connect(self._runtime_job.run)
        self._runtime_job.finished.connect(self._on_runtime_job_finished)
        self._runtime_job.finished.connect(self._runtime_thread.quit)
        self._runtime_thread.finished.connect(self._cleanup_runtime_job)
        self._runtime_thread.start()
        return True

    def _on_runtime_job_finished(
        self,
        profile_id: str,
        action: str,
        status: object,
        error_message: str,
    ) -> None:
        profile = next((item for item in self.profiles if item.id == profile_id), None)
        self._set_runtime_busy(False)
        if profile is None:
            return
        if error_message:
            current_status = self.runtime.status(profile)
            if self.current_profile is not None and self.current_profile.id == profile_id:
                self._render_status(current_status)
                self._load_logs(profile)
            self._refresh_workspace_item(profile.id)
            QMessageBox.critical(
                self,
                tr("MainWindow", "Start failed") if action == "start" else tr("MainWindow", "Stop failed"),
                error_message,
            )
            return
        if self.current_profile is not None and self.current_profile.id == profile_id:
            if status is not None:
                self._render_status(status)
            self._sync_profile_runtime_view(profile)
            self._load_logs(profile)
        self._refresh_workspace_item(profile.id)

    def _cleanup_runtime_job(self) -> None:
        if self._runtime_job is not None:
            self._runtime_job.deleteLater()
            self._runtime_job = None
        if self._runtime_thread is not None:
            self._runtime_thread.deleteLater()
            self._runtime_thread = None

    def _set_runtime_busy(self, busy: bool, action: str | None = None) -> None:
        has_profile = self.current_profile is not None
        self.start_button.setEnabled(not busy and has_profile)
        self.stop_button.setEnabled(not busy and has_profile)
        self.workspace_list.setEnabled(not busy)
        self.add_button.setEnabled(not busy)
        self.delete_button.setEnabled(not busy and has_profile)
        self.refresh_button.setEnabled(not busy and has_profile)
        self.workspace_group.setEnabled(not busy and has_profile)
        self.runtime_group.setEnabled(not busy and has_profile)
        self.auth_group.setEnabled(not busy and has_profile)
        self.copy_button.setEnabled(not busy and has_profile)
        self.copy_frp_button.setEnabled(
            not busy and has_profile and self._combo_value(self.tunnel_type) == "frp"
        )
        self.language_combo.setEnabled(not busy)
        if busy:
            profile = self.current_profile
            self._busy_profile_id = profile.id if profile is not None else None
            self._busy_action = action
            self._busy_dots = 0
            action_text = self._busy_action_text(action)
            self.start_button.setText(
                tr("MainWindow", "Starting...") if action == "starting" else tr("MainWindow", "Start")
            )
            self.stop_button.setText(
                tr("MainWindow", "Stopping...") if action == "stopping" else tr("MainWindow", "Stop")
            )
            if action_text:
                self.status_label.setText(f"{action_text}  PID=-")
                self.status_label.setStyleSheet("font-weight:700; color:#b54708;")
            self.statusBar().showMessage(
                tr("MainWindow", "{action}. Please wait...").format(action=action_text),
                0,
            )
            if not self._busy_timer.isActive():
                self._busy_timer.start()
            if self._busy_profile_id:
                self._refresh_workspace_item(self._busy_profile_id)
            return
        self._busy_timer.stop()
        self._busy_profile_id = None
        self._busy_action = None
        self._busy_dots = 0
        self.start_button.setText(tr("MainWindow", "Start"))
        self.stop_button.setText(tr("MainWindow", "Stop"))
        self.statusBar().clearMessage()

    def _tick_busy_indicator(self) -> None:
        if self._busy_action is None:
            return
        self._busy_dots = (self._busy_dots + 1) % 4
        dots = "." * self._busy_dots
        label = f"{self._busy_action_text(self._busy_action)}{dots}"
        self.status_label.setText(f"{label}  PID=-")
        self.status_label.setStyleSheet("font-weight:700; color:#b54708;")
        if self._busy_profile_id:
            self._refresh_workspace_item(self._busy_profile_id)

    def _sync_profile_runtime_view(self, profile: WorkspaceProfile) -> None:
        if profile.tunnel.type == "cloudflare":
            public_url = self.runtime.resolved_public_url(profile)
            if public_url:
                self.public_url_edit.setText(public_url)
            elif profile.tunnel.cloudflare_mode != "named":
                self.public_url_edit.clear()
        self._refresh_connection_view()

    def _refresh_workspace_item(self, profile_id: str) -> None:
        for index, profile in enumerate(self.profiles):
            if profile.id != profile_id:
                continue
            item = self.workspace_list.item(index)
            if item is not None:
                item.setText(self._workspace_summary(profile))
            break

    def _draft_public_url(self) -> str:
        tunnel_type = self._combo_value(self.tunnel_type)
        if tunnel_type == "frp":
            subdomain = self.subdomain_edit.text().strip()
            server = self.frp_server_edit.text().strip()
            if subdomain and server:
                return f"https://{subdomain}.{server}"
        if tunnel_type == "cloudflare":
            if self.current_profile is not None:
                resolved = self.runtime.resolved_public_url(self.current_profile)
                if resolved:
                    return resolved
            if self._combo_value(self.cloudflare_mode) == "named":
                return self.public_url_edit.text().strip().rstrip("/")
            return ""
        return self.public_url_edit.text().strip().rstrip("/")

    def _draft_endpoint(self) -> str:
        base_url = self._draft_public_url().rstrip("/")
        if not base_url:
            return "-"
        return f"{base_url}{MCP_ENDPOINT_PATH}"

    def _workspace_summary(self, profile: WorkspaceProfile) -> str:
        state = self._workspace_state(profile)
        endpoint = self._profile_endpoint_summary(profile)
        return "\n".join(
            [
                profile.name,
                profile.path,
                tr(
                    "MainWindow",
                    "Tunnel: {tunnel}  Authentication: {auth}",
                ).format(
                    tunnel=self._label_for_value(self.TUNNEL_OPTIONS, profile.tunnel.type),
                    auth=self._label_for_value(self.AUTH_OPTIONS, profile.auth.type),
                ),
                tr(
                    "MainWindow",
                    "Status: {status}  URL: {endpoint}",
                ).format(status=self._state_text(state), endpoint=endpoint or "-"),
            ]
        )

    def _restore_selection(self, profile_id: str) -> None:
        for index, profile in enumerate(self.profiles):
            if profile.id == profile_id:
                self.workspace_list.setCurrentRow(index)
                return

    def _require_profile(self) -> WorkspaceProfile:
        if self.current_profile is None:
            raise RuntimeError(tr("MainWindow", "No workspace is currently selected."))
        return self.current_profile

    def _fill_combo(self, combo: QComboBox, options: list[tuple[str, str]]) -> None:
        for value, label in options:
            combo.addItem(self._translate_option_label(label), value)

    def _retranslate_combo(self, combo: QComboBox, options: list[tuple[str, str]]) -> None:
        for index, (_value, label) in enumerate(options):
            combo.setItemText(index, self._translate_option_label(label))

    def _combo_value(self, combo: QComboBox) -> str:
        return str(combo.currentData())

    def _set_combo_value(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _label_for_value(self, options: list[tuple[str, str]], value: str) -> str:
        for item_value, item_label in options:
            if item_value == value:
                return self._translate_option_label(item_label)
        return value

    def _translate_option_label(self, source: str) -> str:
        translations = {
            "FRP (externally managed)": tr("MainWindow", "FRP (externally managed)"),
            "Cloudflare": tr("MainWindow", "Cloudflare"),
            "Quick tunnel": tr("MainWindow", "Quick tunnel"),
            "Fixed domain": tr("MainWindow", "Fixed domain"),
            "OAuth": tr("MainWindow", "OAuth"),
            "Bearer Token": tr("MainWindow", "Bearer Token"),
            "No authentication": tr("MainWindow", "No authentication"),
            "Trusted": tr("MainWindow", "Trusted"),
            "Safe": tr("MainWindow", "Safe"),
            "Unrestricted": tr("MainWindow", "Unrestricted"),
        }
        return translations.get(source, source)

    def _set_form_label(self, form: QFormLayout, field: QWidget, text: str) -> None:
        label = form.labelForField(field)
        if isinstance(label, QLabel):
            label.setText(text)

    def _busy_action_text(self, action: str | None) -> str:
        if action == "starting":
            return tr("MainWindow", "Starting")
        if action == "stopping":
            return tr("MainWindow", "Stopping")
        return ""

    def _set_row_visible(self, label: QLabel, field: QWidget, visible: bool) -> None:
        label.setVisible(visible)
        field.setVisible(visible)

    def _profile_public_url_for_edit(self, profile: WorkspaceProfile) -> str:
        if profile.tunnel.type == "frp":
            return profile.tunnel.public_url
        resolved = self.runtime.resolved_public_url(profile)
        if resolved:
            return resolved
        if profile.tunnel.cloudflare_mode == "named":
            return profile.tunnel.public_url
        return ""

    def _profile_endpoint_summary(self, profile: WorkspaceProfile) -> str:
        endpoint = self.runtime.resolved_endpoint(profile)
        if endpoint:
            return endpoint
        if profile.tunnel.type == "frp":
            return profile.endpoint
        if profile.tunnel.type == "cloudflare" and profile.tunnel.cloudflare_mode == "named" and profile.tunnel.public_url.strip():
            return f"{profile.tunnel.public_url.rstrip('/')}{MCP_ENDPOINT_PATH}"
        return "-"

    def _workspace_state(self, profile: WorkspaceProfile) -> str:
        if self._busy_profile_id == profile.id and self._busy_action == "starting":
            return "starting"
        if self._busy_profile_id == profile.id and self._busy_action == "stopping":
            return "stopping"
        return self.runtime.summary_state(profile)


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    language_manager = LanguageManager(app)
    window = MainWindow(language_manager)
    window.show()

    def _present_window() -> None:
        screen = app.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            frame = window.frameGeometry()
            frame.moveCenter(available.center())
            window.move(frame.topLeft())
        window.setWindowState((window.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        window.raise_()
        window.activateWindow()

    QTimer.singleShot(0, _present_window)
    return app.exec()
