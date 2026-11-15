"""Opt-in Action 6 D-Log M restoration on local MP4s before import.

Only a validated, journaled output may replace a source. A ready journal is
recoverable even if the process exits between os.replace and the SQLite commit.
No media backups are retained, as requested by the owner.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import tempfile
import queue
from contextlib import closing
from fractions import Fraction
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe
from pymediainfo import MediaInfo

from import_service import Cancelled, digest, beneath
from media_catalog import now, path_key
from source_scope import roots
from processing_markers import COLOR, marked, append as append_marker

LUT_PATH = Path(__file__).resolve().parent / 'assets/luts/action6_dlogm_rec709.cube'
LUT_HASH = '1972eb9b05699ba19d4aaa67aaa5fb00b46935c60ba9219183289f79e044cbb2'
CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def hardware_failure(error):
    message=str(error).lower()
    if any(s in message for s in ('permission denied','no space left','input/output error',
                                  'error opening output','error writing','error muxing','network','broken pipe')):
        return False
    return any(s in message for s in ('qsv','nvenc','amf','hardware cannot','no capable devices',
                                      'device failed','error while opening encoder','unsupported device'))


def exact_comment(path, cancel):
    from location_service import exiftool
    values=json.loads(exiftool(['-j','-G1','-QuickTime:Comment',str(path)],cancel))[0]
    return next((str(v) for k,v in values.items() if k.endswith(':Comment')), '')


def lrf_candidates(rows):
    """User's convention: same-directory, same-stem LRF flags its MP4 for review."""
    mp4s = {path_key(Path(r['current_path']).with_suffix('')): r for r in rows
            if Path(r['current_path']).suffix.lower() == '.mp4'}
    candidates, missing, seen = [], [], set()
    for row in rows:
        path = Path(row['current_path'])
        if path.suffix.lower() != '.lrf':
            continue
        main = mp4s.get(path_key(path.with_suffix('')))
        if main is None:
            missing.append(path.name)
        elif main['id'] not in seen and not main.get('color_revision'):
            seen.add(main['id'])
            candidates.append(dict(main))
    return candidates, missing


def profile(path):
    info = MediaInfo.parse(str(path))
    if len(info.video_tracks) != 1:
        raise ValueError('只支持单视频轨道的主 MP4。')
    video = info.video_tracks[0].to_data()
    required = ('width', 'height', 'frame_rate', 'frame_count', 'bit_depth', 'duration')
    if any(not video.get(k) for k in required):
        raise ValueError('视频参数不完整，无法校验原分辨率、帧率或位深。')
    if video.get('format') not in ('AVC', 'HEVC') or int(video['bit_depth']) not in (8, 10):
        raise ValueError('仅支持 H.264 / H.265、8 / 10 位视频。')
    if video.get('chroma_subsampling') != '4:2:0' or video.get('frame_rate_mode') != 'CFR':
        raise ValueError('仅支持固定帧率、4:2:0 视频；其他格式保留原片。')
    if video.get('scan_type', 'Progressive') != 'Progressive':
        raise ValueError('不支持隔行视频。')
    if any('hdr' in k.lower() and v for k, v in video.items()):
        raise ValueError('检测到 HDR 标记，请勿按 D-Log M 处理。')
    general = info.general_tracks[0].to_data()
    fields = ('format', *required, 'framerate_num', 'framerate_den', 'rotation',
              'pixel_aspect_ratio', 'color_range', 'colour_primaries',
              'transfer_characteristics', 'matrix_coefficients')
    result = {k: video.get(k) for k in fields}
    result['capture'] = general.get('recorded_date') or general.get('encoded_date')
    result['cover'] = general.get('cover') == 'Yes'
    result['comment'] = general.get('comment') or ''
    result['aux'] = [t.to_data() for t in info.tracks if t.track_type not in ('General', 'Video', 'Menu')]
    return result


def private_tracks(spec):
    return [t for t in spec['aux'] if str(t.get('codec_id', t.get('format', ''))).lower() in ('djmd', 'dbgi')]


def timecode_tracks(spec):
    return [t for t in spec['aux'] if t.get('format') == 'QuickTime TC']


def exclude_tracks(tracks):
    args = []
    for track in tracks:
        index = str(track.get('streamorder', ''))
        if not index.isdecimal():
            raise ValueError('无法确认附加轨道编号，保留原片。')
        args += ['-map', '-0:' + index]
    return args


