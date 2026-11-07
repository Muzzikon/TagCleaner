"""Local folder discovery and reversible, same-filesystem file removal."""
import copy
import json
import os
import uuid
from pathlib import Path
from core.cache_utils import atomic_write_json, file_signature
from core.file_renamer import _rename_no_replace
from core.scanner import AUDIO_EXTENSIONS

TRASH_DIR = '.tagcleaner-trash'


def path_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def discover_audio(roots):
    found = {}
    for value in roots:
        root = Path(value)
        if not root.is_dir():
            raise ValueError('Папка недоступна; прежний список сохранён: '+str(root))
        if TRASH_DIR in [p.casefold() for p in root.parts]:
            raise ValueError('Корзину TagCleaner нельзя выбрать как музыкальную папку.')
        def fail(error): raise error
        for parent, dirs, files in os.walk(root, onerror=fail, followlinks=False):
            dirs[:] = [d for d in dirs if d.casefold()!=TRASH_DIR and not (Path(parent)/d).is_symlink()]
            for name in files:
                path = Path(parent)/name
                if path.suffix.casefold() in AUDIO_EXTENSIONS and not path.is_symlink():
                    found[path_key(path)] = str(path.resolve())
    return sorted(found.values(), key=str.casefold)


def reconcile(files, results, roots, progress=None):
    cached={}
    def key_for(path):
        value=str(path)
        if value not in cached:cached[value]=path_key(path)
        return cached[value]
    if progress:progress('Ищем аудиофайлы в папке',0,0,'')
    discovered = discover_audio(roots) if roots else [str(p) for p in files if Path(p).is_file()]
    actual = {key_for(p):p for p in discovered}
    previous = {key_for(p):p for p in files}
    ordered = [actual.pop(key) for key in previous if key in actual]
    ordered += list(actual.values())
    existing = {key_for(p) for p in ordered}
    retained, changed = [], []
    seen = set()
    for index,original in enumerate(results):
        if progress and index%100==0:progress('Сверяем файлы',index,len(results),original.get('file',''))
        key = key_for(original['path'])
        if key not in existing or key in seen: continue
        seen.add(key)
        item = dict(original)
        try:
            if file_signature(item['path']) != item.get('analysis_file_signature'):
                item['_ui_write_recheck_required'] = True
                item['_ui_draft_warning'] = 'Файл изменён вне приложения. Перепроверьте этот трек; прежние правки сохранены.'
                changed.append(item['path'])
        except OSError:
            item['_ui_write_recheck_required'] = True
            changed.append(item['path'])
        retained.append(item)
    known = {key_for(d['path']) for d in retained}
    pending = [p for p in ordered if key_for(p) not in known]
    return {'files':ordered, 'retained':retained, 'pending':pending,
            'added':[p for p in ordered if key_for(p) not in previous],
            'removed':[p for p in files if key_for(p) not in existing], 'changed':changed}


