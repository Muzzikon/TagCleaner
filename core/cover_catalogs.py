"""Extra artwork discovery. Every image remains an explicit, sourced choice."""
import os
import math
import threading
import time
from urllib.parse import urlparse

import requests

from core.release_discovery import same_track

_deezer_lock = threading.RLock()
_deezer_last = 0.0
_deezer_blocked_until = 0.0
_deezer_blocked_reason = ''


def deezer_enabled():
    return os.getenv('TAGCLEANER_COVER_DEEZER', '1').lower().strip() in ('1', 'true', 'yes', 'on')


def extra_providers():
    from core.extra_catalogs import capability
    return [name for name, state in capability().items() if state['enabled']]


def related_choice(artist, title, other_artist, other_title):
    """Retrieve incomplete credits only when title/version and a principal agree.

    This is NOT identity evidence. match_evidence still tests the whole credit.
    An unrelated artist, an omitted remix or a different song is never offered.
    """
    if same_track(artist, title, other_artist, other_title):
        return True
    if not same_track(artist, title, artist, other_title):
        return False
    from core.recording_credit import roles, same_names
    primary = roles(artist, title)[0]
    other_primary = roles(other_artist, other_title)[0]
    return any(same_names([left], [right]) for left in primary for right in other_primary)


def _deezer(endpoint, params=None):
    global _deezer_last
    with _deezer_lock:
        if time.monotonic() < _deezer_blocked_until:
            remaining=max(1,math.ceil(_deezer_blocked_until-time.monotonic()))
            raise RuntimeError(f'{_deezer_blocked_reason or "Deezer временно недоступен"} Повторная попытка через {remaining} с.')
        delay = .3 - (time.monotonic() - _deezer_last)
        if delay > 0:
            time.sleep(delay)
        try:
            with requests.get('https://api.deezer.com/'+endpoint, params=params,
                              headers={'User-Agent': 'TagCleaner/0.14 (artwork lookup)'},
                              timeout=(8, 15), allow_redirects=False) as response:
                if response.status_code >= 300:
                    seconds=60 if response.status_code==429 else 30
                    try:
                        retry=float(response.headers.get('Retry-After',seconds))
                        if math.isfinite(retry) and retry>0:seconds=math.ceil(retry)
                    except (TypeError,ValueError):pass
                    _pause_deezer(f'Deezer: HTTP {response.status_code}.',seconds)
                try:payload = response.json()
                except ValueError:_pause_deezer('Deezer вернул ответ, который не удалось прочитать.',30)
            if not isinstance(payload, dict):
                _pause_deezer('Deezer: неполный ответ каталога.',30)
            if payload.get('error'):
                error=payload['error']
                code=error.get('code','не указан') if isinstance(error,dict) else 'не указан'
                _pause_deezer(f'Deezer: ошибка API, код {code}.',60)
            return payload
        except requests.RequestException as error:
            reason='Deezer: время ожидания ответа истекло.' if isinstance(error,requests.Timeout) else 'Deezer: ошибка соединения с сервисом.'
            _pause_deezer(reason,30)
        finally:
            _deezer_last = time.monotonic()


def _pause_deezer(reason,seconds):
    global _deezer_blocked_until,_deezer_blocked_reason
    _deezer_blocked_until=time.monotonic()+seconds
    _deezer_blocked_reason=reason
    raise RuntimeError(f'{reason} Повторная попытка через {seconds} с.') from None


def search_deezer(artist, title, cancelled=lambda: False):
    if not deezer_enabled() or cancelled():
        return [], []
    rows, errors = [], []
    try:
        payload = _deezer('search', {'q': artist+' '+title, 'limit': 25})
        raw = payload.get('data')
        if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
            raise ValueError('Deezer: неполный ответ поиска.')
        # Search indexes sometimes omit collaborators even though the full
        # track response lists them. One primary-credit query can retrieve it;
        # all candidates are still compared against the original full credit.
        from core.recording_credit import roles
        primary=roles(artist,title)[0]
        if (not raw or not any(related_choice(artist,title,(r.get('artist') or {}).get('name') or '',r.get('title') or '') for r in raw)) and primary and primary[0]!=artist and not cancelled():
            fallback=_deezer('search',{'q':primary[0]+' '+title,'limit':25}).get('data')
            if not isinstance(fallback,list) or any(not isinstance(row,dict) for row in fallback):
                raise ValueError('Deezer: неполный ответ дополнительного поиска.')
            raw=fallback
        details_left = 5
        for track in raw:
            if cancelled():
                break
            credit = (track.get('artist') or {}).get('name') or ''
            track_title = track.get('title') or ''
            # Some responses keep the version only in title_version.
            version = str(track.get('title_version') or '').strip()
            if version and version.casefold().strip('() ') not in track_title.casefold():
                track_title += ' '+version
            if not related_choice(artist, title, credit, track_title):
                continue
            track_id = str(track.get('id') or '')
            if not track_id.isdigit():
                continue
            if not same_track(artist, title, credit, track_title) and details_left:
                details_left -= 1
                try:
                    detail = _deezer('track/'+track_id)
                    if str(detail.get('id')) == track_id:
                        names = list(dict.fromkeys(c['name'] for c in detail.get('contributors') or []
                            if isinstance(c, dict) and c.get('name') and c.get('role') in ('Main', 'Featured')))
                        if names:
                            credit = ' & '.join(names)
                except Exception as error:
                    errors.append(str(error))
            if not related_choice(artist, title, credit, track_title):
                continue
            album = track.get('album') or {}
            urls = list(dict.fromkeys(album[k] for k in ('cover_xl', 'cover_big', 'cover_medium') if album.get(k)))
            if not urls:
                continue
            rows.append({'image_url': urls[0], 'image_fallbacks': urls[1:], 'source': 'Deezer',
                         'source_url': 'https://www.deezer.com/track/'+track_id,
                         'release': album.get('title'), 'release_id': 'deezer:'+str(album.get('id')),
                         'artist': credit, 'title': track_title, 'duration': track.get('duration')})
    except Exception as error:
        message=str(error)
        errors.append(message if message.startswith('Deezer') else 'Deezer: '+message)
    return rows, errors


