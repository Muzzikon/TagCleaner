"""Explicit artwork choices, tied to a file snapshot and a catalog recording."""
import base64
import hashlib
import io
import re
import warnings
import time
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor
import threading

from mutagen import File
from mutagen.id3 import ID3, APIC, TextFrame
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover
from core.cache_utils import atomic_write_json, file_signature, stable_digest, mark_file_changed

MAX_IMAGE = 16 * 1024 * 1024
_analysis_lock=threading.RLock()
_analysis_cache={}
_image_load_lock=threading.RLock()


def pictures(audio):
    if audio is None:
        raise ValueError('Не удалось прочитать аудиофайл.')
    if isinstance(audio, FLAC):
        return [p.data for p in sorted(audio.pictures,key=lambda p:p.type!=3) if p.data]
    tags = audio.tags
    if tags is None:
        return []
    if isinstance(tags, ID3):
        return [p.data for p in sorted(tags.getall('APIC'),key=lambda p:p.type!=3) if p.data]
    if isinstance(audio, MP4):
        return [bytes(p) for p in tags.get('covr', []) if p]
    result = []
    decoded=[Picture(base64.b64decode(p)) for p in tags.get('metadata_block_picture', [])]
    for p in sorted(decoded,key=lambda p:p.type!=3):
        result.append(p.data)
    for p in tags.get('coverart', []):
        result.append(base64.b64decode(p))
    return [p for p in result if p]


def image_info(blob):
    return dict(_image_analysis(blob)[0])


def _image_analysis(blob):
    from PIL import Image
    if not blob or len(blob) > MAX_IMAGE:
        raise ValueError('Обложка должна быть JPEG или PNG размером до 16 МиБ.')
    key=hashlib.sha256(blob).hexdigest()
    with _analysis_lock:
        cached=_analysis_cache.get(key)
        if cached is not None:return cached
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(blob)) as im:
            if im.format not in ('JPEG', 'PNG') or im.width * im.height > 25000000:
                raise ValueError('Поддерживаются JPEG/PNG до 25 мегапикселей.')
            mime = 'image/jpeg' if im.format == 'JPEG' else 'image/png'
            width, height = im.size
            im.verify()
        with Image.open(io.BytesIO(blob)) as im:
            im.load()
            rgba=im.convert('RGBA')
            fingerprint=hashlib.sha256(str(rgba.size).encode()+rgba.tobytes()).hexdigest()
            rgba.close()
            im.thumbnail((180, 180))
            preview = io.BytesIO()
            im.convert('RGB').save(preview, 'JPEG', quality=82)
    info={'mime': mime, 'width': width, 'height': height,
          'preview':'data:image/jpeg;base64,'+base64.b64encode(preview.getvalue()).decode()}
    with _analysis_lock:
        if len(_analysis_cache)>=96:_analysis_cache.pop(next(iter(_analysis_cache)))
        _analysis_cache[key]=(info,fingerprint)
    return info,fingerprint


from core.cover_urls import ARTWORK_DOMAINS, _LASTFM_CDN, _allowed_image_url


def _download_once(url):
    import requests
    for _ in range(6):
        if isinstance(url,str) and url.startswith('http://') and _allowed_image_url('https://'+url[7:]):
            url='https://'+url[7:]
        if not _allowed_image_url(url):
            try:host=urlparse(url).hostname or 'не указан'
            except (ValueError,TypeError,AttributeError):host='некорректный адрес'
            raise ValueError('Недопустимый источник обложки: '+host[:120]+'. Домен или протокол не поддерживается.')
        with requests.get(url, timeout=(8, 18), stream=True, allow_redirects=False) as response:
            if response.status_code in (301,302,303,307,308):
                url=urljoin(url,response.headers.get('Location',''))
                # Legacy CAA indexes still link to HTTP. Upgrade only a trusted
                # artwork host and validate it again before making the request.
                if url.startswith('http://'):url='https://'+url[7:]
                continue
            if response.status_code == 404:return None
            response.raise_for_status()
            chunks=bytearray()
            for chunk in response.iter_content(65536):
                chunks.extend(chunk)
                if len(chunks)>MAX_IMAGE:raise ValueError('Обложка больше 16 МиБ.')
            blob=bytes(chunks);image_info(blob);return blob
    raise ValueError('Источник обложки вернул слишком много перенаправлений.')


def _download_retry(url):
    import requests
    for attempt in range(2):
        try:return _download_once(url)
        except requests.RequestException as error:
            response=getattr(error,'response',None)
            status=response.status_code if response is not None else None
            retry=(isinstance(error,(requests.Timeout,requests.ConnectionError)) and not isinstance(error,requests.exceptions.SSLError)) or status in (429,500,502,503,504)
            try:delay=max(.4,float(response.headers.get('Retry-After',.4))) if response is not None else .4
            except (ValueError,TypeError):delay=.4
            if attempt or not retry or delay>2:raise
            time.sleep(delay)


