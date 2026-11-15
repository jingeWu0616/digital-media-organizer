"""Bounded file work with durable timing, ordered conflicts and explicit retries."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import json
import hashlib
import os
import threading
import time
import uuid

from task_telemetry import observing

LABELS = {'gps': '写入定位', 'color': '颜色还原', 'watermark': '照片水印', 'import': '导入'}
STATES = {'queued': '等待执行', 'running': '处理中', 'paused': '已暂停', 'complete': '完成',
          'skipped': '跳过', 'failed': '失败', 'cancelled': '已停止', 'interrupted': '中断待确认'}
ACTIVE = {'queued', 'running', 'paused'}


def can_use_protected(record):
    if not record or record.get('action')!='gps' or record.get('status')!='skipped' or record.get('kind')!='video' or not record.get('error'):
        return False
    return json.loads(record['options_json']).get('write_mode')=='fast'


def can_use_color_gps_compatible(record):
    from video_gps import GPS_COMPATIBILITY_REQUIRED
    return bool(record and record.get('action')=='color' and record.get('status')=='failed'
                and GPS_COMPATIBILITY_REQUIRED in (record.get('error') or '')
                and json.loads(record['options_json']).get('gps_preservation','fast')=='fast')


def _now():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def file_keys(row):
    path = Path(row['current_path'])
    keys = {'id:' + row.get('id', ''), 'path:' + os.path.normcase(str(path.resolve()))}
    keys.discard('id:')
    try:
        info = path.stat()
        if info.st_ino:
            keys.add(f'inode:{info.st_dev}:{info.st_ino}')
    except OSError:
        pass
    return keys


class TaskQueue:
    def __init__(self, catalog, cache=None, runner=None):
        self.catalog, self.cache = catalog, cache
        self.runner = runner or self._run_service
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='media-task')
        self.lock = threading.RLock()
        self.tasks = []
        self.trusted_updates = {}
        self.readers = Counter()
        self.color_service = None
        self.closed = False
        self.revision = 0
        # A restart must never replay destructive work without another click.
        with closing(catalog.connect()) as db, db:
            db.execute("UPDATE task_actions SET status='interrupted', error='程序中断；请核对原文件后重试。' WHERE status IN ('queued','running','paused')")

    def _save(self, task):
        row = task['record']
        with closing(self.catalog.connect()) as db, db:
            db.execute('UPDATE task_actions SET ' + ','.join(k+'=?' for k in row if k != 'id') + ' WHERE id=?',
                       [v for k,v in row.items() if k != 'id'] + [row['id']])

    def conflicts(self, rows, imports_only=False):
        keys = set().union(*(file_keys(r) for r in rows)) if rows else set()
        with self.lock:
            return any(t['record']['status'] in ACTIVE and (not imports_only or t['record']['action']=='import')
                       and keys & t['keys'] for t in self.tasks)

    def output_predecessors(self, rows):
        result={}
        with self.lock:
            for row in rows:
                candidates=[t for t in self.tasks if t['record']['media_id']==row['id'] and
                            (t['record']['action']=='watermark' or (t['record']['action']=='color' and t['options'].get('output_mode')=='copy'))]
                if candidates and candidates[-1]['record']['status'] in ACTIVE|{'complete'}:
                    result[row['id']]=candidates[-1]['record']['id']
        return result

    def submit(self, action, rows, options, retry_of=None, cancel=None, follow_outputs=None):
        if action not in LABELS or self.closed:
            raise ValueError('任务类型无效或程序正在关闭。')
        rows = list({r['id']: deepcopy(dict(r)) for r in rows}.values())
        options=deepcopy(options)
        if action=='watermark' and options.get('options',{}).get('logo'):
            logo=Path(options['options']['logo'])
            if logo.stat().st_size>20*1024**2:raise ValueError('Logo 请使用小于 20 MB 的图片。')
            fingerprint=hashlib.sha256(logo.read_bytes()).hexdigest()
            if options.get('_logo_hash',fingerprint)!=fingerprint:
                raise ValueError('Logo 已变化，请重新预览水印后提交。')
            options['_logo_hash']=fingerprint
        if not rows: raise ValueError('没有可执行的素材。')
        # Import previews freeze names and source statistics. Do not silently
        # replace that preview with the result of a queued destructive action.
        if self.conflicts(rows, imports_only=action!='import'):
            raise ValueError('这些素材还有待完成的整理任务。请等相关处理完成，再预览整理或添加操作。')
        batch = uuid.uuid4().hex
        created, mono = _now(), time.monotonic()
        prepared = []
        for row in rows:
            if cancel and cancel.is_set():return None
            row_options=deepcopy(options)
            if follow_outputs and row['id'] in follow_outputs:
                if action not in ('gps','watermark'):raise ValueError('此操作不能跟随另存成品。')
                predecessor=follow_outputs[row['id']]
                if self.output_predecessors([row]).get(row['id'])!=predecessor:raise ValueError('另存任务已变化，请重新选择。')
                row_options['_follow_output']=predecessor
            record = dict(id=uuid.uuid4().hex, batch_id=batch, action=action, media_id=row['id'],
                source_path=row['current_path'], source_name=Path(row['current_path']).name, kind=row['kind'],
                snapshot_json=json.dumps(row, ensure_ascii=False), options_json=json.dumps(row_options, ensure_ascii=False),
                created_at=created, started_at=None, completed_at=None, status='queued', elapsed=0.,
                wait_seconds=0., pause_seconds=0., phase='等待执行', phases_json='[]',
                journal_type=None, journal_id=None, error='',
                retry_of=retry_of.get(row['id']) if isinstance(retry_of,dict) else retry_of)
            if action=='import':
                record.update(journal_type='import_item',journal_id=options['items'][row['id']])
            keys = file_keys(row)
            prepared.append(dict(record=record, row=row, options=row_options, keys=keys,
                cancel=threading.Event(), future=None, enqueued=mono, paused_at=None,
                start=None, phase_start=None, phases=[], percent=None, last_saved=mono))
        with self.lock, closing(self.catalog.connect()) as db, db:
            if cancel and cancel.is_set():return None
            for task in prepared:
                if action!='import':
                    # A preceding action can finish while NAS identity checks
                    # are running. Follow only this queue's verified changes.
                    row=task['row']
                    key=lambda r:(r['id'],r['current_path'],r['size'],r['mtime_ns'])
                    visited=set()
                    while key(row) in self.trusted_updates and key(row) not in visited:
                        visited.add(key(row));row=self.trusted_updates[key(row)].copy()
                    task['row']=row
                    task['record']['snapshot_json']=json.dumps(row,ensure_ascii=False)
                r = task['record']
                db.execute(f'INSERT INTO task_actions ({",".join(r)}) VALUES ({",".join("?" for _ in r)})', list(r.values()))
            self.tasks.extend(prepared)
            self.revision += 1
        return batch

    def _sync_imports(self):
        imports={t['options']['import_id'] for t in self.tasks if t['record']['action']=='import'}
        if not imports:return
        with closing(self.catalog.connect()) as db, db:
            for identifier in imports:
                active=any(t['record']['status'] in ACTIVE and t['options'].get('import_id')==identifier for t in self.tasks)
                pending=db.execute("SELECT 1 FROM import_items WHERE import_id=? AND status NOT IN ('complete','skipped') LIMIT 1",(identifier,)).fetchone()
                stopped=any(t['record']['status']=='cancelled' and t['options'].get('import_id')==identifier for t in self.tasks)
                status='running' if active else 'completed' if not pending else 'stopped' if stopped else 'completed_with_errors'
                db.execute('''UPDATE imports SET finished_at=CASE WHEN ? THEN NULL
                    WHEN finished_at IS NULL OR status<>? THEN ? ELSE finished_at END,status=? WHERE id=?''',
                    (active,status,_now(),status,identifier))

    def _finish_phase(self, task):
        if task['phase_start'] is None: return
        task['phases'].append(dict(name=task['record']['phase'], started_at=task['phase_wall'],
                                  completed_at=_now(), seconds=time.monotonic()-task['phase_start'],
                                  detail=dict(task.get('metrics',{}))))
        task['record']['phases_json'] = json.dumps(task['phases'], ensure_ascii=False)
        task['phase_start'] = None

    def _observe(self, task, event):
        with self.lock:
            phase = event.get('phase')
            # Service batch counters don't end the actual file operation.
            if phase in ('已处理', 'finished'): return
            if phase == 'processing': phase = '准备导入'
            changed = phase and phase != task['record']['phase']
            if changed:
                self._finish_phase(task)
                task['record']['phase'] = phase
                task['phase_start'], task['phase_wall'] = time.monotonic(), _now()
                task['percent'] = None
                task['byte_sample'] = None
                metrics=task.setdefault('metrics',{})
                for key in ('fps','speed','bytes_per_second'):metrics.pop(key,None)
            if 'percent' in event: task['percent'] = event['percent']
            metrics=task.setdefault('metrics',{})
            for key in ('encoder','fps','speed','note'):
                if key in event:metrics[key]=event[key]
            if 'completed_bytes' in event:
                now=time.monotonic();sample=task.get('byte_sample');value=event['completed_bytes']
                if sample and value>=sample[1] and now-sample[0]>=.2:
                    metrics['bytes_per_second']=(value-sample[1])/(now-sample[0]);task['byte_sample']=(now,value)
                elif not sample or value<sample[1]:task['byte_sample']=(now,value)
            if event.get('journal_id'):
                task['record'].update(journal_type=event['journal_type'], journal_id=event['journal_id'])
            current = time.monotonic()
            if changed or event.get('journal_id') or current-task['last_saved'] >= 1:
                task['record']['elapsed'] = current-task['start']
                self._save(task)
                task['last_saved'] = current

    def _execute(self, task):
        try:
            with observing(lambda e: self._observe(task, e)):
                options=deepcopy(task['options']);options.pop('_follow_output',None)
                expected=options.pop('_expected_output_hash',None)
                if expected:
                    from import_service import digest
                    with closing(self.catalog.connect()) as db:
                        recovering=(task['record']['action']=='gps' and db.execute("SELECT 1 FROM gps_jobs WHERE media_id=? AND status IN ('writing','ready')",(task['row']['id'],)).fetchone())
                    if not recovering and digest(task['row']['current_path'],task['cancel'])!=expected:
                        raise ValueError('另存成品已变化，未执行后续操作。')
                return self.runner(task['record']['action'], task['row'], options,
                                   task['cancel'], lambda e: self._observe(task, e))
        except Exception as exc:
            return dict(errors=[f'{type(exc).__name__}: {exc}'], stopped=task['cancel'].is_set())

    def tick(self, dispatch=True):
        """Called on the GUI thread. Worker callbacks never touch Qt widgets."""
        completed = []
        with self.lock:
            for task in self.tasks:
                future = task['future']
                if not future or not future.done(): continue
                result = future.result()
                self._finish_phase(task)
                r = task['record']
                errors = result.get('errors') or []
                r['status'] = ('failed' if errors else 'complete' if result.get('complete') else
                               'skipped' if result.get('skipped') else 'cancelled' if result.get('stopped') else 'failed')
                r.update(completed_at=_now(), elapsed=time.monotonic()-task['start'],
                         error='\n'.join(errors + result.get('skip_reasons', [])), phase=STATES[r['status']])
                if result.get('recovered'):
                    r['error']=('已恢复上次定位，本次新坐标未应用。' if r['action']=='gps'
                                else '已恢复上次未完成的操作。')
                task['future'], task['percent'] = None, None
                self._save(task)
                # Refresh only successors whose original snapshot still matches.
                # An unrelated rescan must not bless outside changes.
                if r['status']=='complete':
                    with closing(self.catalog.connect()) as db:
                        fresh = db.execute('SELECT * FROM media WHERE id=?', (r['media_id'],)).fetchone()
                    if fresh:
                        fresh = dict(fresh)
                        old=task['row']
                        self.trusted_updates[(old['id'],old['current_path'],old['size'],old['mtime_ns'])]=fresh.copy()
                        for successor in self.tasks:
                            if successor['record']['status'] not in ('queued','paused'): continue
                            old = successor['row']
                            if old['id']==task['row']['id'] and all(old.get(k)==task['row'].get(k) for k in ('current_path','size','mtime_ns')):
                                successor['row'] = fresh.copy()
                                successor['record']['snapshot_json'] = json.dumps(fresh,ensure_ascii=False)
                                self._save(successor)
                completed.append(dict(r)); self.revision += 1
            if completed:self._sync_imports()
            if dispatch and not self.closed:
                running = [t for t in self.tasks if t['future']]
                occupied = set().union(*(t['keys'] for t in running), set(self.readers))
                earlier = set()
                for task in self.tasks:
                    r = task['record']
                    if r['status'] not in ('queued','paused'): continue
                    dependency=task['options'].get('_follow_output')
                    if dependency and not task.get('output_bound'):
                        with closing(self.catalog.connect()) as db:
                            predecessor=db.execute('SELECT * FROM task_actions WHERE id=?',(dependency,)).fetchone()
                            if predecessor and predecessor['status'] in ACTIVE:continue
                            output=None
                            if predecessor and predecessor['status']=='complete' and predecessor['journal_type'] in ('color','watermark'):
                                job=db.execute(f"SELECT * FROM {predecessor['journal_type']}_jobs WHERE id=?",(predecessor['journal_id'],)).fetchone()
                                if job and job['output_media_id']:
                                    output=db.execute('SELECT * FROM media WHERE id=?',(job['output_media_id'],)).fetchone()
                        if output is None:
                            r.update(status='failed',phase='失败',error='前序另存未成功，后续操作未执行。',completed_at=_now())
                            self._save(task);completed.append(dict(r));self.revision+=1;continue
                        row=dict(output);task['row']=row;task['output_bound']=True
                        task['options']['_expected_output_hash']=job['output_hash']
                        task['keys']={'id:'+row['id'],'path:'+os.path.normcase(os.path.abspath(row['current_path']))}
                        r.update(media_id=row['id'],source_path=row['current_path'],source_name=Path(row['current_path']).name,
                                 snapshot_json=json.dumps(row,ensure_ascii=False),options_json=json.dumps(task['options'],ensure_ascii=False))
                        self._save(task)
                    blocked = bool(task['keys'] & (occupied | earlier))
                    earlier |= task['keys']
                    action = r['action']
                    # One encoder, one import writer and one watermark renderer.
                    lane_busy = action in ('color','import','watermark') and any(t['record']['action']==action for t in running)
                    if r['status']=='paused' or blocked or lane_busy or len(running)>=2: continue
                    start = time.monotonic()
                    r.update(status='running', started_at=_now(), phase='准备处理',
                             wait_seconds=max(0,start-task['enqueued']-r['pause_seconds']))
                    task.update(start=start, phase_start=start, phase_wall=r['started_at'])
                    self._save(task)
                    task['future'] = self.pool.submit(self._execute, task)
                    running.append(task); occupied |= task['keys']; self.revision += 1
        return completed

    @contextmanager
    def read_guard(self, path):
        keys = file_keys(dict(current_path=str(path)))
        with self.lock:
            available = not any(t['future'] and keys & t['keys'] for t in self.tasks)
            if available: self.readers.update(keys)
        try: yield available
        finally:
            if available:
                with self.lock:
                    self.readers.subtract(keys)
                    self.readers += Counter()

    def control(self, batch, command):
        with self.lock:
            for task in self.tasks:
                r = task['record']
                if batch and r['batch_id'] != batch: continue
                if r['status'] not in ACTIVE: continue
                if command=='stop':
                    task['cancel'].set()
                    if task['future']: continue
                    if task['paused_at'] is not None:
                        r['pause_seconds'] += time.monotonic()-task['paused_at']; task['paused_at']=None
                    r.update(status='cancelled', phase='已停止', completed_at=_now(),
                             wait_seconds=max(0,time.monotonic()-task['enqueued']-r['pause_seconds']))
                elif command=='pause' and r['status']=='queued':
                    r.update(status='paused',phase='已暂停'); task['paused_at']=time.monotonic()
                elif command=='resume' and r['status']=='paused':
                    r['pause_seconds'] += time.monotonic()-task['paused_at']
                    task['paused_at']=None; r.update(status='queued',phase='等待执行')
                self._save(task)
            self.revision += 1
            self._sync_imports()

    def snapshot(self):
        with self.lock:
            result=[]
            for task in self.tasks:
                r=dict(task['record'])
                if task['future']: r['elapsed']=time.monotonic()-task['start']
                r['percent']=task['percent']
                r.update(task.get('metrics',{}))
                result.append(r)
            return result

    @property
    def active(self):
        with self.lock: return any(t['record']['status'] in ACTIVE for t in self.tasks)

    def shutdown(self):
        self.closed=True
        self.control(None,'stop')
        self.pool.shutdown(wait=False)

    def retry(self, identifier, *, compatible_gps=False):
        with closing(self.catalog.connect()) as db:
            r = db.execute('SELECT * FROM task_actions WHERE id=?',(identifier,)).fetchone()
        if not r or r['status'] not in ('failed','cancelled','interrupted'):
            raise ValueError('仅可重试失败、停止或中断的任务。')
        options=json.loads(r['options_json'])
        if compatible_gps:
            if not can_use_color_gps_compatible(dict(r)):
                raise ValueError('该任务不需要兼容保留定位。')
            options['gps_preservation']='compatible'
        row=json.loads(r['snapshot_json'])
        if self.conflicts([row]): raise ValueError('该文件还有任务，请完成后再重试。')
        return self.submit(r['action'],[row],options,retry_of=identifier)

    def protected_candidates(self, identifier):
        with closing(self.catalog.connect()) as db:
            selected=db.execute('SELECT * FROM task_actions WHERE id=?',(identifier,)).fetchone()
            if not selected or not can_use_protected(dict(selected)):
                raise ValueError('这条记录不是不支持快速写入而跳过的视频。')
            records=[dict(r) for r in db.execute('SELECT * FROM task_actions WHERE batch_id=? ORDER BY rowid',
                                                (selected['batch_id'],))]
            # A successful or still-active retry must not be offered again.
            retried={r[0] for r in db.execute("SELECT retry_of FROM task_actions WHERE retry_of IS NOT NULL AND status IN ('queued','running','paused','complete')")}
        return [r for r in records if can_use_protected(r) and r['id'] not in retried]

    def submit_protected(self, identifiers, *, confirmed=False, cancel=None):
        if not confirmed:raise ValueError('请先确认改用保护写入。')
        identifiers=list(dict.fromkeys(identifiers))
        if not identifiers:raise ValueError('请选择需要改用保护写入的记录。')
        candidates={r['id']:r for r in self.protected_candidates(identifiers[0])}
        if any(identifier not in candidates for identifier in identifiers):
            raise ValueError('处理范围已变化，请重新选择记录。')
        records=[candidates[identifier] for identifier in identifiers]
        options=json.loads(records[0]['options_json'])
        if any(json.loads(r['options_json'])!=options for r in records):
            raise ValueError('坐标或写入选项不同，请分别确认。')
        rows=[json.loads(r['snapshot_json']) for r in records]
        if self.conflicts(rows):raise ValueError('这些文件还有任务，请完成后再改用保护写入。')
        options['write_mode']='protected'
        return self.submit('gps',rows,options,retry_of={r['media_id']:r['id'] for r in records},cancel=cancel)

    def _run_service(self, action, row, options, cancel, progress):
        if action=='gps':
            from location_service import LocationService
            return LocationService(self.catalog).run([row], **options, cancel=cancel, progress=progress)
        if action=='color':
            from color_service import ColorService
            if self.color_service is None:self.color_service=ColorService(self.catalog)
            return self.color_service.run([row], **options, cancel=cancel, progress=progress)
        if action=='watermark':
            from watermark_service import WatermarkService
            options=dict(options)
            expected=options.pop('_logo_hash',None)
            if expected:
                from import_service import digest
                if digest(options['options']['logo'],cancel)!=expected:
                    raise ValueError('排队期间 Logo 已变化，请重新预览水印。')
            return WatermarkService(self.catalog).run([row], **options, cancel=cancel, progress=progress)
        from import_service import ImportService
        service=ImportService(self.catalog,self.cache)
        item_id=options['items'][row['id']]
        service.run(options['import_id'],cancel,progress,item_ids={item_id})
        item=next(i for i in service.items(options['import_id']) if i['id']==item_id)
        return dict(complete=int(item['status']=='complete'),skipped=int(item['status']=='skipped'),
                    stopped=cancel.is_set(),errors=[item['error']] if item['status']=='failed' else [])
