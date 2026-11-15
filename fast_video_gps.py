"""Bounded MP4/MOV GPS patch: only a final moov, never shift media bytes.

QuickTime mdta keys/ilst format follows Apple's QuickTime File Format spec.
The service journals both metadata images before calling commit. No media
payload is copied; unknown layouts are rejected before any write.
"""
import ctypes
import hashlib
import json
import os
import struct
from contextlib import contextmanager

from import_service import Cancelled

GPS = b'com.apple.quicktime.location.ISO6709'
MAX_META = 32 * 1024 * 1024


class Unsupported(ValueError):
    pass


def box(kind, payload):
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def metadata_bytes(raw, exiftool, cancel):
    packet = box(b'ftyp', b'isom\0\0\0\0isom') + raw
    result = json.loads(exiftool(['-j','-G1','-n','-GPS*','-Location*','-'], cancel, input_data=packet))[0]
    if any(k.endswith(':Error') for k in result):
        raise ValueError('无法读取视频定位元数据')
    return result


def stream_digest(stream, cancel):
    from task_telemetry import progress
    stream.seek(0, 2)
    total, done = stream.tell(), 0
    stream.seek(0)
    digest = hashlib.sha256()
    while True:
        if cancel.is_set(): raise Cancelled()
        data = stream.read(4*1024*1024)
        if not data: return digest.hexdigest()
        digest.update(data)
        done += len(data)
        progress(done, total)


def children(data):
    offset = 0
    result = []
    while offset < len(data):
        if len(data)-offset < 8:
            raise Unsupported('元数据结构不完整')
        size, kind = struct.unpack_from('>I4s', data, offset)
        if size < 8 or offset+size > len(data):
            raise Unsupported('元数据使用了不支持的长度结构')
        result.append((kind, data[offset:offset+size]))
        if len(result) > 10000:
            raise Unsupported('元数据条目过多，请选择保护写入')
        offset += size
    return result


def edit_meta(raw, value):
    prefix = b'\0'*4 if raw[8:12] == b'\0'*4 else b''
    parts = children(raw[8+len(prefix):])
    handlers = [v for k,v in parts if k == b'hdlr']
    if len(handlers) != 1 or handlers[0][16:20] != b'mdta':
        raise Unsupported('不支持的定位元数据类型')
    keys = [v for k,v in parts if k == b'keys']
    lists = [v for k,v in parts if k == b'ilst']
    if len(keys) != 1 or len(lists) != 1 or len(keys[0]) < 16 or keys[0][8:12] != b'\0'*4:
        raise Unsupported('定位键结构不支持快速写入')
    entries = children(keys[0][16:])
    if len(entries) != int.from_bytes(keys[0][12:16], 'big'):
        raise Unsupported('定位键数量不一致')
    matches = [i+1 for i,(k,v) in enumerate(entries) if k == b'mdta' and v[8:] == GPS]
    if len(matches) > 1:
        raise Unsupported('定位键重复')
    if matches:
        index = matches[0]
        new_keys = keys[0]
    else:
        index = len(entries)+1
        new_keys = box(b'keys', b'\0'*4 + struct.pack('>I', index) + keys[0][16:] + box(b'mdta', GPS))
    tag = struct.pack('>I', index)
    items = children(lists[0][8:])
    if sum(k == tag for k,v in items) > 1:
        raise Unsupported('定位数据重复')
    updated = box(tag, box(b'data', struct.pack('>II', 1, 0) + value))
    new_list = box(b'ilst', b''.join(updated if k == tag else v for k,v in items)
                   + (b'' if any(k == tag for k,v in items) else updated))
    return box(b'meta', prefix + b''.join(new_keys if k == b'keys' else new_list if k == b'ilst' else v for k,v in parts))


def edit_moov(raw, lat, lon):
    return edit_moov_value(raw, f'{lat:+011.7f}{lon:+012.7f}/'.encode('ascii'))


def edit_moov_value(raw, value):
    """Preserve an existing ISO6709 string, including altitude and precision."""
    if not isinstance(value, bytes) or not value or len(value) > 4096:
        raise Unsupported('定位内容不支持快速保留')
    parts = children(raw[8:])
    if sum(k == b'mvhd' for k,v in parts) != 1 or not any(k == b'trak' for k,v in parts):
        raise Unsupported('缺少完整电影结构，请选择保护写入')
    if any(k in (b'cmov', b'mvex', b'rmra') for k,v in parts):
        raise Unsupported('压缩、分片或引用视频不支持快速写入')
    metas = [v for k,v in parts if k == b'meta']
    if len(metas) > 1:
        raise Unsupported('多个电影元数据区不支持快速写入')
    if metas:
        meta = edit_meta(metas[0], value)
    else:
        meta = edit_meta(box(b'meta', box(b'hdlr', b'\0'*8 + b'mdta' + b'\0'*13)
                              + box(b'keys', b'\0'*8) + box(b'ilst', b'')), value)
    payload = b''.join(meta if k == b'meta' else v for k,v in parts) + (b'' if metas else meta)
    if len(payload)+8 < len(raw):
        payload += box(b'free', b'\0'*(len(raw)-len(payload)-8))
    return box(b'moov', payload)