def search_discogs(artist, title, cancelled=lambda: False):
    from core.extra_catalogs import _request, _tracks, _credit, _duration
    rows, errors = [], []
    try:
        found = _request('discogs', '/database/search',
                         {'type': 'release', 'artist': artist, 'track': title, 'per_page': 5, 'page': 1}, cache=False)
        seen = set()
        for item in found['results']:
            if cancelled():
                break
            release_id = str(item.get('id') or '')
            if not release_id.isdigit() or release_id in seen:
                continue
            seen.add(release_id)
            release = _request('discogs', '/releases/'+release_id, {}, cache=False)
            # A secondary image can be the back, a booklet or a label.
            image = next((p for p in release.get('images') or []
                          if p.get('type') == 'primary' and p.get('uri')), None)
            if not image:
                continue
            for track in _tracks(release['tracklist']):
                credit = _credit(track.get('artists') or release.get('artists'))
                if not related_choice(artist, title, credit, track['title']):
                    continue
                rows.append({'image_url': image['uri'], 'source': 'Discogs',
                    'source_url': 'https://www.discogs.com/release/'+release_id,
                    'release': release.get('title'), 'release_id': 'discogs:'+release_id,
                    'artist': credit, 'title': track['title'], 'duration': _duration(track.get('duration')),
                    'date': release.get('released'), 'country': release.get('country')})
    except Exception as error:
        errors.append('Discogs: '+str(error))
    return rows, errors


def _lastfm_track(artist,title,cancelled):
    from core.extra_catalogs import _request, CatalogError
    from core.recording_credit import roles
    primary=roles(artist,title)[0]
    for query_artist in dict.fromkeys([artist,*primary[:1]]):
        if cancelled():return None
        try:
            payload=_request('lastfm','',{'method':'track.getInfo','artist':query_artist,'track':title,'autocorrect':0},cache=False)
            track=payload['track']
            if related_choice(artist,title,(track.get('artist') or {}).get('name') or '',track.get('name') or ''):
                return track
        except CatalogError as error:
            if str(error)!='lastfm: api_6':raise
    return None


def search_lastfm(artist, title, cancelled=lambda: False):
    from core.extra_catalogs import _request, CatalogError
    if cancelled():
        return [], []
    try:
        track = _lastfm_track(artist,title,cancelled)
        if track is None:return [], []
        credit = (track.get('artist') or {}).get('name') or ''
        if not related_choice(artist, title, credit, track['name']):
            return [], []
        album = track.get('album') or {}
        images = {p.get('size'): p.get('#text') for p in album.get('image') or [] if isinstance(p, dict)}
        urls = list(dict.fromkeys(images[size] for size in ('mega', 'extralarge', 'large', 'medium') if images.get(size)))
        # Last.fm's documented/default missing-art placeholder is not artwork.
        urls = [url for url in urls if '2a96cbd8b46e442fc41c2b86b821562f' not in url]
        if not urls:
            return [], []
        urls = ['https://'+url[7:] if url.startswith('http://') else url for url in urls]
        try:
            duration = float(track.get('duration') or 0)/1000 or None
        except (TypeError, ValueError):
            duration = None
        source_url = track.get('url') or ''
        if urlparse(source_url).hostname not in ('www.last.fm', 'last.fm'):
            source_url = ''
        elif source_url.startswith('http://'):
            source_url = 'https://'+source_url[7:]
        return [{'image_url': urls[0], 'image_fallbacks': urls[1:], 'source': 'Last.fm',
                 'source_url': source_url, 'release': album.get('title'),
                 'release_id': 'lastfm:'+str(album.get('mbid') or album.get('url') or album.get('title')),
                 'artist': credit, 'title': track['name'], 'duration': duration,
                 'review_reason': 'Last.fm связывает трек с альбомом; конкретное издание не подтверждено.'}], []
    except CatalogError as error:
        if str(error) == 'lastfm: api_6':
            return [], []
        return [], ['Last.fm: '+str(error)]
    except Exception as error:
        return [], ['Last.fm: '+str(error)]
