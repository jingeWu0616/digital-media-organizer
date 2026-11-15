"""Conservative ISO BMFF integrity checks for metadata-only MP4/MOV GPS edits.

Every media payload and non-location atom must match. Chunk addresses are
normalized to their mdat-relative positions; unsupported layouts fail closed.
"""
import hashlib
import json
import struct
from import_service import Cancelled

GPS_KEY=b'com.apple.quicktime.location.ISO6709'
GPS_COMPATIBILITY_REQUIRED = '定位保留需要兼容处理'
CONTAINERS={b'moov',b'trak',b'mdia',b'minf',b'stbl',b'edts',b'dinf',b'udta'}


def integrity(path,cancel):
    from task_telemetry import progress
    with open(path,'rb') as stream:
        stream.seek(0,2);length=stream.tell()
        hashed_bytes=0
        def read(start,size):
            stream.seek(start);value=stream.read(size)
            if len(value)!=size:raise ValueError('视频结构不完整，原文件保留。')
            return value
        def atoms(start,end):
            result=[]
            while start<end:
                if cancel.is_set():raise Cancelled()
                if end-start<8:raise ValueError('不支持的尾部数据，原文件保留。')
                size,kind=struct.unpack('>I4s',read(start,8));header=8
                if size==1:size=struct.unpack('>Q',read(start+8,8))[0];header=16
                elif size==0:size=end-start
                if size<header or start+size>end:raise ValueError('视频结构损坏，原文件保留。')
                result.append((kind,start+header,start+size));start+=size
            return result
        top=atoms(0,length);mdats=[(a,b) for k,a,b in top if k==b'mdat']
        if not mdats or sum(k==b'moov' for k,_,_ in top)!=1 or any(k in (b'moof',b'sidx',b'mfra') for k,_,_ in top):
            raise ValueError('暂不支持分片或无完整媒体数据的视频。')
        def hashed(start,end):
            nonlocal hashed_bytes
            digest=hashlib.sha256();stream.seek(start)
            while start<end:
                if cancel.is_set():raise Cancelled()
                data=stream.read(min(4*1024*1024,end-start))
                if not data:raise ValueError('读取视频失败。')
                digest.update(data);start+=len(data);hashed_bytes+=len(data)
                if len(data)>=1024*1024:progress(hashed_bytes,length)
            return digest.hexdigest()
        def offset(value):
            for i,(start,end) in enumerate(mdats):
                if start<=value<end:return (i,value-start)
            raise ValueError('视频引用外部或未知媒体区域，原文件保留。')
        def meta(start,end):
            prefix=read(start,4)
            # ISO meta has version/flags; QuickTime meta directly starts with hdlr.
            children=atoms(start+4 if prefix==b'\0'*4 else start,end);keys=[];entries=[];other=[]
            for k,a,b in children:
                if k==b'keys':
                    if b-a>16*1024*1024:raise ValueError('视频元数据过大。')
                    version,count=struct.unpack('>II',read(a,8))
                    if version:raise ValueError('未知位置键版本。')
                    cursor=a+8
                    for _ in range(count):
                        size,namespace=struct.unpack('>I4s',read(cursor,8))
                        if size<8 or cursor+size>b:raise ValueError('位置键结构损坏。')
                        keys.append((namespace,read(cursor+8,size-8)));cursor+=size
                    if cursor!=b:raise ValueError('位置键结构不完整。')
            for k,a,b in children:
                if k==b'ilst':
                    for tag,x,y in atoms(a,b):
                        index=int.from_bytes(tag,'big')
                        name=keys[index-1] if 0<index<=len(keys) else (b'four',tag)
                        if name==(b'mdta',GPS_KEY) or tag==b'\xa9xyz':continue
                        entries.append((name,hashed(x,y)))
                elif k not in (b'keys',b'hdlr',b'free'):other.append((k,hashed(a,b)))
            remaining=[k for k in keys if k!=(b'mdta',GPS_KEY)]
            if not entries and not remaining and not other:return None
            handlers=[hashed(a,b) for k,a,b in children if k==b'hdlr']
            return ('meta',remaining,entries,other,handlers)
        def walk(items,depth=0):
            if depth>20:raise ValueError('视频结构层级过深。')
            result=[]
            for k,a,b in items:
                if cancel.is_set():raise Cancelled()
                if k==b'free' or (k==b'wide' and a==b):continue
                if k==b'\xa9xyz':continue
                if k==b'meta':
                    value=meta(a,b)
                    if value:result.append(value)
                elif k in CONTAINERS:
                    children=walk(atoms(a,b),depth+1)
                    if children:result.append((k,children))
                elif k in (b'stco',b'co64'):
                    version,count=struct.unpack('>II',read(a,8));width=4 if k==b'stco' else 8
                    if version or b-a!=8+count*width:raise ValueError('无效视频块地址。')
                    digest=hashlib.sha256()
                    for i in range(count):
                        if cancel.is_set():raise Cancelled()
                        value=int.from_bytes(read(a+8+i*width,width),'big')
                        digest.update(struct.pack('>IQ',*offset(value)))
                    result.append(('chunks',count,digest.hexdigest()))
                else:result.append((k,b-a,hashed(a,b)))
            return result
        signature=walk(top)
        # ExifTool relocates mdat behind moov. Payload order stays fixed and all
        # chunk addresses above must still point to the exact same payload bytes.
        return ([item for item in signature if item[0]!=b'mdat'],
                [item for item in signature if item[0]==b'mdat'])


