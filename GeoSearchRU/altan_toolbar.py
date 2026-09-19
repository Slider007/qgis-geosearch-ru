"""Общая панель инструментов и подменю «Альтан-Эко» для всех модулей компании.

Файл одинаковый во всех модулях вместе с altan_logo.svg (образцы — папка
shared/ в папке проектов). Панель ищется по objectName: первый загруженный модуль её создаёт,
остальные добавляют на неё свои кнопки. Последний выгружаемый модуль её убирает.
Панель можно открепить и перетащить куда угодно, место QGIS запоминает по
objectName. Показать/скрыть — «Вид → Панели инструментов → Альтан-Эко».
"""

import os

from qgis.PyQt import sip
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QToolBar

TOOLBAR_NAME = "Альтан-Эко"
TOOLBAR_ID = "AltanEcoToolbar"  # не менять: по нему модули находят панель
# Подменю в «Модулях»: QGIS объединяет пункты разных модулей по названию,
# поэтому строка должна совпадать буква в букву, включая «&».
MENU = "&Альтан-Эко"
LOGO_PATH = os.path.join(os.path.dirname(__file__), "altan_logo.svg")


def _find(iface):
    return iface.mainWindow().findChild(QToolBar, TOOLBAR_ID)


def add_action(iface, action):
    """Добавить кнопку на общую панель, при необходимости создав её."""
    bar = _find(iface)
    if bar is None:
        bar = iface.addToolBar(TOOLBAR_NAME)
        # iface.addToolBar отдаёт панель во владение Python: без передачи её Qt
        # панель удалится, как только исчезнет последняя ссылка из Python.
        # Она общая для всех модулей, поэтому держать её должно главное окно.
        sip.transferto(bar, iface.mainWindow())
        bar.setObjectName(TOOLBAR_ID)
        bar.setToolTip("Модули Альтан-Эко")
        bar.setMovable(True)
        bar.setFloatable(True)
    if action not in bar.actions():
        bar.addAction(action)
    return bar


def remove_action(iface, action):
    """Убрать кнопку; пустую панель удалить."""
    bar = _find(iface)
    if bar is None:
        return
    try:
        bar.removeAction(action)
        if not bar.actions():
            iface.mainWindow().removeToolBar(bar)
            bar.setObjectName("")  # чтобы до deleteLater её не нашёл другой модуль
            bar.deleteLater()
    except RuntimeError:  # панель уже удалена при закрытии QGIS
        pass


def add_to_menu(iface, action):
    """Добавить пункт в подменю «Модули → Альтан-Эко» и поставить на подменю логотип."""
    iface.addPluginToMenu(MENU, action)
    # на macOS QGIS убирает «&» из названия подменю, поэтому сравнение без него
    title = MENU.replace("&", "")
    for item in iface.pluginMenu().actions():
        if item.menu() is not None and item.text().replace("&", "") == title:
            item.setIcon(QIcon(LOGO_PATH))


def remove_from_menu(iface, action):
    """Убрать пункт; пустое подменю QGIS убирает сам."""
    iface.removePluginMenu(MENU, action)
