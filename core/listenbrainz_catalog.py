"""Optional free text discovery; another MusicBrainz index, not an audio vote."""
import hashlib
import os
import threading
import time
from pathlib import Path
import requests
from core.cache_utils import atomic_write_json,read_json_if_fresh

URL='https://labs.api.listenbrainz.org/recording-search/json'
CACHE_DIR=Path(__file__).resolve().parents[1]/'cache/listenbrainz_catalog'
_lock=threading.RLock()
_last=0.0
_session=requests.Session()
_session.headers.update({'User-Agent':'TagCleaner/0.14 (optional metadata discovery)','Accept':'application/json'})


def enabled():
    return os.getenv('TAGCLEANER_LISTENBRAINZ','0').lower().strip() in ('1','true','yes','on')


def search(artist,title,refresh=False):
    global _last
    query=' '.join(x for x in (artist,title) if x).strip()
    if not title or not query:return []
    path=CACHE_DIR/(hashlib.sha256(query.encode()).hexdigest()+'.json')
    with _lock:
        cached=None if refresh else read_json_if_fresh(path,ttl_days=7)
        if isinstance(cached,dict) and isinstance(cached.get('results'),list):
            if cached['results'] or read_json_if_fresh(path,ttl_days=1):return cached['results']
        delay=1.1-(time.monotonic()-_last)
        if delay>0:time.sleep(delay)
        try:
            response=_session.get(URL,params={'query':query},timeout=(10,20))
            response.raise_for_status();payload=response.json()
        finally:_last=time.monotonic()
        if not isinstance(payload,list) or any(not isinstance(x,dict) or not x.get('recording_mbid') or not x.get('recording_name') or not x.get('artist_credit_name') for x in payload):
            raise ValueError('ListenBrainz: неполный ответ поиска; отсутствие записи не подтверждено.')
        rows=[{'provider':'listenbrainz','recording_id':x['recording_mbid'],'artist':x['artist_credit_name'],
            'title':x['recording_name'],'album':x.get('release_name',''),'release_id':x.get('release_mbid'),
            'duration':None,'discovery_only':True,'evidence_family':'musicbrainz',
            'url':'https://musicbrainz.org/recording/'+x['recording_mbid']} for x in payload]
        atomic_write_json(path,{'results':rows})
        return rows