class TrashStore:
    def __init__(self, app_root):
        self.root = Path(app_root)/'ui_runtime/trash'

    def _save(self, manifest):
        atomic_write_json(self.root/(manifest['id']+'.json'), manifest)

    def delete(self, items, progress=None):
        manifest = {'schema':1, 'id':uuid.uuid4().hex, 'items':[]}
        outcome, prepared = [], []
        for index,item in enumerate(items):
            if progress:progress('Подготовка удаления',index,len(items),item.get('file',''))
            source = Path(item['path'])
            result = {'path':str(source), 'file':source.name, 'success':False}
            outcome.append(result)
            try:
                if not source.exists():
                    result.update(success=True, already_missing=True)
                    continue
                if source.is_symlink() or not source.is_file():
                    raise ValueError('Можно удалить только обычный аудиофайл.')
                signature = file_signature(source)
                if not item.get('analysis_file_signature') or signature != item['analysis_file_signature']:
                    raise ValueError('Файл изменился после анализа. Сначала перепроверьте этот трек.')
                folder = source.parent/TRASH_DIR
                if folder.is_symlink(): raise ValueError('Папка корзины не должна быть ссылкой.')
                folder.mkdir(exist_ok=True)
                target = folder/(uuid.uuid4().hex+source.suffix)
                manifest['items'].append({'path':str(source), 'trash_path':str(target),
                    'signature':signature, 'restored':False, 'item':copy.deepcopy(item)})
                prepared.append((source,target,signature,result))
            except Exception as error:
                result['error'] = str(error)
        # Persist all recovery records before moving any bytes. One linear write
        # also keeps large selections from repeatedly serializing the full batch.
        if prepared:
            try:self._save(manifest)
            except Exception as error:
                for _,_,_,result in prepared:result['error']='Не удалось сохранить журнал корзины: '+str(error)
                return {'id':manifest['id'], 'results':outcome}
        for index,(source,target,signature,result) in enumerate(prepared):
            if progress:progress('Перенос в корзину',index,len(prepared),source.name)
            try:
                if file_signature(source) != signature:raise ValueError('Файл изменился во время подготовки удаления.')
                _rename_no_replace(source,target)
                result.update(success=True,trash_path=str(target))
            except Exception as error:result['error']=str(error)
        if progress:progress('Перенос завершён',len(prepared),len(prepared),'')
        return {'id':manifest['id'], 'results':outcome}

    def latest(self):
        if not self.root.exists(): return None
        for path in sorted(self.root.glob('*.json'), key=lambda p:p.stat().st_mtime_ns, reverse=True):
            try:
                manifest=json.loads(path.read_text(encoding='utf-8'))
                if any(not r.get('restored') and not r.get('purged') and Path(r['trash_path']).is_file() for r in manifest['items']):
                    return manifest['id']
            except (OSError,ValueError,KeyError,TypeError):continue
        return None

    def purge_plan(self):
        """Only files recorded in our manifests, at their exact trash location."""
        from core.permanent_delete import file_plan
        rows, warnings, seen = [], [], set()
        for journal in sorted(self.root.glob('*.json')):
            try:
                manifest = json.loads(journal.read_text(encoding='utf-8'))
                for record in manifest['items']:
                    if record.get('restored') or record.get('purged'):
                        continue
                    source, target = Path(record['path']), Path(record['trash_path'])
                    if (not source.is_absolute() or not target.is_absolute()
                            or target.parent != source.parent/TRASH_DIR
                            or target.suffix.lower() != source.suffix.lower()
                            or len(target.stem) != 32
                            or any(c not in '0123456789abcdef' for c in target.stem)):
                        raise ValueError('Неверный путь файла в журнале корзины: '+str(target))
                    if not target.exists() and not target.is_symlink():
                        continue
                    row = file_plan(target)
                    expected = record['signature']
                    if any(row['signature'][key] != expected[key] for key in ('size','mtime_ns')):
                        warnings.append('Файл в корзине изменился и пропущен: '+str(target))
                        continue
                    key = path_key(target)
                    if key in seen:
                        continue
                    seen.add(key)
                    row.update(file=source.name, journal=str(journal))
                    rows.append(row)
            except (OSError,ValueError,KeyError,TypeError) as error:
                warnings.append(journal.name+': '+str(error))
        return rows, warnings

    def purge(self, rows, progress=None):
        from core.permanent_delete import execute_plan
        outcome = execute_plan(rows, progress)
        successful = {r['path']:r for r in outcome['results'] if r['success']}
        journals = {row['journal'] for row in rows if row['path'] in successful}
        for journal in journals:
            try:
                manifest = json.loads(Path(journal).read_text(encoding='utf-8'))
                for record in manifest['items']:
                    if record['trash_path'] in successful:
                        record['purged'] = True
                        record.pop('item', None)
                atomic_write_json(Path(journal), manifest)
            except Exception as error:
                for row in rows:
                    if row['journal'] == journal and row['path'] in successful:
                        successful[row['path']]['warning'] = 'Файл удалён; журнал не обновлён: '+str(error)
        return outcome

    def restore(self, batch_id, progress=None):
        if not isinstance(batch_id,str) or len(batch_id)!=32 or any(c not in '0123456789abcdef' for c in batch_id):
            raise ValueError('Некорректный номер корзины.')
        manifest=json.loads((self.root/(batch_id+'.json')).read_text(encoding='utf-8'))
        outcome=[]
        records=[r for r in manifest['items'] if not r.get('restored') and not r.get('purged')]
        for index,record in enumerate(records):
            if progress:progress('Восстановление',index,len(records),Path(record['path']).name)
            source=Path(record['trash_path']);target=Path(record['path'])
            result={'path':str(target),'file':target.name,'success':False}
            try:
                if not source.is_file() or source.is_symlink():raise ValueError('Файл не найден в корзине.')
                stat=source.stat();signature=record['signature']
                if stat.st_size!=signature['size'] or stat.st_mtime_ns!=signature['mtime_ns']:
                    raise ValueError('Файл в корзине изменился; автоматическое восстановление отменено.')
                target.parent.mkdir(parents=True,exist_ok=True)
                _rename_no_replace(source,target)
                record['restored']=True;result['success']=True
                if record.get('item'):result['item']=record['item']
            except Exception as error:result['error']=str(error)
            outcome.append(result)
        try:self._save(manifest)
        except Exception as error:
            for result in outcome:
                if result['success']:result['warning']='Файл восстановлен; журнал не обновлён: '+str(error)
        if progress:progress('Восстановление завершено',len(records),len(records),'')
        return outcome
