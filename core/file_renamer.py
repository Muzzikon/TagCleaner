"""Explicit same-directory renames; never rewrite audio or metadata tags."""
import os
import re
import uuid
from pathlib import Path

from core.cache_utils import atomic_write_json, file_signature
from core.tag_writer import backup_original, restore_file_times


_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$', re.I)
_FILENAME_COLON = '\uff1a'
_FILENAME_QUESTION = '\uff1f'


def validate_filename(name, suffix):
    if not isinstance(name, str) or not name or name != name.strip():
        raise ValueError('Введите имя файла без пробелов по краям.')
    name.encode('utf-8', errors='strict')
    if _INVALID.search(name) or name.endswith('.') or name in ('.', '..'):
        raise ValueError('Имя содержит запрещённые Windows символы: < > : " / \\ | ? * или точку в конце. В имени файла вместо : и ? можно использовать ： и ？. В аудиотегах оставьте обычные : и ?.')
    if _RESERVED.fullmatch(name.split('.')[0].rstrip(' ')):
        raise ValueError('Это имя зарезервировано Windows.')
    if len(name.encode('utf-16-le')) // 2 > 255:
        raise ValueError('Имя файла слишком длинное (максимум 255 символов UTF-16).')
    if Path(name).suffix.casefold() != suffix.casefold():
        raise ValueError('Расширение аудиофайла менять нельзя: сохраните '+suffix)
    return name


def suggested_filename(data):
    """Use saved tags only, not uncertain recognition proposals or draft values."""
    artist = str(data.get('artist') or '').strip()
    title = str(data.get('title') or '').strip()
    source = Path(data['path'])
    if not artist or not title:
        return source.name
    # Fullwidth lookalikes are filename-only substitutes, never tag values.
    replacements = {':':_FILENAME_COLON, '?':_FILENAME_QUESTION}
    stem = _INVALID.sub(lambda match: replacements.get(match.group(0), '_'),
                        artist+' - '+title).strip(' .')
    stem = re.sub(_FILENAME_COLON+r'\s+', _FILENAME_COLON, stem)
    if _RESERVED.fullmatch(stem.split('.')[0].rstrip(' ')):
        stem = '_'+stem
    # Truncate by UTF-16 units without splitting an astral character.
    budget = 255-len(source.suffix.encode('utf-16-le'))//2
    while len(stem.encode('utf-16-le'))//2 > budget:
        stem = stem[:-1]
    stem = stem.rstrip(' .')
    return validate_filename(stem+source.suffix, source.suffix)


def build_rename_operation(data, name):
    source = Path(data['path'])
    if source.is_symlink() or not source.is_file():
        raise ValueError('Переименование доступно для обычного существующего аудиофайла.')
    expected = data.get('analysis_file_signature')
    if not expected or file_signature(source) != expected:
        raise ValueError('Файл изменился после анализа. Перепроверьте этот трек перед переименованием.')
    validate_filename(name, source.suffix)
    if source.name == name:
        raise ValueError('Файл уже имеет это имя.')
    target = source.with_name(name)
    case_only = os.name == 'nt' and os.path.normcase(str(source)) == os.path.normcase(str(target))
    if os.path.lexists(target) and not case_only:
        raise ValueError('В этой папке уже есть файл с таким именем. Выберите другое имя.')
    return {'path':str(source), 'file':source.name, 'field':'filename', 'current':source.name,
            'proposed':name, 'status':'MANUAL', 'expected_file_signature':expected}


def _rename_no_replace(source, target):
    """Windows rename refuses existing targets; POSIX link/unlink does the same."""
    if os.name == 'nt':
        if os.path.normcase(str(source)) == os.path.normcase(str(target)):
            temporary = source.with_name('.tagcleaner-rename-'+uuid.uuid4().hex+source.suffix)
            os.rename(source, temporary)
            try:
                os.rename(temporary, target)
            except BaseException:
                os.rename(temporary, source)
                raise
        else:
            os.rename(source, target)
        return
    # Same directory / filesystem only. Unsupported hard links fail safely.
    os.link(source, target)
    try:
        os.unlink(source)
    except BaseException:
        if os.path.samefile(source, target):
            os.unlink(target)
        raise


