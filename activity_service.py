"""Transactional activity drafts, shared event details, and per-media notes."""
import json
import uuid
from contextlib import closing
from media_catalog import now


class ActivityService:
    def __init__(self,catalog):self.catalog=catalog

    def activities(self):
        with closing(self.catalog.connect()) as db:
            return [dict(r) for r in db.execute('''SELECT a.*,count(m.id) total FROM activities a
                LEFT JOIN media m ON m.activity_id=a.id GROUP BY a.id ORDER BY a.created_at DESC,a.rowid DESC''')]

    def rows(self,ids):
        with closing(self.catalog.connect()) as db:
            return [dict(r) for identifier in dict.fromkeys(ids)
                    if (r:=db.execute('SELECT * FROM media WHERE id=?',(identifier,)).fetchone())]

    def apply(self,rows,*,activity_id=None,new_activity=None,activity_patch=None,expected_activity=None,patch=None):
        patch=dict(patch or {});activity_patch=dict(activity_patch or {})
        if not rows:raise ValueError('请选择素材。')
        if set(patch)-{'place','media_note'} or set(activity_patch)-{'name','companions','note'}:raise ValueError('未知字段。')
        if new_activity is not None and activity_patch:raise ValueError('不能同时新建和修改活动。')
        def clean(values,limits):
            result={}
            for key,value in values.items():
                if key not in limits:raise ValueError('未知活动字段。')
                text=str(value).strip()
                if len(text)>limits[key]:raise ValueError(f'内容过长，最多 {limits[key]} 字。')
                if key=='name' and not text:raise ValueError('请填写活动名称。')
                result[key]=text
            return result
        patch=clean(patch,{'place':200,'media_note':1000})
        activity_patch=clean(activity_patch,{'name':100,'companions':300,'note':2000})
        if new_activity is not None:
            new_activity=clean(dict(name='',companions='',note='') | new_activity,{'name':100,'companions':300,'note':2000})
        snapshots={r['id']:r for r in rows}
        with closing(self.catalog.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            current=[]
            for identifier,snapshot in snapshots.items():
                r=db.execute('SELECT * FROM media WHERE id=?',(identifier,)).fetchone()
                if not r or any(r[k]!=snapshot.get(k) for k in ('activity_id','place','media_note','current_path')):
                    raise ValueError('素材信息已变化，请重新打开后修改。')
                if db.execute("SELECT 1 FROM import_items WHERE media_id=? AND status NOT IN ('complete','skipped')",(identifier,)).fetchone():
                    raise ValueError('所选素材有未完成的导入，请先完成或重试导入。')
                current.append(dict(r))
            changes=[];affected=list(snapshots)
            if new_activity is not None:
                activity_id=uuid.uuid4().hex;stamp=now()
                db.execute('INSERT INTO activities VALUES(?,?,?,?,?,?)',(activity_id,new_activity['name'],new_activity['companions'],new_activity['note'],stamp,stamp))
                changes.append(dict(action='新建活动',after=new_activity))
            elif activity_id:
                event=db.execute('SELECT * FROM activities WHERE id=?',(activity_id,)).fetchone()
                if not event:raise ValueError('活动已不可用，请重新选择。')
                if activity_patch:
                    if not expected_activity or any(event[k]!=expected_activity.get(k) for k in ('name','companions','note','updated_at')):
                        raise ValueError('活动已被修改，请重新打开后编辑。')
                    activity_patch={k:v for k,v in activity_patch.items() if event[k]!=v}
                    if activity_patch:
                        changes.append(dict(action='修改活动',before={k:event[k] for k in activity_patch},after=activity_patch))
                        db.execute('UPDATE activities SET '+','.join(k+'=?' for k in activity_patch)+',updated_at=? WHERE id=?',(*activity_patch.values(),now(),activity_id))
                        affected.extend(r[0] for r in db.execute('SELECT id FROM media WHERE activity_id=?',(activity_id,)))
            elif activity_patch:raise ValueError('请先选择活动。')
            for row in current:
                update=dict(patch)
                if activity_id is not None:update['activity_id']=activity_id or None
                update={k:v for k,v in update.items() if row[k]!=v}
                if not update:continue
                change=dict(action='修改素材',media_id=row['id'],name=row['observed_name'],before={k:row[k] for k in update},after=update)
                if 'activity_id' in update:
                    def event_name(identifier):
                        item=db.execute('SELECT name FROM activities WHERE id=?',(identifier,)).fetchone()
                        return item[0] if item else ''
                    change['activity_names']=[event_name(row['activity_id']),event_name(update['activity_id'])]
                changes.append(change)
                db.execute('UPDATE media SET '+','.join(k+'=?' for k in update)+' WHERE id=?',(*update.values(),row['id']))
            if changes:
                db.execute('INSERT INTO activity_changes VALUES(?,?,?,?,?)',(uuid.uuid4().hex,now(),f'{len(set(affected))} 个素材',json.dumps(changes,ensure_ascii=False),json.dumps(list(dict.fromkeys(affected)))))
        return dict(changed=bool(changes),total=len(set(affected)))


def describe_changes(changes):
    labels=dict(name='活动名称',companions='同行人',note='活动备注',place='地点',media_note='素材备注',activity_id='所属活动')
    lines=[]
    for change in changes:
        lines.append(change['action']+('    '+change['name'] if change.get('name') else ''))
        for key,value in change.get('after',{}).items():
            before=change.get('before',{}).get(key,'')
            if key=='activity_id':before,value=change.get('activity_names',['',''])
            lines.append(f"{labels.get(key,key)}\n原内容    {before or '未填写'}\n新内容    {value or '未填写'}")
        lines.append('')
    return '\n'.join(lines)
