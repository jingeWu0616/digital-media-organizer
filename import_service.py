"""Previewed, journaled imports with verified copies and recoverable moves.

No action is taken by build_plan(). Only run() executes a saved import plan.
Source files survive cancellation, mismatched verification and duplicates.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from media_catalog import Catalog, now, path_key
from source_scope import roots, encode
import task_telemetry as telemetry

CHUNK = 4 * 1024 * 1024
TERMINAL = {'complete', 'skipped'}


class Cancelled(Exception):
    pass


def digest(path, cancel=None):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        total, done = os.fstat(stream.fileno()).st_size, 0
        while data := stream.read(CHUNK):
            if cancel and cancel.is_set():
                raise Cancelled()
            value.update(data)
            done += len(data)
            telemetry.progress(done, total)
    return value.hexdigest()


def safe_component(value, maximum=160):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', text):
        text = '_' + text
    if len(text) > maximum:
        text = text[:maximum - 10] + '_' + hashlib.sha256(text.encode()).hexdigest()[:9]
    return text or '未命名'


def validate_name_template(template):
    fields = set(re.findall(r'{([^{}]+)}', template))
    if not fields <= {'date', 'weekday', 'device', 'place', 'original'} or re.search(r'[{}]', re.sub(r'{[^{}]+}', '', template)):
        raise ValueError('命名模板只支持 {date} {weekday} {device} {place} {original}。')


def render_filename(template, values, suffix):
    validate_name_template(template)
    stem = re.sub(r'_{2,}', '_', template.format(**values)).strip('_ ')
    return safe_component(stem) + suffix.lower()


def age_text(birthday, capture):
    if not birthday or not capture:
        return ''
    birth, target = date.fromisoformat(birthday), date.fromisoformat(capture[:10])
    if target < birth:
        return '未出生'
    months = (target.year - birth.year) * 12 + target.month - birth.month
    def anniversary(count):
        year, month0 = divmod(birth.year * 12 + birth.month - 1 + count, 12)
        return date(year, month0 + 1, min(birth.day, calendar.monthrange(year, month0 + 1)[1]))
    if anniversary(months) > target:
        months -= 1
    days = (target - anniversary(months)).days
    years, months = divmod(months, 12)
    return ''.join(f'{n}{label}' for n, label in ((years, '岁'), (months, '个月'), (days, '天')) if n) or '出生当天'


def beneath(path, root):
    resolved = Path(path).resolve()
    if not any(resolved != parent and resolved.is_relative_to(parent) for parent in roots(root)):
        raise ValueError('文件路径超出本次任务目录。')
    return resolved


def unused_path(desired, reserved=None):
    reserved = reserved or set()
    path = desired
    counter = 1
    while path.exists() or path_key(path) in reserved:
        path = desired.with_name(f'{desired.stem}_{counter}{desired.suffix}')
        counter += 1
    return path


def publish_without_replace(partial, final):
    if os.name == 'nt':
        os.rename(partial, final)  # Windows fails if final already exists.
    else:
        os.link(partial, final)   # Exclusive publication, never os.replace.
        Path(partial).unlink()


class ImportService:
    def __init__(self, catalog: Catalog, thumbnails=None):
        self.catalog = catalog
        self.thumbnails = thumbnails

    def build_plan(self, rows, source_root, target_root, options=None):
        options = dict(options or {})
        sources, target = roots(source_root, strict=True), Path(target_root).resolve()
        source = encode(sources)
        if any(parent == target or target.is_relative_to(parent) or parent.is_relative_to(target) for parent in sources):
            raise ValueError('源文件夹和目标文件夹不能相同或互相包含。')
        if any(self.catalog.database.resolve().is_relative_to(parent) for parent in [*sources, target]):
            raise ValueError('素材目录不能包含本地索引数据库。')
        if options.get('birthday'):
            date.fromisoformat(options['birthday'])
        template = options.get('template', '{date}_{weekday}_{device}_{place}_{original}')
        validate_name_template(template)
        days = {}
        if target.exists() and options.get('layout', 'day') == 'day':
            for folder in sorted(target.iterdir()):
                if folder.is_dir() and re.match(r'^\d{4}-\d{2}-\d{2}(?: |$)', folder.name):
                    days.setdefault(folder.name[:10], folder.name)
        plan, reserved = [], set()
        for row in rows:
            src = beneath(row['current_path'], source)
            with closing(self.catalog.connect()) as db:
                unfinished_color = db.execute("SELECT 1 FROM color_jobs WHERE source_path=? COLLATE NOCASE AND status IN ('encoding','ready') LIMIT 1", (str(src),)).fetchone()
                unfinished_gps = db.execute("SELECT 1 FROM gps_jobs WHERE media_id=? AND status IN ('writing','ready') LIMIT 1", (row['id'],)).fetchone()
            if unfinished_gps:
                raise ValueError(f'定位写入尚未收尾，请先在添加定位中恢复该照片  {src.name}')
            if unfinished_color:
                raise ValueError(f'颜色处理尚未收尾，请先在 D-Log M 颜色还原中恢复该文件  {src.name}')
            if src.is_symlink() or not src.is_file():
                raise ValueError(f'源文件不可用  {src}')
            info = src.stat()
            if (info.st_size, info.st_mtime_ns) != (row['size'], row['mtime_ns']):
                raise ValueError(f'文件已经变化，请重新扫描  {src.name}')
            capture = row['capture_time']
            day = capture[:10] if capture else '日期待确认'
            weekday = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[date.fromisoformat(day).weekday()] if capture else ''
            place = (row.get('place') if row.get('place') is not None else options.get('place', '')).strip()
            values = dict(date=day, weekday=weekday, device=(row['device'] or '').replace(' ', ''),
                          place=place, original=src.stem)
            named = bool(re.match(r'^\d{4}-\d{2}-\d{2}_(Mon|Tue|Wed|Thu|Fri|Sat|Sun)_', src.name))
            if named and options.get('keep_named', True):
                filename = src.name
            else:
                filename = render_filename(template, values, src.suffix)
            if capture:
                folder = ' '.join(p for p in (day, place, age_text(options.get('birthday'), capture)) if p)
                folder = days.get(day, safe_component(folder))
                if options.get('layout', 'day') == 'month':
                    base = target / day[:4] / day[5:7] / safe_component(folder)
                else:
                    base = target / folder
            else:
                base = target / '待确认日期'
            base = base / ('照片' if row['kind'] == 'photo' else '视频')
            if row.get('rating', 0):
                base /= f"{row['rating']}星"
            desired = base / (safe_component(Path(filename).stem, 160) + Path(filename).suffix)
            final = unused_path(desired, reserved)
            beneath(final, target)
            reserved.add(path_key(final))
            # A read-only preview can show conflict renames, but does not create folders.
            plan.append(dict(media_id=row['id'], source_path=str(src), source_name=src.name,
                             target_path=str(final), kind=row['kind'], size=info.st_size,
                             mtime_ns=info.st_mtime_ns, capture_time=capture, place=place,
                             existing_path=str(desired) if desired.exists() else None))
        if not plan:
            raise ValueError('请选择需要导入的文件。')
        return dict(source_root=str(source), target_root=str(target), options=options, items=plan)

    def save_plan(self, plan, mode='move'):
        if mode not in ('copy', 'move'):
            raise ValueError('无效的导入方式')
        identifier = uuid.uuid4().hex
        options = dict(plan['options'])
        options['existing_paths'] = {p['media_id']: p['existing_path'] for p in plan['items'] if p.get('existing_path')}
        with closing(self.catalog.connect()) as db, db:
            db.execute('INSERT INTO imports VALUES(?,?,?,?,?,?,?,?)',
                (identifier, now(), None, plan['source_root'], plan['target_root'], mode, json.dumps(options, ensure_ascii=False), 'pending'))
            for p in plan['items']:
                db.execute('''INSERT INTO import_items(id, import_id, media_id, source_path, source_name,
                    target_path, kind, size, mtime_ns, capture_time, place, status)
                    VALUES(:id,:import_id,:media_id,:source_path,:source_name,:target_path,:kind,
                    :size,:mtime_ns,:capture_time,:place,'pending')''', dict(p, id=uuid.uuid4().hex, import_id=identifier))
        return identifier

    def _update(self, item_id, **values):
        with closing(self.catalog.connect()) as db, db:
            db.execute('UPDATE import_items SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE id=?', [*values.values(), item_id])

    def items(self, import_id):
        with closing(self.catalog.connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM import_items WHERE import_id=? ORDER BY rowid', (import_id,))]

    def history(self, limit=200, offset=0):
        with closing(self.catalog.connect()) as db:
            return [dict(r) for r in db.execute('''SELECT b.*,
                count(i.id) total, coalesce(sum(i.kind='photo'),0) photos,
                coalesce(sum(i.kind='video'),0) videos,
                coalesce(sum(i.size),0) bytes,
                coalesce(sum(i.status='complete'),0) complete,
                coalesce(sum(i.status='skipped'),0) skipped,
                coalesce(sum(i.status IN ('failed','source_retained')),0) errors,
                coalesce(sum(i.status='complete' AND i.kind='photo'),0) completed_photos,
                coalesce(sum(i.status='complete' AND i.kind='video'),0) completed_videos
                FROM imports b LEFT JOIN import_items i ON i.import_id=b.id
                GROUP BY b.id ORDER BY b.created_at DESC, b.rowid DESC LIMIT ? OFFSET ?''', (limit, offset))]

    def _duplicate(self, item, batch, fingerprint, cancel):
        options = json.loads(batch['options_json'])
        candidates = []
        with closing(self.catalog.connect()) as db:
            candidates = [r['current_path'] for r in db.execute(
                'SELECT current_path FROM media WHERE content_hash=? AND size=?', (fingerprint, item['size']))]
        existing = options.get('existing_paths', {}).get(item['media_id'])
        if existing:
            candidates.append(existing)
        for path in dict.fromkeys(candidates):
            candidate = Path(path)
            try:
                beneath(candidate, batch['target_root'])
                if candidate.is_file() and candidate.stat().st_size == item['size'] and digest(candidate, cancel) == fingerprint:
                    return candidate
            except (OSError, ValueError):
                continue
        return None

    def _record_destination(self, item, batch, final, fingerprint):
        with closing(self.catalog.connect()) as db, db:
            source = db.execute('SELECT * FROM media WHERE id=?', (item['media_id'],)).fetchone()
            existing = db.execute('SELECT id FROM media WHERE path_key=?', (path_key(final),)).fetchone()
            media_id = existing['id'] if existing else source['id']
            if media_id!=source['id']:
                destination=db.execute('SELECT activity_id,media_note FROM media WHERE id=?',(media_id,)).fetchone()
                for field in ('activity_id','media_note'):
                    if destination[field] and source[field] and destination[field]!=source[field]:
                        raise ValueError('目标已有不同的活动或备注，请核对后重试；源文件保留。')
                db.execute('UPDATE media SET activity_id=?,media_note=? WHERE id=?',
                    (source['activity_id'] or destination['activity_id'],source['media_note'] or destination['media_note'],media_id))
            db.execute('''UPDATE media SET path_key=?, current_path=?, observed_name=?,
                original_name=COALESCE(original_name,?), place=?, content_hash=?, imported_at=?,
                availability='present', last_seen_at=? WHERE id=?''',
                (path_key(final), str(final), final.name, item['source_name'], item['place'], fingerprint, now(), now(), media_id))
            db.execute('''INSERT OR IGNORE INTO name_history(media_id, import_id, old_name, old_path,
                new_name, new_path, created_at) VALUES(?,?,?,?,?,?,?)''',
                (media_id, item['import_id'], item['source_name'], item['source_path'], final.name, str(final), now()))
            db.execute('UPDATE import_items SET media_id=?, target_path=? WHERE id=?', (media_id, str(final), item['id']))

    def _finish_move(self, item, batch, final, fingerprint, cancel):
        self._record_destination(item, batch, final, fingerprint)
        source = beneath(item['source_path'], batch['source_root'])
        if batch['mode'] == 'move' and source.exists():
            if cancel.is_set():
                raise Cancelled()
            # A successful destination readback is required on each recovery run.
            info = source.stat()
            if (info.st_size, info.st_mtime_ns) != (item['size'], item['mtime_ns']) or digest(source, cancel) != fingerprint:
                raise ValueError('源文件已变化，已保留源文件和目标副本。')
            try:
                source.unlink()
            except OSError as exc:
                self._update(item['id'], status='source_retained', error=f'目标已校验，源文件未删除  {exc}')
                return
        self._update(item['id'], status='complete', error=None, partial_path=None, completed_at=now())

    def _process(self, item, batch, cancel):
        telemetry.journal('import_item', item['id'])
        telemetry.phase('核对原片')
        source = beneath(item['source_path'], batch['source_root'])
        final = beneath(item['target_path'], batch['target_root'])
        # Recover publication/delete gaps using the durable hash, never by name alone.
        if item['content_hash'] and final.is_file() and digest(final, cancel) == item['content_hash']:
            self._finish_move(item, batch, final, item['content_hash'], cancel)
            return
        if item['status'] in ('published', 'source_retained'):
            raise ValueError('已经发布的目标文件发生变化，已停止自动处理。')
        info = source.stat()
        if (info.st_size, info.st_mtime_ns) != (item['size'], item['mtime_ns']):
            raise ValueError('源文件已变化，请重新扫描并预览。')
        fingerprint = digest(source, cancel)
        duplicate = self._duplicate(item, batch, fingerprint, cancel)
        if duplicate:
            self._record_destination(item, batch, duplicate, fingerprint)
            self._update(item['id'], status='skipped', content_hash=fingerprint,
                         target_path=str(duplicate), error='内容重复，源文件保留', completed_at=now())
            return
        if self.thumbnails:
            with closing(self.catalog.connect()) as db:
                row = dict(db.execute('SELECT * FROM media WHERE id=?', (item['media_id'],)).fetchone())
            try:
                self.thumbnails.ensure(row, cancel)
            except Exception:
                pass  # Thumbnail failure must not block a verified file transfer.
        final.parent.mkdir(parents=True, exist_ok=True)
        beneath(final.parent / 'check', batch['target_root'])
        partial = final.parent / ('.mediaorganizer-' + item['id'] + '.part')
        if partial.exists():
            if item['partial_path'] != str(partial):
                raise ValueError('发现未归属本记录的临时文件，已保留并停止。')
            partial.unlink()  # Only the exact temporary file recorded by this item.
        self._update(item['id'], status='copying', content_hash=fingerprint,
                     partial_path=str(partial), error=None)
        copy_hash = hashlib.sha256()
        telemetry.phase('复制素材')
        copied = 0
        with source.open('rb') as reader, partial.open('xb') as writer:
            while data := reader.read(CHUNK):
                if cancel.is_set():
                    raise Cancelled()
                writer.write(data)
                copy_hash.update(data)
                copied += len(data)
                telemetry.progress(copied, info.st_size)
            writer.flush()
            os.fsync(writer.fileno())
        telemetry.phase('校验副本')
        if copy_hash.hexdigest() != fingerprint or digest(partial, cancel) != fingerprint:
            raise ValueError('复制校验不一致，源文件已保留。')
        shutil.copystat(source, partial)
        telemetry.phase('保存与归档')
        while True:
            if cancel.is_set():
                raise Cancelled()
            # Recheck collision after preview. Windows rename never overwrites.
            if final.exists():
                final = unused_path(final)
            self._update(item['id'], target_path=str(final))
            try:
                publish_without_replace(partial, final)
                break
            except FileExistsError:
                continue
        self._update(item['id'], status='published', partial_path=None)
        self._finish_move(item, batch, final, fingerprint, cancel)

    def run(self, import_id, cancel=None, progress=None, pause=None, item_ids=None):
        cancel = cancel or threading.Event()
        with closing(self.catalog.connect()) as db, db:
            batch = dict(db.execute('SELECT * FROM imports WHERE id=?', (import_id,)).fetchone())
            db.execute("UPDATE imports SET status='running', finished_at=NULL WHERE id=?", (import_id,))
        items = self.items(import_id)
        processed = sum(item['status'] in TERMINAL for item in items)
        for item in items:
            if item_ids is not None and item['id'] not in item_ids:
                continue
            if item['status'] in TERMINAL:
                continue
            if cancel.is_set():
                break
            while pause is not None and not pause.wait(0.1):
                if cancel.is_set():
                    break
            if cancel.is_set():
                break
            if progress:
                progress(dict(item_id=item['id'], import_id=import_id, filename=item['source_name'], path=item['source_path'], phase='processing', processed=processed, total=len(items), index=processed+1))
            try:
                self._process(item, batch, cancel)
            except Cancelled:
                self._update(item['id'], error='已停止，可继续任务')
                break
            except Exception as exc:
                self._update(item['id'], status='failed', error=f'{type(exc).__name__}  {exc}')
            processed += 1
            if progress:
                progress(dict(item_id=item['id'], import_id=import_id, filename=item['source_name'], path=item['source_path'], phase='finished', processed=processed, total=len(items), index=processed))
        items = self.items(import_id)
        status = 'stopped' if cancel.is_set() else ('completed_with_errors' if any(i['status'] not in TERMINAL for i in items) else 'completed')
        with closing(self.catalog.connect()) as db, db:
            db.execute('UPDATE imports SET status=?, finished_at=? WHERE id=?', (status, now(), import_id))
        return dict(import_id=import_id, status=status, complete=sum(i['status'] == 'complete' for i in items),
                    skipped=sum(i['status'] == 'skipped' for i in items), errors=sum(i['status'] not in TERMINAL for i in items))
