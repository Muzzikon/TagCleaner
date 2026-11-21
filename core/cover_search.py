"""Artwork discovery owns its queries; tag-editor release choices are not input."""
import re
from concurrent.futures import ThreadPoolExecutor
from core.duration_policy import duration_difference
from core.cover_query import default_query, same_cover_track


def provider_key(source):
    name = str(source).split(':', 1)[0].split(' · ', 1)[0].strip().casefold()
    return {'apple music':'apple', 'musicbrainz':'musicbrainz', 'cover art archive':'musicbrainz',
            'deezer':'deezer', 'discogs':'discogs', 'last.fm':'lastfm', 'lastfm':'lastfm',
            'soundcloud':'soundcloud'}.get(name)


def retry_providers(state):
    """None requests a full retry; an empty list has no automatically recoverable issue."""
    if state.get('diagnosis_outcome') in ('cancelled', 'outdated'):
        return None
    errors = state.get('errors') or []
    if not errors:
        return None if state.get('diagnosis_outcome') in ('error', 'incomplete', 'partial') else []
    terminal = r'JPEG/PNG|мегапиксел|больше 16 МиБ|http_401|api_(?:4|10|26)\b|disabled_or_missing_key|источник отключён'
    providers = set()
    for warning in errors:
        if re.search(terminal, warning, re.I):
            continue
        provider = provider_key(warning)
        if provider is None:
            return None
        providers.add(provider)
    return sorted(providers)


def format_cover_warnings(warnings):
    """Group optional catalog outages and format both current and cached warnings."""
    messages = []
    unavailable = set()
    outages = 0
    for warning in warnings:
        match = re.fullmatch(
            r'(?:(?:Discogs|Last\.fm): )?(discogs|lastfm): (?:paused_)?(network_error|rate_limit|http_5\d{2}|http_403_access_denied)',
            warning, re.IGNORECASE,
        )
        if match:
            provider, code = (value.lower() for value in match.groups())
            source = 'Discogs' if provider == 'discogs' else 'Last.fm'
            unavailable.add(source)
            outages += 1
            reason = {
                'network_error': 'не удалось подключиться',
                'rate_limit': 'достигнут лимит запросов',
                'http_403_access_denied': 'сервис отклонил запрос (HTTP 403)',
            }.get(code, 'временный сбой сервера')
            warning = f'{source}: {reason}. Можно повторить поиск позже.'
        else:
            source_match = re.match(r'^(Deezer|SoundCloud|Apple Music|MusicBrainz|Cover Art Archive|Discogs|Last\.fm):\s*(.*)', warning, re.I)
            if source_match:
                source, detail = source_match.groups()
                status = (re.search(r'\bHTTP[ :]+(403|429|5\d\d)\b', detail, re.I)
                          or re.search(r'\b(403|429|5\d\d) (?:Client|Server) Error\b', detail, re.I))
                transient = re.search(r'время ожидания|времени ожидания|истекло время|timed? ?out|timeout|ошибка соединения|не удалось (?:подключиться|соединиться|дождаться)|ConnectionError|ConnectionPool|DNS:|временный сбой сервера|достигнут лимит запросов', detail, re.I)
                if status or transient:
                    unavailable.add(source)
                    outages += 1
                    code = status.group(1) if status else None
                    reason = ('сервис отклонил запрос (HTTP 403)' if code == '403' else
                              'достигнут лимит запросов (HTTP 429)' if code == '429' else
                              'временный сбой сервера' if code else 'не удалось дождаться ответа сервиса')
                    if not detail.endswith('Можно повторить поиск позже.'):
                        warning = f'{source}: {reason}. Можно повторить поиск позже.'
        messages.append(warning)
    return list(dict.fromkeys(messages)), sorted(unavailable), bool(outages and outages == len(messages))


def match_evidence(data, artist, title, duration):
    identity = same_cover_track(data.get('artist',''), data.get('title',''), artist, title)
    difference = duration_difference(data.get('duration'), duration)
    reasons = []
    if not identity: reasons.append('Исполнитель или версия отличаются от сохранённых тегов.')
    if difference is None: reasons.append('Нет длительности для сравнения.')
    elif difference > 5: reasons.append(f'Длительность отличается на {difference:.1f} с.')
    return {'needs_review': bool(reasons), 'match_note': ' '.join(reasons) or 'Совпадают исполнитель, название с версией и длительность.'}


