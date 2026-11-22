"""Mutagen bridge for Android documents. The caller keeps descriptors open."""
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from mutagen import File
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from core.cover_tags import pictures, set_front_cover
from core.local_cleanup import clean_local_text
from core.tag_access import SUPPORTED_EXTENSIONS, read_value, read_values, write_value

FIELDS = ('title', 'artist', 'album', 'albumartist', 'date', 'tracknumber', 'discnumber', 'remixer')
BUFFER_SIZE = 256 * 1024


class NamedReader(io.BufferedReader):
    def __init__(self, descriptor, name):
        super().__init__(io.FileIO(os.dup(descriptor), 'rb'))
        self.document_name = name

    @property
    def name(self):
        return self.document_name


def signature(descriptor):
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Хранилище не предоставило обычный аудиофайл. Скопируйте трек в папку «Музыка».')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def check_signature(descriptor, expected):
    if describe(descriptor) != expected:
        raise ValueError('Файл изменился. Откройте его заново перед записью.')


def describe(descriptor):
    return json.dumps(signature(descriptor))


def open_audio(source, easy=False):
    if not isinstance(source, Path):
        source.seek(0)
    audio = File(source, easy=easy)
    if audio is None:
        raise ValueError('Не удалось прочитать формат аудиофайла.')
    return audio


def fields(audio):
    return {field: read_value(audio, field) for field in FIELDS}


