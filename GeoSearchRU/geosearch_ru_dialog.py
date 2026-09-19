import html

from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

STATUS_COLOURS = {"ok": "#2e7d32", "warning": "#e65100", "error": "#b71c1c"}


class GeoSearchDialog(QDialog):
    def __init__(self, providers, parent=None):
        """providers: [(id, title), …] for the source selector."""
        super().__init__(parent)
        self.setWindowTitle("Поиск адреса")
        self.setMinimumWidth(560)
        self.provider_combo = QComboBox()
        for provider_id, title in providers:
            self.provider_combo.addItem(title, provider_id)
        self.attribution_label = QLabel()
        self.attribution_label.setWordWrap(True)
        self.attribution_label.setOpenExternalLinks(True)
        # Wrapped labels do not grow the dialog, so reserve their lines up front.
        line = self.attribution_label.fontMetrics().lineSpacing()
        self.attribution_label.setMinimumHeight(line * 3 + 4)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setPlaceholderText("Секрет подписи — если в Кабинете включена подпись запросов")
        self.remember_token = QCheckBox("Запомнить ключи на этом компьютере")
        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("Например: г. Тамбов, пр. Энергетиков, 7")
        self.address_edit.setClearButtonEnabled(True)
        self.search_button = QPushButton("Найти")
        self.search_button.setDefault(True)
        self.results_label = QLabel()
        self.results_list = QListWidget()
        self.results_list.setMinimumHeight(110)
        self.normalized_address = QTextBrowser()
        self.normalized_address.setMinimumHeight(line * 7 + 20)  # address, blank, header and four value lines
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(line * 2 + 4)
        self.clear_button = QPushButton("Очистить маркер")
        self.clear_button.setAutoDefault(False)
        self.add_point_button = QPushButton("Добавить точку во временный слой")
        self.add_point_button.setAutoDefault(False)
        self.add_point_button.setToolTip("Добавить выбранный адрес точкой во временный слой «Найденные адреса»")
        self.token_label = token_label = QLabel()
        token_label.setOpenExternalLinks(True)
        form = QFormLayout()
        form.addRow("Источник:", self.provider_combo)
        # Key and secret share one form row: a hidden QFormLayout row still leaves a gap in Qt5.
        keys = QWidget()
        keys_layout = QVBoxLayout(keys)
        keys_layout.setContentsMargins(0, 0, 0, 0)
        keys_layout.addWidget(self.token_edit)
        keys_layout.addWidget(self.secret_edit)
        self.keys_widget = keys
        form.addRow(token_label, keys)
        form.addRow("", self.remember_token)
        form.addRow("Адрес:", self.address_edit)
        search_row = QHBoxLayout()
        search_row.addStretch()
        search_row.addWidget(self.search_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.addButton(self.clear_button, QDialogButtonBox.ButtonRole.ResetRole)
        buttons.addButton(self.add_point_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(search_row)
        layout.addWidget(self.results_label)
        layout.addWidget(self.results_list)
        layout.addWidget(QLabel("Нормализованный адрес и координаты:"))
        layout.addWidget(self.normalized_address)
        layout.addWidget(self.status_label)
        layout.addWidget(self.attribution_label)
        layout.addWidget(buttons)
        self.address_edit.returnPressed.connect(self.search_button.click)
        self.clear_results()

    def set_provider(self, provider_id):
        index = self.provider_combo.findData(provider_id)
        if index >= 0:
            self.provider_combo.setCurrentIndex(index)

    def show_provider(self, provider):
        """Show the key fields, their link and the attribution the provider needs."""
        for widget in (self.token_label, self.keys_widget, self.remember_token):
            widget.setVisible(provider.NEEDS_TOKEN)
        self.secret_edit.setVisible(provider.NEEDS_SECRET)
        if provider.NEEDS_TOKEN:
            self.token_label.setText(f'<a href="{provider.TOKEN_URL}">{provider.TOKEN_LABEL}</a>:')
            self.token_label.setToolTip(provider.TOKEN_HINT)
            self.token_edit.setPlaceholderText(provider.TOKEN_PLACEHOLDER)
        self.attribution_label.setText(provider.ATTRIBUTION or "")
        self.attribution_label.setVisible(bool(provider.ATTRIBUTION))

    def set_results(self, labels):
        self.results_list.clear()
        self.results_list.addItems(labels)
        for row, label in enumerate(labels):
            self.results_list.item(row).setToolTip(label)
        self.results_label.setText(f"Варианты ({len(labels)}):")
        if labels:
            self.results_list.setCurrentRow(0)

    def clear_results(self):
        self.results_list.clear()
        self.add_point_button.setEnabled(False)
        self.results_label.setText("Варианты:")
        self.normalized_address.clear()

    def set_busy(self, busy):
        for widget in (self.search_button, self.address_edit, self.provider_combo):
            widget.setEnabled(not busy)
        for widget in (self.token_edit, self.remember_token, self.secret_edit):
            widget.setEnabled(not busy)
        self.status_label.setText("Идет поиск…" if busy else "")

    def set_status(self, message, level="ok"):
        colour = STATUS_COLOURS[level]
        self.status_label.setText(f'<span style="color:{colour};">{html.escape(message)}</span>')
