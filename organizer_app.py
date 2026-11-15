"""Media Organizer 4.0 — local Qt UI, optional on-demand Amap lookup."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections import OrderedDict, Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QObject, QRunnable,
    QSettings, QSize, Qt, QThreadPool, QTimer, Signal, QUrl, QLockFile)
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap, QColor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton, QFileDialog, QComboBox,
    QTableView, QHeaderView, QAbstractItemView, QTabWidget, QGroupBox, QCheckBox,
    QSplitter, QTextEdit, QDialog, QDialogButtonBox, QMessageBox, QProgressBar, QSizePolicy, QScrollArea, QFrame, QLayout)

from media_catalog import Catalog, default_database, PHOTO_EXTS, VIDEO_EXTS
from source_scope import display as display_roots
from source_dialog import SourceFoldersDialog, ScanFormatsDialog
from thumbnail_cache import ThumbnailCache
from import_service import ImportService
from color_service import ColorService, profile as color_profile, private_tracks
from processing_history import ProcessingHistory, describe_timing
from task_queue import TaskQueue, can_use_protected, can_use_color_gps_compatible
from task_panel import TaskPanel
from location_service import LocationService, GPS_EXTENSIONS, supports_gps
from activity_service import ActivityService
from activity_dialog import ActivityDialog
from location_dialog import LocationDialog
from watermark_service import WatermarkService, PHOTO_TYPES as WATERMARK_TYPES, STYLES as WATERMARK_STYLES
from watermark_dialog import WatermarkDialog
from settings_dialog import SettingsDialog
from ui_common import AdaptiveDialog, MediaDelegate, WrappingText, COLORS, status_color, fit_window

VERSION = '4.0.17'
GPS_TEXT = {'present': '有 GPS', 'missing': '无 GPS', 'unknown': '未确认'}
STATE_TEXT = {'pending': '待处理', 'running': '进行中', 'copying': '复制中',
              'published': '已校验', 'complete': '完成', 'completed': '完成',
              'completed_with_errors': '有异常', 'stopped': '已停止', 'skipped': '重复跳过',
              'failed': '失败', 'source_retained': '源文件保留', 'cancelled': '已停止'}


def human_size(value):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.1f} {unit}'
        value /= 1024


def human_datetime(value):
    """Display the recorded wall time and offset; never guess a camera timezone."""
    if not value:
        return ''
    text = str(value)
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).strftime('%Y-%m-%d')
        stamp = datetime.fromisoformat(text.replace('Z', '+00:00'))
        display = stamp.strftime('%Y-%m-%d %H:%M:%S')
        offset = stamp.strftime('%z')
        if offset:
            display += '  UTC' + offset[:3] + ':' + offset[3:]
        return display
    except ValueError:
        return text


class JobSignals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(object)
    finished = Signal()


class Job(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = JobSignals()

    def run(self):
        try:
            self.signals.done.emit(self.fn(self.signals.progress.emit))
        except Exception as exc:
            self.signals.failed.emit(f'{type(exc).__name__}  {exc}')
        finally:
            self.signals.finished.emit()


class PreviewLoader(QObject):
    ready = Signal(str)

    def __init__(self, cache):
        super().__init__()
        self.cache = cache
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.queue = None
        self.blocked = lambda:False
        self.images = OrderedDict()
        self.pending = set()
        self.failed = set()
        self.jobs = set()
        self.cancel = threading.Event()

    def icon(self, row):
        key = self.cache.key(row)
        if key in self.images:
            self.images.move_to_end(key)
            return self.images[key]
        if self.blocked() or (self.queue and self.queue.active):return None
        if key not in self.pending and key not in self.failed and len(self.pending) < 48 and not self.cancel.is_set():
            self.pending.add(key)
            def read(_):
                if self.queue:
                    with self.queue.read_guard(row['current_path']) as allowed:
                        if not allowed:return None
                        return self.cache.ensure(row,self.cancel)
                return self.cache.ensure(row,self.cancel)
            job = Job(read)
            self.jobs.add(job)
            job.signals.done.connect(lambda path, k=key: self.loaded(k, path))
            job.signals.failed.connect(lambda _, k=key: self.failed.add(k))
            job.signals.finished.connect(lambda j=job, k=key: self.finished(j, k))
            self.pool.start(job)
        return None

    def loaded(self, key, path):
        if path and Path(path).is_file():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                self.images[key] = QIcon(pixmap)
                while len(self.images) > 64:
                    self.images.popitem(last=False)
        self.ready.emit(key)

    def finished(self, job, key):
        self.pending.discard(key)
        self.jobs.discard(job)


class MediaModel(QAbstractTableModel):
    headers = ['预览', '现文件名', '首次导入原名', '状态', '拍摄时间', '类型', 'GPS', '大小', '所在文件夹']

    def __init__(self, loader):
        super().__init__()
        self.rows = []
        self.compact = False
        self.loader = loader
        loader.ready.connect(self.thumbnail_ready)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DecorationRole and col == 0 and not self.compact:
            return self.loader.icon(row)
        if role == Qt.ItemDataRole.ToolTipRole:
            details = [('现文件名', row['observed_name']), ('首次导入原名', row.get('original_name') or '未知'),
                       ('拍摄时间', human_datetime(row.get('capture_time')) or '待确认'), ('路径', row['current_path'])]
            if row.get('capture_time') and not datetime_has_zone(row['capture_time']):
                details.append(('拍摄时区', '未记录'))
            if row.get('metadata_error'):
                details.append(('读取异常', row['metadata_error']))
            return '\n'.join(f'{label}    {value}' for label, value in details)
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 3:
                return QColor(COLORS['warning'] if row.get('metadata_error') else status_color(
                    '已还原' if row.get('color_revision') else ('已另存' if row.get('watermark_revision') else row.get('import_state'))))
            if col == 4 and (not row.get('capture_time') or row.get('date_source') == 'filename_inferred'):
                return QColor(COLORS['warning'])
            if col == 6:
                return QColor(COLORS['success'] if row['gps_status'] == 'present' else COLORS['muted'])
        if role == Qt.ItemDataRole.DisplayRole:
            return ['', row['observed_name'], row.get('original_name') or '',
                    ('读取异常' if row.get('metadata_error') else row.get('import_state', '')) + (' · 已还原' if row.get('color_revision') else '') + (' · 水印版' if row.get('watermark_revision') else ''),
                    (human_datetime(row.get('capture_time')) or '待确认') + (' · 推断' if row.get('date_source') == 'filename_inferred' else ''),
                    '照片' if row['kind'] == 'photo' else '视频', GPS_TEXT[row['gps_status']],
                    human_size(row['size']), str(Path(row['current_path']).parent)][col]

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def thumbnail_ready(self, key):
        for i, row in enumerate(self.rows):
            if self.loader.cache.key(row) == key:
                self.dataChanged.emit(self.index(i, 0), self.index(i, 0), [Qt.ItemDataRole.DecorationRole])


class RowsModel(QAbstractTableModel):
    def __init__(self, headers, fields):
        super().__init__()
        self.headers, self.fields, self.rows = headers, fields, []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role == Qt.ItemDataRole.ForegroundRole:
            field = self.fields[index.column()]
            value = self.rows[index.row()].get(field)
            if field in ('status', 'display_status'):
                return QColor(status_color(value))
            if field == 'errors' and value:
                return QColor(COLORS['error'])
        if index.isValid() and role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            value = self.rows[index.row()].get(self.fields[index.column()])
            if self.fields[index.column()] in ('capture_time', 'created_at', 'completed_at', 'finished_at', 'imported_at'):
                display = human_datetime(value)
                if role == Qt.ItemDataRole.ToolTipRole and value and not datetime_has_zone(value):
                    return display + '\n时区未记录'
                return display
            return STATE_TEXT.get(value, value) if isinstance(value, str) else value

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


def table_for(model, thumbs=False):
    view = QTableView()
    view.setModel(model)
    view.setItemDelegate(MediaDelegate(view))
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setAlternatingRowColors(True)
    view.setShowGrid(False)
    view.setWordWrap(False)
    view.verticalHeader().hide()
    view.horizontalHeader().setStretchLastSection(True)
    if thumbs:
        view.horizontalHeader().setStretchLastSection(False)
        view.horizontalHeader().setMinimumSectionSize(48)
        view.setIconSize(QSize(56, 42))
        view.verticalHeader().setDefaultSectionSize(52)
        for i, width in enumerate([64, 280, 140, 105, 176, 48, 65, 70]):
            view.setColumnWidth(i, width)
        view.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        view.setColumnWidth(8, 160)
        view.horizontalHeader().moveSection(8, 2)
        view.setColumnHidden(8, True)
    else:
        view.verticalHeader().setDefaultSectionSize(34)
    return view


def datetime_has_zone(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


class PlanDialog(AdaptiveDialog):
    def __init__(self, plan, mode, parent):
        super().__init__(parent)
        self.setWindowTitle('确认整理')
        self.resize(1060, 650)
        layout = QVBoxLayout(self)
        count = len(plan['items'])
        text = f'{count} 个文件    {human_size(sum(p["size"] for p in plan["items"]))}'
        layout.addWidget(QLabel(text))
        model = RowsModel(['原文件名', '目标文件名', '目标目录', '拍摄时间', '源目录'],
                          ['source_name', 'target_name', 'target_directory', 'capture_time', 'source_directory'])
        model.replace([dict(item, target_name=Path(item['target_path']).name,
                            target_directory=str(Path(item['target_path']).parent), source_directory=str(Path(item['source_path']).parent)) for item in plan['items']])
        self.model = model
        table = table_for(model)
        self.table = table
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.horizontalHeader().setStretchLastSection(False)
        table.setColumnWidth(0, 230)
        table.setColumnWidth(2, 235)
        table.setColumnWidth(3, 230)
        table.setColumnWidth(4, 180)
        table.horizontalHeader().moveSection(4, 1)
        table.setColumnHidden(4, len({r['source_directory'] for r in model.rows}) <= 1)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table, 2)
        self.file_details = QTextEdit()
        self.file_details.setReadOnly(True)
        self.file_details.setMinimumHeight(100)
        layout.addWidget(self.file_details, 1)
        def selection_details():
            selection = table.selectionModel().selectedRows()
            if not selection:
                self.file_details.clear()
                return
            row = model.rows[selection[0].row()]
            self.file_details.setPlainText('\n\n'.join([
                '原文件名    ' + row['source_name'], '源目录    ' + row['source_directory'], '目标文件名    ' + row['target_name'],
                '目标目录    ' + row['target_directory']]))
        table.selectionModel().selectionChanged.connect(selection_details)
        if model.rows:
            table.selectRow(0)
        layout.addWidget(QLabel('校验成功后删除源文件；重复文件保留在源目录。' if mode == 'move' else '源文件保留。'))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        proceed = buttons.addButton(f'确认{"移动" if mode == "move" else "复制"} {count} 个文件', QDialogButtonBox.ButtonRole.AcceptRole)
        proceed.setObjectName('primary')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ColorDialog(AdaptiveDialog):
    def __init__(self, rows, parent, private_count=0):
        super().__init__(parent)
        self.setWindowTitle('Action 6 D-Log M 颜色还原')
        self.resize(780, 530)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f'主 MP4    {len(rows)} 个    Rec.709'))
        model = RowsModel(['文件名', '路径'], ['observed_name', 'current_path'])
        model.replace(rows)
        self.files = table_for(model)
        self.files.setModel(model)
        self.files.setColumnWidth(0, 260)
        self.model = model
        layout.addWidget(self.files, 1)
        self.mode = QComboBox()
        self.mode.addItem('硬件优先（Intel / NVIDIA / AMD）', 'auto')
        self.mode.addItem('CPU 高质量', 'cpu')
        layout.addWidget(self.mode)
        self.output_mode = QComboBox()
        self.output_mode.addItem('另存还原版（保留原文件）', 'copy')
        self.output_mode.addItem('覆盖原文件（不保留原片备份）', 'replace')
        settings = getattr(parent, 'settings', None)
        self.output_mode.setCurrentIndex(self.output_mode.findData(settings.value('color_output', 'replace') if settings else 'replace'))
        if settings:
            self.mode.setCurrentIndex(max(0, self.mode.findData(settings.value('color_encoder', 'auto'))))
        layout.addWidget(self.output_mode)
        warning = QLabel('保留分辨率、帧率和位深；重新编码不等于无损。LRF 不调色。')
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.confirm = QCheckBox('确认所选文件尚未还原，且为 Action 6 的 D-Log M 素材')
        layout.addWidget(self.confirm)
        self.private_warning = QLabel()
        self.private_warning.setWordWrap(True)
        self.private_warning.setVisible(bool(private_count))
        layout.addWidget(self.private_warning)
        self.confirm_private = QCheckBox('允许还原版移除大疆专有数据（djmd / dbgi）')
        self.confirm_private.setVisible(bool(private_count))
        layout.addWidget(self.confirm_private)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.start = buttons.addButton('还原并另存', QDialogButtonBox.ButtonRole.AcceptRole)
        self.start.setEnabled(False)
        self.start.setObjectName('primary')
        def update_start():
            self.start.setEnabled(self.confirm.isChecked() and (not private_count or self.confirm_private.isChecked()))
        self.confirm.toggled.connect(update_start)
        self.confirm_private.toggled.connect(update_start)
        def output_changed():
            self.confirm.setChecked(False)
            self.confirm_private.setChecked(False)
            replace = self.output_mode.currentData() == 'replace'
            self.start.setText('还原并覆盖原文件' if replace else '还原并另存')
            self.private_warning.setText(f'{private_count} 个视频含大疆专有数据。' +
                ('覆盖后这些数据无法恢复，可能影响后期防抖。' if replace else
                 '还原版不含这些数据；完整数据仍保留在原片中。'))
            update_start()
        self.output_mode.currentIndexChanged.connect(output_changed)
        output_changed()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self, data_dir=None):
        super().__init__()
        self.data_dir = Path(data_dir or default_database().parent)
        self.catalog = Catalog(self.data_dir / 'catalog.sqlite3')
        self.catalog.initialize()
        self.settings = QSettings(str(self.data_dir / 'settings.ini'), QSettings.Format.IniFormat)
        budget = max(64, min(8192, self.settings.value('cache_mib', 512, type=int)))
        self.cache = ThumbnailCache(self.data_dir / 'thumbnails', budget=budget * 1024**2)
        self.loader = PreviewLoader(self.cache)
        self.service = ImportService(self.catalog, self.cache)
        self.color_service = ColorService(self.catalog)
        self.processing_history = ProcessingHistory(self.catalog)
        self.location_service = LocationService(self.catalog)
        self.activity_service = ActivityService(self.catalog)
        self.watermark_service = WatermarkService(self.catalog)
        self.task_queue = TaskQueue(self.catalog, self.cache)
        self.loader.queue = self.task_queue
        self.loader.blocked = lambda:self.busy
        self.task_revision = -1
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.jobs = set()
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.pause.set()
        self.busy = False
        self.closing = False
        self.source_batch = ''
        self._source_roots = []
        self._scan_roots = []
        self._custom_extensions = PHOTO_EXTS | VIDEO_EXTS
        self.source_total = 0
        self.source_rows = []
        self.format_boxes = {}
        self.format_choices = None
        self.query_version = 0
        self.source_version = 0
        self.offset = 0
        self.library_total = 0
        self.import_filter = ''
        self.activity_filter = ''
        self.last_history_refresh = 0
        self.history_offset = 0
        self.history_version = 0
        self.history_detail_version = 0
        self.setWindowTitle('数字媒体整理工具 ' + VERSION)
        self.resize(1280, 850)
        self.setMinimumSize(760, 400)
        self._build()
        pinned = self.settings.value('always_on_top', False, type=bool)
        self.pin_button.setChecked(pinned)
        self.pin_button.setText('已置顶' if pinned else '置顶')
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, pinned)
        self.pin_button.toggled.connect(self.set_pinned)
        self.set_compact(self.settings.value('compact_list', False, type=bool))
        self.update_source_selection()
        self.refresh_library()
        self.refresh_history()

        self.task_timer = QTimer(self)
        self.task_timer.setInterval(250)
        self.task_timer.timeout.connect(self.poll_tasks)
        self.task_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        fit_window(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self,'task_panel'):
            self.fit_source_controls()

    def fit_source_controls(self):
        self.source_controls.layout().activate()
        height=self.source_controls.sizeHint().height()
        if self.height()<600 and self.task_panel.isVisible():
            height=min(height,40 if self.task_panel.expand.isChecked() else 120)
        self.source_controls_scroll.setFixedHeight(height+2)

    def _build(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(6)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        heading = QHBoxLayout()
        title = QLabel('数字媒体整理工具')
        title.setObjectName('title')
        heading.addWidget(title)
        heading.addStretch()
        self.pin_button = QPushButton('置顶')
        self.pin_button.setCheckable(True)
        heading.addWidget(self.pin_button)
        self.settings_button = QPushButton('设置')
        self.settings_button.clicked.connect(lambda: self.open_settings())
        heading.addWidget(self.settings_button)
        self.history_button = QPushButton('历史索引')
        self.history_button.clicked.connect(self.index_history)
        heading.addWidget(self.history_button)
        layout.addLayout(heading)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._import_tab(), '整理文件')
        self.tabs.addTab(self._library_tab(), '素材查询')
        self.tabs.addTab(self._history_tab(), '处理记录')
        layout.addWidget(self.tabs,1)
        bottom = QHBoxLayout()
        self.status = QLabel('准备就绪')
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bottom.addWidget(self.status, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(160)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.hide()
        bottom.addWidget(self.progress_bar)
        self.file_progress_bar = QProgressBar()
        self.file_progress_bar.setFixedWidth(180)
        self.file_progress_bar.hide(); bottom.addWidget(self.file_progress_bar)
        self.pause_button = QPushButton('暂停')
        self.pause_button.clicked.connect(self.toggle_pause)
        self.pause_button.setEnabled(False)
        self.pause_button.hide()
        self.stop_button = QPushButton('停止')
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        self.stop_button.hide()
        bottom.addWidget(self.pause_button)
        bottom.addWidget(self.stop_button)
        layout.addLayout(bottom)
        self.progress_detail = QLineEdit()
        self.progress_detail.setReadOnly(True)
        self.progress_detail.setAccessibleName('当前处理路径')
        self.progress_detail.hide(); layout.addWidget(self.progress_detail)
        self.task_panel = TaskPanel(self.task_queue, RowsModel, table_for, self)
        self.task_panel.expand.toggled.connect(lambda _:QTimer.singleShot(0,self.fit_source_controls))
        layout.addWidget(self.task_panel)
        self.setCentralWidget(central)

    def _path_row(self, name, key):
        row = QHBoxLayout()
        row.addWidget(QLabel(name))
        field = QLineEdit(str((self.settings.value('default_target', '') if key == 'target' else '') or self.settings.value(key, '')))
        row.addWidget(field, 1)
        button = QPushButton('选择文件夹')
        field.chooser = button
        button.clicked.connect(lambda: self.choose_path(field))
        row.addWidget(button)
        return row, field

    def _import_tab(self):
        widget = QWidget()
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(0,6,0,0);outer.setSpacing(5)
        self.source_controls=QWidget()
        layout = QVBoxLayout(self.source_controls)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(5)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.source_controls_scroll=QScrollArea()
        self.source_controls_scroll.setWidgetResizable(True)
        self.source_controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.source_controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.source_controls_scroll.setWidget(self.source_controls)
        self.source_controls.setAutoFillBackground(False)
        outer.addWidget(self.source_controls_scroll)
        source_row, self.source = self._path_row('源文件夹', 'source')
        self.source_folders_button = self.source.chooser
        self.scan_button = QPushButton('扫描文件')
        self.scan_button.setObjectName('primary')
        self.scan_button.clicked.connect(self.scan_source)
        source_row.addWidget(self.scan_button)
        layout.addLayout(source_row)
        scan_options = QHBoxLayout()
        scan_options.addWidget(QLabel('扫描范围'))
        self.scan_recursive = QCheckBox('包含子文件夹')
        scan_options.addWidget(self.scan_recursive)
        scan_options.addSpacing(16); scan_options.addWidget(QLabel('扫描类型'))
        self.scan_kind = QComboBox()
        for label, value in [('全部素材', 'all'), ('仅视频', 'video'), ('仅照片', 'photo'), ('自选格式', 'custom')]:
            self.scan_kind.addItem(label, value)
        scan_options.addWidget(self.scan_kind)
        self.scan_formats_button = QPushButton('选择格式')
        self.scan_formats_button.clicked.connect(self.choose_scan_formats)
        scan_options.addWidget(self.scan_formats_button)
        self.scan_format_summary = QLabel('全部支持格式')
        self.scan_format_summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        scan_options.addWidget(self.scan_format_summary, 1)
        layout.addLayout(scan_options)
        self.archive_options = QWidget()
        archive_layout = QVBoxLayout(self.archive_options)
        archive_layout.setContentsMargins(0, 0, 0, 0); archive_layout.setSpacing(5)
        target_row, self.target = self._path_row('目标文件夹', 'target')
        archive_layout.addLayout(target_row)
        options = QHBoxLayout()
        options.addWidget(QLabel('默认地点'))
        self.place = QLineEdit(str(self.settings.value('place', '')))
        self.place.setPlaceholderText('可选，例如 天津海边')
        self.place.setToolTip('仅用于未单独设置地点的素材；不写入文件 GPS。')
        options.addWidget(self.place)
        self.mode = QComboBox()
        self.mode.addItem('移动到目标', 'move')
        self.mode.addItem('复制到目标', 'copy')
        options.addWidget(self.mode)
        archive_layout.addLayout(options)
        layout.addWidget(self.archive_options)
        self.source_count = QLabel('共 0 个    已选 0 个')
        count_row = QHBoxLayout()
        count_row.addWidget(self.source_count, 1)
        self.archive_toggle=QPushButton('归档选项')
        self.archive_toggle.setCheckable(True);self.archive_toggle.setChecked(True)
        self.archive_toggle.toggled.connect(self.archive_options.setVisible)
        self.archive_toggle.toggled.connect(lambda _:QTimer.singleShot(0,self.fit_source_controls))
        count_row.addWidget(self.archive_toggle)
        layout.addLayout(count_row)
        self.format_scroll = QScrollArea()
        self.format_scroll.setWidgetResizable(True)
        self.format_scroll.setFixedHeight(42)
        self.format_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.format_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.format_scroll.setVisible(False)
        layout.addWidget(self.format_scroll)
        layout=outer
        bar = QHBoxLayout()
        select = QPushButton('全选')
        select.clicked.connect(lambda: self.source_table.selectAll())
        bar.addWidget(select)
        clear = QPushButton('清除选择')
        clear.clicked.connect(lambda: self.source_table.clearSelection())
        bar.addWidget(clear)
        self.source_compact = QCheckBox('紧凑列表')
        self.source_compact.toggled.connect(self.set_compact)
        bar.addWidget(self.source_compact)
        bar.addStretch()
        self.source_activity_button=QPushButton('活动与地点')
        self.source_activity_button.clicked.connect(lambda:self.edit_activity(False));bar.addWidget(self.source_activity_button)
        self.source_gps_button = QPushButton('添加定位')
        self.source_gps_button.clicked.connect(lambda: self.add_location(False))
        bar.addWidget(self.source_gps_button)
        self.source_watermark_button = QPushButton('照片水印')
        self.source_watermark_button.clicked.connect(lambda:self.add_watermark(False))
        bar.addWidget(self.source_watermark_button)
        self.color_button = QPushButton('D-Log M 颜色还原')
        self.color_button.clicked.connect(self.restore_color)
        self.color_button.setEnabled(False)
        bar.addWidget(self.color_button)
        self.preview_button = QPushButton('预览整理')
        self.preview_button.setObjectName('primary')
        self.preview_button.clicked.connect(self.preview_import)
        self.preview_button.setEnabled(False)
        bar.addWidget(self.preview_button)
        layout.addLayout(bar)
        self.source_model = MediaModel(self.loader)
        self.source_table = table_for(self.source_model, True)
        self.source_table.setMinimumHeight(86)
        self.source_table.selectionModel().selectionChanged.connect(self.update_source_selection)
        self.source_model.modelReset.connect(self.update_source_selection)
        self.source_model.modelReset.connect(lambda: self.source_table.setColumnHidden(2, not any(r.get('original_name') for r in self.source_model.rows)))
        self.source_table.doubleClicked.connect(lambda i: self.show_details(self.source_model.rows[i.row()]))
        layout.addWidget(self.source_table, 1)
        try:
            saved = json.loads(str(self.settings.value('source_roots', '[]')))
            if isinstance(saved, list) and saved and all(isinstance(p, str) for p in saved):
                self.set_source_paths(saved)
        except (ValueError, TypeError):
            pass
        self.source.textEdited.connect(lambda _: self.invalidate_source())
        self.scan_recursive.toggled.connect(self.invalidate_source)
        self.scan_kind.currentIndexChanged.connect(self.scan_kind_changed)
        return widget

    def _library_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('查询已导入素材：文件名、活动、地点、同行人或备注')
        self.search.returnPressed.connect(lambda: self.refresh_library(reset=True))
        bar.addWidget(self.search, 1)
        self.kind_filter = QComboBox()
        for label, value in [('全部类型', ''), ('照片', 'photo'), ('视频', 'video')]:
            self.kind_filter.addItem(label, value)
        self.gps_filter = QComboBox()
        for label, value in [('全部定位', ''), ('有 GPS', 'present'), ('无 GPS', 'missing'), ('未确认', 'unknown')]:
            self.gps_filter.addItem(label, value)
        for box in (self.kind_filter, self.gps_filter):
            bar.addWidget(box)
            box.currentIndexChanged.connect(lambda: self.refresh_library(reset=True))
        query = QPushButton('查询')
        query.clicked.connect(lambda: self.refresh_library(reset=True))
        bar.addWidget(query)
        all_rows = QPushButton('全部已导入')
        all_rows.clicked.connect(self.clear_import_filter)
        bar.addWidget(all_rows)
        layout.addLayout(bar)
        self.activity_scope=QLabel();self.activity_scope.setObjectName('secondary');self.activity_scope.hide();layout.addWidget(self.activity_scope)
        self.library_model = MediaModel(self.loader)
        self.library_table = table_for(self.library_model, True)
        self.library_table.doubleClicked.connect(lambda i: self.show_details(self.library_model.rows[i.row()]))
        layout.addWidget(self.library_table, 1)
        footer = QHBoxLayout()
        self.library_detail_button = QPushButton('查看详情')
        self.library_detail_button.clicked.connect(self.library_details)
        footer.addWidget(self.library_detail_button)
        self.library_folder_button = QPushButton('打开所在文件夹')
        self.library_folder_button.clicked.connect(self.open_library_folder)
        footer.addWidget(self.library_folder_button)
        self.library_activity_button=QPushButton('活动与地点')
        self.library_activity_button.clicked.connect(lambda:self.edit_activity(True));footer.addWidget(self.library_activity_button)
        self.library_gps_button = QPushButton('添加定位')
        self.library_gps_button.clicked.connect(lambda: self.add_location(True))
        footer.addWidget(self.library_gps_button)
        self.library_watermark_button = QPushButton('照片水印')
        self.library_watermark_button.clicked.connect(lambda:self.add_watermark(True))
        footer.addWidget(self.library_watermark_button)
        self.library_compact = QCheckBox('紧凑列表')
        self.library_compact.toggled.connect(self.set_compact)
        footer.addWidget(self.library_compact)
        footer.addStretch()
        layout.addLayout(footer)
        footer = QHBoxLayout()
        footer.addStretch()
        self.page_label = QLabel()
        footer.addWidget(self.page_label)
        self.prev_button = QPushButton('上一页')
        self.prev_button.clicked.connect(lambda: self.change_page(-1))
        self.next_button = QPushButton('下一页')
        self.next_button.clicked.connect(lambda: self.change_page(1))
        footer.addWidget(self.prev_button)
        footer.addWidget(self.next_button)
        layout.addLayout(footer)
        self.library_table.selectionModel().selectionChanged.connect(self.update_library_actions)
        self.library_model.modelReset.connect(self.update_library_actions)
        self.update_library_actions()
        return widget

    def update_library_actions(self, *_):
        rows = self.library_table.selectionModel().selectedRows()
        has_photo = any(self.library_model.rows[index.row()]['kind'] == 'photo' for index in rows)
        self.library_detail_button.setEnabled(bool(rows))
        self.library_folder_button.setEnabled(bool(rows))
        self.library_gps_button.setEnabled(not self.busy and any(supports_gps(self.library_model.rows[i.row()]) for i in rows))
        self.library_activity_button.setEnabled(not self.busy and bool(rows))
        self.library_watermark_button.setEnabled(not self.busy and has_photo)

    def _history_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        bar = QHBoxLayout()
        self.record_filter = QComboBox()
        for label, value in [('全部记录', ''), ('导入', 'import'), ('颜色调整', 'color'), ('写入定位', 'gps'), ('照片水印', 'watermark'), ('活动与备注','activity')]:
            self.record_filter.addItem(label, value)
        self.record_filter.currentIndexChanged.connect(self.reset_history_filter)
        bar.addWidget(self.record_filter)
        bar.addStretch()
        refresh = QPushButton('刷新')
        refresh.clicked.connect(self.refresh_history)
        bar.addWidget(refresh)
        self.browse_history_button = QPushButton('查看素材')
        self.browse_history_button.clicked.connect(self.browse_import)
        bar.addWidget(self.browse_history_button)
        self.batch_watermark_button = QPushButton('本批照片水印')
        self.batch_watermark_button.clicked.connect(self.watermark_import_batch)
        bar.addWidget(self.batch_watermark_button)
        self.retry_button = QPushButton('继续 / 重试')
        self.retry_button.clicked.connect(self.retry_import)
        bar.addWidget(self.retry_button)
        layout.addLayout(bar)
        self.imports_model = RowsModel(['处理时间', '类型', '处理对象', '完成照片', '完成视频', '跳过', '异常', '状态'],
            ['created_at', 'type_label', 'subject', 'completed_photos', 'completed_videos', 'skipped', 'errors', 'display_status'])
        self.imports_table = table_for(self.imports_model)
        self.imports_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.imports_table.horizontalHeader().setStretchLastSection(False)
        for column, width in enumerate([180, 90, 240, 70, 70, 50, 50, 110]):
            self.imports_table.setColumnWidth(column, width)
        self.imports_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.imports_table.selectionModel().selectionChanged.connect(self.load_import_items)
        record_panel = QWidget()
        record_layout = QVBoxLayout(record_panel)
        record_layout.setContentsMargins(0, 0, 0, 0)
        record_layout.addWidget(self.imports_table, 1)
        pages = QHBoxLayout()
        pages.addStretch()
        self.history_page_label = QLabel()
        pages.addWidget(self.history_page_label)
        self.history_prev = QPushButton('较新记录')
        self.history_prev.clicked.connect(lambda: self.change_history_page(-1))
        self.history_next = QPushButton('较早记录')
        self.history_next.clicked.connect(lambda: self.change_history_page(1))
        pages.addWidget(self.history_prev)
        pages.addWidget(self.history_next)
        record_layout.addLayout(pages)
        self.history_details = QTextEdit()
        self.history_details.setReadOnly(True)
        self.history_details.setPlaceholderText('选择记录查看详情')
        self.history_details.setMinimumHeight(80)
        self.history_splitter = QSplitter(Qt.Orientation.Vertical)
        self.history_splitter.setChildrenCollapsible(False)
        self.history_splitter.setHandleWidth(8)
        self.history_splitter.addWidget(record_panel)
        self.history_splitter.addWidget(self.history_details)
        self.history_splitter.setSizes([260, 200])
        layout.addWidget(self.history_splitter, 1)
        return widget

    def job(self, fn, done, *, failed=None, progress=None):
        job = Job(fn)
        self.jobs.add(job)
        job.signals.done.connect(done)
        job.signals.failed.connect(failed or self.error)
        if progress:
            job.signals.progress.connect(progress)
        job.signals.finished.connect(lambda j=job: self.jobs.discard(j))
        self.pool.start(job)
        return job

    def set_pinned(self, enabled):
        self.pin_button.setText('已置顶' if enabled else '置顶')
        self.settings.setValue('always_on_top', enabled)
        self.settings.sync()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        self.show()

    def open_settings(self, tab=0):
        if self.busy or self.task_queue.active:
            return
        dialog = SettingsDialog(self, tab)
        dialog.exec()

    def update_formats(self):
        counts = Counter(Path(r['current_path']).suffix.lower() for r in self.source_rows)
        old = self.format_scroll.takeWidget()
        if old:
            old.deleteLater()
        container = QWidget()
        bar = QHBoxLayout(container)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(QLabel('列表筛选'))
        reset = QPushButton('全部格式')
        reset.clicked.connect(self.reset_formats)
        bar.addWidget(reset)
        self.format_boxes = {}
        for ext, count in sorted(counts.items()):
            box = QCheckBox(f'{ext.lstrip(".").upper()}  {count}')
            box.setChecked(self.format_choices is None or ext in self.format_choices)
            box.toggled.connect(self.filter_formats)
            self.format_boxes[ext] = box
            bar.addWidget(box)
        bar.addStretch()
        self.format_scroll.setWidget(container)
        container.setAutoFillBackground(False)
        self.format_scroll.setVisible(bool(counts))

    def reset_formats(self):
        self.format_choices = None
        for box in self.format_boxes.values():
            box.blockSignals(True); box.setChecked(True); box.blockSignals(False)
        self.apply_source_filter(select=True)

    def filter_formats(self, *_):
        self.format_choices = {ext for ext, box in self.format_boxes.items() if box.isChecked()}
        self.apply_source_filter(select=True)

    def apply_source_filter(self, select=False):
        rows = [r for r in self.source_rows if self.format_choices is None or
                Path(r['current_path']).suffix.lower() in self.format_choices]
        selected = {self.source_model.rows[i.row()]['id'] for i in self.source_table.selectionModel().selectedRows()}
        self.source_model.replace(rows[:10000])
        self.source_table.setColumnHidden(8, len({str(Path(r['current_path']).parent) for r in rows}) <= 1)
        if select:
            self.source_table.selectAll()
        else:
            selection = self.source_table.selectionModel()
            from PySide6.QtCore import QItemSelectionModel
            for index, row in enumerate(self.source_model.rows):
                if row['id'] in selected:
                    selection.select(self.source_model.index(index, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self.update_source_selection()

    def set_compact(self, compact):
        for control in (self.source_compact, self.library_compact):
            control.blockSignals(True)
            control.setChecked(compact)
            control.blockSignals(False)
        for view, model in ((self.source_table, self.source_model), (self.library_table, self.library_model)):
            model.compact = compact
            view.setColumnHidden(0, compact)
            view.verticalHeader().setDefaultSectionSize(34 if compact else 52)
            view.viewport().update()
        self.settings.setValue('compact_list', compact)

    def update_source_selection(self, *_):
        indices = self.source_table.selectionModel().selectedRows()
        selected = [self.source_model.rows[i.row()] for i in indices if i.row() < len(self.source_model.rows)]
        count = len(selected)
        summary = f'共 {self.source_total} 个    已选 {count} 个'
        if count:
            summary += '    ' + human_size(sum(r['size'] for r in selected))
        if self.source_total > len(self.source_model.rows):
            summary += f'    当前显示 {len(self.source_model.rows)} 个'
        self.source_count.setText(summary)
        self.preview_button.setText(f'预览整理（{count}）' if count else '预览整理')
        self.preview_button.setEnabled(not self.busy and bool(count))
        mp4_count = sum(r['kind'] == 'video' and Path(r['current_path']).suffix.lower() == '.mp4' for r in selected)
        self.color_button.setEnabled(not self.busy and bool(mp4_count))
        self.source_gps_button.setEnabled(not self.busy and any(supports_gps(r) for r in selected))
        self.source_activity_button.setEnabled(not self.busy and bool(selected))
        self.source_watermark_button.setEnabled(not self.busy and any(r['kind']=='photo' for r in selected))
        has_rows = bool(self.source_model.rows)
        self.scan_button.setText('重新扫描' if has_rows else '扫描文件')
        for button, primary in ((self.scan_button, not has_rows), (self.preview_button, has_rows)):
            name = 'primary' if primary else ''
            if button.objectName() != name:
                button.setObjectName(name)
                button.style().unpolish(button)
                button.style().polish(button)
                button.update()
        self.preview_button.setMinimumWidth(self.preview_button.fontMetrics().horizontalAdvance(self.preview_button.text()) + 28)
        self.preview_button.updateGeometry()
        if self.centralWidget():
            self.source_table.parentWidget().layout().activate()

    def set_busy(self, active, transfer=False):
        self.busy = active
        for button in (self.scan_button, self.preview_button, self.history_button, self.retry_button, self.color_button, self.settings_button, self.library_gps_button, self.library_watermark_button, self.format_scroll):
            button.setEnabled(not active)
        self.update_source_selection()
        self.update_library_actions()
        self.update_history_actions()
        for field in (self.source, self.target, self.place, self.mode, self.source_folders_button,
                      self.source.chooser, self.target.chooser, self.scan_recursive, self.scan_kind, self.scan_formats_button):
            field.setEnabled(not active)
        self.archive_options.setVisible(not active and self.archive_toggle.isChecked())
        if active:
            self.progress_bar.show()
            self.progress_bar.setFormat('处理中')
            self.file_progress_bar.hide()
            self.progress_detail.clear(); self.progress_detail.hide()
        self.stop_button.setVisible(active)
        self.pause_button.setVisible(active and transfer)
        self.stop_button.setEnabled(active)
        self.pause_button.setEnabled(active and transfer)
        self.pause_button.setText('暂停')
        if active:
            self.progress_bar.setRange(0, 0)
        elif self.progress_bar.maximum() == 0:
            self.progress_bar.hide()
        if not active:
            self.progress_detail.hide(); self.file_progress_bar.hide()
        if active:
            self.cancel.clear()
            self.pause.set()

    def error(self, message):
        self.status.setText('发生异常')
        QMessageBox.warning(self, '操作未完成', message)

    def task_error(self, message):
        self.set_busy(False)
        self.error(message)
        self.refresh_history()
        if self.closing:
            self.close()

    def choose_path(self, field):
        if self.busy:
            return
        if field is self.source:
            self.choose_sources()
            return
        value = QFileDialog.getExistingDirectory(self, '选择文件夹', field.text())
        if value:
            if field is self.source:
                self.set_source_paths([value]); self.invalidate_source()
            else:
                field.setText(value)

    def source_paths(self):
        return list(self._source_roots) if len(self._source_roots) > 1 else ([self.source.text().strip()] if self.source.text().strip() else [])

    def set_source_paths(self, paths):
        self._source_roots = list(dict.fromkeys(map(str, paths)))
        many = len(self._source_roots) > 1
        self.source.setReadOnly(many)
        self.source.setText(f'已选 {len(self._source_roots)} 个文件夹' if many else (self._source_roots[0] if self._source_roots else ''))
        self.source.setToolTip('\n'.join(self._source_roots))
        self.source_folders_button.setText(f'选择文件夹（{len(paths)}）' if many else '选择文件夹')

    def choose_sources(self):
        if self.busy: return
        dialog = SourceFoldersDialog(self.source_paths(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_source_paths(dialog.paths()); self.invalidate_source()

    def scan_extensions(self):
        return {'all': PHOTO_EXTS | VIDEO_EXTS, 'photo': PHOTO_EXTS,
                'video': VIDEO_EXTS, 'custom': self._custom_extensions}[self.scan_kind.currentData()]

    def scan_kind_changed(self):
        selected = self.scan_extensions()
        full = '、'.join(e[1:].upper() for e in sorted(selected))
        kind = self.scan_kind.currentData()
        if kind in ('all', 'photo', 'video'):
            text = {'all':'全部支持格式', 'photo':'照片格式', 'video':'视频格式'}[kind] + f'（{len(selected)} 种）'
        else:
            text = full if len(selected)<=4 else '、'.join(e[1:].upper() for e in sorted(selected)[:3]) + f' 等 {len(selected)} 种'
        self.scan_format_summary.setText(text)
        self.scan_format_summary.setToolTip(full)
        self.invalidate_source()

    def choose_scan_formats(self):
        if self.busy: return
        dialog = ScanFormatsDialog(self.scan_extensions(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._custom_extensions = dialog.selected()
            self.scan_kind.setCurrentIndex(self.scan_kind.findData('custom'))
            self.scan_kind_changed()

    def invalidate_source(self, *_):
        if self.busy: return
        self.source_version += 1
        self.source_batch = ''; self._scan_roots = []
        self.source_total = 0; self.source_rows = []; self.format_choices = None
        self.source_model.replace([]); self.update_formats()
        self.status.setText('范围已修改，请重新扫描')

    def save_settings(self):
        for key, field in [('source', self.source), ('target', self.target), ('place', self.place)]:
            self.settings.setValue(key, field.text() if key != 'source' else (self.source_paths() or [''])[0])
        self.settings.setValue('source_roots', json.dumps(self.source_paths(), ensure_ascii=False))
        self.settings.sync()

    def scan_source(self):
        root = self.source_paths()
        if not root:
            self.choose_path(self.source)
            return
        self.save_settings()
        self.source_total = 0
        self.source_rows = []
        self.format_choices = None
        self.update_formats()
        self.source_model.replace([])
        self.source_batch = ''
        self._scan_roots = list(root)
        recursive, extensions = self.scan_recursive.isChecked(), set(self.scan_extensions())
        self.set_busy(True)
        self.status.setText('扫描源文件夹')
        self.job(lambda progress: self.catalog.scan(root, self.cancel, progress, batch_kind='source_scan', recursive=recursive, extensions=extensions, file_guard=self.task_queue.read_guard),
                 self.source_finished, failed=self.task_error, progress=self.scan_progress)

    def scan_progress(self, stats):
        self.source_batch = stats['batch_id']
        self.status.setText(f"扫描中    照片 {stats['photos']}    视频 {stats['videos']}    异常 {stats['errors']}")
        self.progress_detail.setText(stats.get('current_path', ''))
        self.progress_detail.setCursorPosition(0); self.progress_detail.show()
        self.load_source_batch()

    def load_source_batch(self, select=False, selected_ids=None):
        self.source_version += 1
        version, batch = self.source_version, self.source_batch
        def done(result):
            if version != self.source_version:
                return
            keep_ids=selected_ids
            if selected_ids is not None:
                keep_ids={self.source_model.rows[i.row()]['id'] for i in self.source_table.selectionModel().selectedRows()}
            total, rows = result
            self.source_total = total
            self.source_rows = rows
            self.update_formats()
            self.apply_source_filter(select=select)
            if keep_ids is not None:
                from PySide6.QtCore import QItemSelectionModel
                selection=self.source_table.selectionModel()
                for index,row in enumerate(self.source_model.rows):
                    if row['id'] in keep_ids:
                        selection.select(self.source_model.index(index,0),QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        def collect(_):
            rows = []
            while True:
                total, page = self.catalog.search(batch_id=batch, limit=1000, offset=len(rows))
                rows.extend(page)
                if not page or len(rows) >= total:
                    return total, rows
        self.job(collect, done)

    def source_finished(self, stats):
        self.source_batch = stats['batch_id']
        self.set_busy(False)
        self.load_source_batch(select=True)
        self.status.setText(f"扫描{'已停止' if stats['status']=='cancelled' else '完成'}    照片 {stats['photos']}    视频 {stats['videos']}    异常 {stats['errors']}")
        self.refresh_library()
        if stats['status'] != 'cancelled' and self.settings.value('auto_color', True, type=bool) and not self.closing:
            self.check_lrf_candidates()
        if self.closing:
            self.close()

    def check_lrf_candidates(self):
        batch, root = self.source_batch, self.source_paths()
        def collect(_):
            rows, offset = [], 0
            while True:
                total, page = self.catalog.search(batch_id=batch, kind='video', limit=1000, offset=offset)
                rows.extend(page)
                offset += len(page)
                if not page or offset >= total: break
            # Read only companion names when LRF metadata was excluded by the prefilter.
            known = {str(Path(r['current_path'])).casefold() for r in rows}
            for row in list(rows):
                source = Path(row['current_path'])
                proxy = source.with_suffix('.LRF')
                if source.suffix.lower() == '.mp4' and str(proxy).casefold() not in known and proxy.is_file():
                    rows.append(dict(row, id='proxy-' + row['id'], current_path=str(proxy)))
            return self.color_service.auto_candidates(rows)
        def done(result):
            if self.closing or self.busy or batch != self.source_batch or root != self.source_paths(): return
            candidates, missing = result
            if not candidates and not missing: return
            box = QMessageBox(self)
            box.setWindowTitle('发现 LRF 素材')
            box.setText(f'找到 {len(candidates)} 个带同名 LRF 的主 MP4，处理状态未知。\n请核对是否已还原；LRF 不能证明尚未还原。')
            if missing:
                box.setInformativeText(f'{len(missing)} 个 LRF 未找到同目录、同名 MP4，跳过。')
                box.setDetailedText('\n'.join(missing))
            open_button = box.addButton('选择还原方式', QMessageBox.ButtonRole.AcceptRole) if candidates else None
            box.addButton('暂不处理' if candidates else '关闭', QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if open_button and box.clickedButton() == open_button:
                self.restore_color(candidates)
        self.job(collect, done)

    def preview_import(self):
        if self.busy:
            return
        selected = self.source_table.selectionModel().selectedRows()
        rows = [dict(self.source_model.rows[i.row()]) for i in selected]
        if not rows:
            self.error('请选择需要整理的文件。')
            return
        if self.task_queue.conflicts(rows):
            self.error('这些素材还在排队或处理中，请完成后再预览整理，避免预览与实际文件不一致。')
            return
        if not self.target.text().strip():
            self.choose_path(self.target)
            return
        options = dict(place=self.place.text().strip(), birthday=str(self.settings.value('birthday', '')).strip(),
            template=str(self.settings.value('template', '{date}_{weekday}_{device}_{place}_{original}')).strip(),
            layout=self.settings.value('folder_layout', 'day'), keep_named=self.settings.value('keep_named', True, type=bool))
        self.save_settings()
        source, target, mode = self._scan_roots or self.source_paths(), self.target.text(), self.mode.currentData()
        self.set_busy(True)
        self.status.setText('生成整理预览')
        def done(plan):
            self.set_busy(False)
            if self.closing:
                self.close()
                return
            dialog = PlanDialog(plan, mode, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                try:
                    identifier = self.service.save_plan(plan, mode)
                    self.history_offset = 0
                    self.run_import(identifier)
                except Exception as exc:
                    self.error(str(exc))
            else:
                self.status.setText('未执行整理')
        self.job(lambda _: self.service.build_plan(rows, source, target, options), done, failed=self.task_error)

    def restore_color(self, candidates=None):
        if self.busy:
            return
        rows = candidates if isinstance(candidates, list) else [dict(self.source_model.rows[i.row()]) for i in self.source_table.selectionModel().selectedRows()]
        rows = [r for r in rows if r['kind'] == 'video' and Path(r['current_path']).suffix.lower() == '.mp4']
        if not rows:
            self.error('请选择需要还原的主 MP4。')
            return
        root = self._scan_roots or self.source_paths()
        self.set_busy(True)
        self.status.setText('检查视频附加数据')
        def inspect(_):
            count = 0
            for row in rows:
                if self.cancel.is_set():
                    return None
                try:
                    count += bool(private_tracks(color_profile(row['current_path'])))
                except Exception:
                    pass  # The per-file execution check reports unsupported/broken files.
            return count
        def ready(private_count):
            self.set_busy(False)
            if self.closing:
                self.close()
                return
            if private_count is None:
                self.status.setText('已停止检查')
                return
            dialog = ColorDialog(rows, self, private_count=private_count)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.status.setText('未执行颜色还原')
                return
            confirmed, mode = dialog.confirm.isChecked(), dialog.mode.currentData()
            output_mode = dialog.output_mode.currentData()
            allow_loss = dialog.confirm_private.isChecked()
            self.enqueue('color', rows, dict(source_root=root, confirmed_dlogm=confirmed,
                mode=mode, output_mode=output_mode, allow_private_data_loss=allow_loss))
        self.job(inspect, ready, failed=self.task_error)

    def color_progress(self, event):
        total, processed = event.get('total', 0), event.get('processed', 0)
        counter = f"    第 {event.get('index', processed+1)} / {total} 个" if total else ''
        waiting = '    正在停止' if self.cancel.is_set() else ('    当前文件后暂停' if not self.pause.is_set() else '')
        encoder = event.get('encoder', '')
        encoder = ('Intel 硬件' if encoder.endswith('_qsv') else 'NVIDIA 硬件' if encoder.endswith('_nvenc') else 'CPU' if encoder in ('libx264', 'libx265') else '')
        self.status.setText(f"{event['phase']}{counter}    {encoder}{waiting}")
        self.progress_detail.setText(event.get('path') or event.get('filename', ''))
        self.progress_detail.setCursorPosition(0); self.progress_detail.show()
        if total:
            self.progress_bar.setRange(0, total); self.progress_bar.setValue(processed)
            self.progress_bar.setFormat('已处理 %v / %m'); self.progress_bar.show()
        percent = event.get('percent')
        if percent is not None:
            self.file_progress_bar.setRange(0, 1000)
            self.file_progress_bar.setValue(round(percent * 10))
            self.file_progress_bar.setFormat(f'当前视频 {percent:.1f}%')
            self.file_progress_bar.show()
        else:
            self.file_progress_bar.hide()
        if time.monotonic() - self.last_history_refresh > 0.6:
            self.last_history_refresh = time.monotonic()
            self.refresh_history()

    def color_finished(self, result):
        self.set_busy(False)
        self.load_source_batch(select=True)
        self.refresh_library()
        self.refresh_history()
        self.status.setText(f"颜色还原{'已停止' if result['stopped'] else '完成'}    完成 {result['complete']}    跳过 {result['skipped']}    异常 {len(result['errors'])}")
        if result['errors'] and not self.closing:
            box = QMessageBox(QMessageBox.Icon.Warning, '颜色还原有异常',
                f"{len(result['errors'])} 个文件未完成。", parent=self)
            box.setDetailedText('\n\n'.join(result['errors']))
            box.exec()
        if self.closing:
            self.close()

    def edit_activity(self,library=False):
        if self.busy:return
        view,model=(self.library_table,self.library_model) if library else (self.source_table,self.source_model)
        ids=[model.rows[i.row()]['id'] for i in view.selectionModel().selectedRows()]
        if not ids:return
        if self.task_queue.conflicts([r for r in model.rows if r['id'] in ids]):
            self.error('这些素材还有处理任务，请完成后再编辑活动与地点。');return
        self.set_busy(True)
        def ready(data):
            self.set_busy(False)
            rows,events=data
            if not rows:return
            dialog=ActivityDialog(rows,events,self)
            if dialog.exec()!=QDialog.DialogCode.Accepted:return
            self.status.setText(f"活动与备注已保存    {dialog.save_result['total']} 个素材")
            if self.source_batch:self.load_source_batch(select=False)
            self.refresh_library();self.refresh_history()
        self.job(lambda _:(self.activity_service.rows(ids),self.activity_service.activities()),ready,failed=self.task_error)

    def show_activity(self,identifier,name=None):
        self.activity_filter=identifier;self.import_filter='';self.search.clear()
        self.activity_scope.setText('活动    '+(name or '所选活动'));self.activity_scope.show()
        self.kind_filter.setCurrentIndex(0);self.gps_filter.setCurrentIndex(0)
        self.refresh_library(reset=True);self.tabs.setCurrentIndex(1)

    def add_location(self, library=False):
        if self.busy: return
        view, model = (self.library_table, self.library_model) if library else (self.source_table, self.source_model)
        selected = [dict(model.rows[i.row()]) for i in view.selectionModel().selectedRows()]
        rows = [r for r in selected if supports_gps(r)]
        if not rows:
            self.error('请选择支持的照片或 MP4、MOV 视频。'); return
        dialog = LocationDialog(len(rows), self,video_count=sum(r['kind']=='video' for r in rows))
        if len(rows) != len(selected):
            dialog.message.setText(f'已排除 {len(selected)-len(rows)} 个不支持的格式。')
        if dialog.exec() != QDialog.DialogCode.Accepted: return
        value, overwrite = dialog.value(), dialog.overwrite.isChecked()
        write_mode = dialog.write_mode.currentData()
        self.enqueue('gps', rows, dict(**value, overwrite=overwrite, write_mode=write_mode))

    def location_finished(self, result):
        self.set_busy(False)
        if self.source_batch:
            self.load_source_batch(select=True)
        self.refresh_library(); self.refresh_history()
        self.status.setText(f"定位{'已停止' if result['stopped'] else '完成'}    写入 {result['complete']}    跳过 {result['skipped']}    异常 {len(result['errors'])}"
                           + (f"    恢复上次位置 {result['recovered']}" if result['recovered'] else ''))
        if result['errors'] and not self.closing:
            box = QMessageBox(QMessageBox.Icon.Warning, '定位写入有异常', f"{len(result['errors'])} 个素材未完成。", parent=self)
            box.setDetailedText('\n\n'.join(result['errors'] + result.get('skip_reasons', []))); box.exec()
        elif result.get('skip_reasons') and not self.closing:
            box = QMessageBox(QMessageBox.Icon.Information, '部分素材未写入', f"{len(result['skip_reasons'])} 个视频不支持快速写入，可改选保护写入。", parent=self)
            box.setDetailedText('\n\n'.join(result['skip_reasons'])); box.exec()
        if self.closing: self.close()

    def add_watermark(self, library=False):
        if self.busy:return
        view,model=(self.library_table,self.library_model) if library else (self.source_table,self.source_model)
        rows=[dict(model.rows[i.row()]) for i in view.selectionModel().selectedRows()]
        self.prepare_watermark(rows)

    def watermark_import_batch(self):
        record=self.selected_import()
        if self.busy or not record or record['record_type']!='import':return
        identifier=record['id'];self.set_busy(True);self.status.setText('读取本批照片')
        def done(rows):
            self.set_busy(False)
            if self.closing:self.close();return
            self.prepare_watermark(rows)
        self.job(lambda _:self.watermark_service.import_photos(identifier),done,failed=self.task_error)

    def prepare_watermark(self, selected):
        rows=[r for r in selected if r['kind']=='photo' and Path(r['current_path']).suffix.lower() in WATERMARK_TYPES and not r.get('watermark_revision')]
        if not rows:self.error('请选择 JPG、HEIC、PNG 或 TIFF 原照片。');return
        excluded=len(selected)-len(rows)
        self.set_busy(True)
        def ready(enriched):
            self.set_busy(False)
            if self.closing:self.close();return
            dialog=WatermarkDialog(enriched,self)
            if excluded:dialog.message.setText(f'已排除 {excluded} 个视频、不支持的格式或水印成品。')
            if dialog.exec()!=QDialog.DialogCode.Accepted:return
            options,target=dialog.options(),dialog.target.text().strip()
            self.enqueue('watermark', enriched, dict(target_root=target,options=options))
        self.job(lambda _:self.watermark_service.enrich(rows),ready,failed=self.task_error)

    def watermark_finished(self,result):
        self.set_busy(False);self.refresh_library();self.refresh_history()
        self.tabs.setCurrentIndex(2)
        self.status.setText(f"照片水印{'已停止' if result['stopped'] else '完成'}    另存 {result['complete']}    跳过 {result['skipped']}    异常 {len(result['errors'])}")
        if result['errors'] and not self.closing:
            box=QMessageBox(QMessageBox.Icon.Warning,'水印导出有异常',f"{len(result['errors'])} 个素材未完成。",parent=self)
            box.setDetailedText('\n\n'.join(result['errors']));box.exec()
        if self.closing:self.close()

    def run_import(self, identifier):
        items=[i for i in self.service.items(identifier) if i['status'] not in ('complete','skipped')]
        rows=[dict(id=i['media_id'],current_path=i['source_path'],kind=i['kind'],size=i['size'],mtime_ns=i['mtime_ns']) for i in items]
        self.enqueue('import',rows,dict(import_id=identifier,items={i['media_id']:i['id'] for i in items}))

    def enqueue(self, action, rows, options):
        follow=None
        candidates=self.task_queue.output_predecessors(rows) if action in ('gps','watermark') else {}
        if candidates:
            box=QMessageBox(self);box.setWindowTitle('选择处理对象')
            box.setText(f'{len(candidates)} 个素材有另存任务。后续操作作用于：')
            box.setInformativeText('选择新成品时，等待另存成功；其余素材仍处理原片。')
            original=box.addButton('原片',QMessageBox.ButtonRole.AcceptRole)
            output=box.addButton('新成品',QMessageBox.ButtonRole.ActionRole)
            box.addButton('取消',QMessageBox.ButtonRole.RejectRole);box.setDefaultButton(original)
            box.setDetailedText('\n'.join(r['current_path'] for r in rows if r['id'] in candidates))
            for button in box.buttons():
                if button.text() not in ('原片','新成品','取消'):
                    button.setText('文件清单');button.clicked.connect(lambda _=False,b=button:b.setText('文件清单'))
            box.exec()
            if box.clickedButton() is output:follow=candidates
            elif box.clickedButton() is not original:return
        self.set_busy(True)
        self.status.setText('核对任务文件')
        def ready(batch):
            self.set_busy(False)
            if batch is None:
                self.status.setText('已取消加入任务')
                if self.closing:self.close()
                return
            self.status.setText(f'本次加入 {len(rows)} 个文件动作')
            self.archive_toggle.setChecked(False)
            self.history_offset=0
            self.task_panel.expand.setChecked(True)
            if self.closing:self.task_queue.control(None,'stop')
            self.poll_tasks()
        self.job(lambda _:self.task_queue.submit(action,rows,options,cancel=self.cancel,follow_outputs=follow),ready,failed=self.task_error)

    def poll_tasks(self):
        completed=self.task_queue.tick(dispatch=not self.busy and not self.closing)
        records=self.task_queue.snapshot()
        self.task_panel.update_records(records)
        if not self.task_queue.active:
            self.source_table.viewport().update();self.library_table.viewport().update()
        self.settings_button.setEnabled(not self.busy and not self.task_queue.active)
        self.fit_source_controls()
        if self.task_revision != self.task_queue.revision:
            self.task_revision=self.task_queue.revision
            self.refresh_history()
        if completed:
            self.refresh_library()
            if self.source_batch and not self.busy:
                selected_ids={self.source_model.rows[i.row()]['id'] for i in self.source_table.selectionModel().selectedRows()}
                self.load_source_batch(selected_ids=selected_ids)
            if not self.busy:
                errors=sum(r['status']=='failed' for r in records)
                skipped=sum(r['status']=='skipped' for r in records)
                self.status.setText(('任务处理中' if self.task_queue.active else '任务已结束')
                    + (f' · 异常 {errors}' if errors else '') + (f' · 跳过 {skipped}' if skipped else '')
                    + ('，原因见处理记录' if errors or skipped else ''))
        if self.closing and not self.task_queue.active:self.close()

    def import_progress(self, event):
        self.color_progress(dict(event, phase='正在导入' if event['phase']=='processing' else '已处理', encoder=''))

    def import_finished(self, result):
        self.set_busy(False)
        self.status.setText(f"{STATE_TEXT[result['status']]}    完成 {result['complete']}    重复跳过 {result['skipped']}    待处理或异常 {result['errors']}")
        self.refresh_history()
        self.refresh_library()
        self.source_total = 0
        self.source_rows = []
        self.format_choices = None
        self.update_formats()
        self.source_model.replace([])
        self.update_source_selection()
        if self.closing:
            self.close()

    def toggle_pause(self):
        if self.pause.is_set():
            self.pause.clear()
            self.pause_button.setText('继续')
            self.status.setText('当前文件处理后暂停')
        else:
            self.pause.set()
            self.pause_button.setText('暂停')

    def stop(self):
        self.cancel.set()
        self.pause.set()
        self.status.setText('正在停止，等待当前读取返回')

    def index_history(self):
        if self.busy:
            return
        folder = QFileDialog.getExistingDirectory(self, '选择历史素材文件夹')
        if not folder:
            return
        answer = QMessageBox.question(self, '历史素材索引', '只读扫描所选文件夹，不含子文件夹。\n\n' + folder,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return
        self.set_busy(True)
        self.status.setText('扫描历史素材')
        def done(stats):
            self.set_busy(False)
            self.status.setText(f"历史索引结束    照片 {stats['photos']}    视频 {stats['videos']}    异常 {stats['errors']}")
            self.refresh_library()
            if self.closing:
                self.close()
        self.job(lambda progress: self.catalog.scan(folder, self.cancel, progress, recursive=False, file_guard=self.task_queue.read_guard), done,
            failed=self.task_error, progress=lambda s: self.status.setText(f"历史扫描    照片 {s['photos']}    视频 {s['videos']}"))

    def refresh_library(self, reset=False):
        if reset:
            self.offset = 0
        self.query_version += 1
        version = self.query_version
        args = dict(text=self.search.text().strip(), kind=self.kind_filter.currentData(), gps=self.gps_filter.currentData(),
                    import_id=self.import_filter, activity_id=self.activity_filter, imported_only=True, offset=self.offset, limit=200)
        def done(result):
            if version != self.query_version:
                return
            total, rows = result
            self.library_total = total
            self.library_model.replace(rows)
            self.page_label.setText(f'{self.offset+1 if total else 0}–{self.offset+len(rows)} / {total}')
            self.prev_button.setEnabled(self.offset > 0)
            self.next_button.setEnabled(self.offset + len(rows) < total)
        self.job(lambda _: self.catalog.search(**args), done)

    def change_page(self, direction):
        self.offset = max(0, self.offset + 200 * direction)
        self.refresh_library()

    def clear_import_filter(self):
        self.activity_scope.hide()
        self.import_filter = ''
        self.activity_filter = ''
        self.refresh_library(reset=True)

    def selected_import(self):
        selected = self.imports_table.selectionModel().selectedRows()
        return self.imports_model.rows[selected[0].row()] if selected else None

    def refresh_history(self):
        selected = self.selected_import()
        self.history_version += 1
        version, offset, record_type = self.history_version, self.history_offset, self.record_filter.currentData()
        def done(rows):
            if version != self.history_version:
                return
            self.history_prev.setEnabled(offset > 0)
            self.history_next.setEnabled(len(rows) > 200)
            rows = rows[:200]
            self.history_page_label.setText(f'{offset+1 if rows else 0}–{offset+len(rows)}')
            self.imports_model.replace(rows)
            if rows:
                index = next((i for i, r in enumerate(rows) if selected and r['record_key'] == selected['record_key']), 0)
                self.imports_table.selectRow(index)
            else:
                self.load_import_items()
        self.job(lambda _: self.processing_history.page(limit=201, offset=offset, record_type=record_type), done)

    def reset_history_filter(self, *_):
        self.history_offset = 0
        self.refresh_history()

    def update_history_actions(self):
        record = self.selected_import()
        protected=bool(record and record['record_type']=='task' and can_use_protected(record))
        compatible=bool(record and record['record_type']=='task' and can_use_color_gps_compatible(record))
        self.retry_button.setText('兼容保留定位' if compatible else '改用保护写入' if protected else '继续 / 重试')
        self.browse_history_button.setEnabled(bool(record) and record['record_type']!='activity')
        self.batch_watermark_button.setEnabled(not self.busy and bool(record) and record['record_type']=='import' and record['completed_photos']>0)
        self.retry_button.setEnabled(not self.busy and bool(record) and (
            record['record_type'] == 'import' and record['status'] != 'completed' or
            record['record_type']=='task' and record['status'] in ('failed','cancelled','interrupted') or protected))
        self.retry_button.setToolTip('重新调色并以兼容方式保留定位，耗时较长' if compatible else
            '确认后仅重新处理不支持快速写入的视频' if protected else '重试保留原参数；每次尝试分别记录耗时。')

    def change_history_page(self, direction):
        self.history_offset = max(0, self.history_offset + 200 * direction)
        self.refresh_history()

    def load_import_items(self, *_):
        batch = self.selected_import()
        self.history_detail_version += 1
        version = self.history_detail_version
        self.history_details.clear()
        self.update_history_actions()
        if batch:
            identifier, record_type, record_key = batch['id'], batch['record_type'], batch['record_key']
            def done(data):
                rows, timings = data
                selected = self.selected_import()
                if version != self.history_detail_version or not selected or selected['record_key'] != record_key:
                    return
                if record_type == 'task':
                    blocks=[describe_timing(row) for row in rows]
                    for row in rows:
                        options=json.loads(row['options_json'])
                        if row['action']=='gps':
                            gps=row.get('journal') or options
                            fast=(json.loads(gps.get('old_gps_json') or '{}').get('_write_mode')=='fast'
                                  if row.get('journal') else options.get('write_mode')=='fast')
                            blocks.append(f"{'保存坐标' if row['status']=='complete' else '本次坐标'}    {gps['latitude']:.7f}, {gps['longitude']:.7f}\n地点    {gps.get('location_name') or '未填写'}\n写入方式    {'快速写入' if fast else '保护写入'}")
                        elif row['action']=='color':
                            blocks.append('保存方式    '+('另存还原版' if options.get('output_mode')=='copy' else '覆盖原文件'))
                        elif row['action']=='watermark':
                            blocks.append('另存目录    '+options['target_root'])
                elif record_type == 'import':
                    blocks = [f"{selected['type_label']}    {human_datetime(selected['created_at'])}\n"
                              f"源目录    {display_roots(selected['source_root'])}\n目标目录    {selected['target_root']}"]
                    for row in rows:
                        blocks.append('\n'.join([
                            f"{row['source_name']}    {STATE_TEXT.get(row['status'], row['status'])}",
                            f"拍摄时间    {human_datetime(row.get('capture_time')) or '未知'}    地点名称    {row.get('place') or '未填写'}",
                            f"导入目标    {row['target_path']}", f"当前路径    {row.get('current_path') or '未知'}"] +
                            ([f"{'跳过原因' if row['status'] == 'skipped' else '异常' if row['status'] == 'failed' else '说明'}    {row['error']}"] if row.get('error') else [])))
                elif record_type == 'color':
                    blocks = []
                    for row in rows:
                        removed = json.loads(row.get('profile_json') or '{}').get('removed_private_tracks', [])
                        removed_text = ', '.join(str(t.get('codec_id') or t.get('format')) for t in removed) or '无'
                        blocks.append('\n'.join([
                            f"颜色调整    {human_datetime(row['created_at'])}    {selected['display_status']}",
                            f"原文件名    {row['source_name']}", f"处理时路径    {row['source_path']}",
                            f"当前路径    {row.get('current_path') or '未知'}",
                            f"拍摄时间    {human_datetime(row.get('capture_time')) or '未知'}",
                            f"编码器    {row.get('encoder') or '未记录'}",
                            f"保存方式    {'另存还原版' if row.get('output_mode') == 'copy' else '替换原文件'}",
                            f"输出路径    {row.get('output_path') or row['source_path']}",
                            f"已确认移除的专有轨道    {removed_text}",
                            f"完成时间    {human_datetime(row.get('completed_at')) or '未完成'}"] +
                            ([f"异常    {row['error']}"] if row.get('error') else [])))
                elif record_type == 'gps':
                    blocks = ['\n'.join([
                        f"写入定位    {human_datetime(row['created_at'])}    {selected['display_status']}",
                        f"原文件名    {row['source_name']}", f"处理时路径    {row['source_path']}",
                        f"当前路径    {row.get('current_path') or '未知'}",
                        f"地点    {row.get('location_name') or '未填写'}",
                        f"GPS 纬度    {row['latitude']:.7f}    经度    {row['longitude']:.7f}",
                        f"坐标来源    {row['coordinate_source']}",
                        f"写入方式    {'快速写入' if json.loads(row.get('old_gps_json') or '{}').get('_write_mode') == 'fast' else '保护写入'}",
                        f"写入前 GPS    {json.dumps({k:v for k,v in json.loads(row.get('old_gps_json') or '{}').items() if not k.startswith('_')}, ensure_ascii=False)}"] +
                        ([f"异常    {row['error']}"] if row.get('error') else [])) for row in rows]
                elif record_type=='activity':
                    from activity_service import describe_changes
                    blocks=[describe_changes(json.loads(r['details_json'])) for r in rows]
                else:
                    blocks=[]
                    for row in rows:
                        settings=json.loads(row['settings_json'])
                        blocks.append('\n'.join([
                            f"照片水印    {human_datetime(row['created_at'])}    {selected['display_status']}",
                            f"原文件名    {row['source_name']}", f"原片路径    {row['source_path']}",
                            f"另存路径    {row['output_path']}",f"当前路径    {row.get('current_path') or '未发布'}",
                            f"风格    {WATERMARK_STYLES.get(settings['style'],settings['style'])}",
                            f"水印内容\n{row['rendered_text'] or '图片 Logo'}"]+
                            ([f"异常    {row['error']}"] if row.get('error') else [])))
                blocks.extend(describe_timing(row) for row in timings)
                self.history_details.setPlainText('\n\n'.join(blocks))
            self.job(lambda _: (self.processing_history.details(record_type, identifier),
                               self.processing_history.timings(record_type,identifier)), done)

    def browse_import(self):
        batch = self.selected_import()
        if batch:
            if batch['record_type']=='activity':
                self.status.setText('活动修改明细已显示在下方');return
            if batch['record_type'] in ('color', 'gps', 'watermark', 'task'):
                self.job(lambda _: self.processing_history.media(batch.get('output_media_id') or batch['media_id']),
                         lambda row: self.show_details(row) if row else self.error('素材记录已不可用。'))
                return
            self.activity_filter = ''
            self.activity_scope.hide()
            self.import_filter = batch['id']
            self.search.clear()
            self.refresh_library(reset=True)
            self.tabs.setCurrentIndex(1)

    def retry_import(self):
        if self.busy:
            return
        batch = self.selected_import()
        if batch and batch['record_type']=='task':
            if can_use_protected(batch):
                self.offer_protected(batch['id']);return
            if can_use_color_gps_compatible(batch):
                options=json.loads(batch['options_json'])
                saving=('另存还原版，保留原文件' if options.get('output_mode')=='copy' else
                        '覆盖原文件，不保留原片备份')
                box=QMessageBox(QMessageBox.Icon.Question,'兼容保留定位',
                    f"{batch['source_name']}\n\n将重新调色，并通过整文件读写保留 GPS，耗时较长。\n{saving}；全部校验通过后保存。",parent=self)
                proceed=box.addButton('兼容处理',QMessageBox.ButtonRole.AcceptRole)
                cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole)
                box.setDefaultButton(cancel);box.exec()
                if box.clickedButton()!=proceed:return
                try:
                    self.task_queue.retry(batch['id'],compatible_gps=True);self.poll_tasks()
                except Exception as exc:self.error(str(exc))
                return
            if batch['status'] not in ('failed','cancelled','interrupted'):return
            try:
                self.task_queue.retry(batch['id']);self.poll_tasks()
            except Exception as exc:self.error(str(exc))
            return
        if batch and batch['record_type'] == 'import' and batch['status'] != 'completed':
            self.run_import(batch['id'])

    def offer_protected(self, identifier):
        self.set_busy(True);self.status.setText('读取跳过的记录')
        def ready(records):
            self.set_busy(False)
            if self.closing:self.close();return
            selected=next((r for r in records if r['id']==identifier),None)
            if not selected:
                self.status.setText('该文件已重新安排或已完成');return
            options=json.loads(selected['options_json'])
            box=QMessageBox(QMessageBox.Icon.Question,'改用保护写入',
                f"{selected['source_name']}\n\n纬度 {options['latitude']:.7f}    经度 {options['longitude']:.7f}\n"
                f"{'替换已有 GPS' if options.get('overwrite') else '跳过已有 GPS'}\n"
                '创建临时文件，校验后替换原视频，不重新编码。',parent=self)
            one=box.addButton('仅此文件',QMessageBox.ButtonRole.AcceptRole)
            all_files=box.addButton(f"本批跳过的 {len(records)} 个视频",QMessageBox.ButtonRole.ActionRole) if len(records)>1 else None
            cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(cancel)
            box.setDetailedText('\n\n'.join(r['source_path'] for r in records))
            for button in box.buttons():
                if button not in (one,all_files,cancel):
                    button.setText('文件清单')
                    button.clicked.connect(lambda _=False,b=button:b.setText('文件清单'))
            box.exec()
            if box.clickedButton() is one:ids=[identifier]
            elif all_files and box.clickedButton() is all_files:ids=[r['id'] for r in records]
            else:self.status.setText('未改用保护写入');return
            self.set_busy(True);self.status.setText('核对任务文件')
            def submitted(batch_id):
                self.set_busy(False)
                self.status.setText(f'本次加入 {len(ids)} 个保护写入任务' if batch_id else '已取消加入任务')
                self.archive_toggle.setChecked(False);self.task_panel.expand.setChecked(True)
                if self.closing:self.task_queue.control(None,'stop')
                self.poll_tasks()
            self.job(lambda _:self.task_queue.submit_protected(ids,confirmed=True,cancel=self.cancel),submitted,failed=self.task_error)
        self.job(lambda _:self.task_queue.protected_candidates(identifier),ready,failed=self.task_error)

    def library_details(self):
        selected = self.library_table.selectionModel().selectedRows()
        if selected:
            self.show_details(self.library_model.rows[selected[0].row()])

    def open_library_folder(self):
        selected = self.library_table.selectionModel().selectedRows()
        if selected:
            row = self.library_model.rows[selected[0].row()]
            self.reveal_file(row['current_path'])

    def reveal_file(self, path):
        path = Path(path).absolute()
        if not path.parent.is_dir():
            self.error('所在文件夹不可访问，请检查磁盘或 NAS 连接。')
            return
        if os.name == 'nt' and path.is_file():
            subprocess.Popen(['explorer.exe', '/select,', str(path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def show_details(self, row):
        dialog = AdaptiveDialog(self)
        dialog.setWindowTitle('素材详情')
        dialog.resize(820, 650)
        layout = QVBoxLayout(dialog)
        header = QHBoxLayout()
        cached = self.cache.lookup(row)
        if cached:
            preview = QLabel()
            preview.setPixmap(QPixmap(str(cached)).scaled(144, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            header.addWidget(preview)
        heading = QLabel('照片详情' if row['kind'] == 'photo' else '视频详情')
        heading.setObjectName('sectionTitle'); header.addWidget(heading); header.addStretch()
        layout.addLayout(header)
        content = QWidget()
        form = QFormLayout(content)
        form.setContentsMargins(0, 4, 0, 4)
        form.setHorizontalSpacing(16); form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        exported = []
        def section(title, pairs):
            title_label = QLabel(title); title_label.setObjectName('sectionTitle'); form.addRow(title_label)
            exported.append(title)
            for label, value in pairs:
                text = str(value) if value is not None and value != '' else '未知'
                field = WrappingText(text)
                field.setMinimumWidth(0)
                field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                form.addRow(label, field); exported.append(label + '    ' + text)
        section('文件身份', [('现文件名', row['observed_name']), ('首次导入原名', row.get('original_name')),
            ('历史推测原名', row.get('inferred_original')), ('完整路径', row['current_path'])])
        date_source = {'metadata':'元数据', 'filename_inferred':'文件名推断', 'unknown':'未知'}
        capture = [('拍摄时间', human_datetime(row.get('capture_time'))), ('日期依据', date_source.get(row['date_source'], '未知')),
            ('设备', row.get('device'))]
        if row.get('capture_time') and not datetime_has_zone(row['capture_time']): capture.append(('拍摄时区', '未记录'))
        if row['kind'] == 'video': capture.append(('时长（秒）', row.get('duration_seconds')))
        section('拍摄信息', capture)
        section('活动', [('活动名称',row.get('activity_name')),('同行人',row.get('activity_companions')),('活动备注',row.get('activity_note')),('素材备注',row.get('media_note'))])
        section('位置', [('地点名称', row.get('place')), ('纬度', row.get('latitude')), ('经度', row.get('longitude'))])
        section('处理状态', [('导入时间', human_datetime(row.get('imported_at')) or '未记录'),
            ('颜色还原', '已还原为 Rec.709' if row.get('color_revision') else '未记录'),
            ('照片水印', '已添加' if row.get('watermark_revision') else '未记录'), ('读取异常', row.get('metadata_error') or '无')])
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame); scroll.setWidget(content)
        content.setAutoFillBackground(False)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        buttons.rejected.connect(dialog.reject)
        copy = buttons.addButton('复制详情', QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(lambda: QApplication.clipboard().setText('\n'.join(exported)))
        copy_path = buttons.addButton('复制路径', QDialogButtonBox.ButtonRole.ActionRole)
        copy_path.clicked.connect(lambda: QApplication.clipboard().setText(row['current_path']))
        open_file = buttons.addButton('打开文件', QDialogButtonBox.ButtonRole.ActionRole)
        open_file.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(row['current_path'])))
        folder = buttons.addButton('打开所在文件夹', QDialogButtonBox.ButtonRole.ActionRole)
        folder.clicked.connect(lambda: self.reveal_file(row['current_path']))
        if row.get('activity_id'):
            event_button=buttons.addButton('查看同一活动',QDialogButtonBox.ButtonRole.ActionRole)
            event_button.clicked.connect(lambda:(dialog.accept(),self.show_activity(row['activity_id'],row.get('activity_name'))))
        layout.addWidget(buttons)
        dialog.exec()

    def closeEvent(self, event):
        if self.task_queue.active:
            self.closing=True
            self.task_queue.control(None,'stop')
            event.ignore()
            return
        if self.busy:
            self.closing = True
            self.stop()
            event.ignore()
            return
        self.loader.cancel.set()
        # Keep Qt objects alive while outstanding jobs return (local DB or bounded decodes).
        if self.jobs or self.loader.jobs:
            self.closing = True
            QTimer.singleShot(150, self.close)
            event.ignore()
            return
        self.save_settings()
        self.task_timer.stop()
        self.task_queue.shutdown()
        event.accept()


STYLE = '''
QWidget { font-family: "Microsoft YaHei UI"; font-size: 13px; color: #263344; }
QMainWindow, QDialog { background: #f5f7fa; }
QLabel#title { font-size: 20px; font-weight: 600; padding: 0; }
QPushButton { border: 1px solid #d4dce6; border-radius: 5px; padding: 6px 12px; min-height: 18px; background: white; }
QPushButton:hover { background: #edf3fa; }
QPushButton:checked { background: #dceaff; color: #184e99; border-color: #8faedb; }
QPushButton:disabled { color: #929eaa; background: #eef1f5; }
QPushButton#primary { background: #2466c4; color: white; border-color: #2466c4; }
QPushButton#primary:disabled { background: #a4b9d5; border-color: #a4b9d5; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #d4dce6; border-radius: 4px; padding: 6px; min-height: 18px; }
QComboBox { padding-right: 25px; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow { image: url("__DOWN_ARROW__"); width: 12px; height: 8px; }
QComboBox QAbstractItemView { background: white; border: 1px solid #d4dce6; selection-background-color: #eef4fc; }
QTableView { background: white; alternate-background-color: #f7f9fc; border: 1px solid #dce3eb; selection-background-color: #eef4fc; }
QHeaderView::section { background: #eef2f7; border: none; padding: 6px; font-weight: 600; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; border: none; border-bottom: 3px solid transparent; padding: 7px 18px; margin-bottom: 0; }
QTabBar::tab:hover { background: #edf3fa; }
QTabBar::tab:selected { color: #2466c4; border-bottom: 3px solid #2466c4; }
QProgressBar { border: 1px solid #dce3eb; border-radius: 4px; height: 9px; text-align: center; }
QProgressBar::chunk { background: #4b8ce0; }
QLabel#sectionTitle { font-weight: 600; color: #263344; padding-top: 6px; }
QLabel#secondary { color: #64748b; }
QLabel[severity="error"], QTextEdit[severity="error"] { color: #b42318; }
QPushButton:pressed { background: #dde8f5; border-color: #8faedb; }
QPushButton#primary:hover { background: #1f59ad; }
QPushButton#primary:pressed { background: #19498f; }
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #2466c4; }
QSpinBox, QDoubleSpinBox { padding-right: 22px; }
QSpinBox::up-button, QDoubleSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; width: 20px; height: 16px; border-left: 1px solid #d4dce6; }
QSpinBox::down-button, QDoubleSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; width: 20px; height: 16px; border-left: 1px solid #d4dce6; }
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover { background: #edf3fa; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url("__UP_ARROW__"); width: 10px; height: 6px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url("__DOWN_ARROW__"); width: 10px; height: 6px; }
QScrollArea { border: none; background: transparent; }
QToolButton#watermarkLook { background: white; border: 1px solid #d4dce6; border-radius: 5px; padding: 4px; }
QToolButton#watermarkLook:hover { background: #edf3fa; }
QToolButton#watermarkLook:checked { background: #eef4fc; border: 2px solid #2466c4; padding: 3px; }
QToolButton#watermarkLook:focus { border-color: #2466c4; }
QToolButton#watermarkLook:pressed { background: #dde8f5; }
QTextEdit, QPlainTextEdit, QListWidget { background: white; border: 1px solid #d4dce6; border-radius: 4px; }

'''.replace('__DOWN_ARROW__', (Path(__file__).resolve().parent / 'assets/icons/chevron-down.svg').as_posix()).replace('__UP_ARROW__', (Path(__file__).resolve().parent / 'assets/icons/chevron-up.svg').as_posix())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir')
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    folder = Path(args.data_dir or default_database().parent)
    folder.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(folder / 'desktop.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, '程序已运行', '请使用已经打开的数字媒体整理工具。')
        return 1
    try:
        window = MainWindow(folder)
        window.show()
        return app.exec()
    except Exception as exc:
        QMessageBox.critical(None, '无法启动', str(exc))
        return 1
    finally:
        lock.unlock()


if __name__ == '__main__':
    sys.exit(main())