def inspect(descriptor, name, artwork_path=''):
    expected = signature(descriptor)
    with NamedReader(descriptor, name) as reader:
        audio = open_audio(reader)
        cover = next(iter(pictures(audio)), b'')
        details = fields(open_audio(reader, easy=True) if isinstance(audio, MP4) else audio)
        duration = getattr(audio.info, 'length', 0)
    if expected != signature(descriptor):
        raise ValueError('Файл изменился во время чтения.')
    if artwork_path and cover and len(cover) <= 16 * 1024 * 1024:
        Path(artwork_path).write_bytes(cover)
    return json.dumps({'tags': details, 'duration': duration, 'cover': bool(cover),
                       'signature': json.dumps(expected), 'modified': expected[3] // 1_000_000})


def cleanup(values):
    return json.dumps({name: clean_local_text(value) if name in ('title', 'artist', 'album', 'albumartist', 'remixer')
                       else value for name, value in json.loads(values).items()})


def backup_path(directory, uri):
    return Path(directory) / (hashlib.sha256(uri.encode()).hexdigest() + '.bak')


def copy_original(descriptor, name, destination):
    with NamedReader(descriptor, name) as source, destination.open('wb') as target:
        source.seek(0)
        shutil.copyfileobj(source, target, BUFFER_SIZE)
        target.flush()
        os.fsync(target.fileno())


def check_write_target(reader, writer, original):
    # Some document providers expose a different inode for read and write access.
    if os.path.samestat(os.fstat(reader), os.fstat(writer)):
        return
    with original.open('rb') as source, NamedReader(writer, str(original)) as target:
        target.seek(0)
        if hashlib.file_digest(source, 'sha256').digest() == hashlib.file_digest(target, 'sha256').digest():
            return
    raise ValueError('Хранилище открыло для записи другую версию файла. Используйте «Сохранить копию».')


def edit_copy(path, updates, artwork_path, mime, width, height):
    audio = open_audio(path)
    easy = open_audio(path, easy=True) if isinstance(audio, MP4) else audio
    original_cover = pictures(audio)
    original_tags = {key: read_values(easy, key) for key in FIELDS}
    changed = {key: value for key, value in updates.items()
               if key in FIELDS and read_value(easy, key) != value}
    blob = Path(artwork_path).read_bytes() if artwork_path else b''
    if changed:
        if easy.tags is None:
            easy.add_tags()
        for field, value in changed.items():
            write_value(easy, field, value)
        save_audio(easy, path)
    if blob and original_cover[:1] != [blob]:
        audio = open_audio(path)
        set_front_cover(audio, blob, mime, width, height)
        save_audio(audio, path)

    audio = open_audio(path)
    easy = open_audio(path, easy=True) if isinstance(audio, MP4) else audio
    for field, old in original_tags.items():
        if field in changed:
            valid = read_value(easy, field) == changed[field]
        else:
            valid = read_values(easy, field) == old
        if not valid:
            raise ValueError(f'Тег «{field}» не прошёл проверку после записи. Проверьте его формат.')
    covers = pictures(audio)
    if (blob and covers[:1] != [blob]) or (not blob and covers != original_cover):
        raise ValueError('Обложка не прошла проверку после записи.')
    return bool(changed or (blob and original_cover[:1] != [blob]))


def save_audio(audio, path):
    if isinstance(audio.tags, ID3) and audio.tags.version[:2] == (2, 3):
        audio.save(path, v2_version=3, v23_sep=None)
    else:
        audio.save(path)


def write_contents(descriptor, staged):
    expected_hash = hashlib.sha256()
    with staged.open('rb') as source, io.FileIO(os.dup(descriptor), 'rb+') as target:
        target.seek(0)
        for chunk in iter(lambda: source.read(BUFFER_SIZE), b''):
            expected_hash.update(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = target.write(remaining)
                if not written:
                    raise OSError('Хранилище прервало запись файла.')
                remaining = remaining[written:]
        target.truncate()
        os.fsync(target.fileno())
        target.seek(0)
        if hashlib.file_digest(target, 'sha256').digest() != expected_hash.digest():
            raise OSError('Записанный файл не совпал с проверенной копией.')


def restore_times(descriptor, times):
    os.utime(descriptor, ns=times)
    if os.fstat(descriptor).st_mtime_ns != times[1]:
        raise OSError('Хранилище изменило дату файла.')


def can_restore_times(descriptor, times):
    try:
        restore_times(descriptor, times)
    except OSError:
        return False
    return True


def commit(descriptor, staged, original, target_times, original_times):
    try:
        write_contents(descriptor, staged)
        if target_times is not None:
            restore_times(descriptor, target_times)
        os.fsync(descriptor)
    except Exception as error:
        try:
            write_contents(descriptor, original)
            if original_times is not None:
                restore_times(descriptor, original_times)
            os.fsync(descriptor)
        except Exception as rollback:
            raise OSError('Запись прервана. Используйте «Восстановить оригинал» из резервной копии.') from rollback
        raise OSError('Запись прервана. Исходный файл восстановлен.') from error


def edit(descriptor, name, expected, updates, artwork_path, mime, width, height,
         work_dir, backup_dir, uri, output_descriptor=-1, write_descriptor=-1, allow_new_date=False):
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError('Формат не поддерживается.')
    check_signature(descriptor, expected)
    writer = descriptor if write_descriptor == -1 else write_descriptor
    target_signature = signature(writer) if output_descriptor == -1 else None
    timestamps = os.fstat(descriptor)
    times = (timestamps.st_atime_ns, timestamps.st_mtime_ns)
    backup = backup_path(backup_dir, uri)
    # The backup and its staging file are on the same volume for atomic replacement.
    with tempfile.TemporaryDirectory(dir=work_dir) as directory:
        staged = Path(directory) / ('audio' + suffix)
        copy_original(descriptor, name, staged)
        changed = edit_copy(staged, json.loads(updates), artwork_path, mime, width, height)
        check_signature(descriptor, expected)
        if output_descriptor != -1:
            if os.path.samestat(os.fstat(descriptor), os.fstat(output_descriptor)):
                raise ValueError('Выберите другое имя для копии трека.')
            write_contents(output_descriptor, staged)
            return 2 if can_restore_times(output_descriptor, times) else 3
        if not changed:
            return 0
        with tempfile.TemporaryDirectory(dir=backup_dir, prefix='staging-') as backup_stage:
            original = Path(backup_stage) / 'original'
            copy_original(descriptor, name, original)
            os.utime(original, ns=times)
            check_write_target(descriptor, writer, original)
            check_signature(descriptor, expected)
            if signature(writer) != target_signature:
                raise ValueError('Файл изменился во время подготовки записи. Откройте его заново.')
            time_supported = can_restore_times(writer, times)
            if not time_supported and not allow_new_date:
                return 4
            os.replace(original, backup)
        target_times = times if time_supported else None
        commit(writer, staged, backup, target_times, target_times)
    return 1 if time_supported else 5


def restore(descriptor, name, expected, work_dir, backup_dir, uri, write_descriptor=-1):
    original = backup_path(backup_dir, uri)
    check_signature(descriptor, expected)
    writer = descriptor if write_descriptor == -1 else write_descriptor
    target_signature = signature(writer)
    current_stat = os.fstat(descriptor)
    original_stat = original.stat()
    current_times = (current_stat.st_atime_ns, current_stat.st_mtime_ns)
    original_times = (original_stat.st_atime_ns, original_stat.st_mtime_ns)
    with tempfile.TemporaryDirectory(dir=work_dir) as directory:
        current = Path(directory) / 'current'
        copy_original(descriptor, name, current)
        check_write_target(descriptor, writer, current)
        check_signature(descriptor, expected)
        if signature(writer) != target_signature:
            raise ValueError('Файл изменился во время подготовки восстановления. Откройте его заново.')
        time_supported = can_restore_times(writer, current_times)
        commit(writer, original, current, original_times if time_supported else None,
               current_times if time_supported else None)
    return time_supported