def download(url):
    """A second public Deezer CDN serves the same cover path, not another art."""
    import requests
    urls=[url]
    try:
        parsed=urlparse(url)
        if _allowed_image_url(url) and parsed.hostname in ('cdn-images.dzcdn.net','cdns-images.dzcdn.net','e-cdns-images.dzcdn.net') and re.fullmatch(r'/images/cover/[0-9a-fA-F]{32}/[^/]+',parsed.path):
            alternate='cdn-images.dzcdn.net' if parsed.hostname=='e-cdns-images.dzcdn.net' else 'e-cdns-images.dzcdn.net'
            urls.append(parsed._replace(netloc=alternate).geturl())
    except (ValueError,TypeError,AttributeError):pass
    failure=None
    for target in urls:
        try:
            blob=_download_retry(target)
            if blob is not None:return blob
        except requests.RequestException as error:
            response=getattr(error,'response',None)
            status=response.status_code if response is not None else None
            recoverable=status in (403,404,410,500,502,503,504) or (isinstance(error,(requests.Timeout,requests.ConnectionError)) and not isinstance(error,requests.exceptions.SSLError))
            if not recoverable:raise
            failure=error
    if failure is not None:raise failure
    return None


def artwork_error(error):
    """Keep a useful cause/host without dumping long CDN URLs into the panel."""
    import requests
    if isinstance(error,requests.HTTPError) and error.response is not None:
        status=error.response.status_code
        try:host=urlparse(error.response.url).hostname or 'источника'
        except (TypeError,ValueError):host='источника'
        if status==403:return f'Сервер {host} отказал в загрузке изображения (HTTP 403).'
        return f'Не удалось загрузить изображение с {host} (HTTP {status}).'
    if isinstance(error,requests.Timeout):return 'Истекло время ожидания изображения.'
    if isinstance(error,requests.ConnectionError):return 'Не удалось соединиться с сервером изображений.'
    return str(error)


def load_candidate_image(root,candidate,downloads=None):
    """Restore an evicted original without repeating catalog discovery."""
    from core.storage import touch_artwork, trim_artwork

    key=candidate.get('id','')
    if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{64}',key):raise ValueError('Обложка недоступна.')
    cache=Path(root)/'cache/covers';path=cache/(key+'.image')
    with _image_load_lock:
        damaged = False
        try:
            blob = path.read_bytes()
            if hashlib.sha256(blob).hexdigest() == key:
                touch_artwork(path)
                return blob
            damaged = True
        except (FileNotFoundError, PermissionError):
            pass
        url=candidate.get('image_url')
        if candidate.get('upscale') or not _allowed_image_url(url):raise ValueError('Оригинал обложки недоступен. Обновите варианты.')
        try:blob=(download if downloads is None else downloads)(url)
        except Exception as error:raise ValueError('Не удалось загрузить оригинал: '+artwork_error(error)) from None
        if not blob:raise ValueError('Оригинал больше не доступен. Обновите варианты.')
        # An updated remote image must never replace the user's selection.
        if hashlib.sha256(blob).hexdigest()!=key:raise ValueError('Изображение источника изменилось. Обновите варианты и выберите обложку заново.')
        image_info(blob)
        # The verified bytes are usable even if Windows refuses the cache write.
        temp_path = None
        try:
            if damaged or not path.is_file():
                cache.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=cache, suffix='.tmp', delete=False) as temporary:
                    temp_path = Path(temporary.name)
                    temporary.write(blob)
                os.replace(temp_path, path)
                trim_artwork(root, protect=[key])
        except OSError:
            pass
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return blob


def pixel_digest(blob):
    try:
        return _image_analysis(blob)[1]
    except Exception:
        return None


def compatible(data, artist, title, duration):
    from core.release_discovery import same_track
    from core.duration_policy import duration_difference
    # Search uses saved tags only: an uncommitted title is not an identity.
    if not same_track(data.get('artist', ''), data.get('title', ''), artist, title):
        return False
    try:
        difference = duration_difference(data.get('duration'), duration)
        return difference is not None and difference <= 5
    except (ValueError, TypeError):
        return False


def queued_image(root, path):
    """Read a single saved original without loading cover history or the queue."""
    database = Path(root).resolve() / 'ui_runtime/cover_history.sqlite3'
    key = os.path.normcase(str(Path(path).resolve()))
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        row = db.execute('SELECT image FROM cover_queue WHERE path=?', (key,)).fetchone()
    return row[0] if row else None


