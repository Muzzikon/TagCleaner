"""Explicit Bandcamp edition import; no audio download or role inference."""
import json
import math
import re
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse

def source_url(value):
    p=urlparse(str(value).strip())
    if (p.scheme!='https' or not re.fullmatch(r'[a-z0-9-]+\.bandcamp\.com',p.hostname or '')
            or p.username or p.password or p.port not in (None,443)
            or not re.fullmatch(r'/(album|track)/[a-zA-Z0-9_-]+/?',p.path)):
        raise ValueError('Нужна HTTPS-ссылка на страницу альбома или трека Bandcamp.')
    return urlunparse(('https',p.hostname,p.path.rstrip('/'),'','',''))


def fetch_page(url):
    import requests
    url=source_url(url)
    with requests.Session() as session:
        for _ in range(4):
            with session.get(url,timeout=(8,15),allow_redirects=False,stream=True,
                             headers={'User-Agent':'TagCleaner/0.20 (explicit release metadata import)'}) as response:
                if response.is_redirect:
                    url=source_url(urljoin(url,response.headers.get('Location','')))
                    continue
                response.raise_for_status()
                chunks=[];size=0
                for chunk in response.iter_content(65536):
                    size+=len(chunk)
                    if size>2*1024*1024:raise ValueError('Страница Bandcamp слишком велика для импорта.')
                    chunks.append(chunk)
                return b''.join(chunks).decode('utf-8'),url
    raise ValueError('Слишком много перенаправлений Bandcamp.')


class Page(HTMLParser):
    def __init__(self,text):
        super().__init__();self.data=None;self.schemas=[];self.script=None
        self.feed(text)
        if not isinstance(self.data,dict):
            raise ValueError('Bandcamp не вернул метаданные издания: возможна проверка доступа. Откройте ссылку в браузере; отсутствие ответа не означает отсутствие релиза.')

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if 'data-tralbum' in attrs and self.data is None:
            self.data=json.loads(attrs['data-tralbum'])
        if tag=='script' and attrs.get('type')=='application/ld+json':self.script=[]

    def handle_data(self,text):
        if self.script is not None:self.script.append(text)

    def handle_endtag(self,tag):
        if tag=='script' and self.script is not None:
            try:self.schemas.append(json.loads(''.join(self.script)))
            except ValueError:pass
            self.script=None

    def parent_album(self):
        for schema in self.schemas:
            if not isinstance(schema,dict):continue
            album=schema.get('inAlbum') or {}
            if isinstance(album,dict) and album.get('@id'):
                return source_url(album['@id'])
        return None


def release_date(value):
    if not isinstance(value,str) or not value.strip():return ''
    if re.fullmatch(r'\d{4}(?:-\d{2})?(?:-\d{2})?',value):return value
    try:return parsedate_to_datetime(value).date().isoformat()
    except (ValueError,TypeError,OverflowError):return ''


def contexts(page,url,artist,title):
    from core.release_discovery import same_track
    from core.title_identity import comparison_title
    from core.album_artist import is_various
    data=page.data;current=data.get('current') or {}
    tracks=data.get('trackinfo') or []
    if current.get('type') not in ('album','track') or not isinstance(tracks,list):return []
    album_artist=str(current.get('artist') or data.get('artist') or '')
    album=str(current.get('title') or '')
    standalone=current.get('type')=='track'
    if standalone and current.get('album_id'):
        raise ValueError('У трека есть родительский альбом. Без его страницы дата и общее число треков не подставляются.')
    if standalone:album=comparison_title(album_artist,album)
    kind='Single' if standalone else ('EP' if re.search(r'(?:\bEP\b)',album,re.I) else '')
    rows=[]
    for track in tracks:
        if not isinstance(track,dict):continue
        credit=str(track.get('artist') or album_artist)
        raw_title=str(track.get('title') or '')
        track_title=comparison_title(credit,raw_title)
        if not credit or is_various(credit) or not same_track(artist,title,credit,track_title):continue
        position=track.get('track_num')
        numbers=[t.get('track_num') for t in tracks if isinstance(t,dict)]
        complete=sorted(n for n in numbers if type(n) is int)==list(range(1,len(tracks)+1)) and len(numbers)==len(tracks)
        context={'source':'bandcamp','source_url':url,'release_id':'bandcamp:'+url,
                 'track_id':str(track.get('track_id') or track.get('id') or ''),
                 'title':album,'primary_type':kind,'date':release_date(current.get('release_date')),
                 'album_artist_credit':album_artist,'track_title':track_title,'track_artist_credit':credit,
                 'track_number':str(position) if type(position) is int and position>0 else '',
                 'track_total':len(tracks) if complete else None,
                 'source_note':'Данные публичной страницы Bandcamp. Дата относится к этому изданию; номера дисков неизвестны. Проверьте соответствие аудиофайлу перед выбором.'}
        try:
            duration=float(track.get('duration'))
            if math.isfinite(duration) and duration>0:context['duration']=duration
        except (TypeError,ValueError):pass
        rows.append(context)
    return rows


def import_release(url,artist,title):
    text,url=fetch_page(url);page=Page(text)
    if (page.data.get('current') or {}).get('type')=='track' and (page.data.get('current') or {}).get('album_id'):
        parent=page.parent_album()
        if not parent:raise ValueError('Bandcamp не указал ссылку на альбом. Вставьте ссылку на сам альбом.')
        album_id=page.data['current']['album_id']
        text,url=fetch_page(parent);page=Page(text)
        if str((page.data.get('current') or {}).get('id'))!=str(album_id):
            raise ValueError('Страница трека и родительский альбом не совпали. Издание не добавлено.')
    result=contexts(page,url,artist,title)
    if not result:raise ValueError('На странице Bandcamp не найдена эта песня с теми же исполнителями и версией. Поля не изменены.')
    return result
