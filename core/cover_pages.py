"""Public publication artwork; never evidence of a particular release edition."""
import html
import json
import os
import re
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse

import requests

_lock=threading.RLock()
_last_request=0.0
_SC_HOSTS=('soundcloud.com','www.soundcloud.com','m.soundcloud.com')


class MissingPublicationArtwork(ValueError):
    pass


def soundcloud_enabled():
    return os.getenv('TAGCLEANER_COVER_SOUNDCLOUD','1').lower().strip() in ('1','true','yes','on')


from core.cover_urls import source_url


def _soundcloud_text(url,params=None):
    global _last_request
    with _lock:
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
        finally:_last_request=time.monotonic()


def _identity(raw_title):
    value=html.unescape(str(raw_title)).strip()
    if ' by ' in value:value=value.rsplit(' by ',1)[0].strip()
    for separator in (' - ',' – ',' — '):
        if separator in value:
            artist,title=value.split(separator,1)
            return artist.strip(),title.strip()
    return '',value


def _soundcloud_row(url):
    payload=json.loads(_soundcloud_text('https://soundcloud.com/oembed',{'url':url,'format':'json'}))
    if not isinstance(payload,dict):raise ValueError('Некорректный ответ SoundCloud.')
    image=str(payload.get('thumbnail_url') or '')
    p=urlparse(image)
    # oEmbed can substitute the uploader's avatar when a track has no artwork.
    if not p.hostname or not (p.hostname=='sndcdn.com' or p.hostname.endswith('.sndcdn.com')) or not re.match(r'/artworks?-',p.path):
        raise MissingPublicationArtwork('У публикации SoundCloud нет доступной обложки трека; аватар автора не используется.')
    artist,title=_identity(payload.get('title') or '')
    uploader=str(payload.get('author_name') or '')
    return {'image_url':image,'source':'SoundCloud','source_url':url,'release_id':'soundcloud:'+url,
            'release':'Публикация SoundCloud'+(' · '+uploader if uploader else ''),
            'artist':artist,'title':title,'duration':None,
            'review_reason':'Обложка публикации SoundCloud. Загрузивший её пользователь и конкретное издание не подтверждены; проверьте вручную.'}


class _SearchPage(HTMLParser):
    def __init__(self,text):
        super().__init__();self.in_noscript=False;self.has_results_markup=False;self.link=None;self.parts=[];self.rows=[];self.feed(text)

    def handle_starttag(self,tag,attrs):
        if tag=='noscript':self.in_noscript=True;self.has_results_markup=True
        if tag=='a' and self.in_noscript:
            try:
                self.link=source_url(urljoin('https://soundcloud.com',dict(attrs).get('href','')))
                if urlparse(self.link).hostname!='soundcloud.com':self.link=None
            except ValueError:self.link=None
            self.parts=[]

    def handle_data(self,text):
        if self.link:self.parts.append(text)

    def handle_endtag(self,tag):
        if tag=='a' and self.link:
            self.rows.append((self.link,' '.join(self.parts).strip()));self.link=None
        if tag=='noscript':self.in_noscript=False


def search_soundcloud(artist,title,cancelled=lambda:False):
    if not soundcloud_enabled() or cancelled():return [],[]
    from core.cover_catalogs import related_choice
    from core.recording_credit import roles
    from core.release_credits import remove_primary_features
    query_title=remove_primary_features(title,roles(artist,title)[1])
    query=re.sub(r'[^\w\s-]',' ',artist+' '+query_title)
    query=' '.join(query.split())
    rows,errors=[],[]
    try:
        page=_SearchPage(_soundcloud_text('https://soundcloud.com/search/sounds',{'q':query}))
        if not page.has_results_markup:raise ValueError('SoundCloud не вернул публичные результаты поиска; можно добавить обложку по ссылке.')
        seen=set()
        for url,caption in page.rows:
            if cancelled() or len(seen)>=3:break
            credit,song=_identity(caption)
            if url in seen or not credit or not related_choice(artist,title,credit,song):continue
            seen.add(url)
            try:
                row=_soundcloud_row(url)
                # Validate actual publication metadata again, not only search text.
                if row['artist'] and related_choice(artist,title,row['artist'],row['title']):rows.append(row)
            except MissingPublicationArtwork:continue
            except Exception as error:errors.append('SoundCloud: '+str(error))
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
