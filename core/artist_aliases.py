"""Search aliases established by one stable MBID, never by fuzzy name guessing."""
import json
import re
import threading
import hashlib
from pathlib import Path

_lock = threading.RLock()
_loaded = False
_names = {}
_ids = {}
ROOT_DIR = Path(__file__).resolve().parent.parent


def _key(value):
    return " ".join(re.findall(r"\w+", str(value or "").casefold()))


def register_credit(items):
    with _lock:
        _register(items,_names,_ids)


def _register(items,names,identities):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        artist = item.get("artist") or {}
        mbid = artist.get("id")
        if not mbid:
            continue
        values = [item.get("name"), artist.get("name")]
        values += [a.get("name") for a in artist.get("aliases") or [] if isinstance(a, dict)]
        for value in values:
            key = _key(value)
            if key:
                names.setdefault(key, set()).add(mbid)
                identities.setdefault(mbid, {})[key] = str(value)


def _source_snapshot():
    files=[];digest=hashlib.sha256()
    for folder in ('musicbrainz','musicbrainz_release_contexts'):
        for path in sorted((ROOT_DIR/'cache'/folder).glob('*.json')):
            try:
                stat=path.stat()
                digest.update(json.dumps((folder,path.name,stat.st_size,stat.st_mtime_ns),ensure_ascii=False).encode('utf-8'))
                files.append(path)
            except OSError:pass
    return files,digest.hexdigest()


def _index_checksum(names,identities):
    return hashlib.sha256(json.dumps({'names':names,'ids':identities},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


def _bootstrap():
    global _loaded
    with _lock:
        if _loaded:
            return
        paths,manifest=_source_snapshot()
        index=ROOT_DIR/'cache/artist_alias_index.json'
        names={};identities={};loaded=False
        try:
            cached=json.loads(index.read_text(encoding='utf-8'))
            if cached.get('version')==1 and cached.get('manifest')==manifest:
                raw_names=cached['names'];raw_ids=cached['ids']
                if cached.get('checksum')!=_index_checksum(raw_names,raw_ids):raise ValueError('Damaged index')
                if not isinstance(raw_names,dict) or not isinstance(raw_ids,dict):raise ValueError('Bad index')
                if any(not isinstance(k,str) or not isinstance(v,list) or any(not isinstance(m,str) for m in v) for k,v in raw_names.items()):raise ValueError('Bad names')
                if any(not isinstance(k,str) or not isinstance(v,dict) or any(not isinstance(n,str) or not isinstance(x,str) for n,x in v.items()) for k,v in raw_ids.items()):raise ValueError('Bad identities')
                names={k:set(v) for k,v in raw_names.items()};identities=raw_ids;loaded=True
        except (OSError,ValueError,KeyError,TypeError):pass
        if not loaded:
            for path in paths:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        _register(payload.get("artist-credit"),names,identities)
                    elif isinstance(payload, list):
                        for context in payload:
                            _register(context.get("track_artist_credit_items"),names,identities)
                except (OSError, ValueError, AttributeError, TypeError):
                    continue
            try:
                # Persist only disk-derived facts. Live registrations retain
                # their original session lifetime and are merged below.
                if _source_snapshot()[1]==manifest:
                    from core.cache_utils import atomic_write_json
                    packed_names={k:sorted(v) for k,v in names.items()}
                    atomic_write_json(index,{'version':1,'manifest':manifest,'names':packed_names,'ids':identities,'checksum':_index_checksum(packed_names,identities)})
            except (OSError,ValueError,TypeError):pass
        for name,mbids in names.items():_names.setdefault(name,set()).update(mbids)
        for mbid,values in identities.items():_ids.setdefault(mbid,{}).update(values)
        _loaded = True


def same_artist(left, right):
    _bootstrap()
    with _lock:
        a, b = _names.get(_key(left), set()), _names.get(_key(right), set())
        # Homonyms do not establish a rename; require one unambiguous ID.
        return bool(len(a) == len(b) == 1 and a == b)


def search_aliases(value):
    _bootstrap()
    with _lock:
        identities = _names.get(_key(value), set())
        if len(identities) != 1:
            return []
        return sorted(_ids[next(iter(identities))].values(), key=str.casefold)
