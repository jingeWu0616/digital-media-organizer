"""Bounded local JPEG previews. Original media are never recompressed."""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from PIL import Image, ImageOps

RAW_EXTS = {'.raw', '.arw', '.cr2', '.cr3', '.nef', '.dng', '.orf', '.rw2', '.raf'}


class ThumbnailCache:
    def __init__(self, folder, budget=512 * 1024 * 1024, edge=480):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self.edge = edge
        self.lock = threading.Lock()
        self.writes = 0
        self.byte_estimate = 0
        self.prune()

    def key(self, row):
        # Color replacement may preserve both byte size and mtime; its fingerprint changes.
        # Ordinary imports must keep their pre-move cache key, so only color revision enters here.
        revision = row.get('color_revision', '') if hasattr(row, 'get') else ''
        return hashlib.sha256((f"v1/{row['id']}/{row['size']}/{row['mtime_ns']}/{self.edge}" +
                               (f'/{revision}' if revision else '')).encode()).hexdigest()

    def lookup(self, row):
        path = self.folder / (self.key(row) + '.jpg')
        if path.is_file():
            # Updating only cache timestamps allows least-recently-used eviction.
            try:
                if time.time() - path.stat().st_mtime > 3600:
                    os.utime(path, None)
            except OSError:
                pass
            return path
        return None

    def ensure(self, row, cancel=None):
        cached = self.lookup(row)
        if cached:
            return cached
        if cancel and cancel.is_set():
            return None
        source = Path(row['current_path'])
        if row['kind'] == 'video':
            from color_service import command
            result = command([
                '-threads', '1', '-i', str(source), '-frames:v', '1',
                '-vf', f'scale={self.edge}:{self.edge}:force_original_aspect_ratio=decrease',
                '-f', 'image2pipe', '-vcodec', 'mjpeg', '-threads', '1', 'pipe:1'], cancel or threading.Event(),timeout=25)
            if not result:
                raise ValueError('无法生成视频缩略图')
            image = Image.open(io.BytesIO(result))
        elif source.suffix.lower() in RAW_EXTS:
            import rawpy
            with rawpy.imread(str(source)) as raw:
                preview = raw.extract_thumb()
                image = (Image.open(io.BytesIO(preview.data)) if preview.format == rawpy.ThumbFormat.JPEG
                         else Image.fromarray(preview.data))
        else:
            if source.suffix.lower() in {'.heic', '.heif'}:
                from pillow_heif import register_heif_opener
                register_heif_opener()
            image = Image.open(source)
        temp = self.folder / (uuid.uuid4().hex + '.tmp')
        try:
            with image:
                image.draft('RGB', (self.edge, self.edge))
                small = ImageOps.exif_transpose(image)
                small.thumbnail((self.edge, self.edge), Image.Resampling.LANCZOS)
                if small.mode in ('RGBA', 'LA', 'P'):
                    rgba = small.convert('RGBA')
                    background = Image.new('RGB', rgba.size, 'white')
                    background.paste(rgba, mask=rgba.getchannel('A'))
                    small = background
                elif small.mode != 'RGB':
                    small = small.convert('RGB')
                small.save(temp, format='JPEG', quality=78)
            if cancel and cancel.is_set():
                return None
            final = self.folder / (self.key(row) + '.jpg')
            os.replace(temp, final)  # Only replace our regenerable local cache.
            with self.lock:
                self.writes += 1
                self.byte_estimate += final.stat().st_size
                if self.byte_estimate > self.budget:
                    self.prune()
            return final if final.exists() else None
        finally:
            temp.unlink(missing_ok=True)

    def clear(self):
        count = 0
        with self.lock:
            for path in self.folder.glob('*.jpg'):
                # Only generated cache names, never arbitrary images in this folder.
                if len(path.stem) == 64 and all(c in '0123456789abcdef' for c in path.stem) and not path.is_symlink():
                    path.unlink(); count += 1
            self.byte_estimate = sum(p.stat().st_size for p in self.folder.glob('*.jpg') if p.is_file())
        return count

    def prune(self):
        files = []
        for path in self.folder.glob('*.jpg'):
            try:
                info = path.stat()
                files.append((info.st_mtime, info.st_size, path))
            except OSError:
                pass
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= self.budget:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                pass
        self.byte_estimate = total
