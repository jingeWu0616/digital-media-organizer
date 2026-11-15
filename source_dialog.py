"""Lightweight source selection and pre-scan format choices."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QListWidget, QListView, QTreeView, QFileDialog, QDialog,
    QDialogButtonBox, QAbstractItemView, QCheckBox, QGroupBox)
from media_catalog import PHOTO_EXTS, VIDEO_EXTS
from ui_common import AdaptiveDialog


class SourceFoldersDialog(AdaptiveDialog):
    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.setWindowTitle('素材文件夹'); self.resize(720, 420)
        layout = QVBoxLayout(self)
        self.count = QLabel(); layout.addWidget(self.count)
        self.folders = QListWidget()
        self.folders.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.folders.setMinimumHeight(160)
        layout.addWidget(self.folders)
        actions = QHBoxLayout()
        add = QPushButton('添加文件夹'); add.clicked.connect(self.add_folders); actions.addWidget(add)
        self.remove = QPushButton('移除所选'); self.remove.clicked.connect(self.remove_folders)
        actions.addWidget(self.remove); actions.addStretch(); layout.addLayout(actions)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('使用所选文件夹')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept); self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.folders.itemSelectionChanged.connect(self.refresh)
        self.add_paths(paths)

    def paths(self):
        return [self.folders.item(i).text() for i in range(self.folders.count())]

    def add_paths(self, paths):
        existing = {p.casefold() for p in self.paths()}
        for path in paths:
            if str(path).strip() and str(path).casefold() not in existing:
                self.folders.addItem(str(path)); existing.add(str(path).casefold())
                self.folders.item(self.folders.count()-1).setToolTip(str(path))
        self.refresh()

    def refresh(self):
        self.count.setText(f'已选 {self.folders.count()} 个文件夹')
        self.remove.setEnabled(bool(self.folders.selectedItems()))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.folders.count() > 0)

    def add_folders(self):
        dialog = QFileDialog(self, '添加素材文件夹')
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        for name, cls in (('listView', QListView), ('treeView', QTreeView)):
            view = dialog.findChild(cls, name)
            if view: view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.add_paths(dialog.selectedFiles())

    def remove_folders(self):
        for item in self.folders.selectedItems():
            self.folders.takeItem(self.folders.row(item))
        self.refresh()


class ScanFormatsDialog(AdaptiveDialog):
    def __init__(self, selected, parent=None):
        super().__init__(parent)
        self.setWindowTitle('扫描格式'); self.resize(660, 430)
        layout = QVBoxLayout(self); self.boxes = {}
        for name, formats in (('照片', PHOTO_EXTS), ('视频', VIDEO_EXTS)):
            group = QGroupBox(name); grid = QGridLayout(group)
            for i, ext in enumerate(sorted(formats)):
                box = QCheckBox(ext[1:].upper()); box.setChecked(ext in selected)
                self.boxes[ext] = box; grid.addWidget(box, i//6, i%6)
                box.toggled.connect(self.refresh)
            layout.addWidget(group)
        actions = QHBoxLayout()
        for title, checked in (('全选', True), ('清除', False)):
            button = QPushButton(title); button.clicked.connect(lambda _, v=checked: self.select_all(v)); actions.addWidget(button)
        actions.addStretch(); layout.addLayout(actions)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('使用所选格式')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept); self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons); self.refresh()

    def selected(self):
        return {ext for ext, box in self.boxes.items() if box.isChecked()}

    def select_all(self, value):
        for box in self.boxes.values(): box.setChecked(value)

    def refresh(self):
        if hasattr(self, 'buttons'):
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.selected()))
