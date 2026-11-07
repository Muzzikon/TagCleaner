"""Explicit deletion plans: exact ordinary files, no directory removal or recycle bin."""
import os
import stat
from pathlib import Path

from core.cache_utils import file_signature
from core.scanner import AUDIO_EXTENSIONS


def checked_file(value):
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError('Можно удалить только обычный аудиофайл по полному пути.')
    # Reject junctions and symlinks before resolving: resolving first loses the
    # evidence that a path was redirected outside the reviewed directory.
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Удаление через ссылку или точку перенаправления запрещено.')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Можно удалить только обычный аудиофайл.')
    return path, info


def file_plan(value, expected=None):
    path, info = checked_file(value)
    signature = file_signature(path)
    if expected is not None and signature != expected:
        raise ValueError('Файл изменился после анализа. Сначала обновите папку.')
    return {'path':str(path), 'file':path.name, 'signature':signature,
            'identity':[info.st_dev, info.st_ino, info.st_ctime_ns], 'bytes':info.st_size}


def selected_plan(items):
    rows = []
    seen = set()
    for item in items:
        if not item.get('analysis_file_signature'):
            raise ValueError('Сначала обновите папку, чтобы проверить выбранные файлы.')
        row = file_plan(item['path'], item['analysis_file_signature'])
        key = os.path.normcase(row['path'])
        if key not in seen:
            rows.append(row)
            seen.add(key)
    return rows


def execute_plan(rows, progress=None):
    results = []
    for index, row in enumerate(rows):
        if progress:
            progress('Безвозвратное удаление', index, len(rows), row['file'])
        result = {'path':row['path'], 'file':row['file'], 'success':False}
        try:
            current = file_plan(row['path'])
            if current['signature'] != row['signature'] or current['identity'] != row['identity']:
                raise ValueError('Файл изменился после подтверждения списка. Удаление отменено для этого файла.')
            Path(row['path']).unlink()
            result.update(success=True, bytes=row['bytes'])
        except Exception as error:
            result['error'] = str(error)
        results.append(result)
    if progress:
        progress('Удаление завершено', len(rows), len(rows), '')
    return {'results':results}
