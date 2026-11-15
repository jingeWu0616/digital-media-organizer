"""Local, read-only inventory of an explicitly selected media directory.

This module never renames, moves or writes media. Historical inventory batches
are separate from imports; an observed filename is not an original filename.
"""
from __future__ import annotations

import ctypes
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import closing, nullcontext
from datetime import datetime
from pathlib import Path
from source_scope import roots, encode

PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.raw', '.arw',
              '.bmp', '.gif', '.tif', '.tiff', '.cr2', '.cr3', '.nef',
              '.dng', '.orf', '.rw2', '.raf', '.webp', '.avif'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.lrf', '.osv',
              '.m4v', '.mts', '.m2ts', '.360'}
SCHEMA_VERSION = 7


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def path_key(path):
    return os.path.normcase(os.path.abspath(path))


def default_database():
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share'))
    return base / 'MediaOrganizer' / 'catalog.sqlite3'


def require_local_database(path):
    path = Path(path).resolve()
    if str(path).startswith(('\\\\', '//')):
        raise ValueError('索引数据库必须保存在电脑本地硬盘。')
    if os.name == 'nt':
        get_type = ctypes.windll.kernel32.GetDriveTypeW
        get_type.argtypes = [ctypes.c_wchar_p]
        get_type.restype = ctypes.c_uint
        if get_type(path.anchor) == 4:  # DRIVE_REMOTE, including mapped NAS drives
            raise ValueError('索引数据库不能放在网络映射盘上。')
    return path


def guessed_original(name):
    """Only suggest recognizable camera names after an old organizer prefix."""
    if not re.match(r'^\d{4}-\d{2}-\d{2}_(Mon|Tue|Wed|Thu|Fri|Sat|Sun)_', name):
        return None
    stem = Path(name).stem
    match = re.search(r'(?:^|_)((?:DSC_?|DSCF|IMG_|MVI_|GOPR|GX\d{2}|GH\d{2}|DJI_|PXL_)\d[\w.-]*)$', stem, re.I)
    return match.group(1) + Path(name).suffix if match else None


def date_value(value):
    if not value:
        return None
    text = str(value).strip()
    if text.startswith('UTC ') or text.endswith(' UTC'):
        text = text.replace('UTC', '').strip() + '+00:00'
    for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt).isoformat(sep=' ')
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).isoformat(sep=' ')
    except ValueError:
        return None


def coordinate(dms, ref):
    if isinstance(ref, bytes):
        ref = ref.decode('ascii')
    result = float(dms[0]) + float(dms[1]) / 60 + float(dms[2]) / 3600
    if str(ref).upper() in ('S', 'W'):
        result = -result
    return result


