"""Тест диалога добавления: дерево файлов, галочки папок с потомками.
Offscreen (QT_QPA_PLATFORM=offscreen), окно не показывается.

Запуск: python test_dialog.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ui import AddTorrentDialog

FILES = [
    ("ubuntu.iso", 5),
    ("docs/readme.txt", 100),
    ("docs/deep/guide.md", 200),
    ("docs/deep/img/logo.png", 300),
    ("music/track1.mp3", 400),
    ("music/sub/track2.flac", 500),
]   # total = 1505

CH, UN, PART = Qt.CheckState.Checked, Qt.CheckState.Unchecked, Qt.CheckState.PartiallyChecked


def item_by_path(dlg, parts):
    """Элемент дерева по пути от корня, например ('docs', 'deep')."""
    node = None
    for p in parts:
        if node is None:
            kids = [dlg.tw.topLevelItem(i) for i in range(dlg.tw.topLevelItemCount())]
        else:
            kids = [node.child(i) for i in range(node.childCount())]
        node = next((c for c in kids if c.text(0) == p), None)
        assert node is not None, f"нет узла {'/'.join(parts)}"
    return node


def main() -> int:
    app = QApplication([])
    dlg = AddTorrentDialog(None, ".", "testpack", 1505, list(FILES))

    all_idx = set(range(6))

    # 1. структура дерева и изначально всё отмечено
    assert item_by_path(dlg, ("docs", "deep", "img")) is not None, "нет вложенной папки"
    assert dlg.checked_files() == all_idx, "изначально должны быть отмечены все"
    print("[+] дерево построено, все файлы отмечены")

    # 2. снятие папки снимает всех потомков
    item_by_path(dlg, ("docs",)).setCheckState(0, UN)
    assert dlg.checked_files() == {0, 4, 5}, f"после docs-off: {dlg.checked_files()}"
    assert dlg.skipped_files() == {1, 2, 3}
    print("[+] снятие папки docs исключило 3 файла в глубине")

    # 3. возврат папки возвращает потомков
    item_by_path(dlg, ("docs",)).setCheckState(0, CH)
    assert dlg.checked_files() == all_idx
    print("[+] возврат папки вернул потомков")

    # 4. снятие вложенной папки → родитель частичный
    item_by_path(dlg, ("docs", "deep")).setCheckState(0, UN)
    assert dlg.checked_files() == {0, 1, 4, 5}, f"после deep-off: {dlg.checked_files()}"
    assert item_by_path(dlg, ("docs",)).checkState(0) == PART, "docs должен быть частичным"
    assert item_by_path(dlg, ("docs", "deep")).checkState(0) == UN
    print("[+] вложенная папка снята, родитель показан частично отмеченным")

    # 5. возврат глубокой ветки поднимает предков обратно
    item_by_path(dlg, ("docs", "deep", "img")).setCheckState(0, CH)
    assert item_by_path(dlg, ("docs", "deep")).checkState(0) == PART
    item_by_path(dlg, ("docs", "deep", "guide.md")).setCheckState(0, CH)
    assert item_by_path(dlg, ("docs",)).checkState(0) == CH, "docs должен стать отмеченным"
    assert dlg.checked_files() == all_idx
    print("[+] докачка ветки снизу вверх пересчитала предков")

    # 6. снятие одного файла → его папка частичная, размер в подписи честный
    item_by_path(dlg, ("music", "track1.mp3")).setCheckState(0, UN)
    assert item_by_path(dlg, ("music",)).checkState(0) == PART
    assert dlg.checked_files() == {0, 1, 2, 3, 5}
    assert "1.1 КБ из 1.5 КБ" in dlg.lbl_sel.text(), dlg.lbl_sel.text()
    print("[+] один файл снят — папка частичная, подпись размера верна")

    # 7. кнопки «выбрать/снять все»
    dlg._check_all(False)
    assert dlg.checked_files() == set(), "снять все не сработало"
    dlg._check_all(True)
    assert dlg.checked_files() == all_idx, "выбрать все не сработало"
    print("[+] выбрать/снять все работает")

    # 8. одиночный файл: дерева нет, всё отмечено
    single = AddTorrentDialog(None, ".", "solo.bin", 7, [("solo.bin", 7)])
    assert single.checked_files() == {0} and single.skipped_files() == set()
    print("[+] одиночный файл: всё отмечено")

    print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
