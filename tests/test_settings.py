import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from PIL import Image
from PySide6.QtCore import Qt, QMimeData, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QPushButton, QMessageBox, QTextEdit, QLabel
from organizer_app import MainWindow, ColorDialog, PlanDialog
from settings_dialog import SettingsDialog
from location_dialog import LocationDialog
from location_service import protect_key
from watermark_dialog import WatermarkDialog


class SettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtCore import QRect
        screen = patch('ui_common.work_area', return_value=QRect(0, 0, 1920, 1040))
        screen.start(); self.addCleanup(screen.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.window = MainWindow(self.base/'data')
        self.window.show(); self.wait_idle()

    def wait_idle(self):
        until = time.monotonic()+20
        quiet = None
        while time.monotonic() < until:
            self.app.processEvents()
            w = self.window
            if not w.jobs and not w.loader.jobs and not w.busy and not w.task_queue.active:
                quiet = quiet or time.monotonic()
                if time.monotonic()-quiet > .2: return
            else: quiet = None
            time.sleep(.01)
        self.fail('workers did not finish')

    def tearDown(self):
        self.wait_idle(); self.window.close(); self.app.processEvents(); self.temp.cleanup()

    def preserve_clipboard(self):
        clipboard = self.app.clipboard()
        original = clipboard.mimeData()
        saved = {fmt: bytes(original.data(fmt)) for fmt in original.formats()} if original else {}
        def restore():
            if saved:
                mime = QMimeData()
                for fmt, data in saved.items(): mime.setData(fmt, data)
                clipboard.setMimeData(mime)
            else:
                clipboard.clear()
            current = clipboard.mimeData()
            restored = {fmt: bytes(current.data(fmt)) for fmt in current.formats()} if current else {}
            self.assertEqual(restored, saved)
        self.addCleanup(restore)
        return clipboard

    def inspect_errors(self, inspect):
        # Keep the real async job and modal event loop; propagate Qt callback failures.
        settings = SettingsDialog(self.window, tab=5)
        failures = []; visited = []
        original_exec = QDialog.exec
        def execute(dialog):
            def check():
                try:
                    self.assertEqual(dialog.windowTitle(), '最近异常')
                    visited.append(True)
                    inspect(dialog)
                except Exception as exc:
                    failures.append(exc)
                finally:
                    dialog.reject()
            QTimer.singleShot(0, check)
            return original_exec(dialog)
        try:
            with patch.object(QDialog, 'exec', execute):
                settings.show_errors(); self.wait_idle()
            if failures: raise failures[0]
            self.assertEqual(visited, [True])
            self.assertFalse(settings.working)
            self.assertEqual(settings.message.text(), '')
        finally:
            settings.reject()

    def error_database_snapshot(self):
        with closing(self.window.catalog.connect()) as db:
            return list(db.iterdump())

    def assert_error_database_unchanged(self, before):
        after = self.error_database_snapshot()
        # Retained in this run's stdout alongside the assertions, using synthetic data only.
        print(json.dumps({'test': self.id(), 'database_before': before,
                          'database_after': after}, ensure_ascii=False))
        self.assertEqual(after, before)

    def test_copy_errors_preserves_complete_text_and_read_only_records(self):
        clipboard = self.preserve_clipboard(); clipboard.setText('原剪贴板哨兵')
        records = [
            (str(self.base/'合成素材'/'照片 一.jpg'), '读取失败\n第二行 <tag> & "引号"\t末尾'),
            (str(self.base/'合成素材'/'视频🎬.mp4'), '路径不可用\n详情：中文与特殊字符 \\ / %'),
            (str(self.base/'合成素材'/'长异常.jpg'), '完整内容\n' + '长行中文 & < >\t' * 1000),
        ]
        with closing(self.window.catalog.connect()) as db, db:
            db.execute("INSERT INTO batches(id,root,started_at,status) VALUES(?,?,?,?)",
                       ('copy-test', str(self.base/'合成素材'), '2026-11-14T00:00:00Z', 'complete'))
            db.executemany('INSERT INTO scan_errors(batch_id,path,message) VALUES(?,?,?)',
                           [('copy-test', path, message) for path, message in records])
        before = self.error_database_snapshot()
        expected = '\n\n'.join(path + '\n' + message for path, message in reversed(records))
        def inspect(dialog):
            view = dialog.findChild(QTextEdit)
            button = next(b for b in dialog.findChildren(QPushButton) if b.text() == '复制异常')
            self.assertTrue(view.isReadOnly()); self.assertEqual(view.toPlainText(), expected)
            self.assertTrue(button.isEnabled()); self.assertEqual(clipboard.text(), '原剪贴板哨兵')
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            self.assertEqual(clipboard.text(), expected)
            self.assertEqual(clipboard.text(), view.toPlainText())
            self.assertIn('已复制', [label.text() for label in dialog.findChildren(QLabel)])
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            self.assertEqual(clipboard.text(), expected)
        self.inspect_errors(inspect)
        self.assert_error_database_unchanged(before)

    def test_copy_errors_disabled_without_errors_preserves_clipboard(self):
        clipboard = self.preserve_clipboard()
        mime = QMimeData(); mime.setText('不可覆盖\n中文哨兵'); mime.setHtml('<b>不可覆盖</b>')
        clipboard.setMimeData(mime)
        saved = {fmt: bytes(clipboard.mimeData().data(fmt)) for fmt in clipboard.mimeData().formats()}
        before = self.error_database_snapshot()
        def inspect(dialog):
            view = dialog.findChild(QTextEdit)
            button = next(b for b in dialog.findChildren(QPushButton) if b.text() == '复制异常')
            self.assertTrue(view.isReadOnly()); self.assertEqual(view.toPlainText(), '暂无异常记录')
            self.assertFalse(button.isEnabled())
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            self.assertEqual({fmt: bytes(clipboard.mimeData().data(fmt))
                              for fmt in clipboard.mimeData().formats()}, saved)
            self.assertNotIn('已复制', [label.text() for label in dialog.findChildren(QLabel)])
        self.inspect_errors(inspect)
        self.assert_error_database_unchanged(before)

    def test_copy_errors_keeps_recent_thirty_limit(self):
        clipboard = self.preserve_clipboard()
        records = [(str(self.base/f'合成异常-{i}.jpg'), f'异常 {i}') for i in range(32)]
        with closing(self.window.catalog.connect()) as db, db:
            db.executemany('INSERT INTO scan_errors(path,message) VALUES(?,?)', records)
            db.execute('INSERT INTO scan_errors(path,message) VALUES(?,NULL)', ('不显示',))
        before = self.error_database_snapshot()
        expected = '\n\n'.join(path + '\n' + message for path, message in reversed(records[2:]))
        def inspect(dialog):
            view = dialog.findChild(QTextEdit)
            self.assertEqual(view.toPlainText(), expected)
            button = next(b for b in dialog.findChildren(QPushButton) if b.text() == '复制异常')
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            self.assertEqual(clipboard.text(), expected)
        self.inspect_errors(inspect)
        self.assert_error_database_unchanged(before)

    def test_saved_defaults_and_encrypted_key_survive_restart_cancel_has_no_effect(self):
        w = self.window
        d = SettingsDialog(w)
        d.key.setText('test-private-key'); d.city.setText('天津'); d.target.setText(str(self.base/'NAS'))
        d.color_output.setCurrentIndex(1); d.encoder.setCurrentIndex(1); d.cache_mib.setValue(128)
        d.accept(); self.wait_idle()
        self.assertEqual(d.result(), QDialog.DialogCode.Accepted)
        self.assertNotIn('test-private-key', (w.data_dir/'settings.ini').read_text(encoding='utf-8'))
        self.assertEqual(protect_key(w.settings.value('amap_key_protected'), decrypt=True), 'test-private-key')
        color = ColorDialog([],w)
        self.assertEqual(color.output_mode.currentData(),'copy'); self.assertEqual(color.mode.currentData(),'cpu'); color.reject()
        d = SettingsDialog(w); d.clear_key(); d.city.setText('不保存'); d.reject()
        self.assertEqual(protect_key(w.settings.value('amap_key_protected'), decrypt=True), 'test-private-key')
        w.pin_button.click(); self.assertTrue(w.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        w.close(); self.app.processEvents()
        self.window = MainWindow(self.base/'data'); self.window.show(); self.wait_idle()
        w = self.window
        self.assertTrue(w.pin_button.isChecked()); self.assertTrue(w.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertEqual(w.cache.budget,128*1024**2); self.assertEqual(w.target.text(),str(self.base/'NAS'))
        location = LocationDialog(1,w); self.assertEqual(location.city.text(),'天津'); self.assertFalse(hasattr(location,'key')); location.reject()
        d = SettingsDialog(w); d.clear_key(); d.accept(); self.wait_idle()
        self.assertEqual(w.settings.value('amap_key_protected'), '')

    def test_connection_test_uses_pending_key_without_saving_and_location_uses_saved_key(self):
        w=self.window; d=SettingsDialog(w); d.key.setText('pending-secret'); d.city.setText('天津')
        with patch('settings_dialog.search_places',return_value=[]) as search:
            d.test_key(); self.wait_idle()
        search.assert_called_once_with('pending-secret','公园','天津')
        self.assertEqual(d.key_status.text(),'连接成功'); self.assertIsNone(w.settings.value('amap_key_protected'))
        d.accept(); self.wait_idle()
        loc=LocationDialog(1,w); loc.query.setText('海边')
        with patch('location_dialog.search_places',return_value=[]) as search:
            loc.search(); self.wait_idle()
        search.assert_called_once_with('pending-secret','海边','天津'); loc.reject()

    def test_format_filter_excludes_hidden_files_from_plan_and_resets_on_new_scan(self):
        w=self.window; source=self.base/'source'; source.mkdir()
        for name in ['a.JPG','b.jpg','c.png']: Image.new('RGB',(40,30),'blue').save(source/name)
        child=source/'child'; child.mkdir(); Image.new('RGB',(40,30)).save(child/'hidden.jpg')
        w.source.setText(str(source)); w.target.setText(str(self.base/'dest')); w.scan_source(); self.wait_idle()
        self.assertEqual(w.source_total,3); self.assertIn('2',w.format_boxes['.jpg'].text())
        w.format_boxes['.jpg'].setChecked(False)
        self.assertEqual([r['observed_name'] for r in w.source_model.rows],['c.png'])
        # Inspect service input directly: hidden JPEGs must not reach planning.
        build=w.service.build_plan
        with patch.object(w.service,'build_plan',wraps=build) as called, patch.object(PlanDialog,'exec',return_value=QDialog.DialogCode.Rejected):
            w.preview_import(); self.wait_idle()
        self.assertEqual([r['observed_name'] for r in called.call_args.args[0]],['c.png'])
        w.format_boxes['.png'].setChecked(False); self.assertEqual(w.source_model.rowCount(),0)
        self.assertFalse(w.preview_button.isEnabled())
        w.reset_formats(); self.assertEqual(w.source_model.rowCount(),3)
        w.format_boxes['.jpg'].setChecked(False); w.scan_source(); self.wait_idle()
        self.assertEqual(w.source_model.rowCount(),3)

    def test_reveal_file_from_details_selects_actual_file(self):
        path=self.base/'含空格 文件.jpg'; Image.new('RGB',(20,20)).save(path)
        w=self.window
        with patch('organizer_app.subprocess.Popen') as popen:
            w.reveal_file(str(path))
        self.assertEqual(popen.call_args.args[0],['explorer.exe','/select,',str(path.absolute())])
        source=self.base/'media'; source.mkdir(); path.rename(source/path.name)
        w.catalog.scan(source,recursive=False); row=w.catalog.search()[1][0]
        def inspect(dialog):
            buttons=dialog.findChildren(QPushButton)
            folder=next(b for b in buttons if b.text()=='打开所在文件夹'); folder.click()
            return QDialog.DialogCode.Rejected
        with patch.object(w,'reveal_file') as reveal, patch.object(QDialog,'exec',inspect): w.show_details(row)
        reveal.assert_called_once_with(row['current_path'])

    def test_backup_is_unique_and_readable_cache_clear_preserves_non_cache_files(self):
        w=self.window; d=SettingsDialog(w)
        sentinel=w.cache.folder/'keep.jpg'; Image.new('RGB',(20,20)).save(sentinel)
        cached=w.cache.folder/('a'*64+'.jpg'); Image.new('RGB',(20,20)).save(cached)
        d.clear_cache(); self.wait_idle(); self.assertTrue(sentinel.exists()); self.assertFalse(cached.exists())
        backup_dir=self.base/'backups'; backup_dir.mkdir()
        with patch('settings_dialog.QFileDialog.getExistingDirectory',return_value=str(backup_dir)):
            d.backup(); self.wait_idle(); d.backup(); self.wait_idle()
        files=list(backup_dir.glob('*.sqlite3')); self.assertEqual(len(files),2)
        for file in files:
            with closing(sqlite3.connect(file)) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],7)
        with patch.object(QDialog,'exec',return_value=QDialog.DialogCode.Rejected):
            d.show_errors(); self.wait_idle()
        self.assertFalse(d.working); self.assertNotIn('no such', d.message.text()); d.reject()

    def test_watermark_template_editor_has_no_media_exports_and_sets_default(self):
        w=self.window; d=WatermarkDialog([],w,template_mode=True); d.show()
        until=time.monotonic()+10
        while time.monotonic()<until and not d.preview_valid:
            self.app.processEvents(); time.sleep(.02)
        self.assertTrue(d.preview_valid,d.preview_status.text())
        d.custom.setPlainText('默认签名'); d.preset_name.setText('旅行'); d.save_preset()
        until=time.monotonic()+10
        while time.monotonic()<until and not d.preview_valid:
            self.app.processEvents(); time.sleep(.02)
        d.accept(); self.wait_idle()
        self.assertEqual(json.loads(w.settings.value('watermark_default'))['custom'],'默认签名')
        self.assertEqual(w.processing_history.page(record_type='watermark'),[])
        settings=SettingsDialog(w); self.assertEqual(settings.presets.currentText(),'旅行')
        settings.use_preset()
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes): settings.delete_preset()
        self.assertEqual(settings.presets.count(),0); settings.reject()
        self.assertEqual(list(self.base.rglob('*watermarked*')),[])

    def test_invalid_settings_do_not_partially_save_or_replace_existing_key(self):
        w=self.window; d=SettingsDialog(w); d.key.setText('secret'); d.template.setText('{invalid}')
        d.accept(); self.assertNotEqual(d.result(),QDialog.DialogCode.Accepted)
        self.assertIsNone(w.settings.value('amap_key_protected')); d.reject()
        w.settings.setValue('amap_key_protected','invalid-base64')
        d=SettingsDialog(w); self.assertTrue(d.key_read_error)
        d.city.setText('天津'); d.accept(); self.wait_idle()
        self.assertEqual(w.settings.value('amap_key_protected'),'invalid-base64')

if __name__=='__main__': unittest.main()