def execute_rename(operation, backup_root):
    source = Path(operation['path'])
    target = source.with_name(operation['proposed'])
    result = {'path':str(source), 'old_path':str(source), 'new_path':str(target),
              'success':False, 'renamed':False, 'operations':[operation], 'error':None}
    moved = False
    manifest = None
    try:
        checked = build_rename_operation({'path':str(source), 'analysis_file_signature':operation.get('expected_file_signature')}, operation['proposed'])
        if checked['current'] != operation.get('current'):
            raise ValueError('Исходное имя изменилось после предпросмотра.')
        stat = source.stat()
        backup = backup_original(source, backup_root)
        result['backup'] = str(backup)
        if file_signature(source) != operation['expected_file_signature']:
            raise ValueError('Файл изменился во время подготовки резервной копии.')
        manifest = Path(backup_root)/('rename_'+uuid.uuid4().hex+'.json')
        atomic_write_json(manifest, {**result, 'stage':'prepared', 'mtime_ns':stat.st_mtime_ns})
        _rename_no_replace(source, target)
        moved = True
        if target.stat().st_size != stat.st_size:
            raise RuntimeError('Размер файла изменился при переименовании.')
        restore_file_times(target, stat)
        result.update(success=True, renamed=True, verified=True, path=str(target),
                      mtime_preserved=True, mtime_before_ns=stat.st_mtime_ns,
                      mtime_after_ns=target.stat().st_mtime_ns)
    except Exception as error:
        result['error'] = str(error)
        if moved:
            try:
                _rename_no_replace(target, source)
                restore_file_times(source, stat)
                result['rolled_back'] = True
            except Exception as rollback_error:
                result.update(path=str(target), renamed=True, rollback_error=str(rollback_error))
    if manifest is not None:
        try:
            atomic_write_json(manifest, {**result, 'stage':'finished'})
        except Exception as error:
            result['journal_warning'] = str(error)
    return result


def preview_renames(results, indices, name=None):
    """Read actual saved tags, never substitute a proposal or unsaved draft."""
    from core.scanner import scan_file
    from ui.editor import draft_matches_current
    operations, skipped = [], []
    for index in dict.fromkeys(indices):
        data = results[index]
        try:
            if data.get('_ui_draft_warning'):
                raise ValueError('Черновик требует проверки. Откройте редактор этого трека.')
            fresh = scan_file(data['path'])
            if fresh['analysis_file_signature'] != data.get('analysis_file_signature'):
                raise ValueError('Файл изменился после анализа. Перепроверьте этот трек.')
            if data.get('_ui_draft') and not draft_matches_current(data, fresh):
                raise ValueError('В черновике есть незаписанные изменения. Сначала откройте «Посмотреть изменения» и подтвердите запись тегов.')
            if name is None and (not fresh.get('artist') or not fresh.get('title')):
                raise ValueError('В записанных тегах не хватает исполнителя или названия.')
            proposed = name if name is not None else suggested_filename(fresh)
            operation = build_rename_operation(fresh, proposed)
            operation['index'] = index
            operations.append(operation)
        except Exception as error:
            skipped.append({'file':data.get('file'), 'path':data['path'], 'reason':str(error)})
    # Detect collisions even on a case-sensitive development filesystem.
    targets = {}
    for operation in operations:
        target = str(Path(operation['path']).with_name(operation['proposed'])).casefold()
        targets.setdefault(target, []).append(operation)
    duplicates = {id(o) for group in targets.values() if len(group)>1 for o in group}
    accepted = []
    for operation in operations:
        if id(operation) in duplicates:
            skipped.append({'file':operation['file'], 'path':operation['path'],
                            'reason':'Несколько выбранных треков получают одинаковое имя.'})
        else:
            accepted.append(operation)
    return accepted, skipped


def refresh_after_rename(data, outcome, drafts, written_store):
    """Retarget live data and durable user records, preserving tag-write revision."""
    import copy
    from core.cache_utils import _revision_path, read_json_if_fresh
    from ui.editor import path_key
    old_key = path_key(data)
    old_revision = read_json_if_fresh(_revision_path(data['path']), ttl_days=None)
    # Update the live path first, even if an ancillary state write fails.
    data.update(path=outcome['new_path'], file=Path(outcome['new_path']).name)
    warnings = []
    try:
        if old_revision:
            atomic_write_json(_revision_path(data['path']), old_revision)
    except Exception as error:
        warnings.append('Маркер записи: '+str(error))
    signature = file_signature(data['path'])
    data['analysis_file_signature'] = signature
    approval=data.get('_ui_audit_approval')
    if approval and approval.get('signature')==outcome['operations'][0]['expected_file_signature']:
        approval['signature']=signature
    if data.get('_ui_draft') and data['_ui_draft'].get('signature') == outcome['operations'][0]['expected_file_signature']:
        data['_ui_draft']['signature'] = signature
    new_key = path_key(data)
    for store in (drafts, written_store):
        if store is None: continue
        try:
            updated = dict(store.items)
            record = copy.deepcopy(updated.pop(old_key, None))
            # Only migrate a record bound to this exact source signature.
            if record and record.get('signature') == outcome['operations'][0]['expected_file_signature']:
                record['signature'] = signature
                approval=record.get('audit_approval')
                if approval and approval.get('signature')==outcome['operations'][0]['expected_file_signature']:
                    approval['signature']=signature
                updated[new_key] = record
            atomic_write_json(store.path, {'schema':1, 'items':updated})
            store.items = updated
        except Exception as error:
            warnings.append(store.path.name+': '+str(error))
    return warnings
