"""Explicit relocation of verified written tracks; never overwrite an existing path."""
import copy
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from core.cache_utils import file_signature
from ui.editor import current_values


def journal_line(stream, payload):
    stream.write(json.dumps({'time':datetime.now().astimezone().isoformat(), **payload}, ensure_ascii=True)+'\n')
    stream.flush()
    os.fsync(stream.fileno())


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024), b''): h.update(block)
    return h.digest()


def eligible(data):
    return bool(data.get('_ui_written') and not any(data.get(k) for k in
        ('_ui_draft', '_ui_draft_warning', '_ui_write_recheck_required')))


def move_one(data, folder, written_store, journal):
    if not eligible(data): raise ValueError('Сначала успешно запишите теги и завершите ручные правки этого трека.')
    if written_store is None: raise ValueError('Отметки записи недоступны; перенос заблокирован.')
    source = Path(data['path'])
    if source.is_symlink(): raise ValueError('Перенос символических ссылок не поддерживается.')
    source = source.resolve(strict=True)
    target = folder / source.name
    if os.path.normcase(str(source)) == os.path.normcase(str(target.resolve())):
        raise ValueError('Файл уже находится в выбранной папке.')
    if target.exists() or target.is_symlink(): raise FileExistsError('В папке уже есть файл с таким именем. Исходник оставлен на месте.')
    signature = file_signature(source)
    if not signature or signature != data.get('analysis_file_signature'):
        raise ValueError('Файл изменился после записи. Перепроверьте его перед переносом.')
    from core.scanner import scan_file
    if current_values(scan_file(source)) != current_values(data):
        raise ValueError('Текущие теги отличаются от записанных. Перепроверьте трек.')
    original_stat = source.stat()
    owned = False
    journal_line(journal, {'stage':'copying','source':str(source),'target':str(target),
                          'mtime_ns':original_stat.st_mtime_ns})
    try:
        # xb is essential: no overwrite, including a name appearing after the check.
        with source.open('rb') as src, target.open('xb') as dst:
            owned = True
            source_hash = hashlib.sha256()
            for block in iter(lambda:src.read(1024*1024), b''):
                dst.write(block); source_hash.update(block)
            dst.flush(); os.fsync(dst.fileno())
        shutil.copystat(source, target)
        if digest(target) != source_hash.digest(): raise ValueError('Контрольная сумма копии не совпала.')
        if file_signature(source) != signature: raise ValueError('Исходник изменился во время копирования.')
        # Reads can modify atime; restore exact mtime/atime after verification.
        os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        if target.stat().st_mtime_ns != original_stat.st_mtime_ns:
            raise ValueError('Новая папка не сохраняет дату изменения с исходной точностью. Перенос отменён.')
        with target.open('r+b') as durable: os.fsync(durable.fileno())
        candidate = copy.deepcopy(data)
        candidate.update(path=str(target.resolve()), file=target.name,
                         analysis_file_signature=file_signature(target), _ui_moved_from=str(source))
        # Commit the new written mark before removing the only old copy.
        written_store.save_batch([candidate])
        journal_line(journal, {'stage':'verified_copy_ready','source':str(source),'target':str(target),
                              'sha256':source_hash.hexdigest(),'mtime_ns':original_stat.st_mtime_ns})
        source.unlink()
        return candidate
    except Exception:
        if owned:
            try: target.unlink()
            except OSError: pass  # Original still exists; report/journal identifies any leftover copy.
        raise


def move_tracks(results, indices, destination, written_store, reports_dir, progress=None):
    if not isinstance(destination, str) or not destination.strip(): raise ValueError('Выберите папку назначения.')
    folder = Path(destination).expanduser()
    if not folder.is_absolute(): raise ValueError('Укажите полный путь к папке назначения.')
    folder.mkdir(parents=True, exist_ok=True)
    folder = folder.resolve(strict=True)
    ids = list(dict.fromkeys(indices))
    if not ids or any(type(i) is not int or not 0 <= i < len(results) for i in ids):
        raise ValueError('Выберите записанные треки.')
    reports_dir = Path(reports_dir); reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    report = reports_dir / ('UI_move_'+stamp+'.txt')
    updates, outcomes = [], []
    with report.open('x', encoding='utf-8') as journal:
        for pos, index in enumerate(ids, 1):
            old_path = results[index]['path']
            try:
                candidate = move_one(results[index], folder, written_store, journal)
                results[index] = candidate
                updates.append({'index':index,'item':candidate,'old_path':old_path})
                outcome = {'path':old_path,'target':candidate['path'],'success':True}
            except Exception as error:
                outcome = {'path':old_path,'success':False,'error':str(error)}
            outcomes.append(outcome)
            try: journal_line(journal, {'stage':'finished',**outcome})
            except OSError: pass  # Relocation has already completed; never misreport it as a failure.
            if progress: progress(pos, len(ids), outcome)
    return {'results':outcomes,'updates':updates,'report':str(report),'destination':str(folder)}
