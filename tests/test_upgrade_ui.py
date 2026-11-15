import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import unittest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox
from tests import test_task_ui as fixture


class UpgradeUiTests(unittest.TestCase):
    setUpClass=classmethod(fixture.TaskUiTests.setUpClass.__func__)
    setUp=fixture.TaskUiTests.setUp
    tearDown=fixture.TaskUiTests.tearDown
    wait=fixture.TaskUiTests.wait

    def test_choose_output_keeps_explicit_dependency_and_default_original(self):
        w=self.w;row=w.source_model.rows[0]
        batch=w.task_queue.submit('watermark',[row],dict(target_root=str(self.base/'output'),options=dict(custom='test',fields=[])))
        w.task_queue.control(batch,'pause')
        seen=[]
        def select():
            dialog=next((x for x in self.app.topLevelWidgets() if isinstance(x,QMessageBox) and x.isVisible()),None)
            if dialog is None:QTimer.singleShot(10,select);return
            seen.append(dialog.defaultButton().text())
            folder=Path(__file__).resolve().parents[1]/'work/upgrade_416';folder.mkdir(parents=True,exist_ok=True)
            dialog.grab().save(str(folder/'choose-output.png'))
            next(b for b in dialog.buttons() if b.text()=='新成品').click()
        QTimer.singleShot(10,select)
        w.enqueue('gps',[row],dict(latitude=30.,longitude=120.))
        fixture.TaskUiTests.submitted(self)
        self.assertEqual(seen,['原片'])
        self.assertEqual(w.task_queue.tasks[-1]['options']['_follow_output'],w.task_queue.tasks[0]['record']['id'])
        w.task_queue.control(None,'stop')

    def test_thumbnail_requests_defer_while_jobs_are_queued(self):
        w=self.w;row=dict(w.source_model.rows[0]);row['mtime_ns']+=1000
        batch=w.task_queue.submit('gps',[row],dict(latitude=30.,longitude=120.));w.task_queue.control(batch,'pause')
        before=len(w.loader.pending);self.assertIsNone(w.loader.icon(row));self.assertEqual(len(w.loader.pending),before)
        w.task_queue.control(None,'stop')