def read_metadata(path, kind):
    from processing_markers import COLOR, WATERMARK, marked
    result = dict(capture_time=None, date_source='unknown', device=None,
                  latitude=None, longitude=None, gps_status='unknown',
                  duration_seconds=None, metadata_error=None, rating=0, color_revision='',watermark_revision='')
    errors = []
    if kind == 'photo':
        try:
            from PIL import Image
            if path.suffix.lower() in {'.heic', '.heif'}:
                from pillow_heif import register_heif_opener
                register_heif_opener()
            with Image.open(path) as image:
                exif = image.getexif()
                if marked(exif.get(270),WATERMARK):result['watermark_revision']='portable-v1'
                details = exif.get_ifd(34665) if exif else {}
                result['capture_time'] = date_value(details.get(36867) or exif.get(36867))
                result['device'] = str(exif.get(272) or '').strip() or None
                for values in (exif, details):
                    rating = values.get(18246)
                    percent = values.get(18249)
                    try:
                        if rating and 1 <= int(rating) <= 5:
                            result['rating'] = int(rating)
                        elif percent:
                            result['rating'] = sum(int(percent) >= n for n in (1, 25, 50, 75, 99))
                    except (TypeError, ValueError):
                        pass
                gps = exif.get_ifd(34853) if exif else {}
                if gps:
                    lat = coordinate(gps[2], gps[1])
                    lon = coordinate(gps[4], gps[3])
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise ValueError('GPS 坐标超出范围')
                    result.update(latitude=lat, longitude=lon, gps_status='present')
                else:
                    result['gps_status'] = 'missing'
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {exc}')
    else:
        # MediaInfo reads metadata only. Failure does not imply missing GPS.
        try:
            from pymediainfo import MediaInfo
            info = MediaInfo.parse(str(path))
            if not info.video_tracks:
                raise ValueError('未识别到视频轨道')
            for track in info.tracks:
                values = track.to_data()
                if track.track_type == 'General':
                    if marked(values.get('comment'),COLOR):result['color_revision']='portable-v1'
                    result['capture_time'] = date_value(values.get('recorded_date') or values.get('encoded_date'))
                    result['device'] = values.get('comapplequicktimemodel') or values.get('model')
                    if values.get('duration') is not None:
                        result['duration_seconds'] = float(values['duration']) / 1000
                    for key, value in values.items():
                        if 'location' in key.lower():
                            m = re.match(r'^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:[+-].*)?/?$', str(value))
                            if m:
                                lat, lon = map(float, m.groups())
                                if -90 <= lat <= 90 and -180 <= lon <= 180:
                                    result.update(latitude=lat, longitude=lon, gps_status='present')
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {exc}')
    if not result['rating']:
        try:
            with path.open('rb') as stream:
                data = stream.read(65536)
                if path.stat().st_size > 65536:
                    stream.seek(max(0, path.stat().st_size - 65536))
                    data += stream.read(65536)
            match = re.search(rb'xmp:Rating(?:\s*=\s*[\"\x27]|\s*>\s*)([1-5])(?:[\"\x27<\s])', data)
            if match:
                result['rating'] = int(match.group(1))
        except OSError:
            pass
    if result['capture_time']:
        result['date_source'] = 'metadata'
    else:
        prefix = re.match(r'^(\d{4}-\d{2}-\d{2})_', path.name)
        if prefix and date_value(prefix.group(1)):
            result['capture_time'] = prefix.group(1)
            result['date_source'] = 'filename_inferred'
    result['metadata_error'] = '\n'.join(errors) or None
    return result