def gps_value(raw):
    """Read the original UTF-8 Keys payload without ExifTool's ValueConv."""
    metas = [v for k,v in children(raw[8:]) if k == b'meta']
    if len(metas) != 1:
        raise Unsupported('定位元数据区不唯一')
    meta = metas[0]
    prefix = 4 if meta[8:12] == b'\0'*4 else 0
    parts = children(meta[8+prefix:])
    keys = [v for k,v in parts if k == b'keys']
    lists = [v for k,v in parts if k == b'ilst']
    if len(keys) != 1 or len(lists) != 1 or len(keys[0]) < 16 or keys[0][8:12] != b'\0'*4:
        raise Unsupported('定位键结构不支持快速保留')
    entries = children(keys[0][16:])
    if len(entries) != int.from_bytes(keys[0][12:16], 'big'):
        raise Unsupported('定位键数量不一致')
    matches = [i+1 for i,(k,v) in enumerate(entries) if k == b'mdta' and v[8:] == GPS]
    if len(matches) != 1:
        raise Unsupported('定位键缺失或重复')
    items = [v for k,v in children(lists[0][8:]) if k == struct.pack('>I', matches[0])]
    if len(items) != 1:
        raise Unsupported('定位值缺失或重复')
    data = children(items[0][8:])
    if len(data) != 1 or data[0][0] != b'data' or data[0][1][8:16] != struct.pack('>II', 1, 0):
        raise Unsupported('定位值编码不支持快速保留')
    value = data[0][1][16:]
    if not value or len(value) > 4096:
        raise Unsupported('定位内容不支持快速保留')
    return value


def read_moov(stream, *, require_tail=False):
    stream.seek(0, 2)
    length = stream.tell()
    offset = 0
    atoms = []
    while offset < length:
        stream.seek(offset)
        header = stream.read(8)
        if len(header) != 8:
            raise Unsupported('视频结构不完整')
        size, kind = struct.unpack('>I4s', header)
        width = 8
        if size == 1:
            extended = stream.read(8)
            if len(extended) != 8: raise Unsupported('视频长度不完整')
            size = int.from_bytes(extended, 'big'); width = 16
        if size < width or offset+size > length:
            raise Unsupported('视频长度结构不支持快速写入')
        atoms.append((kind,offset,size,width))
        if len(atoms) > 10000: raise Unsupported('视频结构过于复杂')
        offset += size
    if require_tail and (not atoms or atoms[-1][0] != b'moov' or atoms[-1][3] != 8):
        raise Unsupported('元数据不在文件末尾，请选择保护写入')
    if sum(k == b'moov' for k, *_ in atoms) != 1 or not any(k == b'mdat' for k,*_ in atoms) or any(k in (b'moof',b'mfra',b'sidx') or (require_tail and k == b'uuid') for k,*_ in atoms):
        raise Unsupported('分片或特殊视频结构不支持快速写入')
    _, offset, size, width = next(atom for atom in atoms if atom[0] == b'moov')
    if width != 8: raise Unsupported('元数据长度结构不支持快速保留')
    if size > MAX_META: raise Unsupported('元数据过大，请选择保护写入')
    stream.seek(offset)
    before = stream.read(size)
    if len(before) != size: raise Unsupported('视频元数据读取不完整')
    return offset, before


def plan_value(stream, value):
    offset, before = read_moov(stream, require_tail=True)
    after = edit_moov_value(before, value)
    if len(after) > MAX_META: raise Unsupported('新元数据过大')
    return offset, before, after


def plan(stream, lat, lon):
    return plan_value(stream, f'{lat:+011.7f}{lon:+012.7f}/'.encode('ascii'))


@contextmanager
def locked_file(path):
    """Deny other writers and renames for the complete Windows operation."""
    if os.name != 'nt':
        raise Unsupported('快速写入目前仅支持 Windows')
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    name = os.path.abspath(path)
    name = '\\\\?\\UNC\\' + name[2:] if name.startswith('\\\\') else '\\\\?\\' + name
    handle = create(name, 0xC0000000, 1, None, 3, 0x80, None)  # read/write, share READ only
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, 'r+b', buffering=0) as stream:
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise Unsupported('快速写入不支持多个硬链接')
        yield stream


def commit(stream, offset, after):
    # Do not honor cancellation inside this small critical section. A failed
    # write leaves a durable local journal for explicit retry/recovery.
    stream.seek(offset)
    remaining = memoryview(after)
    while remaining:
        written = stream.write(remaining)
        if not written:
            raise OSError('定位元数据未写入完成')
        remaining = remaining[written:]
    stream.truncate(offset + len(after))
    os.fsync(stream.fileno())
    stream.seek(offset)
    if stream.read(len(after)+1) != after:
        raise OSError('定位元数据回读不一致，需要恢复')


def restore_mtime(stream, nanoseconds):
    """Set through the locked handle; avoid a reopen and deferred close time."""
    import msvcrt
    from ctypes import wintypes
    ticks = nanoseconds // 100 + 116444736000000000
    value = wintypes.FILETIME(ticks & 0xffffffff, ticks >> 32)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = kernel.SetFileTime
    fn.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.FILETIME)]
    fn.restype = wintypes.BOOL
    if not fn(msvcrt.get_osfhandle(stream.fileno()), None, None, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())


def validate_recovery(stream, offset, before, after, fingerprint, cancel):
    """Prove unchanged payload, then accept only an old/new/partial patch."""
    stream.seek(0, 2)
    length = stream.tell()
    if not offset+len(before) <= length <= offset+len(after):
        raise ValueError('素材长度已改变，停止自动恢复')
    stream.seek(0)
    digest = hashlib.sha256()
    remaining = offset
    while remaining:
        if cancel.is_set(): raise Cancelled()
        data = stream.read(min(4*1024*1024, remaining))
        if not data: raise ValueError('素材读取不完整')
        digest.update(data); remaining -= len(data)
    digest.update(before)
    if digest.hexdigest() != fingerprint:
        raise ValueError('视频内容已被外部修改，停止自动恢复')
    current = stream.read(MAX_META+1)
    # One forward overwrite can leave an after-prefix followed by old bytes.
    common = 0
    for a, b in zip(current, after):
        if a != b: break
        common += 1
    if current != after and current[common:] != before[common:]:
        raise ValueError('元数据不是本次写入的中断状态，停止自动恢复')
