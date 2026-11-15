"""On-demand Amap lookup and verified, journaled EXIF GPS writes (no image encoding)."""
from __future__ import annotations

import base64
import ctypes
import json
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from geo_coordinates import gcj2wgs_exact, outOfChina
from import_service import Cancelled, digest
from media_catalog import now
from fast_video_gps import Unsupported as FastUnsupported

EXIFTOOL = Path(__file__).resolve().parent / 'tools/exiftool/exiftool.exe'
VIDEO_GPS_EXTENSIONS = {'.mp4', '.mov'}
GPS_EXTENSIONS = {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.tif', '.tiff'}


def coordinates(latitude, longitude, system='WGS84'):
    lat, lon = float(latitude), float(longitude)
    if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('经纬度超出范围。')
    if system not in ('WGS84', 'GCJ02'):
        raise ValueError('未知坐标系。')
    if system == 'GCJ02' and not outOfChina(lat, lon):
        lat, lon = gcj2wgs_exact(lat, lon)
    return round(lat, 7), round(lon, 7)


def protect_key(value, decrypt=False):
    """Windows user-bound DPAPI. Never fall back to plaintext storage."""
    if not value:
        return ''
    if os.name != 'nt':
        raise ValueError('此系统仅支持本次使用 Key。')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value) if decrypt else value.encode('utf-8')
    buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    incoming, outgoing = Blob(len(raw), buf), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ValueError('无法读取或保存此 Windows 用户的高德 Key，请重新填写。')
    try:
        data = ctypes.string_at(outgoing.data, outgoing.size)
        return data.decode('utf-8') if decrypt else base64.b64encode(data).decode('ascii')
    finally:
        kernel.LocalFree(outgoing.data)


def search_places(key, keywords, city=''):
    if not key.strip() or not keywords.strip():
        raise ValueError('请填写高德 Web 服务 Key 和地点。')
    params = dict(key=key.strip(), keywords=keywords.strip(), city=city.strip(),
                  extensions='base', offset=20, page=1, output='JSON')
    # No photo, path, or EXIF is sent to the provider. Never expose URLs containing Key.
    try:
        with urlopen('https://restapi.amap.com/v3/place/text?' + urlencode(params), timeout=12) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError()
        data = json.loads(raw)
    except Exception:
        raise ValueError('高德请求失败，请检查网络后重试。') from None
    if data.get('status') != '1':
        codes = {'10001': 'Key 无效', '10003': '调用额度不足', '10009': 'Key 平台不匹配，请使用 Web 服务 Key',
                 '10004': '调用过于频繁', '10012': '权限不足'}
        code = str(data.get('infocode', '未知'))
        raise ValueError('高德：' + codes.get(code, '请求未获允许') + f'（{code if code.isdigit() else "未知"}）')
    result = []
    for item in data.get('pois', []):
        try:
            lon, lat = map(float, item['location'].split(','))
            coordinates(lat, lon, 'GCJ02')
        except (KeyError, ValueError, TypeError):
            continue
        def text(key):
            return item.get(key) if isinstance(item.get(key), str) else ''
        result.append(dict(name=text('name'), address=' '.join(filter(None,
            [text('pname'), text('cityname'), text('adname'), text('address')])), latitude=lat, longitude=lon))
    return result


