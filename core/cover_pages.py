"""Public publication artwork; never evidence of a particular release edition."""
import html
import json
import os
import re
import threading
import time
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

_lock=threading.RLock()
_last_request=0.0
_search_pause_until=0.0
_search_pause_reason=''
_SC_HOSTS=('soundcloud.com','www.soundcloud.com','m.soundcloud.com')


class MissingPublicationArtwork(ValueError):
    pass


def soundcloud_enabled():
    return os.getenv('TAGCLEANER_COVER_SOUNDCLOUD','1').lower().strip() in ('1','true','yes','on')


from core.cover_urls import source_url, _allowed_image_url


def _soundcloud_text(url,params=None):
    global _last_request, _search_pause_until, _search_pause_reason
    searching = url == 'https://soundcloud.com/search/sounds'
    with _lock:
        if searching and time.monotonic() < _search_pause_until:
            raise ValueError(_search_pause_reason)
        delay=.35-(time.monotonic()-_last_request)
        if delay>0:time.sleep(delay)
        try:
            for _ in range(4):
                p=urlparse(url)
                if p.scheme!='https' or p.hostname not in _SC_HOSTS or p.username or p.password or p.port not in (None,443):
                    raise ValueError('SoundCloud перенаправил запрос на неподдерживаемый адрес.')
                with requests.get(url,params=params,timeout=(8,12),allow_redirects=False,stream=True,
                                  headers={'User-Agent':'TagCleaner/0.21 (public artwork metadata)'}) as response:
                    if response.status_code in (301,302,303,307,308):
                        url=urljoin(url,response.headers.get('Location',''));params=None;continue
                    response.raise_for_status()
                    body=bytearray()
                    for part in response.iter_content(65536):
                        body.extend(part)
                        if len(body)>2*1024*1024:raise ValueError('Ответ SoundCloud слишком велик.')
                    return bytes(body).decode('utf-8')
            raise ValueError('Слишком много перенаправлений SoundCloud.')
        except requests.RequestException as error:
            response = error.response
            status = response.status_code if response is not None else None
            if searching and (status in (403, 429) or (status and 500 <= status < 600)
                              or isinstance(error, (requests.Timeout, requests.ConnectionError))):
                retry_after = response.headers.get('Retry-After', '') if response is not None else ''
                try:
                    seconds = max(30, int(retry_after))
                except ValueError:
                    try:
                        seconds = max(30, parsedate_to_datetime(retry_after).timestamp() - time.time())
                    except (TypeError, ValueError, OverflowError):
                        seconds = 30
                _search_pause_until = time.monotonic() + seconds
                _search_pause_reason = f'HTTP {status} при поиске.' if status else 'Не удалось дождаться ответа сервиса.'
                raise ValueError(_search_pause_reason) from error
            raise
        finally:_last_request=time.monotonic()


def _publication_caption(raw_title, uploader=''):
    value=html.unescape(str(raw_title)).strip()
    attribution = ' by '+uploader
    if uploader and value.casefold().endswith(attribution.casefold()):
        return value[:-len(attribution)].strip()
    return value


def _identity(raw_title):
    value=_publication_caption(raw_title)
    for separator in (' - ',' – ',' — '):
        if separator in value:
            artist,title=value.split(separator,1)
            return artist.strip(),title.strip()
    return '',value


def _soundcloud_artwork(url):
    p = urlparse(url)
    host = p.hostname or ''
    return (_allowed_image_url(url) and (host == 'sndcdn.com' or host.endswith('.sndcdn.com'))
            and bool(re.match(r'/artworks?-', p.path)))


def _soundcloud_row(url):
    try:
        payload=json.loads(_soundcloud_text('https://soundcloud.com/oembed',{'url':url,'format':'json'}))
    except requests.HTTPError as error:
        if error.response is None or error.response.status_code != 403:
            raise
        payload = {}
    if not isinstance(payload,dict):raise ValueError('Некорректный ответ SoundCloud.')
    image=str(payload.get('thumbnail_url') or '')
    is_set = '/sets/' in urlparse(url).path
    # oEmbed can substitute the uploader's avatar when a publication has no artwork.
    if not _soundcloud_artwork(image):
        from core.source_url_resolver import _MetaParser
        meta = _MetaParser()
        meta.feed(_soundcloud_text(url))
        if not payload.get('title'):
            payload['title'] = meta.meta.get('og:title') or meta.meta.get('twitter:title') or ''
        image = ''
        for key in ('og:image', 'twitter:image'):
            value = meta.meta.get(key, '')
            if _soundcloud_artwork(value):
                image = value
                break
    if not _soundcloud_artwork(image):
        raise MissingPublicationArtwork('У публикации SoundCloud нет доступной обложки; аватар автора не используется.')
    uploader=str(payload.get('author_name') or '')
    caption = _publication_caption(payload.get('title') or '', uploader)
    artist,title=_identity(caption)
    release = 'Публикация SoundCloud'+(' · '+uploader if uploader else '')
    reason = 'Обложка публикации SoundCloud. Загрузивший её пользователь и конкретное издание не подтверждены; проверьте вручную.'
    if is_set:
        release = 'Подборка SoundCloud'+(' · '+caption if caption else '')
        artist, title = '', ''
        reason = 'Обложка подборки SoundCloud. Она может относиться ко всей подборке или первому треку. Проверьте соответствие выбранному треку и версии вручную.'
    return {'image_url':image,'source':'SoundCloud','source_url':url,'release_id':'soundcloud:'+url,
            'release':release,'publication_kind':'set' if is_set else 'track',
            'artist':artist or (uploader if not is_set else ''),'title':title,'duration':None,
            'review_reason':reason}


