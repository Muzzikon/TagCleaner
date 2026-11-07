"""Small, source-documented supplement for editions missing from search APIs."""
import json
from pathlib import Path

from functools import lru_cache
from copy import deepcopy


def _title_key(title,artist=''):
    from core.naming import canonical_track_title
    from core.matcher import clean_title
    from core.audd_matcher import normalize_text
    from core.title_identity import comparison_title
    return normalize_text(clean_title(comparison_title(artist,title)))


@lru_cache(maxsize=2)
def _indexed_catalog(path,mtime_ns,size):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    index={}
    if data.get('schema')==1:
        for context in data.get('editions',[]):
            index.setdefault(_title_key(context.get('track_title') or ''),[]).append(context)
    return index


def find(artist,title):
    from core.release_discovery import same_track
    path=Path(__file__).resolve().parent.parent/'data/verified_releases.json'
    try:
        stat=path.stat()
        candidates=_indexed_catalog(str(path),stat.st_mtime_ns,stat.st_size).get(_title_key(title,artist),[])
        return [deepcopy(c) for c in candidates if any(same_track(
            artist,title,credit,c.get('track_title','')) for credit in
            [c.get('track_artist_credit',''),*(c.get('track_artist_aliases') or [])])]
    except (OSError,ValueError,TypeError):return []

def warnings(data):
    """Recording checks are review-only, not aliases between different audio."""
    from core.release_credits import key,authority
    from core.versioning import version_base_title
    from core.naming import canonical_track_title
    title=key(version_base_title(canonical_track_title(data.get('title') or '')))
    duration=data.get('duration')
    result=[]
    if title in ('cracks','cracks begin to show') and duration:
        artist=key(data.get('artist'))
        relevant=any(n in artist for n in ('freestylers','flux pavilion','belle humble'))
        # Broad ranges only flag the conspicuous old/new recording swap.
        if relevant and ((title=='cracks' and 192<=float(duration)<=205 and 'flux pavilion' in key(data.get('title')))
                         or (title=='cracks begin to show' and 272<=float(duration)<=290)):
            result.append('Cracks (Flux Pavilion Remix), около 4:41, и Cracks Begin To Show (2026), около 3:18–3:19, — разные каталожные записи. Длительность и название расходятся; проверьте аудио, название и конкретное издание вручную. Автозамена между ними запрещена.')
    roles=authority(data,data.get('artist') or '',data.get('title') or '')
    if roles and roles.get('conflict'):result.append(roles['reason'])
    return result