class CoverSession:
    def __init__(self, root):
        self.root = Path(root)
        self.cache = self.root / 'cache' / 'covers'
        self.history_path=self.root/'ui_runtime/cover_history.sqlite3'
        self._artifact_lock=threading.RLock()
        self._last_trim=0
        self._new_image_bytes=0
        self.history={}
        self.queue = {}
        self.queued_image_paths = set()
        if self.history_path.is_file():
            try:
                with closing(sqlite3.connect(self.history_path)) as db:
                    tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if 'cover_history' in tables:
                        self.history = {key: json.loads(value) for key, value in db.execute('SELECT path,payload FROM cover_history')}
                    if 'cover_queue' in tables:
                        for key, value, has_image in db.execute('SELECT path,payload,image IS NOT NULL FROM cover_queue'):
                            self.queue[key] = json.loads(value)
                            if has_image:
                                self.queued_image_paths.add(key)
            except (sqlite3.Error,ValueError):pass

    def set_queued(self, data, choice=None, *, original_blob=None, **options):
        """Keep the exact selection independently of search results and cache cleanup."""
        key = os.path.normcase(str(Path(data['path']).resolve()))
        state = data.get('_ui_cover') or {}
        if choice is None:
            if key in self.queue:
                with closing(sqlite3.connect(self.history_path)) as db, db:
                    db.execute('DELETE FROM cover_queue WHERE path=?', (key,))
                self.queue.pop(key)
                self.queued_image_paths.discard(key)
            state.pop('queued', None)
            return
        state = self.attach(data, keep_choice=choice if original_blob is not None else None)
        signature = state.get('signature')
        if not signature or signature != data.get('analysis_file_signature'):
            raise ValueError('Файл изменился. Обновите папку перед добавлением в очередь.')
        from core.storage import artwork_choice_key
        blob = None
        if choice == 'current':
            existing = pictures(File(data['path']))
            if not existing:
                raise ValueError('В файле нет встроенной обложки.')
            entry = {'signature': signature, 'current_id': hashlib.sha256(existing[0]).hexdigest()}
        else:
            candidate = next((item for item in state.get('candidates', []) if artwork_choice_key(item) == choice), None)
            if candidate is None:
                raise ValueError('Выбранная обложка недоступна.')
            if candidate.get('needs_review') and options.get('review_confirmed') is not True:
                raise ValueError('Подтвердите ручной выбор обложки с расхождениями.')
            if original_blob is not None:
                if not 0 < len(original_blob) <= MAX_IMAGE or hashlib.sha256(original_blob).hexdigest() != candidate['id']:
                    raise ValueError('Оригинал не совпадает с выбранной обложкой. Повторите выбор.')
                blob = original_blob
            else:
                blob = self.read_queued_image(data['path'])
                if blob is None or hashlib.sha256(blob).hexdigest() != candidate['id']:
                    blob = load_candidate_image(self.root, candidate)
            entry = {'signature': signature, 'candidate': candidate, 'options': options}
        if file_signature(data['path']) != signature:
            raise ValueError('Файл изменился. Обновите папку перед добавлением в очередь.')
        self.history_path.parent.mkdir(exist_ok=True, parents=True)
        with closing(sqlite3.connect(self.history_path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS cover_queue(path TEXT PRIMARY KEY, payload TEXT NOT NULL, image BLOB)')
            db.execute('INSERT OR REPLACE INTO cover_queue VALUES (?,?,?)', (key, json.dumps(entry, ensure_ascii=False), blob))
        self.queue[key] = entry
        if blob is None:
            self.queued_image_paths.discard(key)
        else:
            self.queued_image_paths.add(key)
        state.pop('write_error', None)
        state.pop('write_restored', None)
        self.show_queued(data, state)

    def show_queued(self, data, state):
        key = os.path.normcase(str(Path(data['path']).resolve()))
        entry = self.queue.get(key)
        if entry is None:
            state.pop('queued', None)
            return
        candidate = entry.get('candidate')
        needs_refresh = bool(candidate and key not in self.queued_image_paths
                             and not (self.cache / (candidate['id'] + '.image')).is_file())
        choice_key = candidate.get('choice_key') if candidate else None
        state['queued'] = {
            'choice_key': choice_key,
            'current': candidate is None,
            'stale': state.get('signature') != entry['signature'],
            'needs_refresh': needs_refresh,
        }
        if needs_refresh:
            state['candidates'] = [item for item in state.get('candidates', []) if item.get('choice_key') != choice_key]
        elif candidate and not any(item.get('choice_key') == choice_key for item in state.get('candidates', [])):
            state['candidates'] = [candidate, *state.get('candidates', [])]

    def write_queued(self, data, backup_session=None, downloads=None):
        key = os.path.normcase(str(Path(data['path']).resolve()))
        entry = self.queue.get(key)
        if entry is None:
            raise ValueError('Для трека нет обложки в очереди.')
        state = self.attach(data)
        if entry['signature'] != state.get('signature'):
            raise ValueError('Файл изменился. Проверьте и добавьте обложку в очередь заново.')
        candidate = entry.get('candidate')
        if candidate:
            blob = self.read_queued_image(data['path'])
            try:
                if state['queued']['needs_refresh']:
                    blob = load_candidate_image(self.root, candidate, downloads)
                    state['candidates'].append(candidate)
                self.write(data, candidate['id'], choice_key=candidate['choice_key'],
                           confirm_embedded=True, replace_existing=True, original_blob=blob,
                           backup_session=backup_session, downloads=downloads, **entry['options'])
            except Exception:
                restored = data.get('_ui_cover', {})
                if restored.get('write_restored') and restored.get('signature') != entry['signature']:
                    updated = {**entry, 'signature': restored['signature']}
                    with closing(sqlite3.connect(self.history_path)) as db, db:
                        db.execute('UPDATE cover_queue SET payload=? WHERE path=?', (json.dumps(updated, ensure_ascii=False), key))
                    self.queue[key] = updated
                raise
        else:
            existing = pictures(File(data['path']))
            if not existing or hashlib.sha256(existing[0]).hexdigest() != entry['current_id']:
                raise ValueError('Встроенная обложка изменилась. Добавьте её в очередь заново.')
            self.mark(data, 'confirmed')
        self.set_queued(data)

    def read_queued_image(self, path):
        key = os.path.normcase(str(Path(path).resolve()))
        if key not in self.queued_image_paths:
            return None
        return queued_image(self.root, key)

    def remember(self,data,state):
        image_id=state.get('written_image_id') or state.get('decision_image_id')
        if not image_id:return
        key=os.path.normcase(str(Path(data['path']).resolve()))
        record={k:state[k] for k in ('changed','changed_at','change_kind','written_image_id','decision_image_id','manual_decision','confirmed_embedded','source','release','backup') if k in state}
        record['image_id']=image_id
        if self.history.get(key)==record:return
        self.history_path.parent.mkdir(exist_ok=True,parents=True)
        with closing(sqlite3.connect(self.history_path)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS cover_history(path TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('INSERT OR REPLACE INTO cover_history VALUES (?,?)',(key,json.dumps(record,ensure_ascii=False)))
        self.history[key]=record

    def state_path(self, data):
        return self.cache / (stable_digest(str(Path(data['path']).resolve()).casefold()) + '.json')

    def save(self, data, state):
        state['signature'] = file_signature(data['path'])
        atomic_write_json(self.state_path(data), {key: value for key, value in state.items() if key != 'queued'})
        data['_ui_cover'] = state
        self.remember(data,state)
        return state

    def restore_view(self, data, state, *, keep_choice=None):
        from core.storage import available_artwork
        state=available_artwork(state,self.root,keep_choice=keep_choice)
        self.show_queued(data, state)
        # Do not present a cached refusal from the old CDN policy as a fresh
        # failure. A new lookup must still establish whether art is available.
        legacy='Last.fm: Недопустимый источник обложки.'
        if legacy in state.get('errors',[]):
            state={**state,'diagnosis_outcome':'outdated',
                   'errors':['Last.fm: сохранён отказ прежней версии приложения. Нажмите «Обновить варианты».' if e==legacy else e for e in state['errors']],
                   'message':'Сохранены результаты прежней версии. Обновите варианты обложки.'}
        elif state.get('diagnosis_outcome')=='error' and state.get('candidates'):
            state={**state,'diagnosis_outcome':'partial'}
        # Earlier builds saved a backup/source only after a real artwork write.
        if state.get('backup') and state.get('source') and 'changed' not in state:
            state['changed']=True
            existing=pictures(File(data['path']))
            if existing:state['written_image_id']=hashlib.sha256(existing[0]).hexdigest()
            return self.save(data,state)
        data['_ui_cover']=state
        self.remember(data,state)
        return state

    def attach(self, data, *, keep_choice=None):
        import json
        try:
            signature = file_signature(data['path'])
            state = data.get('_ui_cover')
            if state and state.get('signature') == signature:
                return self.restore_view(data,state,keep_choice=keep_choice)
            try:
                state = json.loads(self.state_path(data).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                state = None
            if state and state.get('signature') == signature:
                return self.restore_view(data,state,keep_choice=keep_choice)
            existing = pictures(File(data['path']))
            previous=state or {}
            state = {'status': 'ready' if existing else 'pending', 'candidates': []}
            history=self.history.get(os.path.normcase(str(Path(data['path']).resolve()))) or {}
            if not existing and history.get('image_id')=='no_cover':
                state.update({key:history[key] for key in ('manual_decision','decision_image_id') if key in history})
            if existing:
                image_id=hashlib.sha256(existing[0]).hexdigest()
                if history.get('image_id')==image_id:state.update({k:v for k,v in history.items() if k!='image_id'})
                if previous.get('written_image_id')==image_id:
                    for key in ('changed','changed_at','change_kind','written_image_id','backup','source','release','manual_decision','confirmed_embedded'):
                        if key in previous:state[key]=previous[key]
                try: state.update(image_info(existing[0]))
                except Exception: state['message'] = 'В файле есть обложка; предпросмотр недоступен.'
            self.save(data, state)
            self.show_queued(data, state)
            return state
        except Exception as error:
            data['_ui_cover'] = {'status': 'pending', 'message': str(error), 'candidates': []}
            return data['_ui_cover']

    def candidate(self, blob, **facts):
        info = image_info(blob)
        key = hashlib.sha256(blob).hexdigest()
        self.cache.mkdir(parents=True, exist_ok=True)
        path = self.cache / (key + '.image')
        with self._artifact_lock:
            if not path.exists():
                path.write_bytes(blob)
                self._new_image_bytes+=len(blob)
            else:path.touch()
        return {**facts, **info, 'id': key}

    def search(self, data, query=None, progress=None, cancelled=None,prepared=None,downloads=None):
        from core.cover_search import discover, match_evidence, default_query
        state = self.attach(data)
        query = query or default_query(data)
        if not query.get('artist') or not query.get('title'):
            raise ValueError('Укажите исполнителя и название в поиске обложки.')
        before = file_signature(data['path'])
        if before != data.get('analysis_file_signature'):
            raise ValueError('Теги изменились. Обновите папку перед поиском обложек.')
        rows, errors = prepared if prepared is not None else discover(query['artist'],query['title'],query.get('album',''),progress,cancelled)
        candidates, seen, ordered = [], set(), []
        fingerprints = {}
        ready = state['status'] == 'ready'
        cancelled = cancelled or (lambda: False)
        progress = progress or (lambda message: None)
        for row in rows:
            key=(row.get('image_url'),row.get('release_id'))
            if key not in seen:seen.add(key);ordered.append(row)
        archive_lock=threading.Lock()
        def fetch_image(row):
            url=row['image_url']
            if cancelled():return None
            dependency=by_url.get(row.get('fallback_if_missing'))
            if dependency is not None:
                try:
                    if dependency.result() is not None:return None
                except Exception:pass
            # CAA is conservative; CDN images can overlap independently.
            if urlparse(url).hostname=='coverartarchive.org':
                with archive_lock:
                    if cancelled():return None
                    blob=fetch_variants(row)
            else:blob=fetch_variants(row)
            if blob is None or cancelled():return None
            payload,used_url=blob
            candidate = self.candidate(payload, image_url=used_url)
            return candidate, pixel_digest(payload) if ready else None
        def fetch_variants(row):
            failures=[]
            for url in dict.fromkeys([row['image_url'],*row.get('image_fallbacks',[])]):
                if cancelled():return None
                try:
                    blob=downloads(url) if downloads is not None else download(url)
                    if blob is not None:return blob,url
                except Exception as error:
                    # A size fallback must not sidestep a provider rate limit.
                    response=getattr(error,'response',None)
                    if response is not None and response.status_code==429:
                        raise ValueError('Сервер изображений ограничил частоту запросов (HTTP 429). Повторите поиск позже.') from None
                    failures.append(artwork_error(error))
            if failures:raise ValueError('; '.join(dict.fromkeys(failures)))
            return None
        by_url={}
        from core.storage import artwork_choice_key
        with ThreadPoolExecutor(max_workers=4,thread_name_prefix='cover-image') as pool:
            next_image = 0
            for position, row in enumerate(ordered):
                if cancelled() or len(candidates) >= 36:
                    break
                # Keep four slots in flight without waiting for a whole group.
                window_end = min(len(ordered), position + min(4, 36 - len(candidates)))
                while next_image < window_end:
                    pending_row = ordered[next_image]
                    url = pending_row['image_url']
                    if url not in by_url:
                        by_url[url] = pool.submit(fetch_image, pending_row)
                    next_image += 1
                progress('Загружаем: '+str(row.get('release') or row.get('source')))
                try:
                    result = by_url[row['image_url']].result()
                    if result is None:
                        continue
                    candidate, fingerprint = result
                    fingerprints[candidate['id']] = fingerprint
                    facts={k:v for k,v in row.items() if k not in ('image_url','image_fallbacks')}
                    evidence=match_evidence(data,row.get('artist',''),row.get('title',''),row.get('duration'))
                    if row.get('review_reason'):
                        evidence={'needs_review':True,'match_note':row['review_reason']+' '+evidence['match_note']}
                    choice={**candidate,**facts,**evidence}
                    choice['choice_key']=artwork_choice_key(choice)
                    candidates.append(choice)
                except Exception as error:
                    errors.append(str(row.get('source'))+': '+str(error))
            for future in by_url.values():future.cancel()
        if file_signature(data['path']) != before:
            raise ValueError('Файл изменился во время поиска. Обновите папку.')
        interrupted = cancelled()
        catalog_keys = {choice['choice_key'] for choice in candidates}
        local_choices = [choice for choice in state.get('candidates', [])
                         if not choice.get('image_url') and choice['choice_key'] not in catalog_keys]
        choices = [*local_choices, *candidates]
        result = {**state,'status':'ready' if ready else 'pending' if choices or errors or interrupted else 'not_found',
                  'candidates':choices,'catalog_candidate_count':len(candidates),'query':query,'errors':errors[:5],
                  'message': 'Поиск остановлен; можно выбрать из загруженных вариантов.' if interrupted else 'Выберите изображение. Текстовые теги при записи обложки сохраняются.' if candidates else 'Поиск не завершён. Повторите попытку.' if errors else 'В доступных источниках обложки по этому запросу не найдены.'}
        if local_choices and not candidates:
            result['message'] += ' Локальные варианты доступны ниже.'
        result['checked_at']=time.time()
        result['diagnosis_outcome']='cancelled' if interrupted else ('partial' if candidates else 'error') if errors else 'complete'
        if ready:
            existing = pictures(File(data['path']))
            matches = []
            if existing:
                result['current_id']=hashlib.sha256(existing[0]).hexdigest()
                fingerprint = pixel_digest(existing[0])
                for candidate in candidates:
                    if candidate.get('needs_review'): continue
                    if fingerprint and fingerprints.get(candidate['id'])==fingerprint:
                        matches.append({k:candidate.get(k) for k in ('source','source_url','release','release_id')})
            result['verification'] = {'status':'source_match' if matches else 'unverified','matches':matches,
                'message':'Пиксели текущей картинки совпадают с изображением источника для этой записи. Это не подтверждает выбранное издание.' if matches else 'Совпадение с источниками не установлено. Это не означает, что обложка неправильная.'}
        self.save(data,result)
        from core.storage import trim_artwork
        if time.monotonic()-self._last_trim>=30 or self._new_image_bytes>=32*1024*1024:
            try:
                trim_artwork(self.root,protect=[c['id'] for c in choices]+[result.get('current_id')])
                self._last_trim=time.monotonic();self._new_image_bytes=0
            except OSError:pass
        return result

    def import_image(self,data,blob,origin='file'):
        from core.storage import artwork_choice_key,trim_artwork
        if origin not in ('file','clipboard'):raise ValueError('Некорректный источник изображения.')
        state=self.attach(data)
        before=file_signature(data['path'])
        if before!=data.get('analysis_file_signature'):raise ValueError('Файл изменился. Обновите папку перед добавлением обложки.')
        candidate=self.candidate(blob,source='Скриншот из буфера' if origin=='clipboard' else 'Ваш файл',
                                 release='Добавлено вами',image_origin=origin)
        candidate['choice_key']=artwork_choice_key(candidate)
        if file_signature(data['path'])!=before:raise ValueError('Файл изменился во время добавления изображения. Обновите папку.')
        previous=[c for c in state.get('candidates',[]) if artwork_choice_key(c)!=candidate['choice_key']]
        self.save(data,{**state,'candidates':[candidate,*previous[:35]],
                        'message':'Изображение добавлено. Проверьте его и сохраните обложку.'})
        trim_artwork(self.root,protect=[c['id'] for c in data['_ui_cover']['candidates']]+[state.get('current_id')])
        return candidate

    def import_source(self,data,url):
        from core.cover_pages import publication
        from core.cover_search import match_evidence
        from core.storage import artwork_choice_key
        state=self.attach(data)
        before=file_signature(data['path'])
        if before!=data.get('analysis_file_signature'):raise ValueError('Файл изменился. Обновите папку перед загрузкой обложки.')
        row=publication(url,data.get('artist',''),data.get('title',''))
        blob=download(row['image_url'])
        if not blob:raise ValueError('Изображение публикации больше не доступно.')
        evidence=match_evidence(data,row.get('artist',''),row.get('title',''),row.get('duration'))
        candidate=self.candidate(blob,**{k:v for k,v in row.items() if k!='image_url'},
                                 needs_review=True,match_note=row['review_reason']+' '+evidence['match_note'])
        candidate['choice_key']=artwork_choice_key(candidate)
        if file_signature(data['path'])!=before:raise ValueError('Файл изменился во время загрузки. Обновите папку.')
        choices=[c for c in state.get('candidates',[]) if artwork_choice_key(c)!=candidate['choice_key']]
        return self.save(data,{**state,'candidates':[candidate,*choices[:35]],
                              'message':'Обложка публикации добавлена первой. Проверьте её и сохраните выбранный вариант.'})

    def upscale(self,data,candidate_id='current',choice_key=None,crop_square=False,crop_position=50):
        from core.cover_upscale import upscale
        from core.storage import artwork_choice_key,trim_artwork
        state=self.attach(data)
        before=file_signature(data['path'])
        if before!=data.get('analysis_file_signature'):raise ValueError('Файл изменился. Обновите папку перед увеличением обложки.')
        if candidate_id=='current':
            blob=self.image(data,'current')
            original=self.candidate(blob,source='Текущая обложка',release=data.get('album') or 'Из файла',
                                    artist=data.get('artist',''),title=data.get('title',''),duration=data.get('duration'),
                                    needs_review=True,match_note='Увеличение не проверяет правильность текущей обложки. Проверьте её перед записью.')
            original['choice_key']=artwork_choice_key(original)
        else:
            choices=[c for c in state.get('candidates',[]) if c['id']==candidate_id]
            if choice_key is None and len({artwork_choice_key(c) for c in choices})>1:raise ValueError('Выберите конкретное издание перед увеличением.')
            original=next((c for c in choices if choice_key is None or artwork_choice_key(c)==choice_key),None)
            if original is None:raise ValueError('Выберите доступную обложку.')
            blob=self.image(data,original['id'])
        origin=artwork_choice_key(original)
        info=image_info(blob);will_crop=crop_square and info['width']!=info['height']
        if min(info['width'],info['height'])>=1000 and not will_crop:return original
        cached=next((c for c in state.get('candidates',[]) if (c.get('upscale') or {}).get('origin_choice_key')==origin and
                     bool((c.get('upscale') or {}).get('crop'))==bool(will_crop) and
                     (not will_crop or c['upscale']['crop']['position']==crop_position)),None)
        if cached:return cached
        result,details=upscale(blob,crop_square,crop_position)
        details.update(source_id=original['id'],origin_choice_key=origin)
        facts={k:v for k,v in original.items() if k not in ('id','preview','width','height','mime','choice_key','upscale','image_url')}
        enlarged=self.candidate(result,**facts,upscale=details)
        enlarged['choice_key']=artwork_choice_key(enlarged)
        if file_signature(data['path'])!=before:raise ValueError('Файл изменился во время увеличения. Обновите папку.')
        rest=[c for c in state.get('candidates',[]) if artwork_choice_key(c) not in (origin,enlarged['choice_key'])]
        candidates=[enlarged,original,*rest[:34]]
        self.save(data,{**state,'candidates':candidates,'message':'Обложка увеличена. Исходник доступен рядом; выберите вариант перед сохранением.'})
        trim_artwork(self.root,protect=[c['id'] for c in candidates]+[state.get('current_id')])
        return enlarged

    def mark(self, data, decision):
        before=file_signature(data['path'])
        if data.get('analysis_file_signature')!=before:
            raise ValueError('Файл изменился. Обновите папку перед подтверждением обложки.')
        state = dict(self.attach(data))
        if decision not in ('review','confirmed','clear'): raise ValueError('Неверная отметка обложки.')
        if decision=='confirmed' and state['status']!='ready': raise ValueError('В файле нет обложки для подтверждения.')
        if state['status']=='ready':
            existing=pictures(File(data['path']))
            if decision=='confirmed' and not existing:raise ValueError('В файле нет встроенной обложки. Выберите изображение и добавьте его в очередь.')
            if existing:state['decision_image_id']=hashlib.sha256(existing[0]).hexdigest()
        else:state['decision_image_id']='no_cover'
        if file_signature(data['path'])!=before:
            raise ValueError('Файл изменился во время проверки. Повторите выбор после обновления папки.')
        return self.save(data,{**state,'manual_decision':decision,'confirmed_embedded':decision=='confirmed'})

    def image(self, data, candidate_id):
        state = data.get('_ui_cover') or {}
        if state.get('signature')!=file_signature(data['path']): raise ValueError('Файл изменился. Обновите папку.')
        if candidate_id=='current':
            existing = pictures(File(data['path']))
            if not existing: raise ValueError('В файле нет обложки.')
            return existing[0]
        if not re.fullmatch(r'[a-f0-9]{64}',candidate_id) or not any(c.get('id')==candidate_id for c in state.get('candidates',[])):
            raise ValueError('Выберите доступную обложку.')
        candidate=next(c for c in state.get('candidates',[]) if c.get('id')==candidate_id)
        queued = self.queue.get(os.path.normcase(str(Path(data['path']).resolve())), {})
        if (queued.get('candidate') or {}).get('id') == candidate_id:
            blob = self.read_queued_image(data['path'])
            if blob is not None and hashlib.sha256(blob).hexdigest() == candidate_id:
                return blob
        if state.get('current_id') == candidate_id:
            existing = pictures(File(data['path']))
            if existing and hashlib.sha256(existing[0]).hexdigest() == candidate_id:
                return existing[0]
        return load_candidate_image(self.root,candidate)

    def write(self, data, candidate_id, replace_existing=False, review_confirmed=False, choice_key=None,upscale_small=False,confirm_embedded=False,crop_square=False,crop_position=50, *, original_blob=None, backup_session=None, downloads=None):
        state = data.get('_ui_cover') or {}
        if state.get('signature') != file_signature(data['path']) or state.get('signature') != data.get('analysis_file_signature'):
            raise ValueError('Файл изменился после поиска. Обновите папку и найдите обложки заново.')
        from core.storage import artwork_choice_key
        choices=[c for c in state.get('candidates',[]) if c['id']==candidate_id]
        if choice_key is None and len({artwork_choice_key(c) for c in choices})>1:
            raise ValueError('Одинаковая картинка найдена у нескольких изданий. Выберите конкретный вариант заново.')
        candidate=next((c for c in choices if choice_key is None or artwork_choice_key(c)==choice_key),None)
        if candidate is None: raise ValueError('Обложка недоступна. Повторите поиск.')
        if candidate.get('needs_review') and review_confirmed is not True: raise ValueError('Подтвердите ручной выбор: у варианта есть расхождения.')
        blob = original_blob if original_blob is not None else load_candidate_image(self.root, candidate, downloads)
        if hashlib.sha256(blob).hexdigest() != candidate_id:
            raise ValueError('Изображение в очереди повреждено. Выберите его заново.')
        info = image_info(blob)
        upscale_details = candidate.get('upscale')
        needs_processing = min(info['width'], info['height']) < 1000 or (
            crop_square and info['width'] != info['height']
        )
        if (upscale_small or crop_square) and needs_processing:
            from core.cover_upscale import upscale
            blob, upscale_details = upscale(blob, crop_square, crop_position)
            info = image_info(blob)
        embedded_id = hashlib.sha256(blob).hexdigest()
        if file_signature(data['path']) != state['signature']:
            raise ValueError('Файл изменился во время подготовки обложки. Обновите папку.')
        audio = File(data['path'])
        existing=pictures(audio)
        had_cover=bool(existing)
        if had_cover and replace_existing is not True: raise ValueError('Подтвердите замену существующей обложки.')
        if existing and existing[0]==blob:
            self.save(data,{**state,'manual_decision':'confirmed','confirmed_embedded':confirm_embedded or state.get('confirmed_embedded',False),'decision_image_id':embedded_id,'candidates':[],
                'message':'Эта картинка уже встроена. Выбор подтверждён без перезаписи файла.'})
            return state.get('backup')
        from core.scanner import scan_file
        from core.tag_writer import create_backup_session, backup_original, restore_from_backup
        before = scan_file(data['path'], raw_audio=audio)
        if before['analysis_file_signature'] != state['signature']: raise ValueError('Файл изменился. Повторите поиск.')
        # Mutagen deliberately omits empty standard text frames when saving
        # ID3. Their disappearance loses no value; custom TXXX markers and all
        # populated fields must still compare exactly.
        empty_text_frames={key for key,frame in audio.tags.items()
            if isinstance(frame,TextFrame) and re.fullmatch(r'T[A-Z0-9]{3}',key)
            and key!='TXXX' and before['raw_tags'].get(key)==['']} if isinstance(audio.tags,ID3) else set()
        backup = backup_original(data['path'], backup_session or create_backup_session(self.root))
        try:
            mark_file_changed(data['path'])
            if audio.tags is None: audio.add_tags()
            if isinstance(audio.tags, ID3):
                for old in list(audio.tags.getall('APIC')):
                    if old.type in (0,3): del audio.tags[old.HashKey]
                description='TagCleaner front cover'
                while 'APIC:'+description in audio.tags:description+=' '
                audio.tags.add(APIC(encoding=3, mime=info['mime'], type=3, desc=description, data=blob))
            elif isinstance(audio, FLAC):
                p = Picture(); p.type = 3; p.mime = info['mime']; p.width = info['width']; p.height = info['height']; p.data = blob
                retained = [old for old in audio.pictures if old.type not in (0,3)]
                audio.clear_pictures()
                for old in retained: audio.add_picture(old)
                audio.add_picture(p)
            elif isinstance(audio, MP4):
                audio['covr'] = [MP4Cover(blob, imageformat=MP4Cover.FORMAT_JPEG if info['mime']=='image/jpeg' else MP4Cover.FORMAT_PNG), *list(audio.tags.get('covr',[]))[1:]]
            else:
                p = Picture(); p.type = 3; p.mime = info['mime']; p.width = info['width']; p.height = info['height']; p.data = blob
                retained = [value for value in audio.tags.get('metadata_block_picture',[]) if Picture(base64.b64decode(value)).type not in (0,3)]
                for key in ('coverart','coverartmime'):
                    if key in audio.tags: del audio.tags[key]
                audio['metadata_block_picture'] = [base64.b64encode(p.write()).decode('ascii'), *retained]
            audio.save()
            saved_audio = File(data['path'])
            if blob not in pictures(saved_audio): raise ValueError('Обложка не прошла проверку после записи.')
            fresh = scan_file(data['path'], raw_audio=saved_audio)
            from ui.editor import FIELDS
            if any(fresh.get(k) != before.get(k) for k in FIELDS): raise ValueError('Текстовые теги изменились при записи обложки.')
            old_tags={k:v for k,v in before['raw_tags'].items() if k not in empty_text_frames}
            new_tags={k:v for k,v in fresh['raw_tags'].items() if not (k in empty_text_frames and v==[''])}
            if new_tags != old_tags:
                changed=sorted(k for k in set(old_tags)|set(new_tags) if old_tags.get(k)!=new_tags.get(k))
                raise ValueError('Дополнительные теги изменились при записи обложки: '+', '.join(changed[:8])+'.')
        except Exception as error:
            restored = restore_from_backup(data['path'], backup)
            if not restored.get('verified'): raise RuntimeError('Не удалось восстановить файл. Резервная копия: ' + str(backup))
            # Rollback changes the revision marker even when all audio bytes
            # match the original. Refresh the snapshot so retry is possible.
            try:
                fresh=scan_file(data['path']);data.update(fresh)
                if data.get('_ui_draft'):data['_ui_draft']['signature']=fresh['analysis_file_signature']
                self.save(data,{**state,'write_error':str(error),'write_restored':True,
                    'message':'Запись отменена. Исходный файл восстановлен; можно повторить попытку.'})
            except Exception:
                data['_ui_write_recheck_required']=True
                data['_ui_cover']={**state,'write_error':str(error),'write_restored':True,
                    'message':'Исходный файл восстановлен. Обновите папку перед повторной записью.'}
            raise
        data.update(fresh)
        # Only artwork changed, so existing text drafts remain applicable.
        if data.get('_ui_draft'):
            data['_ui_draft']['signature'] = fresh['analysis_file_signature']
        self.save(data, {'status': 'ready', **info, 'current_id':embedded_id, 'candidates': [], 'source': candidate.get('source'), 'release': candidate.get('release'), 'upscale':upscale_details, 'manual_decision':'confirmed','confirmed_embedded':confirm_embedded, 'changed':True,'changed_at':time.time_ns(),'change_kind':'replaced' if had_cover else 'added','written_image_id':embedded_id, 'verification':{'status':'user_selected','message':'Выбрано вами вручную.'}, 'backup': str(backup)})
        return str(backup)
