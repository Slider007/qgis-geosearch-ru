import html

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPushButton, QTextBrowser, QVBoxLayout,
)

# Цвета состояния подбираются под тему: на тёмном фоне тёмно-красный и тёмно-зелёный
# почти не читаются, поэтому для тёмной темы взяты светлые оттенки тех же цветов.
STATUS_COLOURS = {"ok": "#2e7d32", "warning": "#e65100", "error": "#b71c1c"}
STATUS_COLOURS_DARK = {"ok": "#81c784", "warning": "#ffb74d", "error": "#ef9a9a"}


def status_colour(widget, level):
    dark = widget.palette().window().color().lightness() < 128
    return (STATUS_COLOURS_DARK if dark else STATUS_COLOURS)[level]

ADD_POINT_TEXT = "Добавить точку во временный слой"
ADD_POINT_TOOLTIP = "Добавить выбранный адрес точкой во временный слой «Найденные адреса»"
ADD_SHAPE_TEXT = "Добавить границы во временный слой"
ADD_SHAPE_TOOLTIP = "Добавить выбранный объект ЕГРН с границами во временный слой «Кадастровые объекты»"


class GeoSearchDialog(QDialog):
    def __init__(self, providers, parent=None):
        """providers: [(id, title), …] for the source selector."""
        super().__init__(parent)
        self.setWindowTitle("Поиск адреса")
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
        self.cadastral_edit = QLineEdit()
        self.cadastral_edit.setPlaceholderText("Например: 68:29:0309001:24")
        self.cadastral_edit.setClearButtonEnabled(True)
        self.cadastral_edit.setToolTip(
            "Кадастровый номер участка, здания или сооружения. Ищется в НСПД (ЕГРН) "
            "независимо от выбранного источника адресов; можно указать и номер квартала.")
        self.cadastral_button = QPushButton("Найти в ЕГРН")
        self.cadastral_button.setAutoDefault(False)
        self.cadastral_button.setToolTip("Найти объект ЕГРН и показать его границы на карте")
        self.bounded = QCheckBox("Искать только в пределах карты")
        self.remembered_bounded = False  # выбор пользователя; у источника без рамки галочка гасится
        self.pick_button = QPushButton("Адрес по точке на карте")
        self.pick_button.setCheckable(True)
        self.pick_button.setAutoDefault(False)
        self.pick_button.setToolTip("Щёлкнуть по карте и получить ближайший адрес")
        self.info_button = QPushButton("Что здесь?")
        self.info_button.setCheckable(True)
        self.info_button.setAutoDefault(False)
        self.info_button.setToolTip(
            "Щёлкнуть по карте и увидеть объекты OpenStreetMap в этой точке: "
            "здание, участок, дорогу, ЛЭП, а также границы, внутри которых точка лежит")
        self.results_label = QLabel()
        self.results_list = QListWidget()
        self.results_list.setMinimumHeight(110)
        # Строка объекта ЕГРН длинная; без этого внизу списка появлялась горизонтальная прокрутка.
        self.results_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.results_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.normalized_address = QTextBrowser()
        self.normalized_address.setMinimumHeight(line * 7 + 20)  # address, blank, header and four value lines
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(line * 3 + 4)  # длинные объяснения ошибок занимают три строки
        self.clear_button = QPushButton("Очистить маркер")
        self.clear_button.setToolTip("Убрать с карты красный крестик, поставленный поиском")
        self.clear_button.setAutoDefault(False)
        self.add_point_button = QPushButton(ADD_POINT_TEXT)
        self.add_point_button.setAutoDefault(False)
        self.add_point_button.setToolTip(ADD_POINT_TOOLTIP)
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
        form.addRow("", search_row)
        cadastral_row = QHBoxLayout()
        cadastral_row.addWidget(self.cadastral_edit, 1)
        cadastral_row.addWidget(self.cadastral_button)
        form.addRow("Кадастровый номер:", cadastral_row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        # Qt подставляет английское «Close», если перевод Qt не загружен: в русском окне это чужеродно.
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.addButton(self.pick_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.info_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.clear_button, QDialogButtonBox.ButtonRole.ResetRole)
        buttons.addButton(self.add_point_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.results_label)
        layout.addWidget(self.results_list)
        layout.addWidget(QLabel("Сведения о выбранном объекте:"))
        layout.addWidget(self.normalized_address)
        layout.addWidget(self.status_label)
        layout.addWidget(self.attribution_label)
        layout.addWidget(buttons)
        # Скрывать до addButton бесполезно: QDialogButtonBox показывает добавленную кнопку заново.
        self.show_info_button(False)
        # Минимальная ширина считается по самому ряду кнопок, а не задаётся в пикселях:
        # при крупном системном шрифте подписи иначе обрезаются, а на Qt6 шрифт родителя
        # на момент создания ещё не применён, и расчёт «по шрифту» промахивается.
        row = buttons.buttons()
        needed = sum(b.sizeHint().width() for b in row) + 8 * len(row)
        self.setMinimumWidth(max(560, needed))
        self.address_edit.returnPressed.connect(self.search_button.click)
        self.cadastral_edit.returnPressed.connect(self.cadastral_button.click)
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

    def show_add_mode(self, boundaries):
        """Кнопка добавления: у объекта ЕГРН добавляются границы, у адреса — точка."""
        self.add_point_button.setText(ADD_SHAPE_TEXT if boundaries else ADD_POINT_TEXT)
        self.add_point_button.setToolTip(ADD_SHAPE_TOOLTIP if boundaries else ADD_POINT_TOOLTIP)

    def show_attribution(self, provider):
        self.attribution_label.setText(provider.ATTRIBUTION or "")
        self.attribution_label.setVisible(bool(provider.ATTRIBUTION))

    def show_provider(self, provider):
        """Show the attribution the provider needs and whether it can search inside the map frame."""
        self.show_attribution(provider)
        self.bounded.setEnabled(provider.SUPPORTS_BBOX)
        # Отмеченная, но неактивная галочка читается как «ограничение работает»: снимаем видимую отметку.
        # Сигналы глушим, иначе снятие уйдёт в настройки и сотрёт выбор пользователя.
        self.bounded.blockSignals(True)
        self.bounded.setChecked(self.remembered_bounded and provider.SUPPORTS_BBOX)
        self.bounded.blockSignals(False)
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
        self.show_add_mode(False)
        self.results_label.setText("Варианты:")
        self.normalized_address.clear()

    def set_busy(self, busy):
        for widget in (self.search_button, self.address_edit, self.provider_combo, self.keys_button,
                       self.cadastral_edit, self.cadastral_button, self.pick_button, self.info_button):
            widget.setEnabled(not busy)
        self.status_label.setText("Идет поиск…" if busy else "")

    def set_status(self, message, level="ok"):
        colour = status_colour(self, level)
        self.status_label.setText(f'<span style="color:{colour};">{html.escape(message)}</span>')
