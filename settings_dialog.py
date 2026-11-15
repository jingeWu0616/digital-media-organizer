"""Local preferences, with explicit saves and independent maintenance actions."""
import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTabWidget, QWidget, QLineEdit, QCheckBox, QComboBox, QSpinBox, QLabel,
    QPushButton, QDialogButtonBox, QFileDialog, QTextEdit, QMessageBox, QApplication, QSizePolicy)
from location_service import protect_key, search_places
from watermark_dialog import WatermarkDialog
from ui_common import AdaptiveDialog, WrappingText
from import_service import render_filename, validate_name_template


class SettingsDialog(AdaptiveDialog):
    def __init__(self, host, tab=0):
        super().__init__(host)
        self.host = host
        self.working = False
        self.setWindowTitle('设置')
        self.resize(800, 560)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        settings = host.settings
        def page(name):
            widget = QWidget(); form = QFormLayout(widget)
            form.setVerticalSpacing(10)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            self.tabs.addTab(widget, name)
            return form
        form = page('地图定位')
        self.key = QLineEdit(); self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText('高德 Web 服务 Key')
        self.key_read_error = False
        try:
            self.key.setText(protect_key(str(settings.value('amap_key_protected', '')), decrypt=True))
        except (ValueError, OSError):
            self.key_read_error = True
            self.key.setPlaceholderText('已保存的 Key 无法解密，请重新填写')
        self.key_dirty = False
        self.key.textChanged.connect(lambda: setattr(self, 'key_dirty', True))
        row = QHBoxLayout(); row.addWidget(self.key, 1)
        reveal = QCheckBox('显示'); reveal.toggled.connect(lambda enabled: self.key.setEchoMode(QLineEdit.EchoMode.Normal if enabled else QLineEdit.EchoMode.Password))
        row.addWidget(reveal)
        clear = QPushButton('清除'); clear.clicked.connect(self.clear_key); row.addWidget(clear)
        form.addRow('API Key', row)
        self.city = QLineEdit(str(settings.value('amap_city', '')))
        form.addRow('默认搜索城市', self.city)
        self.test_button = QPushButton('测试连接'); self.test_button.clicked.connect(self.test_key)
        form.addRow(self.test_button)
        self.key_status = QLabel(); self.key_status.setWordWrap(True); form.addRow(self.key_status)

        form = page('整理规则')
        self.template = QLineEdit(str(settings.value('template', '{date}_{weekday}_{device}_{place}_{original}')))
        self.template.setToolTip('{date} 日期  {weekday} 星期  {device} 设备  {place} 地点  {original} 原名')
        form.addRow('文件名模板', self.template)
        self.insert_field = QComboBox()
        self.insert_field.addItem('插入字段', '')
        for key, label in [('date','日期'),('weekday','星期'),('device','设备'),('place','地点'),('original','原文件名')]:
            self.insert_field.addItem(label, '{'+key+'}')
        self.insert_field.activated.connect(self.insert_template_field)
        form.addRow('', self.insert_field)
        self.name_preview = WrappingText()
        self.name_preview.setMinimumWidth(0)
        self.name_preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.name_preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow('生成示例', self.name_preview)
        self.preview_context = QLabel('以 DJI_0001.jpg 为例，2026-11-09，DJI Osmo Action 6，天津海边')
        self.preview_context.setObjectName('secondary')
        self.preview_context.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        form.addRow('', self.preview_context)
        self.template.textChanged.connect(self.update_name_preview)
        self.update_name_preview()
        self.birthday = QLineEdit(str(settings.value('birthday', ''))); self.birthday.setPlaceholderText('YYYY-MM-DD，可选')
        form.addRow('宝宝生日', self.birthday)
        self.folder_layout = QComboBox()
        self.folder_layout.addItem('日期 / 照片或视频 / 星级', 'day')
        self.folder_layout.addItem('年 / 月 / 日期 / 照片或视频 / 星级', 'month')
        self.folder_layout.setCurrentIndex(max(0, self.folder_layout.findData(settings.value('folder_layout', 'day'))))
        form.addRow('目录结构', self.folder_layout)
        self.keep_named = QCheckBox('保留已整理过的文件名'); self.keep_named.setChecked(settings.value('keep_named', True, type=bool))
        form.addRow(self.keep_named)
        self.target = QLineEdit(str(settings.value('default_target', '')))
        row = QHBoxLayout(); row.addWidget(self.target, 1)
        choose = QPushButton('选择文件夹'); choose.clicked.connect(self.choose_target); row.addWidget(choose)
        form.addRow('常用目标文件夹', row)

        form = page('颜色还原')
        self.auto_color = QCheckBox('扫描后检查 LRF'); self.auto_color.setChecked(settings.value('auto_color', True, type=bool)); form.addRow(self.auto_color)
        self.color_output = QComboBox()
        self.color_output.addItem('覆盖原文件', 'replace'); self.color_output.addItem('另存还原版', 'copy')
        self.color_output.setCurrentIndex(max(0, self.color_output.findData(settings.value('color_output', 'replace'))))
        form.addRow('默认保存方式', self.color_output)
        self.encoder = QComboBox()
        self.encoder.addItem('硬件优先，不可用时用 CPU', 'auto'); self.encoder.addItem('CPU 高质量', 'cpu')
        self.encoder.setCurrentIndex(max(0, self.encoder.findData(settings.value('color_encoder', 'auto'))))
        form.addRow('默认编码方式', self.encoder)
        warning = QLabel('覆盖不保留原片备份；涉及专有数据丢失时，执行前单独确认。')
        warning.setWordWrap(True); form.addRow(warning)

        form = page('水印模板')
        edit = QPushButton('编辑样式与常用模板'); edit.clicked.connect(self.edit_watermarks); form.addRow(edit)
        self.presets = QComboBox(); form.addRow('已保存模板', self.presets)
        self.refresh_presets()
        use = QPushButton('设为默认'); use.clicked.connect(self.use_preset); form.addRow(use)
        delete = QPushButton('删除所选模板'); delete.clicked.connect(self.delete_preset); form.addRow(delete)
        self.preset_status = QLabel(); form.addRow(self.preset_status)

        form = page('界面与缓存')
        self.compact = QCheckBox('默认紧凑列表'); self.compact.setChecked(settings.value('compact_list', False, type=bool)); form.addRow(self.compact)
        self.cache_mib = QSpinBox(); self.cache_mib.setRange(64, 8192); self.cache_mib.setSuffix(' MB')
        self.cache_mib.setValue(settings.value('cache_mib', 512, type=int)); form.addRow('缩略图缓存上限', self.cache_mib)
        self.clear_button = QPushButton('清理缩略图缓存'); self.clear_button.clicked.connect(self.clear_cache); form.addRow(self.clear_button)

        form = page('数据维护')
        location = QLineEdit(str(host.catalog.database)); location.setReadOnly(True); form.addRow('记录数据库', location)
        open_folder = QPushButton('打开数据文件夹'); open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(host.data_dir.resolve())))); form.addRow(open_folder)
        self.backup_button = QPushButton('备份记录数据库'); self.backup_button.clicked.connect(self.backup); form.addRow(self.backup_button)
        errors = QPushButton('查看最近异常'); errors.clicked.connect(self.show_errors); form.addRow(errors)
        note = QLabel('数据库备份包含索引和处理记录，不包含照片、视频或高德 Key。'); note.setWordWrap(True); form.addRow(note)
        self.message = QLabel(); self.message.setWordWrap(True); layout.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存设置')
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName('primary')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept); self.buttons.rejected.connect(self.reject); layout.addWidget(self.buttons)
        for button in (self.test_button, edit, use, delete, self.clear_button, open_folder, self.backup_button, errors):
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.tabs.setCurrentIndex(tab)

    def insert_template_field(self, index):
        token = self.insert_field.itemData(index)
        if token:
            self.template.insert(token)
            self.template.setFocus()
        self.insert_field.setCurrentIndex(0)

    def update_name_preview(self):
        try:
            template = self.template.text().strip()
            if not template: raise ValueError('请输入文件名模板')
            values = dict(date='2026-11-09', weekday='Mon', device='DJIOsmoAction6', place='天津海边', original='DJI_0001')
            self.name_preview.setText(render_filename(template, values, '.jpg'))
            self.name_preview.setProperty('severity', '')
        except (ValueError, KeyError) as exc:
            self.name_preview.setText(str(exc))
            self.name_preview.setProperty('severity', 'error')
        self.name_preview.style().unpolish(self.name_preview); self.name_preview.style().polish(self.name_preview)

    def clear_key(self):
        self.key_dirty = True
        self.key.clear()

    def choose_target(self):
        value = QFileDialog.getExistingDirectory(self, '常用目标文件夹', self.target.text())
        if value: self.target.setText(value)

    def run_action(self, worker, done, failed=None):
        if self.working: return
        self.working = True; self.tabs.setEnabled(False); self.buttons.setEnabled(False)
        def finish(callback, value):
            self.working = False; self.tabs.setEnabled(True); self.buttons.setEnabled(True)
            callback(value)
        self.host.job(worker, lambda value: finish(done, value),
            failed=lambda value: finish(failed or self.message.setText, value))

    def test_key(self):
        key, city = self.key.text().strip(), self.city.text().strip()
        if not key: self.key_status.setText('请填写高德 Web 服务 Key。'); return
        self.key_status.setText('正在测试…')
        self.run_action(lambda _: search_places(key, '公园', city),
            lambda _: self.key_status.setText('连接成功'), self.key_status.setText)

    def refresh_presets(self):
        try: values = json.loads(str(self.host.settings.value('watermark_presets', '{}')))
        except (ValueError, TypeError): values = {}
        self.preset_values = values if isinstance(values, dict) else {}
        self.presets.clear(); self.presets.addItems(sorted(self.preset_values))

    def edit_watermarks(self):
        dialog = WatermarkDialog([], self.host, template_mode=True)
        dialog.setParent(self, Qt.WindowType.Dialog)
        dialog.exec(); self.refresh_presets()

    def use_preset(self):
        name = self.presets.currentText()
        if name not in self.preset_values: return
        self.host.settings.setValue('watermark_default', json.dumps(self.preset_values[name], ensure_ascii=False))
        self.host.settings.sync(); self.preset_status.setText('默认模板已保存')

    def delete_preset(self):
        name = self.presets.currentText()
        if name not in self.preset_values: return
        if QMessageBox.question(self, '删除模板', f'删除模板“{name}”？', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return
        del self.preset_values[name]
        self.host.settings.setValue('watermark_presets', json.dumps(self.preset_values, ensure_ascii=False)); self.host.settings.sync()
        self.refresh_presets(); self.preset_status.setText('模板已删除')

    def clear_cache(self):
        loader = self.host.loader
        loader.cancel.set()
        def work(_):
            loader.pool.waitForDone()
            return self.host.cache.clear()
        def finish(value):
            loader.images.clear(); loader.failed.clear(); loader.cancel.clear()
            self.host.source_table.viewport().update(); self.host.library_table.viewport().update()
            self.message.setText(f'已清理 {value} 个缓存文件')
        def failed(message):
            loader.cancel.clear(); self.message.setText(message)
        self.run_action(work, finish, failed)

    def backup(self):
        folder = QFileDialog.getExistingDirectory(self, '选择数据库备份文件夹', str(self.host.data_dir))
        if not folder: return
        target = Path(folder) / ('catalog-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.sqlite3')
        def work(_):
            with target.open('xb'): pass
            with closing(self.host.catalog.connect()) as source, closing(sqlite3.connect(target)) as output:
                source.backup(output)
                if output.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('备份校验失败，请保留原数据库。')
            return str(target)
        self.run_action(work, lambda value: self.message.setText('备份完成  ' + value))

    def show_errors(self):
        def read(_):
            blocks = []
            with closing(self.host.catalog.connect()) as db:
                for table in ('scan_errors', 'import_items', 'color_jobs', 'gps_jobs', 'watermark_jobs'):
                    # Only known table names are interpolated; errors are read-only.
                    column = 'message' if table == 'scan_errors' else 'error'
                    path = 'path' if table == 'scan_errors' else 'source_path'
                    for row in db.execute(f'SELECT {path}, {column} FROM {table} WHERE {column} IS NOT NULL ORDER BY rowid DESC LIMIT 30'):
                        blocks.append(str(row[0]) + '\n' + str(row[1]))
            return '\n\n'.join(blocks)
        def show(text):
            dialog = AdaptiveDialog(self); dialog.setWindowTitle('最近异常'); dialog.resize(760, 500)
            layout = QVBoxLayout(dialog); view = QTextEdit(); view.setReadOnly(True); view.setPlainText(text or '暂无异常记录'); layout.addWidget(view)
            row = QHBoxLayout(); layout.addLayout(row)
            copy = QPushButton('复制异常'); copy.setEnabled(bool(text)); row.addWidget(copy)
            status = QLabel(); row.addWidget(status); row.addStretch()
            def copy_errors():
                if not text: return
                QApplication.clipboard().setText(view.toPlainText())
                status.setText('已复制')
            copy.clicked.connect(copy_errors)
            dialog.exec()
        self.run_action(read, show)

    def accept(self):
        if self.working: return
        try:
            template = self.template.text().strip()
            if not template: raise ValueError('请输入文件名模板。')
            validate_name_template(template)
            birthday = self.birthday.text().strip()
            if birthday: date.fromisoformat(birthday)
            encrypted = None if self.key_read_error and not self.key_dirty else protect_key(self.key.text().strip())
        except ValueError as exc:
            self.message.setText(str(exc)); return
        settings = self.host.settings
        values = dict(amap_city=self.city.text().strip(), template=template, birthday=birthday,
            folder_layout=self.folder_layout.currentData(), keep_named=self.keep_named.isChecked(),
            default_target=self.target.text().strip(), auto_color=self.auto_color.isChecked(),
            color_output=self.color_output.currentData(), color_encoder=self.encoder.currentData(),
            compact_list=self.compact.isChecked(), cache_mib=self.cache_mib.value())
        if encrypted is not None: values['amap_key_protected'] = encrypted
        previous_target = str(settings.value('default_target', ''))
        for key, value in values.items(): settings.setValue(key, value)
        settings.sync()
        if settings.status() != settings.Status.NoError:
            self.message.setText('设置保存失败，请检查文件权限。'); return
        if not self.host.target.text().strip() or values['default_target'] != previous_target:
            self.host.target.setText(values['default_target'])
        self.host.set_compact(self.compact.isChecked())
        self.host.cache.budget = self.cache_mib.value() * 1024**2
        def prune(_):
            with self.host.cache.lock: self.host.cache.prune()
        self.host.job(prune, lambda _: None)
        super().accept()

    def reject(self):
        if not self.working: super().reject()

    def closeEvent(self, event):
        if self.working: event.ignore()
        else: super().closeEvent(event)
