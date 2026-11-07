"""Bound generated artwork/checkpoints; never remove music, decisions or backups."""
import json
import os
import re
import time
from pathlib import Path
from contextlib import closing
from core.cache_utils import atomic_write_json

ARTWORK_LIMIT = 512 * 1024 * 1024
ARTWORK_DAYS = 7


def safe_files(folder):
    folder = Path(folder)
    if not folder.exists() or folder.is_symlink() or folder.is_junction(): return
    for directory, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = [d for d in dirs if not Path(directory,d).is_symlink() and not Path(directory,d).is_junction()]
        for name in files:
            path = Path(directory,name)
            if not path.is_symlink() and path.resolve().is_relative_to(folder.resolve()): yield path


def available_artwork(state, root):
    result = dict(state or {})
    cache = Path(root)/'cache/covers'
    def exists(key): return isinstance(key,str) and re.fullmatch('[a-f0-9]{64}',key) and (cache/(key+'.image')).is_file()
    old = result.get('candidates',[])
    from core.cover_urls import _allowed_image_url
    def recoverable(c):
        return (isinstance(c.get('id'),str) and re.fullmatch('[a-f0-9]{64}',c['id']) and
                not c.get('upscale') and _allowed_image_url(c.get('image_url')) and
                isinstance(c.get('preview'),str) and c['preview'].startswith(('data:image/jpeg;base64,','data:image/png;base64,')))
    queued = (result.get('queued') or {}).get('choice_key')
    result['candidates'] = [{**c,'choice_key':artwork_choice_key(c)} for c in old
                            if exists(c.get('id')) or recoverable(c) or (queued and artwork_choice_key(c) == queued)]
    if not exists(result.get('current_id')): result.pop('current_id',None)
    if len(old)!=len(result['candidates']): result['message']='Срок хранения части вариантов истёк. Нажмите «Найти варианты», чтобы загрузить их снова.'
    return result


def artwork_choice_key(candidate):
    # Two editions can use the same bytes but carry different match evidence.
    from core.cache_utils import stable_digest
    return stable_digest({k:candidate.get(k) for k in ('id','source','release_id','release','artist','title','duration','needs_review','review_reason')})


def trim_artwork(root, *, limit=ARTWORK_LIMIT, max_age_days=ARTWORK_DAYS, protect=()):
    cache = Path(root)/'cache/covers';now=time.time();protected=set(protect)
    entries=[]
    if not cache.is_dir() or cache.is_symlink() or cache.is_junction() or not cache.resolve().is_relative_to(Path(root).resolve()):
        return {'removed_bytes':0,'removed_images':0,'remaining_bytes':0}
    # Only direct content-addressed images are disposable. Do not traverse or
    # resolve thousands of durable per-track JSON records on every search.
    with os.scandir(cache) as children:
        for entry in children:
            if not re.fullmatch(r'[a-f0-9]{64}\.image',entry.name):continue
            try:
                if not entry.is_file(follow_symlinks=False):continue
                stat=entry.stat(follow_symlinks=False);entries.append((stat.st_mtime,Path(entry.path),stat.st_size))
            except OSError:pass
    total=sum(size for _,_,size in entries);removed=0;count=0
    for mtime,path,size in sorted(entries):
        if path.stem in protected:continue
        if now-mtime<=max_age_days*86400 and total<=limit:continue
        try:path.unlink();removed+=size;total-=size;count+=1
        except OSError:pass
    return {'removed_bytes':removed,'removed_images':count,'remaining_bytes':total}


def compact_database(path, cutoff=None):
    import sqlite3
    path=Path(path)
    if not path.is_file() or path.is_symlink():return {'removed_rows':0,'reclaimed_bytes':0}
    before=path.stat().st_size;removed=0
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=2)) as db,db:
        db.execute('PRAGMA busy_timeout=2000')
        if cutoff is not None:
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in ('completed','stages'):
                if name in tables:
                    removed+=max(0,db.execute('DELETE FROM '+name+' WHERE updated_at<?',(cutoff,)).rowcount)
            db.commit()
        pages=db.execute('PRAGMA page_count').fetchone()[0]
        free=db.execute('PRAGMA freelist_count').fetchone()[0]
        if free>256 and free/max(1,pages)>.1:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('VACUUM')
    return {'removed_rows':removed,'reclaimed_bytes':max(0,before-path.stat().st_size)}


def maintain(root, *, force=False, protect=()):
    import sqlite3
    root=Path(root);marker=root/'cache/storage_maintenance.json';now=time.time()
    if not force:
        try:
            if now-json.loads(marker.read_text(encoding='utf-8')).get('at',0)<86400:return {'skipped':True}
        except (OSError,ValueError,TypeError):pass
    errors=[];result=trim_artwork(root,protect=protect)
    try:days=max(7,float(os.environ.get('TAGCLEANER_RESUME_DAYS','7')))
    except ValueError:days=7
    for path,cutoff in ((root/'cache/run_state.sqlite3',now-days*86400),(root/'ui_runtime/library.sqlite3',None)):
        try:result[path.name]=compact_database(path,cutoff)
        except (OSError,sqlite3.Error) as error:errors.append(path.name+': '+str(error))
    result.update(at=now,errors=errors)
    if not errors:atomic_write_json(marker,result)
    return result


def summary(root):
    import sqlite3
    root=Path(root);groups={}
    for path in safe_files(root/'cache'):
        try:
            key=path.relative_to(root/'cache').parts[0]
            row=groups.setdefault(key,{'bytes':0,'files':0});row['bytes']+=path.stat().st_size;row['files']+=1
        except OSError:pass
    databases=[]
    for path in (root/'cache/run_state.sqlite3',root/'ui_runtime/library.sqlite3'):
        if not path.is_file():continue
        try:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
                free=db.execute('PRAGMA freelist_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]
            databases.append({'name':path.name,'bytes':path.stat().st_size,'free_bytes':free})
        except (OSError,sqlite3.Error):pass
    return {'cache_bytes':sum(g['bytes'] for g in groups.values()),'groups':groups,'databases':databases,
            'artwork_limit_bytes':ARTWORK_LIMIT,'artwork_days':ARTWORK_DAYS,
            'policy':'Изображения: до 512 МиБ и 7 дней. Устаревшие этапы: срок продолжения проверки (не менее 7 дней). Музыка, резервные копии, ручные решения и аудиоотпечатки сохраняются.'}
