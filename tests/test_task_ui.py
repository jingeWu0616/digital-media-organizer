import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from PIL import Image
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QDialog, QPushButton
from organizer_app import MainWindow, STYLE
from source_dialog import SourceFoldersDialog


class TaskUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([]);cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.source=self.base/'photos';self.source.mkdir()
        for i in range(4):Image.new('RGB',(60,40),(i*50,80,150)).save(self.source/f'DJI_20261109090432_015{i}_D.jpg')
        self.w=MainWindow(self.base/'data');self.w.settings.setValue('auto_color',False)
        self.w.show();self.w.set_source_paths([self.source]);self.w.scan_source();self.wait()

    def wait(self):
        end=time.monotonic()+20
        while time.monotonic()<end:
            self.app.processEvents();time.sleep(.01)
            if not self.w.busy and not self.w.jobs and not self.w.loader.jobs and not self.w.task_queue.active:return
        self.fail('任务未结束')

    def tearDown(self):
        self.w.task_queue.control(None,'stop');self.wait();self.w.close();self.app.processEvents();self.temp.cleanup()

    def submitted(self):
        end=time.monotonic()+10
        while self.w.busy and time.monotonic()<end:self.app.processEvents();time.sleep(.01)
        self.assertFalse(self.w.busy)

    def test_one_folder_entry_reopens_selected_scope(self):
        w=self.w
        self.assertIs(w.source.chooser,w.source_folders_button)
        self.assertEqual(sum('多选文件夹' in b.text() for b in w.findChildren(QPushButton)),0)
        def choose(dialog):
            self.assertEqual(dialog.paths(),[str(self.source)])
            dialog.add_paths([self.base]);return QDialog.DialogCode.Accepted
        with patch.object(SourceFoldersDialog,'exec',choose):w.source.chooser.click()
        self.assertEqual(w.source_paths(),[str(self.source),str(self.base)])
        self.assertEqual(w.source_model.rows,[])

    def test_can_schedule_next_action_and_view_durable_timing(self):
        w=self.w;row=w.source_model.rows[0]
        w.enqueue('gps',[row],dict(latitude=30.,longitude=120.))
        self.submitted()
        self.assertFalse(w.busy);self.assertTrue(w.source_gps_button.isEnabled())
        w.enqueue('gps',[row],dict(latitude=31.,longitude=121.,overwrite=True))
        self.wait()
        fresh=w.processing_history.media(row['id']);self.assertAlmostEqual(fresh['latitude'],31.)
        self.assertEqual(len(w.processing_history.page(record_type='gps')),2)
        w.tabs.setCurrentIndex(2);w.refresh_history();self.wait()
        self.assertIn('实际执行',w.history_details.toPlainText());self.assertIn('排队',w.history_details.toPlainText())
        self.assertEqual(w.task_panel.overall.value(),2)
        w.task_panel.expand.click();self.assertFalse(w.task_panel.body.isVisible())

    def test_parallel_rows_unknown_phase_pause_and_scope_change(self):
        w=self.w
        def run(action,row,options,cancel,progress):
            progress(dict(phase='还原颜色' if action=='color' else '写入临时视频',percent=42 if action=='color' else None))
            cancel.wait(5);return dict(stopped=True)
        w.task_queue.runner=run
        w.enqueue('color',w.source_model.rows[:2],{})
        self.submitted()
        w.enqueue('gps',w.source_model.rows[2:],dict(latitude=30.,longitude=120.))
        self.submitted()
        end=time.monotonic()+2
        while time.monotonic()<end:
            self.app.processEvents()
            if len([r for r in w.task_queue.snapshot() if r['phase'] in ('还原颜色','写入临时视频')])==2:break
            time.sleep(.01)
        w.poll_tasks();running=[r for r in w.task_panel.model.rows if r['status']=='running']
        self.assertEqual(len(running),2);self.assertEqual({r['percent'] for r in running},{42,None})
        w.task_panel.pause.click();w.poll_tasks()
        self.assertEqual(sum(r['status']=='paused' for r in w.task_queue.snapshot()),2)
        w.set_source_paths([self.base]);w.invalidate_source();self.assertEqual(w.source_model.rows,[])
        self.assertTrue(w.task_queue.active)
        with patch('ui_common.work_area',return_value=QRect(0,0,960,520)):
            from ui_common import fit_window
            w.resize(1000,680);fit_window(w);self.app.processEvents()
            self.assertTrue(w.rect().contains(w.task_panel.geometry()))
        w.task_panel.stop.click();self.wait()
        self.assertEqual(w.source_model.rows,[])
        self.assertTrue(all(r['status']=='cancelled' for r in w.task_queue.snapshot()))

    def test_slow_nas_preparation_does_not_freeze_ui_and_can_stop(self):
        import threading
        from PySide6.QtCore import QTimer
        import task_queue
        entered=threading.Event();release=threading.Event();real=task_queue.file_keys
        fired=[]
        def slow(row):
            entered.set();release.wait(3);return real(row)
        with patch('task_queue.file_keys',slow):
            self.w.enqueue('gps',self.w.source_model.rows[:1],dict(latitude=30.,longitude=120.))
            QTimer.singleShot(10,lambda:fired.append(True))
            end=time.monotonic()+2
            while not fired and time.monotonic()<end:self.app.processEvents();time.sleep(.01)
            self.assertTrue(fired);self.assertTrue(entered.is_set())
            self.w.stop();release.set();self.wait()
        self.assertEqual(self.w.task_queue.snapshot(),[])
        self.assertIn('已取消加入任务',self.w.status.text())


if __name__=='__main__':unittest.main()