def exiftool(args, cancel=None, timeout=120, input_data=None):
    if not EXIFTOOL.is_file():
        raise ValueError('ExifTool 缺失，请恢复 tools/exiftool 文件夹。')
    cancel = cancel or threading.Event()
    # UTF-8 argument file avoids the Windows ANSI command-line filename conversion.
    if any('\n' in str(a) or '\r' in str(a) for a in args):
        raise ValueError('文件名含不支持的换行字符。')
    payload = ('\n'.join(['-charset', 'filename=UTF8', *map(str, args)]) + '\n').encode('utf-8')
    command = [str(EXIFTOOL), '-config', '', '-@', '-']
    if input_data is not None:
        # The fast writer passes a small metadata-only MP4 through stdin, so
        # ExifTool never needs to reopen the exclusively locked original.
        command = [str(EXIFTOOL), '-config', '', *args]
        payload = input_data
    process = subprocess.Popen(command, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    start = time.monotonic()
    try:
        first = True
        while True:
            if cancel.is_set():
                raise Cancelled()
            if time.monotonic() - start > timeout:
                raise TimeoutError('元数据读取超时。' if input_data is not None else '媒体元数据处理超时，原文件保留。')
            try:
                out, err = process.communicate(payload if first else None, timeout=.2)
                if process.returncode:
                    raise ValueError(err.decode('utf-8', errors='replace')[-1500:] or 'ExifTool 处理失败')
                return out
            except subprocess.TimeoutExpired:
                first = False
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def photo_metadata(path, cancel=None):
    values = json.loads(exiftool(['-j', '-G1', '-n', '-EXIF:all', '-XMP:all', '-IPTC:all',
        '-MakerNotes:all', '-ICC_Profile:all', '-FileType', '-ImageDataHash',
        '-api', 'ImageHashType=SHA256', str(path)], cancel))[0]
    if any(k.endswith(':Error') for k in values) or not values.get('File:ImageDataHash'):
        raise ValueError('无法验证照片图像数据，原文件保留。')
    return values


def write_photo_verified(path,temp,lat,lon,before,cancel):
    # ExifTool -o creates a new file; it never writes the original here.
    exiftool(['-o', str(temp), '-GPS:all=', '-XMP:GPS*=',
        f'-EXIF:GPSLatitude={abs(lat):.7f}', f'-EXIF:GPSLongitude={abs(lon):.7f}',
        '-EXIF:GPSLatitudeRef=' + ('N' if lat >= 0 else 'S'),
        '-EXIF:GPSLongitudeRef=' + ('E' if lon >= 0 else 'W'),
        '-EXIF:GPSMapDatum=WGS-84', str(path)], cancel)
    after = photo_metadata(temp, cancel)
    def preserved(values):
        offsets = {'StripOffsets', 'TileOffsets', 'ThumbnailOffset', 'PreviewImageStart', 'JpgFromRawStart'}
        return {k: v for k, v in values.items() if k != 'SourceFile' and not k.startswith('GPS:')
                and ':GPS' not in k and k.split(':')[-1] not in offsets}
    old, new = preserved(before), preserved(after)
    # ExifTool supplies these required TIFF defaults when creating a new EXIF block.
    defaults = {'IFD0:XResolution', 'IFD0:YResolution', 'IFD0:ResolutionUnit', 'IFD0:YCbCrPositioning'}
    if any(new.get(k) != v for k, v in old.items()) or set(new) - set(old) - defaults:
        raise ValueError('图像或原有元数据校验不一致，原文件保留。')
    actual_lat, actual_lon = float(after['GPS:GPSLatitude']), float(after['GPS:GPSLongitude'])
    if after['GPS:GPSLatitudeRef'] == 'S': actual_lat = -actual_lat
    if after['GPS:GPSLongitudeRef'] == 'W': actual_lon = -actual_lon
    if abs(actual_lat - lat) > .000001 or abs(actual_lon - lon) > .000001:
        raise ValueError('GPS 回读校验失败，原文件保留。')


class LocationService:
    def __init__(self, catalog):
        self.catalog = catalog
        self.lock = threading.Lock()

    def _update(self, identifier, **values):
        with closing(self.catalog.connect()) as db, db:
            db.execute('UPDATE gps_jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                       [*values.values(), identifier])

    def _finish(self, job):
        source = Path(job['source_path'])
        info = source.stat()
        with closing(self.catalog.connect()) as db, db:
            row = db.execute('SELECT current_path FROM media WHERE id=?', (job['media_id'],)).fetchone()
            if not row or Path(row['current_path']).resolve() != source.resolve():
                raise ValueError('素材路径变化，定位记录待恢复。')
            db.execute('''UPDATE media SET latitude=?,longitude=?,gps_status='present',size=?,mtime_ns=?,
                content_hash=?,last_seen_at=? WHERE id=?''', (job['latitude'], job['longitude'],
                info.st_size, info.st_mtime_ns, job['output_hash'], now(), job['media_id']))
            db.execute("UPDATE gps_jobs SET status='complete',completed_at=?,error=NULL WHERE id=?", (now(), job['id']))
            old_gps = json.loads(job['old_gps_json'] or '{}')
            if old_gps.pop('_recovery', None) is not None:
                db.execute('UPDATE gps_jobs SET old_gps_json=? WHERE id=?',
                           (json.dumps(old_gps,ensure_ascii=False), job['id']))

    def _recover(self, row, cancel):
        with closing(self.catalog.connect()) as db:
            jobs = [dict(r) for r in db.execute("SELECT * FROM gps_jobs WHERE media_id=? AND status IN ('writing','ready') ORDER BY rowid", (row['id'],))]
        for job in jobs:
            import task_telemetry as telemetry
            telemetry.journal('gps',job['id']);telemetry.phase('恢复上次定位')
            if json.loads(job['old_gps_json'] or '{}').get('_write_mode') == 'fast':
                try:
                    self._recover_fast(job, row, cancel)
                except FastUnsupported as exc:
                    raise ValueError('快速定位恢复失败：' + str(exc)) from exc
                return True
            source, temp = Path(job['source_path']), Path(job['temp_path'])
            if source.resolve() != Path(row['current_path']).resolve() or temp.parent != source.parent or temp.name != f'.mediaorganizer-gps-{job["id"]}{source.suffix}':
                raise ValueError('定位恢复路径不匹配。')
            if job['status'] == 'ready':
                current = digest(source, cancel)
                if current == job['output_hash']:
                    self._finish(job)
                    temp.unlink(missing_ok=True)
                    return True
                if current != job['input_hash']:
                    raise ValueError('待恢复的素材已被外部修改，停止写入。')
                if temp.is_file() and digest(temp, cancel) == job['output_hash']:
                    if cancel.is_set():
                        raise Cancelled()
                    os.replace(temp, source)
                    self._finish(job)
                    return True
                raise ValueError('待恢复的定位文件缺失或校验失败。')
            temp.unlink(missing_ok=True)
            self._update(job['id'], status='failed', error='上次写入中断；原文件保留。')
        return False

    def _verify_fast_gps(self, raw, job, cancel):
        from fast_video_gps import metadata_bytes
        import re
        value = metadata_bytes(raw, exiftool, cancel).get('Keys:GPSCoordinates', '')
        numbers = re.findall(r'[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', str(value))
        if len(numbers) < 2 or abs(float(numbers[0])-job['latitude']) > 1e-6 or abs(float(numbers[1])-job['longitude']) > 1e-6:
            raise ValueError('快速写入 GPS 回读失败，请恢复该素材')

    def _recover_fast(self, job, row, cancel):
        from fast_video_gps import MAX_META, locked_file, edit_moov, validate_recovery, commit, stream_digest, restore_mtime
        path = Path(job['source_path'])
        if path.resolve() != Path(row['current_path']).resolve() or job['temp_path']:
            raise ValueError('快速定位恢复路径不匹配')
        if len(job['old_gps_json']) > MAX_META*3:
            raise ValueError('定位恢复记录过大')
        data = json.loads(job['old_gps_json'])['_recovery']
        before, after = (base64.b64decode(data[k], validate=True) for k in ('before','after'))
        offset = data['offset']
        if not isinstance(offset, int) or offset < 8 or len(before) > MAX_META or len(after) > MAX_META or not before:
            raise ValueError('定位恢复记录损坏')
        if after != edit_moov(before, job['latitude'], job['longitude']):
            raise ValueError('定位恢复内容不匹配')
        with locked_file(path) as stream:
            info = os.fstat(stream.fileno())
            if data['inode'] and info.st_ino != data['inode']:
                raise ValueError('素材已被替换，停止自动恢复')
            validate_recovery(stream, offset, before, after, job['input_hash'], cancel)
            if cancel.is_set(): raise Cancelled()
            commit(stream, offset, after)
            stream.seek(offset)
            self._verify_fast_gps(stream.read(len(after)), job, cancel)
            restore_mtime(stream, data['mtime_ns'])
            job['output_hash'] = stream_digest(stream, cancel)
            self._update(job['id'], status='ready', output_hash=job['output_hash'])
            self._finish(job)

    def _process_fast(self, row, lat, lon, name, coordinate_source, overwrite, cancel, progress):
        import task_telemetry as telemetry
        from fast_video_gps import locked_file, plan, commit, metadata_bytes, stream_digest, restore_mtime
        path = Path(row['current_path']).resolve(strict=True)
        with locked_file(path) as stream:
            info = os.fstat(stream.fileno())
            if (info.st_size, info.st_mtime_ns) != (row['size'], row['mtime_ns']):
                raise ValueError('素材已变化，请重新扫描后写入')
            progress('检查快速写入条件')
            offset, before, after = plan(stream, lat, lon)
            if shutil.disk_usage(path.parent).free < len(after)-len(before):
                raise ValueError('素材所在磁盘空间不足，未修改原文件')
            old = metadata_bytes(before, exiftool, cancel)
            gps = {k:v for k,v in old.items() if ':GPS' in k or ':Location' in k}
            if not overwrite and any('Latitude' in k or 'Longitude' in k or 'Coordinates' in k for k in gps):
                return 'skipped'
            if any(not k.startswith(('Keys:', 'Composite:')) for k in gps):
                raise FastUnsupported('已有其他类型定位，请选择保护写入')
            progress('核对原片')
            fingerprint = stream_digest(stream, cancel)
            identifier = uuid.uuid4().hex
            payload = dict(offset=offset, inode=info.st_ino, mtime_ns=info.st_mtime_ns,
                before=base64.b64encode(before).decode('ascii'), after=base64.b64encode(after).decode('ascii'))
            # The existing local SQLite transaction journals metadata only.
            # Existing catalog backup/restore includes recovery data as well.
            gps['_write_mode'] = 'fast'
            gps['_recovery'] = payload
            job = dict(id=identifier, media_id=row['id'], source_path=str(path), source_name=path.name,
                created_at=now(), status='writing', latitude=lat, longitude=lon, location_name=name,
                coordinate_source=coordinate_source, old_gps_json=json.dumps(gps,ensure_ascii=False),
                input_hash=fingerprint, output_hash=None, temp_path='')
            writing = False
            try:
                with closing(self.catalog.connect()) as db, db:
                    db.execute(f'INSERT INTO gps_jobs ({",".join(job)}) VALUES ({",".join("?" for _ in job)})', list(job.values()))
                telemetry.journal('gps', identifier)
                if cancel.is_set(): raise Cancelled()
                progress('快速写入定位')
                writing = True
                commit(stream, offset, after)
                progress('回读定位与校验指纹')
                stream.seek(offset)
                self._verify_fast_gps(stream.read(len(after)), job, cancel)
                restore_mtime(stream, info.st_mtime_ns)
                job['output_hash'] = stream_digest(stream, cancel)
                self._update(identifier, status='ready', output_hash=job['output_hash'])
                self._finish(job)
            except BaseException as exc:
                if writing:
                    self._update(identifier, error='快速定位未完成，请重新添加定位恢复；不要移动该素材。' + str(exc))
                else:
                    gps.pop('_recovery', None)
                    self._update(identifier, status='failed', error='未修改素材：' + str(exc))
                    self._update(identifier, old_gps_json=json.dumps(gps,ensure_ascii=False))
                raise
        return 'complete'

    def _process(self, row, lat, lon, name, coordinate_source, overwrite, cancel, write_mode='protected', progress=lambda _: None):
        import task_telemetry as telemetry
        path = Path(row['current_path'])
        if path.is_symlink() or path.stat().st_nlink > 1:
            raise ValueError('不修改符号链接或具有多个硬链接的素材。')
        path = path.resolve(strict=True)
        with closing(self.catalog.connect()) as db:
            pending = db.execute("SELECT 1 FROM import_items WHERE media_id=? AND status NOT IN ('complete','skipped') LIMIT 1", (row['id'],)).fetchone()
            pending_color=db.execute("SELECT 1 FROM color_jobs WHERE media_id=? AND status IN ('encoding','ready') LIMIT 1",(row['id'],)).fetchone()
        if pending_color:raise ValueError('有未完成的颜色处理，请先恢复。')
        if pending:
            raise ValueError('有未完成的导入，请先完成导入。')
        if self._recover(row, cancel):
            return 'recovered'
        info = path.stat()
        if (info.st_size, info.st_mtime_ns) != (row['size'], row['mtime_ns']):
            raise ValueError('素材已变化，请重新扫描后写入。')
        if row['kind'] == 'video' and write_mode == 'fast':
            return self._process_fast(row, lat, lon, name, coordinate_source, overwrite, cancel, progress)
        progress('核对原片')
        fingerprint = digest(path, cancel)
        video = row['kind']=='video'
        if video:
            from video_gps import metadata
            before=metadata(path,exiftool,cancel)
        else:before = photo_metadata(path, cancel)
        gps = {k: v for k, v in before.items() if k.startswith('GPS:') or ':GPS' in k}
        if not overwrite and any('Latitude' in k or 'Longitude' in k or 'Coordinates' in k for k in gps):
            return 'skipped'
        if video and any(k.startswith('XMP') for k in gps):
            raise ValueError('此视频含 XMP 定位，暂不替换，原文件保留。')
        identifier = uuid.uuid4().hex
        temp = path.parent / f'.mediaorganizer-gps-{identifier}{path.suffix}'
        job = dict(id=identifier, media_id=row['id'], source_path=str(path), source_name=path.name,
            created_at=now(), status='writing', latitude=lat, longitude=lon, location_name=name,
            coordinate_source=coordinate_source, old_gps_json=json.dumps(gps, ensure_ascii=False),
            input_hash=fingerprint, temp_path=str(temp))
        with closing(self.catalog.connect()) as db, db:
            db.execute(f'INSERT INTO gps_jobs ({",".join(job)}) VALUES ({",".join("?" for _ in job)})', list(job.values()))
        telemetry.journal('gps', identifier)
        try:
            if shutil.disk_usage(path.parent).free < info.st_size * 2 + 16 * 1024**2:
                raise ValueError('剩余空间不足，原文件保留。')
            if video:
                from video_gps import write_verified
                progress('写入临时视频并校验轨道')
                write_verified(path,temp,lat,lon,exiftool,cancel)
            else:
                progress('写入临时照片并校验')
                write_photo_verified(path,temp,lat,lon,before,cancel)
            progress('复核原片')
            if digest(path, cancel) != fingerprint:
                raise ValueError('写入期间照片被外部修改，停止替换。')
            shutil.copystat(path, temp)
            with temp.open('rb+') as file:
                os.fsync(file.fileno())
            progress('校验成品指纹')
            job['output_hash'] = digest(temp, cancel)
            if cancel.is_set():
                raise Cancelled()
            self._update(identifier, status='ready', output_hash=job['output_hash'])
            job['status'] = 'ready'
            progress('保存定位')
            os.replace(temp, path)
            self._finish(job)
            return 'complete'
        except BaseException as exc:
            if job['status'] != 'ready':
                temp.unlink(missing_ok=True)
                self._update(identifier, status='failed', error='已停止，原文件保留' if isinstance(exc, Cancelled) else str(exc))
            else:
                self._update(identifier, error='待恢复  ' + str(exc))
            raise

    def run(self, rows, latitude, longitude, *, location_name='', coordinate_source='手动 WGS84',
            overwrite=False, cancel=None, progress=None, pause=None, write_mode='protected'):
        if write_mode not in ('protected', 'fast'):
            raise ValueError('未知 GPS 写入方式')
        lat, lon = coordinates(latitude, longitude)
        cancel = cancel or threading.Event()
        result = dict(complete=0, skipped=0, recovered=0, errors=[], stopped=False, skip_reasons=[])
        if not self.lock.acquire(False):
            raise ValueError('已有定位任务正在运行。')
        try:
            rows = list(rows)
            for index, row in enumerate(rows):
                def report(phase, processed):
                    if progress: progress(dict(filename=Path(row['current_path']).name, path=row['current_path'], phase=phase, encoder='', processed=processed, total=len(rows), index=index+1))
                while pause and not pause.wait(.2) and not cancel.is_set():
                    pass
                if cancel.is_set():
                    result['stopped'] = True
                    break
                if not supports_gps(row):
                    result['skipped'] += 1
                    report('跳过', index+1)
                    continue
                try:
                    report('写入定位', index)
                    status = self._process(row, lat, lon, location_name, coordinate_source, overwrite, cancel,
                                           write_mode, lambda phase: report(phase, index))
                    result['skipped' if status == 'skipped' else 'complete'] += 1
                    if status == 'recovered': result['recovered'] += 1
                except Cancelled:
                    result['stopped'] = True
                    break
                except FastUnsupported as exc:
                    result['skipped'] += 1
                    result['skip_reasons'].append(f'{row["current_path"]}    {exc}')
                except Exception as exc:
                    result['errors'].append(f'{Path(row["current_path"]).name}    {exc}')
                report('已处理', index+1)
            return result
        finally:
            self.lock.release()


def supports_gps(row):
    return Path(row['current_path']).suffix.lower() in (GPS_EXTENSIONS if row['kind']=='photo' else VIDEO_GPS_EXTENSIONS if row['kind']=='video' else set())
