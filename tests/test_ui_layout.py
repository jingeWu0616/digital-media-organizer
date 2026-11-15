"""Regression checks for naming feedback and constrained desktop work areas."""
import os
import json
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from PySide6.QtCore import QRect, QPoint, Qt, QMimeData
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QPushButton
from organizer_app import MainWindow, ColorDialog, STYLE
from settings_dialog import SettingsDialog
from location_dialog import LocationDialog
from watermark_dialog import WatermarkDialog
from ui_common import WrappingText


class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion'); cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.screen = patch('ui_common.work_area', return_value=QRect(0, 0, 1920, 1040))
        self.screen.start(); self.addCleanup(self.screen.stop)
        self.window = MainWindow(self.base/'data')
        self.window.show(); self.wait()

    def wait(self):
        deadline = time.monotonic()+20
        quiet = None
        while time.monotonic()<deadline:
            self.app.processEvents()
            w = self.window
            if not w.busy and not w.task_queue.active and not w.jobs and not w.loader.jobs:
                quiet = quiet or time.monotonic()
                if time.monotonic()-quiet > .25: return
            else: quiet = None
            time.sleep(.01)
        self.fail('workers did not finish')

    def tearDown(self):
        self.wait(); self.window.close(); self.app.processEvents(); self.temp.cleanup()

    def photo(self):
        source = self.base/'source'; source.mkdir()
        path = source/'DJI_0001.jpg'
        exif = Image.Exif(); exif[272] = 'DJI Osmo Action 6'
        exif[34665] = {36867:'2026:11:09 09:04:32'}
        Image.new('RGB', (60, 40), 'blue').save(path, exif=exif)
        w = self.window; w.source.setText(str(source)); w.scan_source(); self.wait()
        return source, path, w.source_model.rows[0]

    def test_naming_example_matches_real_plan_and_does_not_write_files(self):
        source, path, row = self.photo(); before = path.read_bytes()
        d = SettingsDialog(self.window, tab=1); d.show(); self.wait()
        for template in ('{date}_{weekday}_{device}_{place}_{original}',
                         '{original}__{place}', 'CON', 'a'*190+'{original}'):
            d.template.setText(template); self.app.processEvents()
            plan = self.window.service.build_plan([row], source, self.base/'target',
                dict(template=template, place='天津海边', keep_named=False))
            self.assertEqual(d.name_preview.text(), Path(plan['items'][0]['target_path']).name)
        d.template.setText('前后'); d.template.setCursorPosition(1)
        d.insert_template_field(d.insert_field.findData('{original}'))
        self.assertEqual(d.template.text(), '前{original}后')
        d.template.setText('{wrong}'); d.accept()
        self.assertEqual(d.name_preview.property('severity'), 'error')
        self.assertNotEqual(d.result(), QDialog.DialogCode.Accepted)
        self.assertIsNone(self.window.settings.value('template'))
        d.reject(); self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.base/'target').exists())

    def test_short_work_area_keeps_dialog_actions_visible(self):
        area = QRect(0, 0, 960, 520)
        with patch('ui_common.work_area', return_value=area):
            for make in (lambda:SettingsDialog(self.window, tab=1),
                         lambda:LocationDialog(1, self.window),
                         lambda:ColorDialog([], self.window),
                         lambda:WatermarkDialog([], self.window, template_mode=True)):
                d = make(); d.show(); self.wait()
                if isinstance(d, WatermarkDialog):
                    until = time.monotonic()+10
                    while not d.preview_valid and time.monotonic()<until: self.wait()
                    self.assertTrue(d.preview_valid)
                self.assertTrue(area.contains(d.geometry()), (type(d).__name__, d.geometry()))
                for box in d.findChildren(QDialogButtonBox):
                    for button in box.buttons():
                        if button.isVisible():
                            rect = QRect(button.mapTo(d, QPoint(0,0)), button.size())
                            self.assertTrue(d.rect().contains(rect), (button.text(), rect))
                if isinstance(d,WatermarkDialog):
                    self.assertTrue(d.rect().contains(QRect(d.preview.mapTo(d,QPoint(0,0)),d.preview.size())))
                else:
                    self.assertEqual(d.content_scroll.horizontalScrollBar().maximum(), 0, type(d).__name__)
                d.reject(); self.wait()

    def test_detail_long_path_wraps_and_copies_unmodified(self):
        _, path, row = self.photo()
        row = dict(row, current_path='C:\\素材\\'+'相同日期前缀_DJI_20261109_'*24+'0152_D.mp4')
        clipboard = self.app.clipboard()
        mime = clipboard.mimeData()
        previous = {fmt:bytes(mime.data(fmt)) for fmt in mime.formats()} if mime else {}
        try:
            def inspect(d):
                d.show(); self.wait()
                field = next(f for f in d.findChildren(WrappingText) if f.text()==row['current_path'])
                self.assertGreater(field.height(), field.fontMetrics().height()*2)
                self.assertEqual(field.horizontalScrollBar().maximum(), 0)
                next(b for b in d.findChildren(QPushButton) if b.text()=='复制路径').click()
                self.assertEqual(clipboard.text(), row['current_path'])
                self.assertEqual(field.toPlainText(), row['current_path'])
                d.reject(); return QDialog.DialogCode.Rejected
            with patch.object(QDialog, 'exec', inspect): self.window.show_details(row)
        finally:
            if self.app.platformName()=='offscreen':
                # Offscreen has a private clipboard. Clear Python-owned MIME
                # objects before Qt plugin teardown (Windows PySide lifetime).
                clipboard.clear()
            else:
                restored = QMimeData()
                for fmt, data in previous.items(): restored.setData(fmt, data)
                clipboard.setMimeData(restored)
        self.assertTrue(path.exists())

    def test_numeric_arrows_change_values_and_honor_limits(self):
        d = SettingsDialog(self.window, tab=4); d.show(); self.wait()
        spin = d.cache_mib; spin.setValue(100)
        QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=QPoint(spin.width()-10,8))
        self.assertEqual(spin.value(), 101)
        QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=QPoint(spin.width()-10,spin.height()-8))
        self.assertEqual(spin.value(), 100)
        spin.setValue(spin.maximum())
        QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=QPoint(spin.width()-10,8))
        self.assertEqual(spin.value(), spin.maximum())
        d.reject()

    def test_library_actions_follow_selection_and_busy_state(self):
        source, _, row = self.photo(); w = self.window
        w.refresh_library(); self.wait()
        self.assertEqual(w.library_total,0)
        self.assertEqual(w.library_model.rows,[])
        plan=w.service.build_plan([row],source,self.base/'archive')
        w.service.run(w.service.save_plan(plan,'copy'))
        w.refresh_library(); self.wait()
        self.assertFalse(w.library_detail_button.isEnabled())
        self.assertFalse(w.library_folder_button.isEnabled())
        self.assertFalse(w.library_gps_button.isEnabled())
        w.library_table.selectRow(0)
        self.assertTrue(w.library_detail_button.isEnabled())
        self.assertTrue(w.library_folder_button.isEnabled())
        self.assertTrue(w.library_gps_button.isEnabled())
        w.set_busy(True)
        self.assertFalse(w.library_gps_button.isEnabled())
        self.assertFalse(w.library_watermark_button.isEnabled())
        w.set_busy(False)
        w.refresh_library(); self.wait()
        self.assertFalse(w.library_detail_button.isEnabled())

    def watermark_ready(self,d):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            self.app.processEvents();time.sleep(.01)
            if d.preview_valid and not d.preview_running and not d.timer.isActive():return
        self.fail(d.preview_status.text())

    def test_watermark_looks_preserve_content_and_saved_defaults(self):
        from watermark_presets import LOOKS
        from watermark_service import STYLES
        d=WatermarkDialog([],self.window,template_mode=True);d.show()
        d.title.setText('我的旅行');d.signature.setText('摄影：我');d.custom.setPlainText('值得记住的一天')
        d.fields['camera'].setChecked(False)
        for key,look in LOOKS.items():
            d.look_buttons[key].click()
            self.assertEqual(d.title.text(),'我的旅行');self.assertEqual(d.signature.text(),'摄影：我')
            self.assertEqual(d.custom.toPlainText(),'值得记住的一天');self.assertFalse(d.fields['camera'].isChecked())
            self.assertEqual(STYLES[key],look['name'])
        self.watermark_ready(d)
        d.preset_name.setText('我的户外模板');d.save_preset();d.accept();self.wait()
        saved=json.loads(self.window.settings.value('watermark_default'))
        self.assertEqual(saved['signature'],'摄影：我');self.assertEqual(saved['style'],'outdoor')
        other=WatermarkDialog([],self.window,template_mode=True);other.show();self.watermark_ready(other)
        self.assertEqual(other.title.text(),'我的旅行')
        other.title.setText('取消的标题');other.reject();self.wait()
        self.assertEqual(json.loads(self.window.settings.value('watermark_default')),saved)

    def test_watermark_own_preview_is_read_only_and_validation_recovers(self):
        _,path,_=self.photo();before=path.read_bytes();w=self.window
        d=WatermarkDialog([],w,template_mode=True);d.show();self.watermark_ready(d)
        with patch('watermark_dialog.QFileDialog.getOpenFileName',return_value=(str(path),'')):d.choose_preview()
        self.watermark_ready(d)
        self.assertEqual(d.rows[d.photo.currentIndex()]['current_path'],str(path))
        self.assertNotIn('device',d.rows[d.photo.currentIndex()])
        self.assertEqual(d.cache[2][272],'DJI Osmo Action 6')
        d.custom.setPlainText('字'*301)
        self.wait();self.wait()
        self.assertFalse(d.start.isEnabled());self.assertIn('300',d.preview_status.text())
        d.custom.setPlainText('有效文字');self.watermark_ready(d)
        self.assertTrue(d.start.isEnabled());self.assertTrue(d.detail_button.isEnabled())
        self.assertEqual(d.preview.pixmap().devicePixelRatioF(),d.preview.devicePixelRatioF())
        def inspect(view):
            from PySide6.QtWidgets import QScrollArea
            scroll=view.findChild(QScrollArea)
            self.assertEqual(scroll.widget().pixmap().size(),d.pixmap.size())
            return QDialog.DialogCode.Rejected
        with patch.object(QDialog,'exec',inspect):d.detail_button.click()
        d.reject();self.wait()
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(w.catalog.search()[0],1);self.assertEqual(w.catalog.search(imported_only=True)[0],0)

    def test_duplicate_reason_and_resizable_history_detail(self):
        source, path, _ = self.photo(); w = self.window
        (source/'DJI_0002.jpg').write_bytes(path.read_bytes())
        w.scan_source(); self.wait()
        plan = w.service.build_plan(w.source_model.rows, source, self.base/'archive')
        identifier = w.service.save_plan(plan, 'copy')
        result = w.service.run(identifier)
        self.assertEqual((result['complete'], result['skipped']), (1,1))
        w.tabs.setCurrentIndex(2); w.resize(1000,680); w.refresh_history(); self.wait()
        text = w.history_details.toPlainText()
        self.assertIn('跳过原因', text)
        self.assertNotIn('异常    内容重复', text)
        self.assertEqual(w.imports_model.rows[0]['errors'], 0)
        before = w.history_details.height()
        self.assertGreaterEqual(before, 160)
        w.history_splitter.setSizes([100,400]); self.wait()
        self.assertGreater(w.history_details.height(), before)
        self.assertTrue(path.exists())

    def test_list_density_selection_and_original_name_visibility(self):
        _, _, row = self.photo(); w = self.window
        w.resize(1000,680); self.wait()
        self.assertTrue(w.source_table.isColumnHidden(2))
        self.assertFalse(w.library_table.isColumnHidden(2))
        w.source_model.replace([dict(row, original_name='原片.jpg')]); self.wait()
        self.assertFalse(w.source_table.isColumnHidden(2))
        w.source_table.selectAll()
        before = [i.row() for i in w.source_table.selectionModel().selectedRows()]
        # The explicit pre-scan scope controls occupy one additional row in 4.0.12.
        for compact, minimum in ((False,4),(True,7)):
            w.set_compact(compact); self.wait()
            self.assertGreaterEqual(w.source_table.viewport().height()//w.source_table.verticalHeader().defaultSectionSize(), minimum)
            self.assertEqual([i.row() for i in w.source_table.selectionModel().selectedRows()], before)
        with patch('ui_common.work_area', return_value=QRect(0,0,960,520)):
            w.hide(); w.show(); self.wait()
            self.assertTrue(QRect(0,0,960,520).contains(w.geometry()))
        self.assertEqual([i.row() for i in w.source_table.selectionModel().selectedRows()], before)


if __name__ == '__main__': unittest.main()
