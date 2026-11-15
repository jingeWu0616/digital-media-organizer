"""One paginated, read-only timeline over existing import and color journals."""
from contextlib import closing
import json
from datetime import datetime


class ProcessingHistory:
    def __init__(self, catalog):
        self.catalog = catalog

    def page(self, limit=201, offset=0, record_type=''):
        if record_type not in ('', 'import', 'color', 'gps', 'watermark', 'activity'):
            raise ValueError('未知记录类型')
        with closing(self.catalog.connect()) as db:
            # Page the combined timeline first; never slice the two sources separately.
            events = db.execute('''SELECT * FROM (
                SELECT 'import' record_type, id, created_at, rowid sequence FROM imports
                UNION ALL
                SELECT 'color' record_type, id, created_at, rowid sequence FROM color_jobs
                    WHERE id NOT IN (SELECT journal_id FROM task_actions WHERE journal_type='color')
                UNION ALL
                SELECT 'gps' record_type, id, created_at, rowid sequence FROM gps_jobs
                    WHERE id NOT IN (SELECT journal_id FROM task_actions WHERE journal_type='gps')
                UNION ALL
                SELECT 'watermark' record_type, id, created_at, rowid sequence FROM watermark_jobs
                    WHERE id NOT IN (SELECT journal_id FROM task_actions WHERE journal_type='watermark')
                UNION ALL
                SELECT 'activity' record_type, id, created_at, rowid sequence FROM activity_changes
                UNION ALL
                SELECT 'task' record_type, id, created_at, rowid sequence FROM task_actions WHERE action<>'import'
                ) WHERE (?='' OR record_type=? OR (record_type='task' AND id IN
                    (SELECT id FROM task_actions WHERE action=?)))
                ORDER BY julianday(created_at) DESC, sequence DESC, record_type DESC, id DESC
                LIMIT ? OFFSET ?''', (record_type, record_type, record_type, limit, offset)).fetchall()
            result = []
            for event in events:
                kind, identifier = event['record_type'], event['id']
                if kind == 'task':
                    from task_queue import LABELS, STATES
                    row = dict(db.execute('SELECT * FROM task_actions WHERE id=?',(identifier,)).fetchone())
                    row.update(type_label=LABELS[row['action']], subject=row['source_name'], total=1,
                        completed_photos=int(row['status']=='complete' and row['kind']=='photo'),
                        completed_videos=int(row['status']=='complete' and row['kind']=='video'),
                        skipped=int(row['status']=='skipped'),errors=int(row['status']=='failed'),
                        display_status=STATES[row['status']])
                    if row['action']=='gps' and json.loads(row['options_json']).get('write_mode')=='fast':
                        row['type_label'] += ' · 快速'
                    if row['journal_type']=='gps' and row['journal_id']:
                        saved=db.execute('SELECT old_gps_json FROM gps_jobs WHERE id=?',(row['journal_id'],)).fetchone()
                        if saved:
                            row['type_label']='写入定位'+(' · 快速' if json.loads(saved[0] or '{}').get('_write_mode')=='fast' else '')
                    if row['journal_type'] in ('color','watermark') and row['journal_id']:
                        output=db.execute(f"SELECT output_media_id FROM {row['journal_type']}_jobs WHERE id=?",(row['journal_id'],)).fetchone()
                        if output:row['output_media_id']=output[0]
                elif kind == 'import':
                    row = dict(db.execute('SELECT * FROM imports WHERE id=?', (identifier,)).fetchone())
                    stats = db.execute('''SELECT count(*) total,
                        coalesce(sum(status='complete' AND kind='photo'),0) completed_photos,
                        coalesce(sum(status='complete' AND kind='video'),0) completed_videos,
                        coalesce(sum(status='skipped'),0) skipped,
                        coalesce(sum(status IN ('failed','source_retained')),0) errors
                        FROM import_items WHERE import_id=?''', (identifier,)).fetchone()
                    row.update(dict(stats), type_label='导入 · ' + ('移动' if row['mode'] == 'move' else '复制'))
                    row['subject'] = f"{row['total']} 个文件"
                    row['display_status'] = row['status']
                elif kind == 'color':
                    row = dict(db.execute('SELECT * FROM color_jobs WHERE id=?', (identifier,)).fetchone())
                    row.update(type_label='颜色调整', subject=row['source_name'], total=1,
                        completed_photos=0, completed_videos=int(row['status'] == 'complete'), skipped=0,
                        errors=int(row['status'] == 'failed' or bool(row['error'])))
                    row['display_status'] = {'encoding': '处理中 / 待恢复', 'ready': '待收尾',
                        'complete': '已还原', 'failed': '失败'}.get(row['status'], row['status'])
                elif kind == 'gps':
                    row = dict(db.execute('SELECT g.*,m.kind FROM gps_jobs g JOIN media m ON m.id=g.media_id WHERE g.id=?', (identifier,)).fetchone())
                    row.update(type_label='写入定位' + (' · 快速' if json.loads(row['old_gps_json'] or '{}').get('_write_mode') == 'fast' else ''), subject=row['source_name'], total=1,
                        completed_photos=int(row['status'] == 'complete' and row['kind']=='photo'), completed_videos=int(row['status'] == 'complete' and row['kind']=='video'), skipped=0,
                        errors=int(row['status'] == 'failed' or bool(row['error'])))
                    row['display_status'] = {'writing': '写入中 / 待恢复', 'ready': '待收尾',
                        'complete': '已写入', 'failed': '失败'}.get(row['status'], row['status'])
                elif kind=='activity':
                    row=dict(db.execute('SELECT * FROM activity_changes WHERE id=?',(identifier,)).fetchone())
                    kinds=[r[0] for identifier in json.loads(row['media_ids_json'])
                           if (r:=db.execute('SELECT kind FROM media WHERE id=?',(identifier,)).fetchone())]
                    row.update(type_label='活动与备注',total=len(kinds),completed_photos=kinds.count('photo'),completed_videos=kinds.count('video'),skipped=0,errors=0,status='complete',display_status='已保存')
                else:
                    row = dict(db.execute('SELECT * FROM watermark_jobs WHERE id=?',(identifier,)).fetchone())
                    row.update(type_label='照片水印',subject=row['source_name'],total=1,
                        completed_photos=int(row['status']=='complete'),completed_videos=0,skipped=0,
                        errors=int(row['status']=='failed' or bool(row['error'])))
                    row['display_status']={'rendering':'处理中 / 待恢复','ready':'待收尾','complete':'已另存','failed':'失败'}.get(row['status'],row['status'])
                row.update(record_type=kind, record_key=kind + ':' + identifier)
                result.append(row)
            return result

    def details(self, record_type, identifier):
        with closing(self.catalog.connect()) as db:
            if record_type == 'task':
                rows=[dict(r) for r in db.execute('SELECT * FROM task_actions WHERE id=?',(identifier,))]
                for row in rows:
                    if row['journal_type'] in ('color','gps','watermark') and row['journal_id']:
                        job=db.execute(f"SELECT * FROM {row['journal_type']}_jobs WHERE id=?",(row['journal_id'],)).fetchone()
                        if job:row['journal']=dict(job)
                return rows
            if record_type == 'import':
                return [dict(r) for r in db.execute('''SELECT i.*, m.current_path FROM import_items i
                    LEFT JOIN media m ON m.id=i.media_id WHERE import_id=? ORDER BY i.rowid''', (identifier,))]
            if record_type == 'color':
                return [dict(r) for r in db.execute('''SELECT c.*, m.current_path, m.capture_time, m.place
                    FROM color_jobs c LEFT JOIN media m ON m.id=coalesce(c.output_media_id,c.media_id) WHERE c.id=?''', (identifier,))]
            if record_type == 'gps':
                return [dict(r) for r in db.execute('''SELECT g.*,m.current_path FROM gps_jobs g
                    LEFT JOIN media m ON m.id=g.media_id WHERE g.id=?''', (identifier,))]
            if record_type == 'watermark':
                return [dict(r) for r in db.execute('''SELECT w.*,m.current_path FROM watermark_jobs w
                    LEFT JOIN media m ON m.id=w.output_media_id WHERE w.id=?''',(identifier,))]
            if record_type=='activity':
                return [dict(r) for r in db.execute('SELECT * FROM activity_changes WHERE id=?',(identifier,))]
            raise ValueError('未知记录类型')

    def timings(self, record_type, identifier):
        with closing(self.catalog.connect()) as db:
            if record_type=='import':
                return [dict(r) for r in db.execute('''SELECT t.* FROM task_actions t JOIN import_items i
                    ON t.journal_id=i.id AND t.journal_type='import_item' WHERE i.import_id=? ORDER BY t.rowid''',(identifier,))]
            return [dict(r) for r in db.execute('SELECT * FROM task_actions WHERE journal_type=? AND journal_id=? ORDER BY rowid',
                                               (record_type,identifier))]

    def media(self, media_id):
        with closing(self.catalog.connect()) as db:
            row = db.execute('''SELECT m.*,a.name activity_name,a.companions activity_companions,a.note activity_note
                FROM media m LEFT JOIN activities a ON a.id=m.activity_id WHERE m.id=?''', (media_id,)).fetchone()
            return dict(row) if row else None


