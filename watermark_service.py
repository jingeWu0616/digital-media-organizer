"""Local photo watermark rendering and journaled, exclusive JPEG exports."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import threading
import uuid
import warnings
import tempfile
from contextlib import closing
from pathlib import Path

from PIL import Image, ImageCms, ImageDraw, ImageFont, ImageOps
from pillow_heif import register_heif_opener

from import_service import Cancelled, beneath, digest, publish_without_replace, safe_component, unused_path
from location_service import exiftool
from media_catalog import now, path_key, read_metadata
from watermark_layouts import EXTENDED_STYLES, render_layout
from processing_markers import WATERMARK, marked, append as append_marker

register_heif_opener()
PHOTO_TYPES = {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.tif', '.tiff'}
STYLES = {'corner': '极简签名', 'bar': '深色参数栏', 'frame': '经典白边',
          'gallery':'留白画廊', 'postcard':'旅行明信片', 'film':'胶片日期', 'memory':'家庭纪念', 'outdoor':'户外记录'}
FONTS = {'雅黑': 'msyh.ttc', '宋体': 'simsun.ttc', '黑体': 'simhei.ttf'}
DEFAULTS = dict(style='corner', fields=['date', 'camera'], custom='', position='bottom-right',
                size=3, opacity=85, theme='dark', font='雅黑', logo='', logo_size=12, quality=92,
                title='', signature='')
COPY_TAGS = ['EXIF:DateTimeOriginal', 'EXIF:CreateDate', 'EXIF:ModifyDate',
    'EXIF:OffsetTime', 'EXIF:OffsetTimeOriginal', 'EXIF:OffsetTimeDigitized',
    'EXIF:SubSecTimeOriginal', 'EXIF:SubSecTimeDigitized', 'EXIF:Make', 'EXIF:Model',
    'EXIF:LensModel', 'EXIF:ExposureTime', 'EXIF:FNumber', 'EXIF:ISO', 'EXIF:FocalLength',
    'EXIF:FocalLengthIn35mmFormat', 'EXIF:Artist', 'EXIF:Copyright', 'GPS:all']


def normalize(options):
    result = dict(DEFAULTS, **{k: v for k, v in options.items() if k in DEFAULTS})
    if result['style'] not in STYLES or result['font'] not in FONTS or result['theme'] not in ('dark', 'light'):
        raise ValueError('未知水印样式。')
    if result['position'] not in ('bottom-right', 'bottom-left', 'top-right', 'top-left'):
        raise ValueError('未知水印位置。')
    if not isinstance(result['fields'], list) or not set(result['fields']) <= {'date', 'camera', 'location', 'parameters'}:
        raise ValueError('未知水印内容。')
    result['fields'] = sorted(set(result['fields']))
    for key, low, high in [('size',1,6),('opacity',10,100),('logo_size',3,25),('quality',80,100)]:
        value = float(result[key])
        if not math.isfinite(value) or not low <= value <= high: raise ValueError('水印参数超出范围。')
        result[key] = int(value) if key in ('opacity','quality') else value
    result['custom'] = str(result['custom']).strip()
    if len(result['custom']) > 300: raise ValueError('自定义文字最多 300 字。')
    for key, label in [('title','标题'),('signature','署名')]:
        result[key] = str(result[key]).strip()
        if len(result[key])>60 or '\n' in result[key] or '\r' in result[key]: raise ValueError(label+'最多 60 字，且不能换行。')
    result['logo'] = str(result['logo']).strip()
    if not result['fields'] and not any(result[key] for key in ('custom','logo','title','signature')):
        raise ValueError('请选择水印内容或上传 Logo。')
    return result


def font_path(options):
    path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / FONTS[options['font']]
    if not path.is_file(): raise ValueError('所选字体不可用，请选择其他字体。')
    return path


def load_logo(path):
    if not path: return None
    path = Path(path)
    if path.suffix.lower() not in ('.png', '.jpg', '.jpeg') or path.stat().st_size > 20 * 1024**2:
        raise ValueError('Logo 请使用小于 20 MB 的 PNG 或 JPG。')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as source:
            if source.width * source.height > 16_000_000: raise ValueError('Logo 图片过大。')
            image = ImageOps.exif_transpose(source).convert('RGBA')
    image.thumbnail((1600,1600), Image.Resampling.LANCZOS)
    return image


def read_photo(path, preview=False):
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as source:
            if getattr(source, 'n_frames', 1) != 1: raise ValueError('多帧照片请先导出单张图片。')
            if source.mode in ('I', 'F', 'I;16', 'I;16B', 'I;16L'):
                raise ValueError('16 位或浮点图片请先导出普通照片。')
            exif = source.getexif()
            values = dict(exif)
            values.update(exif.get_ifd(34665) if exif else {})
            icc = source.info.get('icc_profile')
            if preview: source.draft('RGB', (1400,1400))
            oriented = ImageOps.exif_transpose(source)
            if oriented.mode == 'CMYK' and icc:
                profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB'))
                image = ImageCms.profileToProfile(oriented, ImageCms.ImageCmsProfile(io.BytesIO(icc)), profile, outputMode='RGB')
                icc = profile.tobytes()
            elif 'A' in oriented.getbands() or 'transparency' in source.info:
                rgba = oriented.convert('RGBA')
                image = Image.new('RGB', rgba.size, 'white'); image.paste(rgba, mask=rgba.getchannel('A'))
            else:
                image = oriented.convert('RGB')
            if preview:
                image.thumbnail((1200,1200), Image.Resampling.LANCZOS)
                if icc:
                    try:
                        image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(icc)), ImageCms.createProfile('sRGB'), outputMode='RGB')
                    except (OSError, ValueError, ImageCms.PyCMSError):
                        pass
            return image, values, icc


def photo_values(path):
    # Read headers only so a repeated export can skip without decoding full-size pixels.
    with Image.open(path) as source:
        exif=source.getexif();values=dict(exif)
        values.update(exif.get_ifd(34665) if exif else {})
        # Pillow exposes UTF-8 EXIF ASCII fields as Latin-1; preserve the text.
        if isinstance(values.get(270),str):
            try:values[270]=values[270].encode('latin1').decode('utf-8')
            except (UnicodeEncodeError,UnicodeDecodeError):pass
        return values


def caption(values, row, options):
    parts = []
    if 'date' in options['fields']:
        date = values.get(36867) or values.get(36868) or row.get('capture_time')
        if date:
            date = str(date).strip().replace('T',' ')
            if len(date) >= 10: date = date[:10].replace(':','-') + date[10:19]
            parts.append(date)
    if 'camera' in options['fields']:
        model = values.get(272) or row.get('device')
        if model: parts.append(str(model).strip())
    if 'location' in options['fields'] and row.get('watermark_place'):
        parts.append(row['watermark_place'])
    if 'parameters' in options['fields']:
        settings = []
        for key, kind in ((37386,'focal'),(33437,'aperture'),(33434,'shutter'),(34855,'iso')):
            try: value = float(values[key])
            except (KeyError, TypeError, ValueError, ZeroDivisionError): continue
            if not math.isfinite(value) or value <= 0: continue
            settings.append(f'{value:g}mm' if kind == 'focal' else f'f/{value:g}' if kind == 'aperture'
                else (f'1/{round(1/value)}s' if value < 1 else f'{value:g}s') if kind == 'shutter' else f'ISO {value:g}')
        if settings: parts.append('  '.join(settings))
    if options.get('title'): parts.insert(0,options['title'])
    if options['custom']: parts.append(options['custom'])
    if options.get('signature'): parts.append(options['signature'])
    return '\n'.join(parts)


def render(image, text, options, logo=None):
    """Same proportional geometry for preview and full-size output; only paint the small patch."""
    if options['style'] in EXTENDED_STYLES:
        return render_layout(image,text,options,logo,font_path(options))
    width, height = image.size
    short = min(width,height)
    pad = max(2, round(short * .025))
    size = max(1, round(short * options['size'] / 100))
    font = ImageFont.truetype(str(font_path(options)), size)
    gap = max(1, round(size * .3))
    brand = logo.copy() if logo else None
    if brand:
        extent = max(1, round(short * options['logo_size'] / 100))
        scale=extent/max(brand.size)
        brand=brand.resize((max(1,round(brand.width*scale)),max(1,round(brand.height*scale))),Image.Resampling.LANCZOS)
    available = width - pad * 2 - (brand.width + pad if brand else 0)
    if available <= 0: raise ValueError('照片尺寸过小，无法放置水印。')
    draw = ImageDraw.Draw(Image.new('RGB',(1,1)))
    lines = []
    for paragraph in text.splitlines():
        line = ''
        for char in paragraph:
            if line and draw.textlength(line + char, font=font) > available:
                lines.append(line); line = ''
            line += char
        if line: lines.append(line)
    text_width = math.ceil(max((draw.textlength(line,font=font) for line in lines), default=0))
    line_height = max(1, sum(font.getmetrics()))
    text_height = len(lines) * (line_height + gap) - (gap if lines else 0)
    block_height = max(text_height, brand.height if brand else 0) + pad * 2
    if block_height > height * .55:
        raise ValueError('水印内容过多，请减少文字或调小字号。')
    corner = options['style'] == 'corner'
    patch_width = min(width, text_width + pad * 2 + (brand.width + pad if brand else 0)) if corner else width
    dark = options['theme'] == 'dark'
    alpha = round(255 * options['opacity']/100)
    bg = (18,22,28) if dark else (255,255,255)
    fg = (255,255,255) if dark else (25,30,36)
    patch = Image.new('RGBA',(patch_width,block_height), (*bg,0 if corner else alpha if options['style']=='bar' else 255))
    paint = ImageDraw.Draw(patch)
    x = pad
    if brand:
        if alpha != 255: brand.putalpha(brand.getchannel('A').point(lambda a: round(a*alpha/255)))
        patch.alpha_composite(brand,(pad,(block_height-brand.height)//2)); x += brand.width+pad
    y = (block_height-text_height)//2
    for line in lines:
        if corner:
            paint.text((x+1,y+1),line,font=font,fill=(*((0,0,0) if dark else (255,255,255)),alpha),anchor='lt')
        paint.text((x,y),line,font=font,fill=(*fg,alpha),anchor='lt')
        y += line_height+gap
    if options['style'] == 'frame':
        output = Image.new('RGB',(width,height+block_height),bg); output.paste(image,(0,0)); output.paste(patch,(0,height),patch)
    else:
        output = image.copy()
        x = width-patch_width if corner and options['position'].endswith('right') else 0
        y = 0 if corner and options['position'].startswith('top') else height-block_height
        output.paste(patch,(x,y),patch)
    return output


def metadata(path, cancel):
    return json.loads(exiftool(['-j','-G1','-n', *['-'+t for t in COPY_TAGS], str(path)], cancel))[0]


def copy_metadata(source, target, dimensions, cancel):
    description=append_marker(photo_values(source).get(270),WATERMARK)
    # One process copies tags, then returns both readbacks as JSON (avoid three startups per photo).
    with tempfile.TemporaryDirectory(prefix='watermark-marker-') as folder:
        text=Path(folder)/'description.txt';text.write_bytes(description.encode('utf-8'))
        before, after = json.loads(exiftool(['-q','-overwrite_original', '-TagsFromFile', str(source), *['-'+t for t in COPY_TAGS],
            '-EXIF:ImageDescription<='+str(text), '-EXIF:Orientation#=1', f'-EXIF:ExifImageWidth#={dimensions[0]}',
            f'-EXIF:ExifImageHeight#={dimensions[1]}', str(target), '-execute',
            '-charset','filename=UTF8','-j','-G1','-n', *['-'+t for t in COPY_TAGS],str(source),str(target)], cancel))
    if any(k != 'SourceFile' and after.get(k) != v for k,v in before.items()):
        raise ValueError('拍摄信息或 GPS 复制校验失败，未发布水印照片。')
    if photo_values(target).get(270)!=description:raise ValueError('水印处理标识未保留，未发布照片。')


class WatermarkService:
    def __init__(self, catalog):
        self.catalog = catalog
        self.lock = threading.Lock()

    def enrich(self, rows):
        """Local names only; no map calls in preview or export."""
        with closing(self.catalog.connect()) as db:
            result = []
            for original in rows:
                row = dict(original)
                gps = db.execute("SELECT location_name FROM gps_jobs WHERE media_id=? AND status='complete' ORDER BY rowid DESC LIMIT 1", (row['id'],)).fetchone()
                row['watermark_place'] = (gps['location_name'] if gps else '') or row.get('place') or ''
                result.append(row)
            return result

    def import_photos(self, identifier):
        with closing(self.catalog.connect()) as db:
            return [dict(r) for r in db.execute('''SELECT DISTINCT m.* FROM media m JOIN import_items i ON i.media_id=m.id
                WHERE i.import_id=? AND i.status='complete' AND m.kind='photo' ORDER BY m.current_path''', (identifier,))]

    def _update(self, identifier, **values):
        with closing(self.catalog.connect()) as db, db:
            db.execute('UPDATE watermark_jobs SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', [*values.values(),identifier])

    def _finish(self, job):
        output = Path(job['output_path'])
        info, meta = output.stat(), read_metadata(output,'photo')
        with closing(self.catalog.connect()) as db, db:
            source = db.execute('SELECT * FROM media WHERE id=?',(job['media_id'],)).fetchone()
            if source is None: raise ValueError('原素材记录缺失，保留待恢复记录。')
            indexed = db.execute('SELECT id FROM media WHERE path_key=?',(path_key(output),)).fetchone()
            output_id = indexed['id'] if indexed else job['output_media_id']
            row = dict(source)
            row.update(meta, id=output_id, path_key=path_key(output), current_path=str(output), observed_name=output.name,
                original_name=source['original_name'] or job['source_name'], inferred_original=None, imported_at=None,
                size=info.st_size,mtime_ns=info.st_mtime_ns,content_hash=job['output_hash'],watermark_revision=job['output_hash'],
                color_revision='',first_indexed_at=now(),last_seen_at=now(),availability='present')
            db.execute(f'INSERT OR IGNORE INTO media ({",".join(row)}) VALUES ({",".join("?" for _ in row)})',list(row.values()))
            db.execute('UPDATE media SET watermark_revision=?,content_hash=?,original_name=coalesce(original_name,?) WHERE id=?',
                       (job['output_hash'],job['output_hash'],row['original_name'],output_id))
            db.execute("UPDATE watermark_jobs SET output_media_id=?,status='complete',completed_at=?,error=NULL WHERE id=?",(output_id,now(),job['id']))

    def _recover(self, job, cancel):
        import task_telemetry as telemetry
        telemetry.journal('watermark',job['id']);telemetry.phase('恢复上次水印成品')
        root = Path(job['target_root']).resolve(strict=True)
        output, temp = beneath(job['output_path'],root), beneath(job['temp_path'],root)
        if temp.name != f'.mediaorganizer-watermark-{job["id"]}.jpg' or temp.parent != output.parent or output.suffix.lower() != '.jpg':
            raise ValueError('水印恢复路径不匹配。')
        if output.exists():
            if digest(output,cancel) != job['output_hash']: raise ValueError('水印目标已被其他文件占用，未覆盖。')
        else:
            if not temp.is_file() or digest(temp,cancel) != job['output_hash']: raise ValueError('待恢复的水印文件缺失或校验失败。')
            if cancel.is_set(): raise Cancelled()
            publish_without_replace(temp,output)
        self._finish(job)
        temp.unlink(missing_ok=True)

    def _process(self, row, root, options, logo, logo_hash, font_hash, cancel):
        import task_telemetry as telemetry
        telemetry.phase('核对原照片')
        source = Path(row['current_path'])
        if source.is_symlink() or not source.is_file(): raise ValueError('源照片不可用。')
        source = source.resolve(strict=True)
        stat = source.stat()
        if (stat.st_size,stat.st_mtime_ns) != (row['size'],row['mtime_ns']): raise ValueError('照片已变化，请重新扫描。')
        fingerprint = digest(source,cancel)
        with closing(self.catalog.connect()) as db:
            if row.get('watermark_revision') or db.execute("SELECT 1 FROM watermark_jobs WHERE output_hash=? AND status='complete' LIMIT 1",(fingerprint,)).fetchone():
                return 'skipped'
            if db.execute("SELECT 1 FROM gps_jobs WHERE media_id=? AND status IN ('writing','ready') LIMIT 1",(row['id'],)).fetchone():
                raise ValueError('定位尚未收尾，请先恢复定位任务。')
        values = photo_values(source)
        if marked(values.get(270),WATERMARK):return 'skipped'
        image = None
        try:
            text = caption(values,row,options)
            if not text and logo is None: raise ValueError('这张照片没有所选信息，请添加自定义文字或 Logo。')
            recipe = dict(options, logo_hash=logo_hash, font_hash=font_hash, engine=1, rendered_text=text)
            recipe.pop('logo',None)
            # Empty new text slots must not change legacy recipes or recovery lookup.
            for key in ('title','signature'):
                if not recipe[key]: recipe.pop(key)
            settings_json = json.dumps(recipe,ensure_ascii=False,sort_keys=True)
            settings_hash = hashlib.sha256(settings_json.encode()).hexdigest()
            with closing(self.catalog.connect()) as db:
                prior = [dict(r) for r in db.execute('''SELECT * FROM watermark_jobs WHERE input_hash=? AND settings_hash=?
                    AND target_root=? AND status IN ('complete','ready','rendering') ORDER BY rowid DESC''',(fingerprint,settings_hash,str(root)))]
            for job in prior:
                if job['status']=='ready':
                    self._recover(job,cancel); return 'recovered'
                if job['status']=='complete' and Path(job['output_path']).is_file() and digest(job['output_path'],cancel)==job['output_hash']:
                    return 'skipped'
                if job['status']=='rendering':
                    temp = beneath(job['temp_path'],root)
                    if temp.name != f'.mediaorganizer-watermark-{job["id"]}.jpg': raise ValueError('中断记录路径异常。')
                    temp.unlink(missing_ok=True)
                    self._update(job['id'],status='failed',error='上次处理中断，重新导出。')
            identifier = uuid.uuid4().hex
            output = unused_path(root/(safe_component(source.stem,120)+'_watermark.jpg'))
            temp = root/f'.mediaorganizer-watermark-{identifier}.jpg'
            if output.resolve()==source: raise ValueError('水印目标不能覆盖原照片。')
            job = dict(id=identifier,media_id=row['id'],output_media_id=uuid.uuid4().hex,
                source_path=str(source),source_name=source.name,output_path=str(output),target_root=str(root),temp_path=str(temp),
                created_at=now(),status='rendering',input_hash=fingerprint,settings_hash=settings_hash,
                settings_json=settings_json,rendered_text=text)
            with closing(self.catalog.connect()) as db, db:
                db.execute(f'INSERT INTO watermark_jobs ({",".join(job)}) VALUES ({",".join("?" for _ in job)})',list(job.values()))
            telemetry.journal('watermark', identifier)
            try:
                telemetry.phase('渲染照片水印')
                image, _, icc = read_photo(source)
                if shutil.disk_usage(root).free < max(stat.st_size*2, image.width*image.height*4)+16*1024**2:
                    raise ValueError('目标磁盘剩余空间不足。')
                if cancel.is_set(): raise Cancelled()
                rendered = render(image,text,options,logo)
                try:
                    dimensions = rendered.size
                    with temp.open('xb') as stream:
                        rendered.save(stream,format='JPEG',quality=options['quality'],subsampling=0,icc_profile=icc)
                    telemetry.phase('校验水印与拍摄信息')
                    copy_metadata(source,temp,dimensions,cancel)
                    with Image.open(temp) as check:
                        check.load()
                        if check.size != dimensions or check.getexif().get(274)!=1: raise ValueError('水印成品尺寸或方向校验失败。')
                finally:
                    rendered.close()
                telemetry.phase('复核原照片')
                if source.stat().st_size != stat.st_size or source.stat().st_mtime_ns != stat.st_mtime_ns or digest(source,cancel)!=fingerprint:
                    raise ValueError('导出期间原照片发生变化，未发布成品。')
                os.utime(temp,ns=(stat.st_atime_ns,stat.st_mtime_ns))
                with temp.open('rb+') as stream: os.fsync(stream.fileno())
                telemetry.phase('校验成品指纹')
                job['output_hash'] = digest(temp,cancel)
                if cancel.is_set(): raise Cancelled()
                self._update(identifier,status='ready',output_hash=job['output_hash']);job['status']='ready'
                telemetry.phase('保存水印照片')
                publish_without_replace(temp,output)
                self._finish(job)
                return 'complete'
            except BaseException as exc:
                if job['status']!='ready':
                    temp.unlink(missing_ok=True)
                    self._update(identifier,status='failed',error='已停止' if isinstance(exc,Cancelled) else str(exc))
                else:
                    self._update(identifier,error='待恢复  '+str(exc))
                raise
        finally:
            if image is not None:image.close()

    def run(self, rows, target_root, options, *, cancel=None, progress=None, pause=None):
        options = normalize(options)
        root = Path(target_root).expanduser().resolve()
        if not str(target_root).strip(): raise ValueError('请选择水印照片的保存文件夹。')
        if root==self.catalog.database.parent.resolve() or self.catalog.database.resolve().is_relative_to(root):
            raise ValueError('水印照片请保存到独立的媒体文件夹。')
        cancel = cancel or threading.Event()
        if not self.lock.acquire(False): raise ValueError('已有水印任务运行中。')
        logo = None
        result = dict(complete=0,skipped=0,errors=[],stopped=False,recovered=0)
        try:
            if cancel.is_set(): result['stopped']=True;return result
            root.mkdir(parents=True,exist_ok=True)
            logo = load_logo(options['logo'])
            logo_hash = hashlib.sha256(logo.tobytes()+str(logo.size).encode()).hexdigest() if logo else ''
            font_hash = digest(font_path(options),cancel)
            rows = self.enrich(rows)
            for index, row in enumerate(rows):
                def report(phase, processed):
                    if progress: progress(dict(filename=Path(row['current_path']).name, path=row['current_path'], phase=phase, encoder='', processed=processed, total=len(rows), index=index+1))
                while pause and not pause.wait(.2) and not cancel.is_set(): pass
                if cancel.is_set(): result['stopped']=True;break
                if row['kind']!='photo' or Path(row['current_path']).suffix.lower() not in PHOTO_TYPES:
                    result['skipped']+=1;report('跳过', index+1);continue
                with closing(self.catalog.connect()) as db:
                    previous = db.execute('SELECT coalesce(max(rowid),0) FROM watermark_jobs').fetchone()[0]
                try:
                    report('导出照片水印', index)
                    status = self._process(row,root,options,logo,logo_hash,font_hash,cancel)
                    result['skipped' if status=='skipped' else 'complete']+=1
                    if status=='recovered': result['recovered']+=1
                except Cancelled:
                    result['stopped']=True;break
                except Exception as exc:
                    result['errors'].append(Path(row['current_path']).name+'    '+str(exc))
                    # Preflight failures (unreadable photos, missing captions) also belong in the same timeline.
                    with closing(self.catalog.connect()) as db, db:
                        recorded = db.execute('SELECT 1 FROM watermark_jobs WHERE rowid>? AND media_id=? LIMIT 1',(previous,row['id'])).fetchone()
                        if not recorded:
                            identifier=uuid.uuid4().hex
                            recipe=json.dumps(options,ensure_ascii=False,sort_keys=True)
                            db.execute('''INSERT INTO watermark_jobs(id,media_id,output_media_id,source_path,source_name,
                                output_path,target_root,temp_path,created_at,status,input_hash,settings_hash,settings_json,rendered_text,error)
                                VALUES(?,?,?,?,?,?,?,?,?,'failed','',?,?,'',?)''',
                                (identifier,row['id'],uuid.uuid4().hex,row['current_path'],Path(row['current_path']).name,
                                 str(root/(safe_component(Path(row['current_path']).stem,120)+'_watermark.jpg')),str(root),
                                 str(root/f'.mediaorganizer-watermark-{identifier}.jpg'),now(),
                                 hashlib.sha256(recipe.encode()).hexdigest(),recipe,str(exc)))
                report('已处理', index+1)
            return result
        finally:
            if logo: logo.close()
            self.lock.release()
