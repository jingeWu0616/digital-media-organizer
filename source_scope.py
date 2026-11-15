"""Source selections shared by scanning, import recovery and color processing."""
import json
from pathlib import Path


def roots(value, *, recursive=False, strict=False, preserve_paths=False):
    if isinstance(value, str) and value.startswith('['):
        value = json.loads(value)
    if isinstance(value, (str, Path)):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError('请选择素材文件夹。')
    result, originals = [], []
    for item in value:
        if not isinstance(item, (str, Path)) or not str(item).strip():
            raise ValueError('素材文件夹不能为空。')
        path = Path(item).resolve(strict=strict)
        if strict and not path.is_dir():
            raise ValueError('素材文件夹不可访问  ' + str(path))
        if path not in result:
            result.append(path); originals.append(Path(item).absolute())
    if recursive:
        keep = [i for i,p in enumerate(result) if not any(p != other and p.is_relative_to(other) for other in result)]
        result, originals = [result[i] for i in keep], [originals[i] for i in keep]
    return originals if preserve_paths else result


def encode(value):
    paths = [str(p) for p in roots(value)]
    return paths[0] if len(paths) == 1 else json.dumps(paths, ensure_ascii=False)


def display(value):
    return '\n'.join(str(p) for p in roots(value))