def metadata(path,exiftool,cancel):
    return json.loads(exiftool(['-j','-G1','-n','-GPS*','-Location*',str(path)],cancel))[0]


def write_verified(source,temp,lat,lon,exiftool,cancel):
    from task_telemetry import phase
    phase('核对原视频轨道')
    before=integrity(source,cancel)
    phase('写入临时视频')
    exiftool(['-o',str(temp),'-QuickTime:GPSCoordinates=',
              f'-Keys:GPSCoordinates={lat:+.7f}{lon:+.7f}/',str(source)],cancel,timeout=1800)
    phase('校验媒体与专有轨道')
    if integrity(temp,cancel)!=before:raise ValueError('视频数据或附加轨道校验不一致，原文件保留。')
    phase('回读定位')
    values=metadata(temp,exiftool,cancel)
    gps=values.get('Keys:GPSCoordinates','')
    import re
    numbers=re.findall(r'[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?',str(gps))
    if len(numbers)<2 or abs(float(numbers[0])-lat)>1e-6 or abs(float(numbers[1])-lon)>1e-6:
        raise ValueError('视频 GPS 回读校验失败，原文件保留。')


def preserve_keys_after_color(source,temp,cancel,mode='fast'):
    """Keep our precise location tag through FFmpeg's normal cover-safe writer.

    Only the color job's disposable encoded file is edited in place here.
    """
    from pathlib import Path
    from location_service import exiftool
    from task_telemetry import phase, detail
    from fast_video_gps import Unsupported, read_moov, gps_value, plan_value, locked_file, commit, metadata_bytes
    if mode not in ('fast','compatible'):
        raise ValueError('未知的定位保留方式')
    source=Path(source)
    temp=Path(temp)
    if (temp.resolve()==source.resolve() or temp.resolve().parent!=source.resolve().parent
            or temp.is_symlink() or temp.stat().st_nlink!=1
            or not temp.name.startswith('.mediaorganizer-color-') or temp.suffix!='.part'):
        raise ValueError('颜色临时文件路径不匹配。')
    phase('检查原片定位')
    def gps_fields(values):
        return {k:v for k,v in values.items() if not k.startswith('Composite:')
                and (':GPS' in k or ':Location' in k)}
    original=gps_fields(metadata(source,exiftool,cancel))
    expected=original.get('Keys:GPSCoordinates')
    if not original:return
    current=gps_fields(metadata(temp,exiftool,cancel))
    if expected is None:
        if any(current.get(k)!=v for k,v in original.items()):
            raise ValueError('原片定位格式未完整保留，原片不覆盖。')
        phase('定位校验通过');return
    if mode=='fast':
        writing=False
        try:
            with source.open('rb') as stream:
                _,source_moov=read_moov(stream)
            value=gps_value(source_moov)
            with locked_file(temp) as stream:
                if current.get('Keys:GPSCoordinates')==expected:
                    _,existing=read_moov(stream)
                    if gps_value(existing)==value:
                        if any(current.get(k)!=v for k,v in original.items()):
                            raise ValueError('原片定位信息未完整保留，原片不覆盖。')
                        phase('定位校验通过');return
                offset,before,after=plan_value(stream,value)
                phase('定位保留前完整性检查')
                signature=integrity(temp,cancel)
                if cancel.is_set():raise Cancelled()
                phase('快速保留定位')
                writing=True
                commit(stream,offset,after)
                detail(gps_metadata_bytes=len(after))
                phase('定位保留后完整性检查')
                if integrity(temp,cancel)!=signature:
                    raise ValueError('还原版视频数据或元数据变化，原片不覆盖。')
                phase('定位回读校验')
                _,saved=read_moov(stream,require_tail=True)
                actual=gps_fields(metadata_bytes(saved,exiftool,cancel))
                if gps_value(saved)!=value or any(actual.get(k)!=v for k,v in original.items()):
                    raise ValueError('还原版定位未完整保留，原片不覆盖。')
            return
        except Unsupported as exc:
            if writing:
                raise ValueError('快速保留定位后的结构校验失败，原片不覆盖。') from exc
            reason=str(exc).replace('，请选择保护写入','')
            raise ValueError(f'{GPS_COMPATIBILITY_REQUIRED}：{reason}。原片未修改，可在处理记录中选择兼容保留定位。') from exc
    phase('定位保留前完整性检查')
    before=integrity(temp,cancel)
    try:
        phase('兼容保留定位')
        try:
            with source.open('rb') as stream:value=gps_value(read_moov(stream)[1])
        except Unsupported:
            value=None
        gps_args=(['-Keys:GPSCoordinates#='+value.decode('utf-8')] if value is not None else
                  ['-tagsFromFile',str(source),'-Keys:GPSCoordinates'])
        exiftool(['-overwrite_original',*gps_args,str(temp)],cancel,timeout=1800)
        phase('定位保留后完整性检查')
        actual=gps_fields(metadata(temp,exiftool,cancel))
        if integrity(temp,cancel)!=before or any(actual.get(k)!=v for k,v in original.items()):
            raise ValueError('还原版定位或媒体校验失败，原片保留。')
        if value is not None:
            with temp.open('rb') as stream:saved=gps_value(read_moov(stream)[1])
            if saved!=value:raise ValueError('定位原始内容未完整保留，原片不覆盖。')
    finally:
        # ExifTool may leave this task-owned scratch file when cancelled.
        Path(str(temp)+'_exiftool_tmp').unlink(missing_ok=True)
