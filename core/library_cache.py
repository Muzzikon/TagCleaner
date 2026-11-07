"""Incremental local library snapshots, independent of audio recognition caches."""
import json
import sqlite3
import zlib
from contextlib import closing
from pathlib import Path

from core.cache_utils import file_signature
from core.run_state import DECISION_COMPAT_VERSION


class LibraryCache:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:db.execute('CREATE TABLE IF NOT EXISTS tracks (path TEXT PRIMARY KEY, data TEXT NOT NULL)')
        except sqlite3.Error:
            db.close()
            raise
        return db

    def save(self, records):
        rows = []
        for original in records:
            signature = original.get('analysis_file_signature')
            if not signature or original.get('_audit_error') or original.get('_ui_write_recheck_required'):
                continue
            data = {k:v for k,v in original.items() if k not in ('_ui_draft', '_ui_draft_warning', '_ui_audit')}
            data['_library_decision_version'] = DECISION_COMPAT_VERSION
            payload=json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
            rows.append((signature['path'], sqlite3.Binary(zlib.compress(payload,1))))
        if rows:
            with closing(self.connect()) as db, db:
                db.executemany('INSERT INTO tracks(path,data) VALUES (?,?) ON CONFLICT(path) '
                               'DO UPDATE SET data=excluded.data WHERE tracks.data!=excluded.data', rows)

    def load(self, files, progress=None):
        from core.scanner import scan_file
        from ui.editor import FIELDS
        results, fresh = [], []
        with closing(self.connect()) as db, db:
            for index, path in enumerate(files):
                if progress and index % 100 == 0:
                    progress(index, len(files), str(path))
                try:
                    signature = file_signature(path)
                    row = db.execute('SELECT data FROM tracks WHERE path=?', (signature['path'],)).fetchone()
                    try:
                        payload=row[0] if row else None
                        if isinstance(payload,bytes):payload=zlib.decompress(payload)
                        data = json.loads(payload) if payload is not None else None
                    except (ValueError, TypeError, zlib.error):
                        data = None
                    if not isinstance(data, dict) or data.get('analysis_file_signature') != signature:
                        previous=data if isinstance(data,dict) else {}
                        old_cover=previous.get('_ui_cover') or {}
                        old_signature=previous.get('analysis_file_signature') or {}
                        data = scan_file(path)
                        # Removing the generated cache also removes revision
                        # markers. Carry proven artwork history across that
                        # event only when the actual file stat is unchanged.
                        if (old_cover.get('changed') or (old_cover.get('backup') and old_cover.get('source'))) and all(
                                old_signature.get(k)==signature.get(k) for k in ('path','size','mtime_ns')):
                            data['_ui_cover']={**old_cover,'signature':signature,'candidates':[]}
                            data['_ui_cover'].pop('current_id',None)
                        data['change_plan'] = {'identity':'UNSCANNED', **{
                            f:{'current':str(data.get(f) or ''), 'proposed':str(data.get(f) or ''),
                               'status':'KEEP', 'reason':'Локальные теги. Распознавание ещё не запускалось.'}
                            for f in FIELDS}}
                        fresh.append(data)
                    # JSON decoding/scan_file already owns this record; copying
                    # hundreds of MB of provider evidence serves no isolation.
                    if data.get('_library_decision_version') != DECISION_COMPAT_VERSION:
                        plan=data.get('change_plan') or {}
                        if plan.get('identity') != 'UNSCANNED':
                            # Keep local tags and evidence, but never execute SAFE
                            # decisions produced by an earlier rule set.
                            data['change_plan']={'identity':'REVIEW', **{
                                f:{'current':str(data.get(f) or ''),'proposed':str(data.get(f) or ''),
                                   'recognition_proposed':(plan.get(f) or {}).get('proposed'),
                                   'status':'KEEP','reason':'Правила обновились. Старое решение сохранено для сравнения; перед автоматической записью нужен пересчёт.'}
                                for f in FIELDS}}
                    data['_library_decision_version'] = DECISION_COMPAT_VERSION
                    data['path']=str(path)
                    data['file']=Path(path).name
                    data.pop('_ui_audit', None)
                    data.pop('_ui_draft', None)
                    results.append(data)
                except sqlite3.Error:
                    raise
                except Exception as error:
                    # A malformed tag must not hide the rest of the folder.
                    results.append({'path':str(path), 'file':Path(path).name, '_audit_error':str(error),
                                    'change_plan':{'identity':'UNSCANNED'}})
        self.save(fresh)
        if progress:
            progress(len(files), len(files), '')
        return results
