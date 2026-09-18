import html

from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPushButton, QTextBrowser, QVBoxLayout,
)

STATUS_COLOURS = {"ok": "#2e7d32", "warning": "#e65100", "error": "#b71c1c"}


class GeoSearchDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GeoSearch RU — поиск адреса")
        self.setMinimumWidth(560)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("Токен DaData API")
        self.remember_token = QCheckBox("Запомнить токен на этом компьютере")
        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("Например: г. Тамбов, пр. Энергетиков, 7")
        self.address_edit.setClearButtonEnabled(True)
        self.search_button = QPushButton("Найти")
        self.search_button.setDefault(True)
        self.results_label = QLabel()
        self.results_list = QListWidget()
        self.results_list.setMinimumHeight(110)
        self.normalized_address = QTextBrowser()
        self.normalized_address.setMinimumHeight(115)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.clear_button = QPushButton("Очистить маркер")
        self.clear_button.setAutoDefault(False)
        form = QFormLayout()
        form.addRow("Токен DaData:", self.token_edit)
        form.addRow("", self.remember_token)
        form.addRow("Адрес:", self.address_edit)
        search_row = QHBoxLayout()
        search_row.addStretch()
        search_row.addWidget(self.search_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.addButton(self.clear_button, QDialogButtonBox.ButtonRole.ResetRole)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(search_row)
        layout.addWidget(self.results_label)
        layout.addWidget(self.results_list)
        layout.addWidget(QLabel("Нормализованный адрес и координаты:"))
        layout.addWidget(self.normalized_address)
        layout.addWidget(self.status_label)
        layout.addWidget(buttons)
        self.address_edit.returnPressed.connect(self.search_button.click)
        self.clear_results()

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
        self.results_label.setText("Варианты:")
        self.normalized_address.clear()

    def set_busy(self, busy):
        for widget in (self.search_button, self.address_edit, self.token_edit, self.remember_token):
            widget.setEnabled(not busy)
        self.status_label.setText("Идет поиск…" if busy else "")

    def set_status(self, message, level="ok"):
        colour = STATUS_COLOURS[level]
        self.status_label.setText(f'<span style="color:{colour};">{html.escape(message)}</span>')
