import html

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout,
)

STATUS_COLOURS = {"ok": "#2e7d32", "warning": "#e65100", "error": "#b71c1c"}


class ProviderKeys:
    """Key fields, «Проверить» button and check result for one provider."""

    def __init__(self, provider):
        self.provider = provider
        self.box = QGroupBox(provider.SOURCE)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText(provider.TOKEN_PLACEHOLDER)
        self.token_edit.setClearButtonEnabled(True)
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setPlaceholderText("Только если в Кабинете включена подпись запросов")
        self.secret_edit.setClearButtonEnabled(True)
        self.check_button = QPushButton("Проверить")
        self.check_button.setAutoDefault(False)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        link = QLabel(f'<a href="{provider.TOKEN_URL}">Где взять</a>')
        link.setOpenExternalLinks(True)
        link.setToolTip(provider.TOKEN_HINT)
        form = QFormLayout(self.box)
        form.addRow(f"{provider.TOKEN_LABEL}:", self.token_edit)
        if provider.NEEDS_SECRET:
            form.addRow("Секрет подписи:", self.secret_edit)
        row = QHBoxLayout()
        row.addWidget(link)
        row.addStretch()
        row.addWidget(self.check_button)
        form.addRow(row)
        form.addRow(self.status_label)

    def values(self):
        return self.token_edit.text().strip(), self.secret_edit.text().strip()

    def set_status(self, message, level="ok"):
        colour = STATUS_COLOURS[level]
        self.status_label.setText(f'<span style="color:{colour};">{html.escape(message)}</span>')


class KeysDialog(QDialog):
    check_requested = pyqtSignal(str)  # provider id

    def __init__(self, providers, credentials, remember, parent=None):
        """providers: providers that need a key; credentials: {provider id: [token, secret]}."""
        super().__init__(parent)
        self.setWindowTitle("Поиск адреса — ключи")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        intro = QLabel("Впишите ключи сервисов и нажмите «Проверить» — модуль сделает один пробный запрос. "
                       "OpenStreetMap работает без ключа.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.sections = {}
        for provider in providers:
            section = ProviderKeys(provider)
            token, secret = credentials.get(provider.ID, ["", ""])
            section.token_edit.setText(token)
            section.secret_edit.setText(secret)
            section.check_button.clicked.connect(lambda _=False, pid=provider.ID: self.check_requested.emit(pid))
            layout.addWidget(section.box)
            self.sections[provider.ID] = section
        self.remember = QCheckBox("Хранить ключи на этом компьютере (в настройках QGIS, открытым текстом)")
        self.remember.setChecked(remember)
        layout.addWidget(self.remember)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def focus(self, provider_id):
        section = self.sections.get(provider_id)
        if section:
            section.token_edit.setFocus()

    def credentials(self):
        return {pid: list(section.values()) for pid, section in self.sections.items()}