def cover_release_priority(row):
    if row.get('release_type') == 'Single' or (
        row.get('source') == 'Apple Music' and re.search(r'\s[-–—]\sSingle$', row.get('release') or '', re.I)
    ):
        return 0
    if row.get('publication_kind') == 'track':
        return 1
    return 2


def needs_publication_search(state):
    from core.cover_pages import soundcloud_enabled

    if not soundcloud_enabled() or state.get('track_artwork_checked') or not state.get('query'):
        return False
    choices = [c for c in state.get('candidates', []) if c.get('image_url') and provider_key(c.get('source'))]
    return bool(choices) and all(cover_release_priority(c) == 2 for c in choices)


def _discover(artist, title, album='', progress=None, cancelled=None, provider='all'):
    from core.catalog_search_resolver import search_itunes, search_musicbrainz
    from core.musicbrainz_client import get_official_release_contexts
    progress = progress or (lambda message: None)
    cancelled = cancelled or (lambda: False)
    results, errors = [], []
    from core.cover_catalogs import related_choice
    def matches(a,t): return related_choice(artist,title,a or '',t or '')
    if provider in ('all','apple') and not cancelled():
        progress('Поиск изображений Apple Music')
        try:
            for row in search_itunes(artist,title,refresh=True):
                if cancelled(): break
                if not matches(row.get('artist'),row.get('title')): continue
                raw = row.get('raw') or {}; url = raw.get('artworkUrl100') or ''
                if not url: continue
                results.append({'image_url':re.sub(r'/\d+x\d+bb\.', '/1200x1200bb.',url),
                    'image_fallbacks':list(dict.fromkeys([re.sub(r'/\d+x\d+bb\.', '/600x600bb.',url),url])),
                    'source':'Apple Music','source_url':row.get('url'),'release':row.get('album'),
                    'release_id':'itunes:'+str(row.get('collection_id')),'artist':row.get('artist'),
                    'title':row.get('title'),'duration':row.get('duration'),'date':str(row.get('release_date') or '')[:10]})
        except Exception as error: errors.append('Apple Music: '+str(error))
    if provider in ('all','musicbrainz') and not cancelled():
        progress('Поиск официальных изданий MusicBrainz')
        try:
            recordings = [r for r in search_musicbrainz(artist,title,refresh=True) if r.get('recording_id') and matches(r.get('artist'),r.get('title'))]
            seen = set()
            for recording in recordings[:3]:
                if cancelled(): break
                for c in get_official_release_contexts(recording['recording_id'],refresh=True):
                    rid=str(c.get('release_id') or '')
                    if rid in seen or not re.fullmatch(r'[0-9a-fA-F-]{36}',rid): continue
                    if not matches(c.get('track_artist_credit') or recording['artist'], c.get('track_title') or recording['title']): continue
                    seen.add(rid)
                    results.append({'image_url':'https://coverartarchive.org/release/'+rid+'/front-1200',
                        'image_fallbacks':['https://coverartarchive.org/release/'+rid+'/front-500','https://coverartarchive.org/release/'+rid+'/front'],
                        'source':'Cover Art Archive','source_url':'https://musicbrainz.org/release/'+rid,
                        'release':c.get('title'),'release_id':rid,'artist':c.get('track_artist_credit') or recording['artist'],
                        'title':c.get('track_title') or recording['title'],'duration':c.get('duration') or recording.get('duration'),
                        'date':c.get('date'),'country':c.get('country'),'release_type':c.get('primary_type')})
                    group=str(c.get('release_group_id') or '')
                    if re.fullmatch(r'[0-9a-fA-F-]{36}',group) and 'group:'+group not in seen:
                        seen.add('group:'+group)
                        results.append({'image_url':'https://coverartarchive.org/release-group/'+group+'/front-1200',
                            'fallback_if_missing':'https://coverartarchive.org/release/'+rid+'/front-1200',
                            'image_fallbacks':['https://coverartarchive.org/release-group/'+group+'/front-500','https://coverartarchive.org/release-group/'+group+'/front'],
                            'source':'Cover Art Archive · группа релизов',
                            'source_url':'https://musicbrainz.org/release-group/'+group,
                            'release':c.get('title'), 'release_id':'group:'+group,
                            'artist':c.get('track_artist_credit') or recording['artist'],
                            'title':c.get('track_title') or recording['title'],
                            'duration':c.get('duration') or recording.get('duration'),
                            'review_reason':'Обложка группы релизов; конкретное издание не подтверждено.'})
        except Exception as error: errors.append('MusicBrainz: '+str(error))
    if album.strip(): results.sort(key=lambda r: album.casefold() not in (r.get('release') or '').casefold())
    return results, errors