class Catalog:
    def __init__(self, database=None):
        self.database = require_local_database(database or default_database())

    def connect(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def initialize(self):
        with closing(self.connect()) as db, db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6, SCHEMA_VERSION):
                raise ValueError('索引版本不兼容，请使用创建该索引的软件版本。')
            if version in (1, 2, 3, 4, 5, 6):
                backup = self.database.with_name(self.database.name + '.pre-v7-' + uuid.uuid4().hex + '.backup')
                with closing(sqlite3.connect(backup)) as destination:
                    db.backup(destination)
                    if destination.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                        raise ValueError('升级前数据库备份校验失败，停止升级。')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'historical_index',
                    root TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                    status TEXT NOT NULL, photos INTEGER NOT NULL DEFAULT 0,
                    videos INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
                    unchanged INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
                    ignored INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS media (
                    id TEXT PRIMARY KEY, path_key TEXT NOT NULL UNIQUE,
                    current_path TEXT NOT NULL, observed_name TEXT NOT NULL,
                    original_name TEXT, inferred_original TEXT, kind TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                    capture_time TEXT, date_source TEXT NOT NULL,
                    device TEXT, latitude REAL, longitude REAL, gps_status TEXT NOT NULL,
                    duration_seconds REAL, metadata_error TEXT,
                    first_indexed_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    availability TEXT NOT NULL DEFAULT 'present'
                );
                CREATE INDEX IF NOT EXISTS idx_media_capture ON media(capture_time);
                CREATE INDEX IF NOT EXISTS idx_media_kind_gps ON media(kind, gps_status);
                CREATE INDEX IF NOT EXISTS idx_media_name ON media(observed_name);
                CREATE TABLE IF NOT EXISTS batch_items (
                    batch_id TEXT REFERENCES batches(id), media_id TEXT REFERENCES media(id),
                    observed_path TEXT NOT NULL, observed_name TEXT NOT NULL,
                    result TEXT NOT NULL, PRIMARY KEY(batch_id, media_id)
                );
                CREATE TABLE IF NOT EXISTS scan_errors (
                    batch_id TEXT REFERENCES batches(id), path TEXT, message TEXT
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(media)')}
            for name, kind in {'rating': 'INTEGER NOT NULL DEFAULT 0', 'place': 'TEXT',
                               'content_hash': 'TEXT', 'imported_at': 'TEXT',
                               'color_revision': "TEXT NOT NULL DEFAULT ''",
                               'watermark_revision': "TEXT NOT NULL DEFAULT ''",
                               'activity_id': 'TEXT REFERENCES activities(id)', 'media_note': "TEXT NOT NULL DEFAULT ''"}.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE media ADD COLUMN {name} {kind}')
            db.executescript('''
                CREATE INDEX IF NOT EXISTS idx_media_hash ON media(content_hash, size);
                CREATE TABLE IF NOT EXISTS imports (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, finished_at TEXT,
                    source_root TEXT NOT NULL, target_root TEXT NOT NULL, mode TEXT NOT NULL,
                    options_json TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS import_items (
                    id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES imports(id),
                    media_id TEXT NOT NULL REFERENCES media(id),
                    source_path TEXT NOT NULL, source_name TEXT NOT NULL,
                    target_path TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, capture_time TEXT, place TEXT,
                    status TEXT NOT NULL, content_hash TEXT, partial_path TEXT, error TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_import_items_status ON import_items(import_id, status);
                CREATE INDEX IF NOT EXISTS idx_import_items_source ON import_items(source_path COLLATE NOCASE, status);
                CREATE TABLE IF NOT EXISTS name_history (
                    id INTEGER PRIMARY KEY, media_id TEXT NOT NULL REFERENCES media(id),
                    import_id TEXT NOT NULL REFERENCES imports(id),
                    old_name TEXT NOT NULL, old_path TEXT NOT NULL, new_name TEXT NOT NULL,
                    new_path TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(media_id, import_id, old_path)
                );
                CREATE INDEX IF NOT EXISTS idx_name_history_old ON name_history(old_name);
                CREATE TABLE IF NOT EXISTS color_jobs (
                    id TEXT PRIMARY KEY, media_id TEXT NOT NULL REFERENCES media(id),
                    source_path TEXT NOT NULL, source_name TEXT NOT NULL,
                    created_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL,
                    input_hash TEXT, output_hash TEXT, lut_hash TEXT NOT NULL,
                    encoder TEXT, temp_path TEXT NOT NULL, source_size INTEGER,
                    source_mtime_ns INTEGER, source_atime_ns INTEGER, profile_json TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_color_output ON color_jobs(output_hash, status);
                CREATE INDEX IF NOT EXISTS idx_color_media ON color_jobs(media_id, created_at);
                CREATE TABLE IF NOT EXISTS gps_jobs (
                    id TEXT PRIMARY KEY, media_id TEXT NOT NULL REFERENCES media(id),
                    source_path TEXT NOT NULL, source_name TEXT NOT NULL,
                    created_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL,
                    latitude REAL NOT NULL, longitude REAL NOT NULL, location_name TEXT,
                    coordinate_source TEXT NOT NULL, old_gps_json TEXT,
                    input_hash TEXT, output_hash TEXT, temp_path TEXT NOT NULL, error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_gps_media ON gps_jobs(media_id, status);
                CREATE TABLE IF NOT EXISTS watermark_jobs (
                    id TEXT PRIMARY KEY, media_id TEXT NOT NULL REFERENCES media(id),
                    output_media_id TEXT NOT NULL, source_path TEXT NOT NULL, source_name TEXT NOT NULL,
                    output_path TEXT NOT NULL, target_root TEXT NOT NULL, temp_path TEXT NOT NULL,
                    created_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL,
                    input_hash TEXT NOT NULL, output_hash TEXT, settings_hash TEXT NOT NULL,
                    settings_json TEXT NOT NULL, rendered_text TEXT NOT NULL, error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_watermark_repeat ON watermark_jobs(input_hash,settings_hash,target_root,status);
                CREATE INDEX IF NOT EXISTS idx_watermark_output ON watermark_jobs(output_hash,status);
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(color_jobs)')}
            for name, kind in {'output_mode': "TEXT NOT NULL DEFAULT 'replace'",
                               'output_path': 'TEXT', 'output_media_id': 'TEXT'}.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE color_jobs ADD COLUMN {name} {kind}')
            db.execute('CREATE INDEX IF NOT EXISTS idx_color_input ON color_jobs(input_hash,output_mode,status)')
            db.executescript("""
                CREATE TABLE IF NOT EXISTS activities (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, companions TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_media_activity ON media(activity_id);
                CREATE TABLE IF NOT EXISTS activity_changes (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, subject TEXT NOT NULL,
                    details_json TEXT NOT NULL, media_ids_json TEXT NOT NULL
                );
            """)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS task_actions (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, action TEXT NOT NULL,
                    media_id TEXT NOT NULL, source_path TEXT NOT NULL, source_name TEXT NOT NULL,
                    kind TEXT NOT NULL, snapshot_json TEXT NOT NULL, options_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                    status TEXT NOT NULL, elapsed REAL NOT NULL DEFAULT 0,
                    wait_seconds REAL NOT NULL DEFAULT 0, pause_seconds REAL NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT '', phases_json TEXT NOT NULL DEFAULT '[]',
                    journal_type TEXT, journal_id TEXT, error TEXT NOT NULL DEFAULT '',
                    retry_of TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_task_journal ON task_actions(journal_type,journal_id);
                CREATE INDEX IF NOT EXISTS idx_task_batch ON task_actions(batch_id,status);
            ''')
            db.execute('PRAGMA user_version=7')

    def scan(self, root, cancel=None, progress=None, retry_metadata=False, batch_kind='historical_index', recursive=True, extensions=None, file_guard=None):
        # Resolve for dedup/security, but retain the caller's established path
        # spelling for catalog identity (mapped drives and Windows short paths).
        selected_roots = roots(root, recursive=recursive, strict=True, preserve_paths=True)
        root = selected_roots[0]
        extensions = PHOTO_EXTS | VIDEO_EXTS if extensions is None else {str(e).lower() for e in extensions}
        if not extensions or not extensions <= PHOTO_EXTS | VIDEO_EXTS:
            raise ValueError('请至少选择一种支持的素材格式。')
        # Refuse any scan that would create a database in the selected source.
        if any(self.database.resolve().is_relative_to(parent.resolve()) for parent in selected_roots):
            raise ValueError('素材目录不能包含索引数据库，请选择更具体的素材文件夹。')
        cancel = cancel or threading.Event()
        self.initialize()
        batch_id = uuid.uuid4().hex
        stats = dict(photos=0, videos=0, bytes=0, unchanged=0, errors=0, ignored=0)
        started = now()
        enumeration_failed = False
        last_progress = 0
        current_path = str(root)
        with closing(self.connect()) as db:
            if batch_kind not in ('historical_index', 'source_scan'):
                raise ValueError('无效的扫描类型')
            db.execute('INSERT INTO batches(id, root, started_at, status, kind) VALUES(?,?,?,?,?)',
                       (batch_id, encode(selected_roots), started, 'running', batch_kind))
            db.commit()

            def save_progress(status='running'):
                nonlocal last_progress
                last_progress = time.monotonic()
                db.execute('''UPDATE batches SET photos=:photos, videos=:videos,
                    bytes=:bytes, unchanged=:unchanged, errors=:errors, ignored=:ignored,
                    status=:status, finished_at=:finished WHERE id=:id''',
                    dict(stats, status=status, finished=None if status == 'running' else now(), id=batch_id))
                db.commit()
                if progress:
                    progress(dict(stats, batch_id=batch_id, status=status, current_path=current_path))

            def log_error(path, message):
                stats['errors'] += 1
                db.execute('INSERT INTO scan_errors VALUES(?,?,?)', (batch_id, str(path), message))
                # Never retain a write transaction while the next NAS read blocks.
                db.commit()

            def walk_error(exc):
                nonlocal enumeration_failed
                enumeration_failed = True
                log_error(exc.filename or root, str(exc))

            try:
                # Stream directory entries; don't construct a full NAS file list.
                def walk_roots():
                    for parent in selected_roots:
                        if cancel.is_set():
                            break
                        yield from os.walk(parent, onerror=walk_error, followlinks=False)
                for directory, dirs, files in walk_roots():
                    current_path = directory
                    if time.monotonic() - last_progress >= .25:
                        save_progress()
                    if cancel.is_set():
                        break
                    if not recursive:
                        dirs[:] = []
                    safe_dirs = []
                    for name in dirs:
                        entry = Path(directory) / name
                        try:
                            if entry.is_symlink() or entry.is_junction():
                                stats['ignored'] += 1
                            else:
                                safe_dirs.append(name)
                        except OSError as exc:
                            walk_error(exc)
                    dirs[:] = safe_dirs
                    for name in files:
                        if cancel.is_set():
                            break
                        if name.startswith('.mediaorganizer-'):
                            stats['ignored'] += 1
                            continue
                        path = Path(directory) / name
                        ext = path.suffix.lower()
                        if ext not in extensions:
                            stats['ignored'] += 1
                            continue
                        try:
                            with file_guard(path) if file_guard else nullcontext(True) as readable:
                                if not readable:
                                    raise OSError("素材正在处理，请稍后重新扫描该文件。")
                                current_path = str(path)
                                if time.monotonic() - last_progress >= .25:
                                    save_progress()
                                info = path.lstat()
                                if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                                    stats['ignored'] += 1
                                    continue
                                kind = 'photo' if ext in PHOTO_EXTS else 'video'
                                stats['photos' if kind == 'photo' else 'videos'] += 1
                                stats['bytes'] += info.st_size
                                old = db.execute('SELECT * FROM media WHERE path_key=?', (path_key(path),)).fetchone()
                                unchanged = old and old['size'] == info.st_size and old['mtime_ns'] == info.st_mtime_ns
                                if unchanged and not retry_metadata:
                                    metadata = {key: old[key] for key in (
                                        'capture_time', 'date_source', 'device', 'latitude', 'longitude',
                                        'gps_status', 'duration_seconds', 'metadata_error', 'rating','color_revision','watermark_revision')}
                                    stats['unchanged'] += 1
                                else:
                                    metadata = read_metadata(path, kind)
                                after = path.stat()
                                if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                                    raise OSError('读取期间文件发生变化，请稍后重新扫描。')
                                timestamp = now()
                                item = dict(metadata, id=old['id'] if old else uuid.uuid4().hex,
                                    path_key=path_key(path), current_path=str(path), observed_name=name,
                                    original_name=old['original_name'] if old else None,
                                    inferred_original=guessed_original(name), kind=kind, size=info.st_size,
                                    mtime_ns=info.st_mtime_ns, first_indexed_at=old['first_indexed_at'] if old else timestamp,
                                    last_seen_at=timestamp, availability='present')
                                columns = list(item)
                                updates = ','.join(f'{c}=excluded.{c}' for c in columns if c not in ('id', 'path_key', 'original_name', 'first_indexed_at'))
                                db.execute(f'INSERT INTO media ({",".join(columns)}) VALUES ({",".join(":" + c for c in columns)}) ON CONFLICT(path_key) DO UPDATE SET {updates}', item)
                                if old and not unchanged:
                                    db.execute("UPDATE media SET content_hash=NULL WHERE id=?", (item['id'],))
                                result = 'unchanged' if unchanged and not retry_metadata else 'indexed'
                                if metadata['metadata_error']:
                                    result = 'metadata_error'
                                    log_error(path, metadata['metadata_error'])
                                db.execute('INSERT INTO batch_items VALUES(?,?,?,?,?)',
                                           (batch_id, item['id'], str(path), name, result))
                                if file_guard: db.commit()
                        except OSError as exc:
                            enumeration_failed = True
                            log_error(path, str(exc))
                        if (stats['photos'] + stats['videos']) % 25 == 0:
                            save_progress()
                if cancel.is_set():
                    status = 'cancelled'
                else:
                    status = 'completed_with_errors' if stats['errors'] else 'completed'
                    if not enumeration_failed:
                        for parent in selected_roots:
                            prefix = path_key(parent).rstrip(os.sep) + os.sep
                            scope = '' if recursive else ' AND instr(substr(path_key, ?), ?) = 0'
                            scope_args = () if recursive else (len(prefix) + 1, os.sep)
                            ext_sql = ' AND (' + ' OR '.join('lower(substr(current_path, -?))=?' for _ in extensions) + ')'
                            ext_args = [v for ext in sorted(extensions) for v in (len(ext), ext)]
                            db.execute('''UPDATE media SET availability='not_seen'
                                WHERE substr(path_key, 1, ?) = ? AND id NOT IN
                                (SELECT media_id FROM batch_items WHERE batch_id=?)''' + scope + ext_sql,
                                (len(prefix), prefix, batch_id, *scope_args, *ext_args))
                save_progress(status)
            except BaseException:
                save_progress('failed')
                raise
        return dict(stats, batch_id=batch_id, status=status)

    def search(self, text='', kind='', gps='', batch_id='', offset=0, limit=200, import_id='', imported_only=False, activity_id=''):
        conditions, args = [], []
        if text:
            escaped = text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            conditions.append('(' + ' OR '.join(f"COALESCE(m.{c}, '') LIKE ? ESCAPE '\\'" for c in
                ('observed_name', 'original_name', 'inferred_original', 'current_path', 'device', 'capture_time', 'place', 'media_note')) +
                " OR EXISTS(SELECT 1 FROM activities a WHERE a.id=m.activity_id AND (a.name LIKE ? ESCAPE '\\' OR a.companions LIKE ? ESCAPE '\\' OR a.note LIKE ? ESCAPE '\\'))"
                " OR EXISTS(SELECT 1 FROM name_history h WHERE h.media_id=m.id AND (h.old_name LIKE ? ESCAPE '\\' OR h.old_path LIKE ? ESCAPE '\\')))" )
            args.extend(['%' + escaped + '%'] * 13)
        if activity_id:
            conditions.append('m.activity_id=?'); args.append(activity_id)
        if kind:
            conditions.append('m.kind=?')
            args.append(kind)
        if gps:
            conditions.append('m.gps_status=?')
            args.append(gps)
        if batch_id:
            conditions.append('EXISTS(SELECT 1 FROM batch_items b WHERE b.media_id=m.id AND b.batch_id=?)')
            args.append(batch_id)
        if import_id:
            completed = " AND i.status='complete'" if imported_only else ''
            conditions.append('EXISTS(SELECT 1 FROM import_items i WHERE i.media_id=m.id AND i.import_id=?'+completed+')')
            args.append(import_id)
        if imported_only:
            conditions.append('m.imported_at IS NOT NULL')
            if not import_id:
                conditions.append("EXISTS(SELECT 1 FROM import_items done WHERE done.media_id=m.id AND done.status='complete')")
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with closing(self.connect()) as db:
            total = db.execute('SELECT count(*) FROM media m' + where, args).fetchone()[0]
            state = '''CASE WHEN m.imported_at IS NOT NULL THEN '已导入'
                WHEN EXISTS(SELECT 1 FROM import_items i WHERE i.source_path=m.current_path COLLATE NOCASE AND i.status='failed') THEN '导入失败'
                WHEN EXISTS(SELECT 1 FROM import_items i WHERE i.source_path=m.current_path COLLATE NOCASE AND i.status='skipped') THEN '重复已跳过'
                WHEN EXISTS(SELECT 1 FROM batch_items b JOIN batches s ON s.id=b.batch_id
                    WHERE b.media_id=m.id AND s.kind='source_scan') THEN '待整理'
                ELSE '历史索引' END AS import_state'''
            rows = db.execute('''SELECT m.*, (SELECT name FROM activities WHERE id=m.activity_id) activity_name, (SELECT companions FROM activities WHERE id=m.activity_id) activity_companions, (SELECT note FROM activities WHERE id=m.activity_id) activity_note, ''' + state + ' FROM media m' + where + ' ORDER BY m.current_path LIMIT ? OFFSET ?',
                              [*args, limit, offset]).fetchall()
        return total, [dict(r) for r in rows]

    def batches(self):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM batches ORDER BY started_at DESC, rowid DESC LIMIT 200')]

    def errors(self, batch_id):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute('SELECT path, message FROM scan_errors WHERE batch_id=? LIMIT 200', (batch_id,))]
