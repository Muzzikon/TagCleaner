"""Artwork discovery owns its queries; tag-editor release choices are not input."""
import re
from concurrent.futures import ThreadPoolExecutor
from core.duration_policy import duration_difference
from core.cover_query import default_query, same_cover_track


def match_evidence(data, artist, title, duration):
    identity = same_cover_track(data.get('artist',''), data.get('title',''), artist, title)
    difference = duration_difference(data.get('duration'), duration)
    reasons = []
    if not identity: reasons.append('Исполнитель или версия отличаются от сохранённых тегов.')
    if difference is None: reasons.append('Нет длительности для сравнения.')
    elif difference > 5: reasons.append(f'Длительность отличается на {difference:.1f} с.')
    return {'needs_review': bool(reasons), 'match_note': ' '.join(reasons) or 'Совпадают исполнитель, название с версией и длительность.'}


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
                        'date':c.get('date'),'country':c.get('country')})
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


def discover(artist, title, album='', progress=None, cancelled=None):
    """Overlap independent providers while preserving each provider's throttle."""
    from core import cover_catalogs
    from core import cover_pages
    cancelled=cancelled or (lambda:False)
    with ThreadPoolExecutor(max_workers=6,thread_name_prefix='cover-catalog') as pool:
        publication_version=bool(re.search(r'\b(remix|bootleg|edit|flip|vip|mashup)\b',title,re.I))
        futures = [(label, pool.submit(_discover, artist, title, '', progress, cancelled, provider))
                   for label, provider in (('Apple Music', 'apple'), ('MusicBrainz', 'musicbrainz'))]
        if cover_catalogs.deezer_enabled():
            futures.append(('Deezer', pool.submit(cover_catalogs.search_deezer, artist, title, cancelled)))
        for provider in cover_catalogs.extra_providers():
            futures.append((provider, pool.submit(getattr(cover_catalogs,'search_'+provider), artist, title, cancelled)))
        public_future=pool.submit(cover_pages.search_soundcloud,artist,title,cancelled) if cover_pages.soundcloud_enabled() and publication_version else None
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
        if cover_pages.soundcloud_enabled() and not cancelled() and (public_future is not None or not results):
            if public_future is None:public_future=pool.submit(cover_pages.search_soundcloud,artist,title,cancelled)
            try:
                rows, warnings = public_future.result()
            except Exception as error:
                errors.append(f'SoundCloud: {error}')
            else:
                errors.extend(warnings)
                results = rows + results if publication_version else results + rows
    if album.strip():results.sort(key=lambda row:album.casefold() not in (row.get('release') or '').casefold())
    return results,errors