class _SearchPage(HTMLParser):
    def __init__(self,text):
        super().__init__();self.in_noscript=False;self.has_results_markup=False;self.link=None;self.parts=[];self.rows=[];self.feed(text)

    def handle_starttag(self,tag,attrs):
        if tag=='noscript':self.in_noscript=True;self.has_results_markup=True
        if tag=='a' and self.in_noscript:
            try:
                self.link=source_url(urljoin('https://soundcloud.com',dict(attrs).get('href','')))
                if urlparse(self.link).hostname!='soundcloud.com' or '/sets/' in urlparse(self.link).path:self.link=None
            except ValueError:self.link=None
            self.parts=[]

    def handle_data(self,text):
        if self.link:self.parts.append(text)

    def handle_endtag(self,tag):
        if tag=='a' and self.link:
            self.rows.append((self.link,' '.join(self.parts).strip()));self.link=None
        if tag=='noscript':self.in_noscript=False


def _publication_match(artist, title, credit, song):
    from core.cover_catalogs import related_choice
    # Possessives in a version caption do not identify a different remix author.
    possessive = r"((?:\(|\[)[^()\[\]]+?)[’']s(\s+(?:remix|mix|edit|bootleg|flip|vip)\b[^()\[\]]*[)\]])"
    title = re.sub(possessive, r'\1\2', title, flags=re.I)
    song = re.sub(possessive, r'\1\2', song, flags=re.I)
    return related_choice(artist, title, credit, song)


def search_soundcloud(artist,title,cancelled=lambda:False):
    if not soundcloud_enabled() or cancelled():return [],[]
    from core.recording_credit import roles
    from core.release_credits import remove_primary_features
    primary, names = roles(artist, title)
    query_title = remove_primary_features(title, names)
    credits = (artist, primary[0]) if primary else (artist,)
    queries = dict.fromkeys(' '.join(re.sub(r'[^\w\s-]', ' ', credit+' '+query_title).split()) for credit in credits)
    rows,errors=[],[]
    seen = set()
    try:
        for query in queries:
            if cancelled() or rows or len(seen) >= 3:
                break
            page = _SearchPage(_soundcloud_text('https://soundcloud.com/search/sounds', {'q':query}))
            if not page.has_results_markup:
                raise ValueError('SoundCloud не вернул публичные результаты поиска; можно добавить обложку по ссылке.')
            for url, caption in page.rows:
                if cancelled() or len(seen) >= 3:
                    break
                credit, song = _identity(caption)
                if url in seen or not _publication_match(artist, title, credit or artist, song):
                    continue
                seen.add(url)
                try:
                    row = _soundcloud_row(url)
                    # Check publication metadata again after selecting a search result.
                    if row['artist'] and _publication_match(artist, title, row['artist'], row['title']):
                        rows.append(row)
                except MissingPublicationArtwork:
                    continue
                except requests.HTTPError as error:
                    if error.response is not None and error.response.status_code in (404, 410):
                        continue
                    raise
                except ValueError as error:
                    errors.append('SoundCloud: '+str(error))
    except Exception as error:errors.append('SoundCloud: '+str(error))
    return rows,errors


def publication(url,artist='',title=''):
    url=source_url(url)
    if urlparse(url).hostname=='soundcloud.com':return _soundcloud_row(url)
    from core.bandcamp_releases import fetch_page,Page,contexts
    from core.source_url_resolver import _MetaParser
    text,url=fetch_page(url);page=Page(text);meta=_MetaParser();meta.feed(text)
    image=meta.meta.get('og:image') or meta.meta.get('twitter:image')
    if not image:raise ValueError('На странице Bandcamp не найдена обложка.')
    try:matching=contexts(page,url,artist,title) if artist and title else []
    except ValueError:matching=[]
    current=page.data.get('current') or {};match=matching[0] if matching else {}
    return {'image_url':image,'source':'Bandcamp','source_url':url,'release_id':'bandcamp:'+url,
            'release':str(current.get('title') or 'Публикация Bandcamp'),
            'artist':match.get('track_artist_credit') or str(current.get('artist') or page.data.get('artist') or ''),
            'title':match.get('track_title') or (str(current.get('title') or '') if current.get('type')=='track' else ''),
            'duration':match.get('duration'),
            'review_reason':'Обложка страницы Bandcamp, выбранной по ссылке. Проверьте трек, версию и издание вручную.'}