def describe_timing(row):
    from task_queue import LABELS, STATES
    row=dict(row)
    if row['status']=='running' and row['started_at']:
        row['elapsed']=max(row['elapsed'],(datetime.now().astimezone()-datetime.fromisoformat(row['started_at'])).total_seconds())
    elif row['status']=='queued':
        row['wait_seconds']=max(0,(datetime.now().astimezone()-datetime.fromisoformat(row['created_at'])).total_seconds()-row['pause_seconds'])
    def seconds(value):return f'{value:.2f} 秒'
    parts=[f"{row['source_name']} · {LABELS[row['action']]} · {STATES[row['status']]}",
        f"处理时路径    {row['source_path']}",f"加入时间    {row['created_at']}",
        f"开始时间    {row['started_at'] or '尚未开始'}",f"结束时间    {row['completed_at'] or '尚未结束'}",
        f"实际执行    {seconds(row['elapsed'])}    排队    {seconds(row['wait_seconds'])}    暂停    {seconds(row['pause_seconds'])}"]
    if row.get('retry_of'):parts.append('本次为重试，原尝试记录保留')
    if row['status']=='running':parts.append('当前阶段    '+row['phase']+' · 进行中')
    for phase in json.loads(row['phases_json']):
        parts.append(f"  {phase['name']}    {phase['started_at']} → {phase['completed_at']}    {seconds(phase['seconds'])}")
        detail=phase.get('detail',{})
        if detail.get('encoder'):parts.append('    编码器    '+detail['encoder'])
        if detail.get('note'):parts.append('    '+detail['note'])
    if row.get('error'):parts.append(('跳过原因    ' if row['status']=='skipped' else '说明    ')+row['error'])
    job=row.get('journal',{})
    if job.get('encoder'):parts.append('编码器    '+job['encoder'])
    if job.get('output_path'):parts.append(('另存路径    ' if row['action']=='watermark' else '输出路径    ')+job['output_path'])
    if job.get('rendered_text'):parts.append('水印内容\n'+job['rendered_text'])
    if job.get('profile_json'):
        removed=json.loads(job['profile_json']).get('removed_private_tracks',[])
        parts.append('已确认移除的专有轨道    '+(', '.join(str(t.get('codec_id') or t.get('format')) for t in removed) or '无'))
    return '\n'.join(parts)

