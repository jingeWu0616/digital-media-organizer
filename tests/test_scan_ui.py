import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from PySide6.QtWidgets import QApplication, QDialogButtonBox
from organizer_app import MainWindow, PlanDialog, STYLE
from source_dialog import SourceFoldersDialog, ScanFormatsDialog


class ScanUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([]); cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.base=Path(self.temp.name)
        self.a=self.base/'a'; self.b=self.base/'b'; (self.a/'child').mkdir(parents=True); self.b.mkdir()
        for parent in (self.a, self.b, self.a/'child'):
            Image.new('RGB',(40,30),'blue').save(parent/'photo.jpg')
            Image.new('RGB',(40,30),'red').save(parent/'photo.png')
        self.window=MainWindow(self.base/'data'); self.window.settings.setValue('auto_color',False)
        self.window.show(); self.wait()

    def wait(self):
        end=time.monotonic()+20
        while time.monotonic()<end:
            self.app.processEvents()
            if not self.window.busy and not self.window.task_queue.active and not self.window.jobs and not self.window.loader.jobs:return
            time.sleep(.01)
        self.fail('UI did not finish')

    def tearDown(self):
        self.wait(); self.window.close(); self.app.processEvents(); self.temp.cleanup()

    def test_multiselect_recursive_filter_and_reopen(self):
        w=self.window; w.set_source_paths([self.a,self.b]); w._custom_extensions={'.jpg'}
        w.scan_kind.setCurrentIndex(w.scan_kind.findData('custom')); w.scan_source(); self.wait()
        self.assertEqual(len(w.source_model.rows),2)
        self.assertFalse(w.source_table.isColumnHidden(8))
        self.assertIn(str(self.a.name), w.source_model.index(0,8).data())
        plan=w.service.build_plan(w.source_model.rows,w.source_paths(),self.base/'archive')
        preview=PlanDialog(plan,'copy',w)
        self.assertFalse(preview.table.isColumnHidden(4))
        self.assertIn('源目录',preview.file_details.toPlainText());preview.close()
        self.assertTrue(w.source.isReadOnly())
        w.scan_recursive.setChecked(True)
        self.assertEqual(w.source_model.rows,[]);self.assertFalse(w.color_button.isEnabled())
        w.scan_source();self.wait();self.assertEqual(len(w.source_model.rows),3)
        self.assertEqual(w._scan_roots,[str(self.a),str(self.b)])
        self.assertIn('JPG',w.scan_format_summary.text())
        w.save_settings();w.close();self.wait()
        self.window=MainWindow(self.base/'data');self.window.show();self.wait()
        self.assertEqual(self.window.source_paths(),[str(self.a),str(self.b)])
        self.assertFalse(self.window.scan_recursive.isChecked())

    def test_dialog_selection_cancel_and_empty_guard(self):
        dialog=SourceFoldersDialog([str(self.a)],self.window)
        dialog.add_paths([str(self.b),str(self.a)]);self.assertEqual(len(dialog.paths()),2)
        dialog.folders.selectAll();dialog.remove_folders()
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
        dialog.reject();self.assertEqual(self.window.source_paths(),[])
        formats=ScanFormatsDialog({'.jpg'},self.window);formats.select_all(False)
        self.assertFalse(formats.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
        formats.boxes['.mp4'].setChecked(True);self.assertEqual(formats.selected(),{'.mp4'})

    def test_visible_progress_and_scope_locked_while_running(self):
        w=self.window;w.set_busy(True,transfer=True)
        self.assertFalse(w.scan_recursive.isEnabled());self.assertFalse(w.scan_kind.isEnabled())
        w.color_progress(dict(phase='还原颜色',path='Z:/媒体/视频.mp4',filename='视频.mp4',encoder='CPU',index=2,total=5,processed=1,percent=42.5))
        self.assertEqual(w.progress_bar.value(),1);self.assertEqual(w.progress_bar.maximum(),5)
        self.assertEqual(w.file_progress_bar.value(),425)
        self.assertIn('2 / 5',w.status.text());self.assertEqual(w.progress_detail.text(),'Z:/媒体/视频.mp4')
        w.color_progress(dict(phase='校验视频',filename='视频.mp4',index=2,total=5,processed=1))
        self.assertFalse(w.file_progress_bar.isVisible());self.assertIn('校验视频',w.status.text())
        w.set_busy(False);self.assertEqual(w.progress_bar.value(),1)
