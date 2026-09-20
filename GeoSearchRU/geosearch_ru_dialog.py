import html

from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPushButton, QTextBrowser, QVBoxLayout,
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
        self.keys_button = QPushButton("Ключи…")
        self.keys_button.setAutoDefault(False)
        self.keys_button.setToolTip("Токен DaData и ключ Яндекса — ввести и проверить")
        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("Например: г. Тамбов, пр. Энергетиков, 7")
        self.address_edit.setClearButtonEnabled(True)
        self.search_button = QPushButton("Найти")
        self.search_button.setDefault(True)
        self.bounded = QCheckBox("Искать только в пределах карты")
        self.pick_button = QPushButton("Адрес по точке на карте")
        self.pick_button.setCheckable(True)
        self.pick_button.setAutoDefault(False)
        self.pick_button.setToolTip("Щёлкнуть по карте и получить ближайший адрес")
        self.info_button = QPushButton("Что здесь?")
        self.info_button.setCheckable(True)
        self.info_button.setAutoDefault(False)
        self.info_button.setVisible(False)  # показывается настройкой, см. show_info_button
        self.info_button.setToolTip(
            "Щёлкнуть по карте и увидеть объекты OpenStreetMap в этой точке: "
            "здание, участок, дорогу, ЛЭП, а также границы, внутри которых точка лежит")
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
        form = QFormLayout()
        source_row = QHBoxLayout()
        source_row.addWidget(self.provider_combo, 1)
        source_row.addWidget(self.keys_button)
        form.addRow("Источник:", source_row)
        form.addRow("Адрес:", self.address_edit)
        search_row = QHBoxLayout()
        search_row.addWidget(self.bounded)
        search_row.addStretch()
        search_row.addWidget(self.search_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.addButton(self.pick_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.info_button, QDialogButtonBox.ButtonRole.ActionRole)
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

    def set_provider_titles(self, titles):
        """titles: {provider id: text shown in the source selector}."""
        for index in range(self.provider_combo.count()):
            title = titles.get(self.provider_combo.itemData(index))
            if title:
                self.provider_combo.setItemText(index, title)

    def show_info_button(self, visible):
        """«Что здесь?» по умолчанию скрыта: возможность есть, но окно не загромождает."""
        self.info_button.setVisible(visible)
        if not visible:
            self.info_button.setChecked(False)

    def show_attribution(self, provider):
        self.attribution_label.setText(provider.ATTRIBUTION or "")
        self.attribution_label.setVisible(bool(provider.ATTRIBUTION))

    def show_provider(self, provider):
        """Show the attribution the provider needs and whether it can search inside the map frame."""
        self.show_attribution(provider)
        self.bounded.setEnabled(provider.SUPPORTS_BBOX)
        self.bounded.setToolTip(
            "Искать адрес только внутри того, что видно на карте"
            if provider.SUPPORTS_BBOX else f"{provider.SOURCE} не умеет ограничивать поиск рамкой карты")

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
        for widget in (self.search_button, self.address_edit, self.provider_combo, self.keys_button,
                       self.pick_button, self.info_button):
            widget.setEnabled(not busy)
        self.status_label.setText("Идет поиск…" if busy else "")

    def set_status(self, message, level="ok"):
        colour = STATUS_COLOURS[level]
        self.status_label.setText(f'<span style="color:{colour};">{html.escape(message)}</span>')
