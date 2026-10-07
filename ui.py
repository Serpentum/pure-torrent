"""GUI PureTorrent: PySide6, тёмная тема, таблица торрентов."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QDragEnterEvent,
                           QDropEvent, QFont, QKeySequence, QPainterPath, QPalette,
                           QPen)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPushButton, QSpinBox, QStyledItemDelegate, QToolBar,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QSizePolicy,
)

from version import __version__
from engine import (STATE_CHECKING, STATE_DOWNLOADING, STATE_ERROR,
                    STATE_METADATA, STATE_PAUSED, STATE_SEEDING, Engine, Snapshot)
from torrent import TorrentMeta, parse_magnet

COL_NAME, COL_STATE, COL_PROGRESS, COL_SIZE, COL_DL, COL_UL, COL_PEERS, COL_ETA = range(8)
HASH_ROLE = Qt.ItemDataRole.UserRole + 1
PROGRESS_ROLE = Qt.ItemDataRole.UserRole + 2

STATE_LABELS = {
    STATE_METADATA: "Метаданные",
    STATE_CHECKING: "Проверка",
    STATE_DOWNLOADING: "Загрузка",
    STATE_SEEDING: "Раздача",
    STATE_PAUSED: "Пауза",
    STATE_ERROR: "Ошибка",
}


def human_size(n: float) -> str:
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            if unit == "Б":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def human_rate(n: int) -> str:
    return human_size(n) + "/с"


def eta_str(secs: int) -> str:
    if secs < 0:
        return "—"
    if secs == 0:
        return "0 с"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м {s}с"
    return f"{s} с"


def apply_dark_theme(app: QApplication):
    app.setStyle("Fusion")
    pal = QPalette()
    window = QColor(43, 43, 51)
    base = QColor(31, 31, 39)
    alt = QColor(38, 38, 47)
    text = QColor(230, 230, 236)
    disabled = QColor(120, 120, 130)
    button = QColor(51, 51, 61)
    highlight = QColor(61, 109, 242)
    pal.setColor(QPalette.ColorRole.Window, window)
    pal.setColor(QPalette.ColorRole.WindowText, text)
    pal.setColor(QPalette.ColorRole.Base, base)
    pal.setColor(QPalette.ColorRole.AlternateBase, alt)
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.Button, button)
    pal.setColor(QPalette.ColorRole.ButtonText, text)
    pal.setColor(QPalette.ColorRole.Highlight, highlight)
    pal.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.white)
    pal.setColor(QPalette.ColorRole.ToolTipBase, base)
    pal.setColor(QPalette.ColorRole.ToolTipText, text)
    pal.setColor(QPalette.ColorRole.PlaceholderText, disabled)
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    app.setPalette(pal)
    font = QFont("Segoe UI", 10)
    app.setFont(font)
    app.setStyleSheet("""
        QToolBar { spacing: 3px; padding: 4px; background: #232329; border: none; }
        QToolBar QToolButton { padding: 5px 10px; border-radius: 4px; }
        QTreeWidget { border: 1px solid #2f2f38; border-radius: 4px; }
        QTreeWidget::item { padding: 8px 2px; }
        QHeaderView::section { background: #26262e; color: #b8b8c2; padding: 5px;
                               border: none; border-right: 1px solid #2f2f38; }
        QStatusBar { background: #232329; color: #9aa0ac; }
        QMenu { border: 1px solid #3a3a44; }
        QLineEdit, QSpinBox { background: #1f1f27; border: 1px solid #3a3a44;
                              border-radius: 4px; padding: 4px 6px; }
    """)


class ProgressDelegate(QStyledItemDelegate):
    """Толстый скруглённый прогресс-бар с процентами по центру."""

    def paint(self, painter, option, index):
        if index.column() != COL_PROGRESS:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        v = max(0.0, min(1.0, index.data(PROGRESS_ROLE) or 0.0))
        r = QRectF(option.rect.adjusted(4, 5, -4, -5))
        path = QPainterPath()
        path.addRoundedRect(r, 5.0, 5.0)
        painter.setPen(QPen(QColor(56, 56, 68), 1))
        painter.setBrush(QColor(37, 37, 46))
        painter.drawPath(path)
        if v > 0.004:
            painter.save()
            painter.setClipPath(path)
            fill = QColor(76, 175, 132) if v >= 1.0 else QColor(61, 109, 242)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRect(QRectF(r.left(), r.top(), r.width() * v, r.height()))
            painter.restore()
        f = QFont(option.font)
        f.setBold(True)
        f.setPointSizeF(max(8.5, f.pointSizeF()))
        painter.setFont(f)
        painter.setPen(QColor(238, 238, 245))
        painter.drawText(option.rect, Qt.AlignmentFlag.AlignCenter, f"{v * 100:.1f}%")
        painter.restore()


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        self.ed_dir = QLineEdit(settings.download_dir)
        btn_browse = QPushButton("Обзор…")
        btn_browse.clicked.connect(self._browse)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.ed_dir)
        h.addWidget(btn_browse)
        form.addRow("Папка загрузок:", row)
        self.sp_port = QSpinBox()
        self.sp_port.setRange(1024, 65535)
        self.sp_port.setValue(settings.listen_port)
        form.addRow("Порт:", self.sp_port)
        self.sp_peers = QSpinBox()
        self.sp_peers.setRange(5, 200)
        self.sp_peers.setValue(settings.max_peers)
        form.addRow("Пиров на торрент:", self.sp_peers)
        self.sp_dl = QSpinBox()
        self.sp_dl.setRange(0, 10_000_000)
        self.sp_dl.setValue(settings.download_limit_kbs)
        self.sp_dl.setSuffix(" КБ/с")
        self.sp_dl.setSpecialValueText("Без лимита")
        form.addRow("Лимит загрузки:", self.sp_dl)
        self.sp_ul = QSpinBox()
        self.sp_ul.setRange(0, 10_000_000)
        self.sp_ul.setValue(settings.upload_limit_kbs)
        self.sp_ul.setSuffix(" КБ/с")
        self.sp_ul.setSpecialValueText("Без лимита")
        form.addRow("Лимит отдачи:", self.sp_ul)
        self.cb_encrypt = QCheckBox("Шифрование протокола (MSE, обход блокировок)")
        self.cb_encrypt.setChecked(settings.encrypt)
        form.addRow(self.cb_encrypt)
        note = QLabel("Порт применяется после перезапуска. 0 = без лимита.")
        note.setStyleSheet("color: #9aa0ac;")
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Папка загрузок", self.ed_dir.text())
        if d:
            self.ed_dir.setText(d)

    def values(self) -> dict:
        return {
            "download_dir": self.ed_dir.text().strip(),
            "listen_port": self.sp_port.value(),
            "max_peers": self.sp_peers.value(),
            "download_limit_kbs": self.sp_dl.value(),
            "upload_limit_kbs": self.sp_ul.value(),
            "encrypt": self.cb_encrypt.isChecked(),
        }


class AddTorrentDialog(QDialog):
    """«Что качаем и куда»: инфа о торренте + выбор диска/папки."""

    def __init__(self, parent, default_dir: str, name: str, size: int,
                 files: list, is_magnet: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Добавить торрент")
        self.setMinimumWidth(520)
        self._files = files
        root = QVBoxLayout(self)

        title = QLabel(name)
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        root.addWidget(title)

        if is_magnet:
            info = "magnet-ссылка · размер и список файлов станут известны после получения метаданных"
            root.addWidget(QLabel(info))
        else:
            n_files = len(files)
            self.lbl_info = QLabel(f"{human_size(size)} · {n_files} файл(ов)")
            root.addWidget(self.lbl_info)
            if n_files > 1:
                lw = QListWidget()
                lw.setMaximumHeight(220)
                self._building = True
                MAX_SHOW = 2000
                for i, (p, s) in enumerate(files[:MAX_SHOW]):
                    it = QListWidgetItem(f"{p} — {human_size(s)}")
                    it.setFlags(Qt.ItemFlag.ItemIsUserCheckable |
                                Qt.ItemFlag.ItemIsEnabled |
                                Qt.ItemFlag.ItemIsSelectable)
                    it.setCheckState(Qt.CheckState.Checked)
                    it.setData(Qt.ItemDataRole.UserRole, i)
                    lw.addItem(it)
                if n_files > MAX_SHOW:
                    tail = QListWidgetItem(f"… и ещё {n_files - MAX_SHOW} (исключить нельзя)")
                    tail.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    lw.addItem(tail)
                self._building = False
                lw.itemChanged.connect(self._on_file_item_changed)
                root.addWidget(lw)
                self.lw = lw
                rowf = QWidget()
                hf = QHBoxLayout(rowf)
                hf.setContentsMargins(0, 0, 0, 0)
                b_all = QPushButton("Выбрать все")
                b_all.clicked.connect(lambda: self._check_all(True))
                b_none = QPushButton("Снять все")
                b_none.clicked.connect(lambda: self._check_all(False))
                self.lbl_sel = QLabel("")
                self.lbl_sel.setStyleSheet("color: #9aa0ac;")
                hf.addWidget(b_all)
                hf.addWidget(b_none)
                hf.addStretch(1)
                hf.addWidget(self.lbl_sel)
                root.addWidget(rowf)
                self._update_sel_size()

        root.addWidget(QLabel("Куда сохранять:"))
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        self.cmb_drive = QComboBox()
        from PySide6.QtCore import QDir
        for d in QDir.drives():
            self.cmb_drive.addItem(d.absoluteFilePath())
        h.addWidget(self.cmb_drive, 1)
        self.ed_sub = QLineEdit()
        self.ed_sub.setPlaceholderText("подпапка (можно пустую)")
        h.addWidget(self.ed_sub, 2)
        btn_browse = QPushButton("Обзор…")
        btn_browse.clicked.connect(self._browse)
        h.addWidget(btn_browse)
        root.addWidget(row)
        self.lbl_path = QLabel("")
        self.lbl_path.setStyleSheet("color: #9aa0ac;")
        root.addWidget(self.lbl_path)

        self.cb_seq = QCheckBox("Последовательная загрузка (для видео)")
        root.addWidget(self.cb_seq)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Скачать")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        # стартовые значения из default_dir
        self._set_from_dir(default_dir)
        self.cmb_drive.currentTextChanged.connect(self._update_path)
        self.ed_sub.textChanged.connect(self._update_path)

    def _set_from_dir(self, dir_path: str):
        p = Path(dir_path)
        drive = f"{p.anchor}" if p.anchor else "C:/"
        idx = self.cmb_drive.findText(drive)
        if idx >= 0:
            self.cmb_drive.setCurrentIndex(idx)
        try:
            rel = str(p.relative_to(drive))
        except ValueError:
            rel = ""
        self.ed_sub.setText(rel)
        self._update_path()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Выбрать папку", self.save_path())
        if d:
            self._set_from_dir(d)

    def _update_path(self):
        self.lbl_path.setText(self.save_path())

    def save_path(self) -> str:
        drive = self.cmb_drive.currentText()
        sub = self.ed_sub.text().strip().strip("/\\")
        return str(Path(drive) / sub) if sub else drive

    def sequential(self) -> bool:
        return self.cb_seq.isChecked()

    # ---------- выбор файлов ----------

    def _on_file_item_changed(self, _item):
        if not self._building:
            self._update_sel_size()

    def _check_all(self, on: bool):
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self._building = True
        for i in range(self.lw.count()):
            it = self.lw.item(i)
            if it.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                it.setCheckState(state)
        self._building = False
        self._update_sel_size()

    def _update_sel_size(self):
        sel = sum(self._files[i][1] for i in self.checked_files())
        total = sum(s for _, s in self._files)
        if sel == total:
            self.lbl_sel.setText("")
            self.lbl_info.setText(f"{human_size(total)} · {len(self._files)} файл(ов)")
        else:
            self.lbl_sel.setText(f"скачается {human_size(sel)} из {human_size(total)}")
            self.lbl_info.setText(f"{len(self._files)} файл(ов), отмечено {len(self.checked_files())}")

    def checked_files(self) -> set:
        lw = getattr(self, "lw", None)
        if lw is None:
            return set(range(len(getattr(self, "_files", []))))
        out = set()
        for i in range(lw.count()):
            it = lw.item(i)
            if (it.flags() & Qt.ItemFlag.ItemIsUserCheckable) \
                    and it.checkState() == Qt.CheckState.Checked:
                out.add(it.data(Qt.ItemDataRole.UserRole))
        return out

    def skipped_files(self) -> set:
        """Индексы файлов, снятых галочкой (не качать)."""
        return set(range(len(self._files))) - self.checked_files()


class MainWindow(QMainWindow):
    def __init__(self, engine: Engine):
        super().__init__()
        self.engine = engine
        self.setWindowTitle(f"PureTorrent {__version__}")
        self.resize(980, 560)
        self.setAcceptDrops(True)
        self._build_toolbar()
        self._build_tree()
        self._build_statusbar()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    # ---------- построение интерфейса ----------

    def _build_toolbar(self):
        tb = QToolBar("Главная")
        tb.setMovable(False)
        self.addToolBar(tb)
        self.act_add = QAction("＋ .torrent", self)
        self.act_add.setShortcut(QKeySequence.StandardKey.Open)
        self.act_add.triggered.connect(self.add_torrent_dialog)
        tb.addAction(self.act_add)
        self.act_magnet = QAction("🧲 Магнит-ссылка", self)
        self.act_magnet.triggered.connect(self.add_magnet_dialog)
        tb.addAction(self.act_magnet)
        tb.addSeparator()
        self.act_pause = QAction("⏸ Пауза", self)
        self.act_pause.triggered.connect(self.pause_selected)
        tb.addAction(self.act_pause)
        self.act_resume = QAction("▶ Продолжить", self)
        self.act_resume.triggered.connect(self.resume_selected)
        tb.addAction(self.act_resume)
        tb.addSeparator()
        self.act_remove = QAction("🗑 Удалить", self)
        self.act_remove.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        self.act_remove.triggered.connect(self.remove_selected)
        tb.addAction(self.act_remove)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tb.addWidget(spacer)
        self.act_settings = QAction("⚙ Настройки", self)
        self.act_settings.triggered.connect(self.open_settings)
        tb.addAction(self.act_settings)

    def _build_tree(self):
        self.tree = QTreeWidget()
        self.tree.setColumnCount(8)
        self.tree.setHeaderLabels(["Название", "Статус", "Прогресс", "Размер",
                                   "Загрузка", "Отдача", "Пиры", "Осталось"])
        header = self.tree.header()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(COL_NAME, 340)
        header.setSectionResizeMode(COL_PROGRESS, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(COL_PROGRESS, 170)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSortingEnabled(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.context_menu)
        self.tree.itemDoubleClicked.connect(self.open_folder_item)
        self.tree.setItemDelegate(ProgressDelegate(self.tree))
        self.tree.sortItems(COL_NAME, Qt.SortOrder.AscendingOrder)
        self.setCentralWidget(self.tree)

    def _build_statusbar(self):
        self.lbl_speed = QLabel("↓ 0 Б/с    ↑ 0 Б/с")
        self.lbl_dht = QLabel("")
        self.lbl_port = QLabel(f"Порт: {self.engine.settings.listen_port}")
        bar = self.statusBar()
        bar.addWidget(self.lbl_speed)
        bar.addPermanentWidget(self.lbl_dht)
        bar.addPermanentWidget(self.lbl_port)

    # ---------- обновление ----------

    def _selected_hashes(self) -> list:
        return [it.data(0, HASH_ROLE) for it in self.tree.selectedItems()]

    def refresh(self):
        snaps = self.engine.snapshot()
        by_hash = {s.hash: s for s in snaps}
        self.tree.setSortingEnabled(False)
        existing = {}
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            existing[it.data(0, HASH_ROLE)] = it
        for h in list(existing):
            if h not in by_hash:
                self.tree.takeTopLevelItem(
                    self.tree.indexOfTopLevelItem(existing.pop(h)))
        for s in snaps:
            it = existing.get(s.hash)
            if it is None:
                it = QTreeWidgetItem()
                it.setData(0, HASH_ROLE, s.hash)
                it.setText(COL_NAME, s.name)
                self.tree.addTopLevelItem(it)
                existing[s.hash] = it
            self._fill_item(it, s)
        self.tree.setSortingEnabled(True)
        dl, ul = self.engine.totals()
        self.lbl_speed.setText(f"↓ {human_rate(dl)}    ↑ {human_rate(ul)}")
        nodes = self.engine.dht_nodes()
        self.lbl_dht.setText(f"DHT: {nodes}" if nodes else "")
        for ev in self.engine.events()[-3:]:
            self.statusBar().showMessage(ev, 8000)
        selection = bool(self._selected_hashes())
        self.act_pause.setEnabled(selection)
        self.act_resume.setEnabled(selection)
        self.act_remove.setEnabled(selection)

    def _fill_item(self, it: QTreeWidgetItem, s: Snapshot):
        state = STATE_LABELS.get(s.state, s.state)
        if s.state == STATE_ERROR and s.error:
            state = f"Ошибка: {s.error[:60]}"
        it.setText(COL_NAME, s.name)
        it.setText(COL_STATE, state)
        it.setData(COL_PROGRESS, PROGRESS_ROLE, s.progress)
        it.setText(COL_SIZE, human_size(s.size))
        it.setText(COL_DL, human_rate(s.dl) if s.dl else "—")
        it.setText(COL_UL, human_rate(s.ul) if s.ul else "—")
        it.setText(COL_PEERS, f"{s.peers} ({s.seeds} сид.)" if s.seeds else f"{s.peers}")
        it.setText(COL_ETA, eta_str(s.eta))
        color = QColor(230, 230, 236)
        if s.state == STATE_ERROR:
            color = QColor(235, 110, 110)
        elif s.state == STATE_DOWNLOADING:
            color = QColor(120, 190, 255)
        elif s.state == STATE_SEEDING:
            color = QColor(130, 210, 140)
        for c in range(8):
            it.setForeground(c, color)
        it.setToolTip(COL_NAME, f"{s.save_path}\n{human_size(s.done)} из {human_size(s.size)}")

    # ---------- действия ----------

    def add_torrent_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Выбрать .torrent", "", "Торренты (*.torrent);;Все файлы (*)")
        for f in files:
            self.add_torrent_path(f)

    def add_torrent_path(self, path: str, interactive: bool = True):
        try:
            meta = TorrentMeta.from_bytes(Path(path).read_bytes())
        except Exception as e:
            QMessageBox.warning(self, "Не удалось добавить",
                                f"{Path(path).name}:\n{e}")
            return
        if interactive:
            dlg = AddTorrentDialog(self, self.engine.settings.download_dir,
                                   meta.name, meta.total_size,
                                   [(f.path, f.length) for f in meta.files])
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            save_path = dlg.save_path()
            sequential = dlg.sequential()
        else:
            save_path = self.engine.settings.download_dir
            sequential = False
        try:
            h = self.engine.add_torrent_file(
                path, save_path=save_path,
                skip_files=dlg.skipped_files() if interactive else None)
            if sequential:
                self.engine.set_sequential(h, True)
            self.statusBar().showMessage(f"Добавлено: {meta.name}", 5000)
        except Exception as e:
            QMessageBox.warning(self, "Не удалось добавить",
                                f"{Path(path).name}:\n{e}")

    def add_magnet_interactive(self, uri: str, interactive: bool = True):
        try:
            ih, name, _trackers = parse_magnet(uri)
            if ih is None:
                raise ValueError("в magnet-ссылке нет info-hash")
        except Exception as e:
            if interactive:
                QMessageBox.warning(self, "Не удалось добавить", str(e))
            return
        if interactive:
            dlg = AddTorrentDialog(self, self.engine.settings.download_dir,
                                   name or f"магнит {ih.hex()[:16]}…", 0, [],
                                   is_magnet=True)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            save_path = dlg.save_path()
            sequential = dlg.sequential()
        else:
            save_path = self.engine.settings.download_dir
            sequential = False
        try:
            h = self.engine.add_magnet(uri, save_path=save_path)
            if sequential:
                self.engine.set_sequential(h, True)
            self.statusBar().showMessage("Магнит добавлен", 5000)
        except Exception as e:
            QMessageBox.warning(self, "Не удалось добавить", str(e))

    def add_magnet_dialog(self):
        uri, ok = QInputDialog.getText(
            self, "Магнит-ссылка", "Вставь magnet-ссылку:",
            QLineEdit.EchoMode.Normal, "")
        if ok and uri.strip():
            self.add_magnet_interactive(uri.strip())

    def pause_selected(self):
        for h in self._selected_hashes():
            self.engine.pause(h)

    def resume_selected(self):
        for h in self._selected_hashes():
            self.engine.resume(h)

    def remove_selected(self):
        hashes = self._selected_hashes()
        if not hashes:
            return
        msg = QMessageBox(self)
        msg.setWindowTitle("Удалить торрент")
        msg.setText(f"Удалить выбранные торренты ({len(hashes)})?")
        cb = QCheckBox("Также удалить скачанные файлы с диска")
        msg.setCheckBox(cb)
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setStandardButtons(QMessageBox.StandardButton.Yes |
                               QMessageBox.StandardButton.Cancel)
        if msg.exec() == QMessageBox.StandardButton.Yes:
            delete = cb.isChecked()
            for h in hashes:
                self.engine.remove(h, delete)

    def open_settings(self):
        dlg = SettingsDialog(self.engine.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.engine.update_settings(dlg.values())
            self.lbl_port.setText(f"Порт: {self.engine.settings.listen_port}")

    def open_folder_item(self, item: QTreeWidgetItem, _col: int):
        for s in self.engine.snapshot():
            if s.hash == item.data(0, HASH_ROLE):
                QDesktopServices.openUrl(QUrl.fromLocalFile(s.save_path))
                return

    # ---------- контекстное меню ----------

    def context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        hashes = self._selected_hashes() or [item.data(0, HASH_ROLE)]
        snaps = {s.hash: s for s in self.engine.snapshot()}
        main = snaps.get(hashes[0])
        if main is None:
            return
        menu = QMenu(self)
        if main.paused:
            act = menu.addAction("▶ Продолжить")
            act.triggered.connect(lambda: [self.engine.resume(h) for h in hashes])
        else:
            act = menu.addAction("⏸ Пауза")
            act.triggered.connect(lambda: [self.engine.pause(h) for h in hashes])
        act_seq = menu.addAction("Последовательная загрузка")
        act_seq.setCheckable(True)
        act_seq.setChecked(main.sequential)
        act_seq.triggered.connect(
            lambda on: [self.engine.set_sequential(h, on) for h in hashes])
        act_check = menu.addAction("Перепроверить файлы")
        act_check.triggered.connect(
            lambda: [self.engine.force_recheck(h) for h in hashes])
        menu.addSeparator()
        act_dir = menu.addAction("Открыть папку")
        act_dir.triggered.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(main.save_path)))
        act_copy = menu.addAction("Копировать magnet-ссылку")
        act_copy.triggered.connect(lambda: self._copy_magnet(hashes[0]))
        menu.addSeparator()
        act_rm = menu.addAction("🗑 Удалить…")
        act_rm.triggered.connect(self.remove_selected)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _copy_magnet(self, hexh: str):
        from PySide6.QtGui import QGuiApplication
        p = self.engine.torrent_file_path(hexh)
        if p is not None:
            try:
                from torrent import TorrentMeta
                meta = TorrentMeta.from_bytes(p.read_bytes())
                QGuiApplication.clipboard().setText(meta.magnet_uri())
                self.statusBar().showMessage("Магнит скопирован", 4000)
                return
            except Exception:
                pass
        snaps = {s.hash: s for s in self.engine.snapshot()}
        s = snaps.get(hexh)
        if s:
            QGuiApplication.clipboard().setText(
                f"magnet:?xt=urn:btih:{s.hash}&dn={s.name}")
            self.statusBar().showMessage("Магнит скопирован", 4000)

    # ---------- drag & drop ----------

    def dragEnterEvent(self, event: QDragEnterEvent):
        md = event.mimeData()
        if md.hasUrls() or (md.hasText() and "magnet:?" in md.text()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        md = event.mimeData()
        if md.hasUrls():
            for url in md.urls():
                f = url.toLocalFile()
                if f.lower().endswith(".torrent"):
                    self.add_torrent_path(f)
        text = md.text() if md.hasText() else ""
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("magnet:?"):
                self.add_magnet_interactive(line)
        event.acceptProposedAction()