def discover(artist, title, album='', progress=None, cancelled=None, *, providers=None):
    """Overlap independent providers while preserving each provider's throttle."""
    from core import cover_catalogs
    from core import cover_pages
    cancelled=cancelled or (lambda:False)
    requested = set(providers) if providers is not None else None
    def selected(provider):
        return requested is None or provider in requested
    with ThreadPoolExecutor(max_workers=6,thread_name_prefix='cover-catalog') as pool:
        publication_version=bool(re.search(r'\b(remix|bootleg|edit|flip|vip|mashup)\b',title,re.I))
        futures = [(label, pool.submit(_discover, artist, title, '', progress, cancelled, provider))
                   for label, provider in (('Apple Music', 'apple'), ('MusicBrainz', 'musicbrainz'))
                   if selected(provider)]
        if selected('deezer') and cover_catalogs.deezer_enabled():
            futures.append(('Deezer', pool.submit(cover_catalogs.search_deezer, artist, title, cancelled)))
        for provider in cover_catalogs.extra_providers():
            if selected(provider):
                futures.append((provider, pool.submit(getattr(cover_catalogs,'search_'+provider), artist, title, cancelled)))
        public_future=pool.submit(cover_pages.search_soundcloud,artist,title,cancelled) if selected('soundcloud') and cover_pages.soundcloud_enabled() and (publication_version or requested is not None) else None
        # Keep the original source order, regardless of which service is faster.
        results,errors=[],[]
        for label, future in futures:
            try:
                rows, warnings = future.result()
            except Exception as error:
                errors.append(f'{label}: {error}')
                continue
            results.extend(rows)
            errors.extend(warnings)
        if selected('soundcloud') and cover_pages.soundcloud_enabled() and not cancelled() and public_future is None:
            # An EP match does not tell us about separate track artwork.
            # An unverified CAA URL does not establish that a cover exists.
            has_single = any(cover_release_priority(row) == 0
                             and row.get('source') in ('Apple Music', 'Deezer', 'Discogs')
                             and same_cover_track(artist, title, row.get('artist'), row.get('title'))
                             for row in results)
            if not has_single:
                if progress:
                    progress('Поиск отдельных обложек SoundCloud')
                public_future = pool.submit(cover_pages.search_soundcloud, artist, title, cancelled)
        if public_future is not None and not cancelled():
            try:
                rows, warnings = public_future.result()
            except Exception as error:
                errors.append(f'SoundCloud: {error}')
            else:
                errors.extend(warnings)
                results.extend(rows)
        if requested is not None:
            enabled = {'apple', 'musicbrainz', *cover_catalogs.extra_providers()}
            if cover_catalogs.deezer_enabled(): enabled.add('deezer')
            if cover_pages.soundcloud_enabled(): enabled.add('soundcloud')
            names = {'deezer':'Deezer', 'discogs':'Discogs', 'lastfm':'Last.fm', 'soundcloud':'SoundCloud'}
            errors.extend(names.get(provider, provider)+': источник отключён или ключ не настроен.'
                          for provider in sorted(requested-enabled))
    album_hint = album.strip().casefold()
    results.sort(key=lambda row: (cover_release_priority(row), album_hint not in (row.get('release') or '').casefold()))
    return results,errors