def command(args, cancel, progress=None, cwd=None, timeout=None, frame_progress=None):
    """Read progress while Windows pipes are open, never via communicate timeout."""
    started = time.monotonic()
    process = subprocess.Popen([get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
        '-nostdin', *(['-progress', 'pipe:1', '-nostats'] if frame_progress else []), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, creationflags=CREATION_FLAGS)
    messages = queue.Queue()
    output, errors = [], []
    def drain(stream, target, report=False):
        for line in iter(stream.readline, b''):
            if report: messages.put(line)
            else: target.append(line)
            if target is errors and len(target)>128: del target[:-128]
        stream.close()
    readers = [threading.Thread(target=drain,args=(process.stdout,output,bool(frame_progress)),daemon=True),
               threading.Thread(target=drain,args=(process.stderr,errors),daemon=True)]
    for reader in readers: reader.start()
    last_report = 0.
    def report_lines():
        from task_telemetry import detail
        while True:
            try: line=messages.get_nowait().decode('ascii',errors='replace').strip()
            except queue.Empty: break
            key, _, value=line.partition('=')
            if key=='frame' and value.strip().isdigit():frame_progress(int(value))
            elif key in ('fps','speed'):detail(**{key:value.strip()})
    try:
        while True:
            if cancel.is_set():
                raise Cancelled()
            if timeout and time.monotonic() - started > timeout:
                raise TimeoutError('编码器检测超时')
            report_lines()
            if process.poll() is not None:
                for reader in readers: reader.join()
                report_lines()
                if process.returncode:
                    raise RuntimeError(b''.join(errors).decode('utf-8',errors='replace')[-2500:] or 'FFmpeg 处理失败')
                return b''.join(output)
            if progress and time.monotonic()-last_report>=.3:
                progress();last_report=time.monotonic()
            time.sleep(.03)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for reader in readers: reader.join(timeout=3)


class ColorService:
    def __init__(self, catalog):
        self.catalog = catalog
        self.lock = threading.Lock()
        self.encoders = {}
        self.encoder_reasons = {}

    def auto_candidates(self, rows):
        candidates, missing = lrf_candidates(rows)
        # A preserved original is still D-Log, but a recorded copy need not prompt again.
        # This is a reminder filter only; run() always rehashes before modifying anything.
        with closing(self.catalog.connect()) as db:
            candidates = [r for r in candidates if not (r.get('content_hash') and db.execute(
                "SELECT 1 FROM color_jobs WHERE input_hash=? AND output_mode='copy' AND status='complete' LIMIT 1",
                (r['content_hash'],)).fetchone())]
        return candidates, missing

    def history(self, limit=200, offset=0):
        with closing(self.catalog.connect()) as db:
            return [dict(r) for r in db.execute('''SELECT c.*, coalesce(m.current_path,c.output_path,c.source_path) current_path
                FROM color_jobs c LEFT JOIN media m ON m.id=coalesce(c.output_media_id,c.media_id)
                ORDER BY c.created_at DESC, c.rowid DESC LIMIT ? OFFSET ?''', (limit, offset))]

    def _update(self, identifier, **values):
        with closing(self.catalog.connect()) as db, db:
            db.execute('UPDATE color_jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                       [*values.values(), identifier])

    def _finish(self, job):
        source = Path(job['source_path'])
        output = Path(job.get('output_path') or source)
        info = output.stat()
        with closing(self.catalog.connect()) as db, db:
            media = db.execute('SELECT current_path FROM media WHERE id=?', (job['media_id'],)).fetchone()
            # Catalog entries may use Windows 8.3 paths while beneath() returns long paths.
            if not media or Path(media['current_path']).resolve() != source.resolve():
                raise ValueError('素材路径已变化，保留颜色处理恢复记录。')
            media_id = job['media_id']
            if job.get('output_mode') == 'copy':
                media_id = job['output_media_id']
                row = dict(db.execute('SELECT * FROM media WHERE id=?', (job['media_id'],)).fetchone())
                indexed = db.execute('SELECT id FROM media WHERE path_key=?', (path_key(output),)).fetchone()
                if indexed:
                    media_id = indexed['id']
                    db.execute('UPDATE color_jobs SET output_media_id=? WHERE id=?', (media_id, job['id']))
                row.update(id=media_id, path_key=path_key(output), current_path=str(output),
                    observed_name=output.name, original_name=row['original_name'] or source.name,
                    first_indexed_at=now(), imported_at=None)
                db.execute(f'INSERT OR IGNORE INTO media ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', list(row.values()))
                db.execute('''INSERT OR IGNORE INTO batch_items (batch_id,media_id,observed_path,observed_name,result)
                    SELECT batch_id,?,?,?,'color_copy' FROM batch_items WHERE media_id=?''',
                    (media_id, str(output), output.name, job['media_id']))
                db.execute('UPDATE media SET content_hash=? WHERE id=?', (job['input_hash'], job['media_id']))
            db.execute('''UPDATE media SET size=?, mtime_ns=?, content_hash=?, color_revision=?,
                last_seen_at=? WHERE id=?''',
                (info.st_size, info.st_mtime_ns, job['output_hash'], job['output_hash'], now(),
                 media_id))
            db.execute("UPDATE color_jobs SET status='complete', completed_at=?, error=NULL WHERE id=?",
                       (now(), job['id']))

    def _recover(self, source, cancel, output_mode='replace'):
        with closing(self.catalog.connect()) as db:
            jobs = [dict(r) for r in db.execute('''SELECT * FROM color_jobs
                WHERE source_path=? COLLATE NOCASE AND status IN ('encoding','ready') ORDER BY rowid''', (str(source),))]
        for job in jobs:
            import task_telemetry as telemetry
            telemetry.journal('color',job['id']);telemetry.phase('恢复上次颜色处理')
            if job.get('output_mode', 'replace') != output_mode:
                raise ValueError('有待恢复的颜色任务，请选择与上次一致的保存方式后恢复。')
            temp = Path(job['temp_path'])
            if temp.parent != source.parent or temp.name != f'.mediaorganizer-color-{job["id"]}.part':
                raise ValueError('恢复记录中的临时路径不匹配。')
            if job['status'] == 'ready':
                if output_mode == 'copy':
                    output = Path(job['output_path'])
                    if output.parent != source.parent or output == source or output.suffix.lower() != '.mp4':
                        raise ValueError('还原版路径不匹配。')
                    if output.exists():
                        if digest(output, cancel) != job['output_hash']:
                            raise ValueError('还原版路径已被其他文件占用，停止发布。')
                        self._finish(job)
                        temp.unlink(missing_ok=True)
                        return True
                    if digest(source, cancel) != job['input_hash']:
                        raise ValueError('待恢复的原文件已被外部修改。')
                    if temp.exists() and digest(temp, cancel) == job['output_hash']:
                        self._publish(temp, output, output_mode)
                        self._finish(job)
                        return True
                    raise ValueError('待恢复的还原版缺失或校验失败，原片保留。')
                current = digest(source, cancel)
                if current == job['output_hash']:
                    self._finish(job)
                    temp.unlink(missing_ok=True)
                    return True
                if current != job['input_hash']:
                    raise ValueError('待恢复的原文件已被外部修改，停止覆盖。')
                if temp.exists() and digest(temp, cancel) == job['output_hash']:
                    if cancel.is_set():
                        raise Cancelled()
                    os.replace(temp, source)
                    self._finish(job)
                    return True
            # Interrupted unvalidated output is never published.
            temp.unlink(missing_ok=True)
            self._update(job['id'], status='failed', error='上次处理未完成；原片保留，重新处理。')
        return False

    @staticmethod
    def _publish(temp, output, output_mode):
        if output_mode == 'replace':
            os.replace(temp, output)
        elif os.name == 'nt':
            os.rename(temp, output)  # Windows refuses to overwrite an existing destination.
        else:
            os.link(temp, output)
            temp.unlink()

    def _encoder(self, spec, mode, cancel):
        from task_telemetry import detail, phase
        codec = 'hevc' if spec['format'] == 'HEVC' else 'h264'
        bits = int(spec['bit_depth'])
        key = codec, bits, mode, spec['width'], spec['height'], spec['frame_rate']
        if key in self.encoders:
            if key in self.encoder_reasons:detail(note=self.encoder_reasons[key])
            return self.encoders[key]
        candidates = []
        failures = []
        if mode == 'auto' and not (codec == 'h264' and bits == 10):
            candidates = [(codec + '_qsv', ['-preset', 'medium', '-global_quality', '18']),
                          (codec + '_nvenc', ['-preset', 'p5', '-rc', 'vbr', '-cq', '18', '-b:v', '0']),
                          (codec + '_amf', ['-usage','transcoding','-quality','balanced','-rc','cqp',
                                           '-qp_i','18','-qp_p','18'] + (['-profile','main10','-bitdepth','10'] if codec=='hevc' and bits==10 else []))]
        for name, options in candidates:
            pixel = 'p010le' if bits == 10 else 'nv12'
            phase('检测编码器'); detail(encoder=name)
            try:
                # Probe the real dimensions, frame rate and bit depth, then read back.
                with tempfile.TemporaryDirectory(prefix='encoder-',dir=self.catalog.database.parent) as folder:
                    output=Path(folder)/'probe.mp4'
                    rate=(f"{spec['framerate_num']}/{spec['framerate_den']}" if spec.get('framerate_num') and spec.get('framerate_den') else spec['frame_rate'])
                    command(['-f', 'lavfi', '-i', f"color=size={spec['width']}x{spec['height']}:rate={rate}",
                             '-frames:v', '2','-filter_threads','2','-c:v', name, *options, '-pix_fmt', pixel,
                             '-f','mp4',str(output)], cancel, timeout=20)
                    actual=profile(output)
                    if any(str(actual[k])!=str(spec[k]) for k in ('format','width','height','bit_depth')) or abs(float(actual['frame_rate'])-float(spec['frame_rate']))>.0001:
                        raise RuntimeError('硬件输出参数与原片不一致')
                self.encoders[key] = (name, options, pixel)
                return self.encoders[key]
            except (RuntimeError, TimeoutError, ValueError) as exc:
                failures.append(name+': '+str(exc)[-500:])
                continue
        name = 'libx265' if codec == 'hevc' else 'libx264'
        options = ['-preset', 'fast', '-crf', '18', '-threads', '4']
        if codec == 'hevc':
            options += ['-x265-params', 'pools=4:frame-threads=2']
        result = (name, options, 'yuv420p10le' if bits == 10 else 'yuv420p')
        if mode=='auto':
            self.encoder_reasons[key]='使用 CPU：'+('；'.join(failures) or '当前编码格式/位深不支持硬件编码')
            detail(note=self.encoder_reasons[key])
        self.encoders[key] = result
        return result

    def _validate(self, source, temp, before, cancel, heartbeat):
        from task_telemetry import phase
        phase('核对视频参数')
        after = profile(temp)
        for field in ('format', 'width', 'height', 'bit_depth', 'frame_count'):
            if str(before[field]) != str(after[field]):
                raise ValueError(f'输出校验失败  {field}')
        if exact_comment(temp,cancel) != append_marker(before.get('comment'),COLOR):
            raise ValueError('处理标识或原备注未保留，原片不覆盖。')
        if all(s.get('framerate_num') and s.get('framerate_den') for s in (before, after)):
            rates = [Fraction(int(s['framerate_num']), int(s['framerate_den'])) for s in (before, after)]
            if rates[0] != rates[1]:
                raise ValueError('输出校验失败  精确帧率')
        for field, tolerance in [('frame_rate', .0001), ('duration', 2),
                                 ('rotation', .001), ('pixel_aspect_ratio', .001)]:
            if abs(float(before.get(field) or 0) - float(after.get(field) or 0)) > tolerance:
                raise ValueError(f'输出校验失败  {field}')
        if before['capture'] and before['capture'] != after['capture']:
            raise ValueError('拍摄时间元数据未保留，原片不覆盖。')
        removed = before.get('removed_private_tracks', [])
        if len(before['aux']) - len(removed) != len(after['aux']):
            raise ValueError('音频或附加轨道数量变化，原片不覆盖。')
        # MP4 timecode is rebuilt by the muxer from the copied timecode metadata.
        old_tc, new_tc = timecode_tracks(before), timecode_tracks(after)
        tc_fields = ('time_code_of_first_frame', 'time_code_of_last_frame', 'frame_count')
        if [[t.get(k) for k in tc_fields] for t in old_tc] != [[t.get(k) for k in tc_fields] for t in new_tc]:
            raise ValueError('时间码校验失败，原片不覆盖。')
        if before.get('cover') != after.get('cover'):
            raise ValueError('内嵌封面未保留，原片不覆盖。')
        if len(before['aux']) > len(removed) + len(old_tc) or before.get('cover'):
            phase('校验音频与附加轨道')
            def auxiliary_hash(file, excluded):
                return command(['-i', str(file), '-map', '0', '-map', '-0:V:0', *exclude_tracks(excluded), '-c', 'copy',
                    '-f', 'streamhash', '-hash', 'sha256', '-'], cancel)
            if auxiliary_hash(source, removed + old_tc) != auxiliary_hash(temp, new_tc):
                raise ValueError('音频或附加轨道内容变化，原片不覆盖。')
        phase('完整解码校验')
        import task_telemetry as telemetry
        command(['-xerror', '-err_detect', 'explode', '-threads', '4', '-i', str(temp),
                 '-map', '0:V:0', '-f', 'null', '-'], cancel,
                 frame_progress=lambda value:telemetry.detail(percent=min(100,100*value/int(before['frame_count']))))
        if after.get('matrix_coefficients') != 'BT.709':
            raise ValueError('输出 Rec.709 色彩标记校验失败。')

    def _process(self, row, root, mode, cancel, progress, lut_folder, output_mode='replace', allow_private_data_loss=False, gps_preservation='fast'):
        import task_telemetry as telemetry
        telemetry.phase('核对原片')
        source = beneath(row['current_path'], root)
        phase, percent = '核对原片', None
        def report():
            if progress:
                progress(dict(filename=source.name, path=str(source), phase=phase,
                              encoder=locals_encoder[0], percent=percent))
        locals_encoder = ['']
        report()
        # Media may live on SMB/NAS. Only the catalog must be local; validated
        # output stays beside the source for same-directory publication/recovery.
        if source.is_symlink() or source.stat().st_nlink > 1:
            raise ValueError('不覆盖符号链接或具有多个硬链接的文件。')
        with closing(self.catalog.connect()) as db:
            pending = db.execute('''SELECT 1 FROM import_items WHERE source_path=? COLLATE NOCASE
                AND status NOT IN ('complete','skipped') LIMIT 1''', (str(source),)).fetchone()
            pending_gps=db.execute("SELECT 1 FROM gps_jobs WHERE media_id=? AND status IN ('writing','ready') LIMIT 1",(row['id'],)).fetchone()
        if pending_gps:
            raise ValueError('定位写入尚未收尾，请先在添加定位中恢复该素材。')
        if pending:
            raise ValueError('该文件有未完成的导入任务，请先完成导入，避免覆盖待传输文件。')
        if self._recover(source, cancel, output_mode):
            return 'recovered'
        fingerprint = digest(source, cancel)
        with closing(self.catalog.connect()) as db:
            # Verified GPS edits retain the color marker while updating the file
            # hash. The current bytes must still match that trusted catalog hash.
            done = db.execute("""SELECT 1 FROM color_jobs WHERE output_hash=? AND status IN ('complete','ready')
                UNION ALL SELECT 1 FROM media WHERE content_hash=? AND color_revision<>'' LIMIT 1""",
                              (fingerprint,fingerprint)).fetchone()
        if done:
            # Also mark a copied/renamed output discovered under a new media ID.
            with closing(self.catalog.connect()) as db, db:
                db.execute('UPDATE media SET content_hash=?, color_revision=? WHERE id=?',
                           (fingerprint, fingerprint, row['id']))
            return 'skipped'
        spec = profile(source)
        if marked(spec.get('comment'),COLOR):
            with closing(self.catalog.connect()) as db, db:
                db.execute('UPDATE media SET content_hash=?,color_revision=? WHERE id=?',(fingerprint,fingerprint,row['id']))
            return 'skipped'
        if spec.get('comment'):spec['comment']=exact_comment(source,cancel)
        if output_mode == 'copy':
            with closing(self.catalog.connect()) as db:
                copies = db.execute("SELECT output_path,output_hash FROM color_jobs WHERE input_hash=? AND output_mode='copy' AND status='complete'", (fingerprint,)).fetchall()
            for copy in copies:
                if Path(copy['output_path']).is_file() and digest(copy['output_path'], cancel) == copy['output_hash']:
                    return 'skipped'
        info = source.stat()
        if (info.st_size, info.st_mtime_ns) != (row['size'], row['mtime_ns']):
            raise ValueError('文件已变化，请重新扫描后处理。')
        identifier = uuid.uuid4().hex
        output = source
        if output_mode == 'copy':
            output = source.with_name(source.stem + '_Rec709.mp4')
            suffix = 2
            while output.exists():
                output = source.with_name(source.stem + f'_Rec709_{suffix}.mp4')
                suffix += 1
        temp = source.parent / f'.mediaorganizer-color-{identifier}.part'
        job = dict(id=identifier, media_id=row['id'], source_path=str(source), source_name=source.name,
                   created_at=now(), status='encoding', input_hash=fingerprint, lut_hash=LUT_HASH,
                   temp_path=str(temp), source_size=info.st_size, source_mtime_ns=info.st_mtime_ns,
                   source_atime_ns=info.st_atime_ns, output_mode=output_mode,
                   output_path=str(output), output_media_id=uuid.uuid4().hex if output_mode == 'copy' else row['id'])
        with closing(self.catalog.connect()) as db, db:
            db.execute(f'INSERT INTO color_jobs ({",".join(job)}) VALUES ({",".join("?" for _ in job)})', list(job.values()))
        telemetry.journal('color', identifier)
        try:
            removed = private_tracks(spec)
            if removed and not allow_private_data_loss:
                raise ValueError('此视频含大疆专有数据，请确认允许还原版移除这些数据后再执行。')
            spec['removed_private_tracks'] = removed
            excluded = exclude_tracks(removed + timecode_tracks(spec))
            if shutil.disk_usage(source.parent).free < info.st_size * 2 + 256 * 1024**2:
                raise ValueError('素材所在磁盘剩余空间不足；预留至少原文件两倍大小及 256 MB。')
            encoder, options, pixel = self._encoder(spec, mode, cancel)
            telemetry.detail(encoder=encoder)
            locals_encoder[0] = encoder
            self._update(identifier, encoder=encoder, profile_json=json.dumps(spec, ensure_ascii=False))
            phase = '还原颜色'
            percent = 0
            heartbeat = report
            def frames(value):
                nonlocal percent
                percent = min(100, 100 * value / int(spec['frame_count']))
                report()
            heartbeat()
            # Use an ASCII basename + cwd: avoids FFmpeg filter-path quoting on Windows.
            filters = ("scale=in_color_matrix=bt709:out_range=full,format=gbrpf32le,"
                       "lut3d=file=action6_dlogm_rec709.cube:interp=tetrahedral,"
                       f"scale=out_color_matrix=bt709:out_range=limited,format={pixel}")
            def encode():
                command(['-n', '-noautorotate', '-threads', '4', '-i', str(source), '-map', '0',
                    *excluded,
                    '-copy_unknown', '-map_metadata', '0', '-map_chapters', '0', '-c', 'copy',
                    '-metadata', 'comment='+append_marker(spec.get('comment'),COLOR),
                    # Uppercase V excludes attached cover art; its packets remain stream-copied.
                    '-c:V:0', encoder, *options, '-filter_threads', '4', '-filter:V:0', filters,
                    '-fps_mode:V:0', 'passthrough', '-colorspace:V:0', 'bt709', '-color_trc:V:0', 'bt709',
                    # MP4's normal metadata writer preserves attached cover art (covr).
                    '-color_primaries:V:0', 'bt709', '-color_range:V:0', 'tv',
                    '-f', 'mp4', str(temp)], cancel, heartbeat, cwd=lut_folder, frame_progress=frames)
            try:
                encode()
            except RuntimeError as exc:
                if mode != 'auto' or encoder in ('libx264', 'libx265') or not hardware_failure(exc):
                    raise
                # A tiny capability probe cannot prove support for 8K/high-fps input.
                temp.unlink(missing_ok=True)
                encoder, options, cpu_pixel = self._encoder(spec, 'cpu', cancel)
                telemetry.phase('硬件编码失败，改用 CPU')
                telemetry.detail(encoder=encoder,note=str(exc)[-1500:])
                self.encoders[('hevc' if spec['format']=='HEVC' else 'h264',int(spec['bit_depth']),'auto',spec['width'],spec['height'],spec['frame_rate'])]=(encoder,options,cpu_pixel)
                self.encoder_reasons[('hevc' if spec['format']=='HEVC' else 'h264',int(spec['bit_depth']),'auto',spec['width'],spec['height'],spec['frame_rate'])]='硬件编码失败，使用 CPU：'+str(exc)[-1500:]
                locals_encoder[0] = encoder; percent = 0
                filters = filters.rsplit('format=', 1)[0] + 'format=' + cpu_pixel
                self._update(identifier, encoder=encoder)
                encode()
            phase = '校验视频'
            percent = None
            heartbeat()
            from video_gps import preserve_keys_after_color
            preserve_keys_after_color(source,temp,cancel,mode=gps_preservation)
            self._validate(source, temp, spec, cancel, heartbeat)
            # Recheck the input, including bytes, immediately before publication.
            phase = '复核原片'; heartbeat()
            current = source.stat()
            if (current.st_size, current.st_mtime_ns) != (info.st_size, info.st_mtime_ns) or digest(source, cancel) != fingerprint:
                raise ValueError('处理期间原文件发生变化，原片不覆盖。')
            shutil.copystat(source, temp)
            with temp.open('rb+') as output_file:
                os.fsync(output_file.fileno())
            telemetry.phase('校验成品指纹')
            job['output_hash'] = digest(temp, cancel)
            if cancel.is_set():
                raise Cancelled()
            self._update(identifier, status='ready', output_hash=job['output_hash'])
            job['status'] = 'ready'
            phase = '保存成品'; heartbeat()
            self._publish(temp, output, output_mode)
            self._finish(job)
            return 'complete'
        except BaseException as exc:
            # After ready is durable, preserve its recovery evidence on any exception.
            if job['status'] != 'ready':
                self._update(identifier, status='failed', error='已停止，原片保留' if isinstance(exc, Cancelled) else str(exc))
                temp.unlink(missing_ok=True)
            else:
                self._update(identifier, error='等待收尾恢复  ' + str(exc))
            raise

    def run(self, rows, source_root, *, confirmed_dlogm=False, mode='auto', output_mode='replace', allow_private_data_loss=False, gps_preservation='fast', cancel=None, progress=None, pause=None):
        if gps_preservation not in ('fast','compatible'):
            raise ValueError('未知的定位保留方式')
        if not confirmed_dlogm:
            raise ValueError('请明确选择 Action 6 的 D-Log M 素材。')
        if mode not in ('auto', 'cpu'):
            raise ValueError('未知编码模式')
        if output_mode not in ('copy', 'replace'):
            raise ValueError('请选择另存或替换原文件。')
        if not self.lock.acquire(blocking=False):
            raise ValueError('已有颜色还原任务正在运行。')
        cancel = cancel or threading.Event()
        result = dict(complete=0, skipped=0, errors=[], stopped=False, recovered=0)
        try:
            lut_bytes = LUT_PATH.read_bytes()
            if hashlib.sha256(lut_bytes).hexdigest() != LUT_HASH:
                raise ValueError('Action 6 官方 LUT 缺失或校验失败。')
            # Freeze the verified LUT for the full batch, including long-running encodes.
            lut_snapshot = tempfile.TemporaryDirectory(prefix='color-lut-', dir=self.catalog.database.parent)
            with lut_snapshot:
                lut_folder = Path(lut_snapshot.name)
                (lut_folder / LUT_PATH.name).write_bytes(lut_bytes)
                return self._run_rows(rows, source_root, mode, cancel, progress, pause, result, lut_folder, output_mode, allow_private_data_loss, gps_preservation)
        finally:
            self.lock.release()

    def _run_rows(self, rows, source_root, mode, cancel, progress, pause, result, lut_folder, output_mode='replace', allow_private_data_loss=False, gps_preservation='fast'):
        root = roots(source_root, strict=True)
        rows = list({r['id']: r for r in rows}.values())
        for index, row in enumerate(rows):
            if cancel.is_set():
                result['stopped'] = True
                break
            while pause and not pause.wait(.2):
                if cancel.is_set():
                    break
            if cancel.is_set():
                result['stopped'] = True
                break
            if row['kind'] != 'video' or Path(row['current_path']).suffix.lower() != '.mp4':
                result['skipped'] += 1
                if progress: progress(dict(filename=Path(row['current_path']).name, path=row['current_path'], phase='跳过', encoder='', processed=index+1, total=len(rows), index=index+1))
                continue
            def report(event):
                if progress: progress(dict(event, processed=index, total=len(rows), index=index+1))
            try:
                status = self._process(row, root, mode, cancel, report, lut_folder, output_mode, allow_private_data_loss, gps_preservation)
                result['skipped' if status == 'skipped' else 'complete'] += 1
                if status=='recovered':result['recovered']+=1
            except Cancelled:
                result['stopped'] = True
                break
            except Exception as exc:
                result['errors'].append(f'{Path(row["current_path"]).name}    {exc}')
            if progress: progress(dict(filename=Path(row['current_path']).name, path=row['current_path'], phase='已处理', encoder='', processed=index+1, total=len(rows), index=index+1))
        return result
