"""Explicit edition discovery. Results are choices, never automatic writes."""
import re
from core.naming import canonical_track_title
from core.matcher import clean_title
from core.audd_matcher import normalize_text
from core.identity_fusion import identities_match

def same_track(artist,title,other_artist,other_title,*,known_entities=()):
    # Do not accept a fuzzy neighbouring song or an original instead of VIP.
    from core.title_identity import comparison_title
    from core.recording_credit import compatible
    if not compatible(artist,title,other_artist,other_title,known_entities):return False
    title=comparison_title(artist,title)
    other_title=comparison_title(other_artist,other_title)
    # Participants were checked as whole credits above. Remove their display
    # captions before comparing the song and version, not any version words.
    from core.release_credits import remove_primary_features
    from core.recording_credit import roles
    names=roles(artist,title,known_entities)[1]+roles(other_artist,other_title,known_entities)[1]
    title=remove_primary_features(title,names)
    other_title=remove_primary_features(other_title,names)
    left=normalize_text(clean_title(canonical_track_title(title)))
    right=normalize_text(clean_title(canonical_track_title(other_title)))
    return bool(left and left==right and identities_match(
        {'artist':other_artist,'title':title},{'artist':other_artist,'title':other_title}))

def apple_context(row,album,country='US'):
    """Keep release facts from the collection and track facts from its song."""
    raw=row.get('raw') or {};name=album.get('collectionName') or row.get('album','')
    aa=album.get('artistName') or '';discs=raw.get('discCount')
    return {'source':'itunes','source_url':raw.get('collectionViewUrl') or row.get('url'),
        'release_id':'itunes:'+str(row['collection_id']),'track_id':str(row.get('track_id','')),
        'title':name,'primary_type':'Single' if re.search(r'\s[-–—]\sSingle$',name,re.I) else 'EP' if re.search(r'\s[-–—]\sEP$|\sEP$',name,re.I) else '',
        'secondary_types':['Compilation'] if aa.casefold() in ('various artists','various') else [],
        'date':str(album.get('releaseDate') or '')[:10],
        'album_artist_credit':aa,'track_title':row.get('title',''),'track_artist_credit':row.get('artist',''),
        'track_number':str(row.get('track_number') or ''),
        'track_total':row.get('track_count') if discs==1 else None,
        'disc_number':raw.get('discNumber'),'disc_total':discs,'country':country,
        **({'duration':float(raw['trackTimeMillis'])/1000} if raw.get('trackTimeMillis') else {})}


def _agreed_shazam(data,compatible):
    identity=data.get('shazam_identity') or {};best=identity.get('best') or {}
    agreed=identity.get('status')=='STRONG' or (identity.get('status')=='PARTIAL' and (identity.get('agreement_count') or 0)>=2)
    if not agreed or identity.get('identity_ambiguous') or identity.get('verification_incomplete'):
        return {}
    if not compatible(best.get('artist') or '',best.get('title') or ''):
        return {}
    return (best.get('raw') or {}).get('track') or {}


def _audio_apple_editions(data,compatible,request):
    """Resolve a known Shazam album ID, without another audio upload or search."""
    track=_agreed_shazam(data,compatible)
    album_id=str(track.get('albumadamid') or '')
    if not re.fullmatch(r'[1-9][0-9]{0,17}',album_id):return []
    for country in ('US','GB','AU'):
        payload=request('https://itunes.apple.com/lookup',{'id':album_id,'entity':'song','limit':200,'country':country},'itunes')
        if not isinstance(payload,dict) or not isinstance(payload.get('results'),list) or payload.get('errorMessage'):
            raise ValueError('Неполный ответ Apple Music по известному альбому.')
        rows=payload['results']
        if any(not isinstance(r,dict) for r in rows):raise ValueError('Некорректный состав ответа Apple Music.')
        album=next((r for r in rows if r.get('wrapperType')=='collection' and str(r.get('collectionId'))==album_id),None)
        if album is None:continue
        result=[]
        for raw in rows:
            if raw.get('kind')!='song' or str(raw.get('collectionId'))!=album_id:continue
            if not compatible(raw.get('artistName') or '',raw.get('trackName') or ''):continue
            row={'raw':raw,'collection_id':album_id,'track_id':raw.get('trackId'),
                 'title':raw.get('trackName') or '', 'artist':raw.get('artistName') or '',
                 'track_number':raw.get('trackNumber'),'track_count':raw.get('trackCount')}
            result.append(apple_context(row,album,country))
        if result:return result
    return []


def _audio_recording_ids(data,compatible):
    from core.duration_policy import duration_requires_review
    from core.isrc_resolver import lookup_recordings_by_isrc, normalize_isrc
    from core.naming import canonical_artist_credit_from_items

    isrc=normalize_isrc(_agreed_shazam(data,compatible).get('isrc'))
    if not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{3}[0-9]{7}',isrc):return []
    return list(dict.fromkeys(r['id'] for r in lookup_recordings_by_isrc(isrc)
        if r.get('id') and compatible(canonical_artist_credit_from_items(r.get('artist-credit')),r.get('title') or '')
        and not duration_requires_review(data.get('duration'), (r.get('length') or 0)/1000)))[:3]


def edition_matches_query(context,query):
    query_key=normalize_text(query)
    if not query_key:return False
    titles=[context.get('title') or '',*(context.get('search_aliases') or [])]
    if any(query_key in normalize_text(title) for title in titles):return True
    # The catalogue may call a one-track single simply "Song" while the
    # store and the user's query also name its exact version.
    artist=context.get('track_artist_credit') or ''
    return (context.get('primary_type')=='Single' and str(context.get('track_total'))=='1'
            and same_track(artist,query,artist,context.get('track_title') or ''))


def discover(data,artist,title,album_query='',source_url='',progress=None):
    from core.catalog_search_resolver import search_itunes,search_musicbrainz,_request_json
    from core.musicbrainz_client import get_official_release_contexts
    from core.change_planner import _musicbrainz_for_final_identity
    contexts=[];warnings=[]
    from core.verified_releases import find as verified_editions
    from core.release_credits import matches, recognized_credit
    from core.title_identity import strip_download_label
    artist, title, album_query = (' '.join(value.split()) for value in (artist, title, album_query))
    recognized = recognized_credit(data, artist, title)
    title = strip_download_label(title) or title
    verified=verified_editions(artist,title)
    def compatible(other_artist,other_title):
        return (same_track(artist,title,other_artist,other_title)
                or bool(recognized and same_track(recognized['artist'],recognized['title'],other_artist,other_title))
                or any(matches(c,other_artist,other_title) for c in verified))
    queries = [(artist, title)]
    if recognized and recognized != {'artist':artist, 'title':title}:
        queries.insert(0, (recognized['artist'], recognized['title']))
    queries += [(c['track_artist_credit'],title) for c in verified if c.get('track_artist_credit')]
    queries = list(dict.fromkeys(queries))[:3 if len(queries)>1 and recognized else 2]
    def stage(message):
        if progress:progress(message)
    if source_url:
        # An explicit source should not wait for unrelated catalog searches.
        from urllib.parse import urlparse
        from core.cover_urls import edition_source_url
        source_url=edition_source_url(source_url)
        if urlparse(source_url).hostname=='soundcloud.com':
            from core.soundcloud_releases import import_release
            source_name='SoundCloud'
        else:
            from core.bandcamp_releases import import_release
            source_name='Bandcamp'
        stage('Читаем выбранную страницу '+source_name)
        try:contexts.extend(import_release(source_url,artist,title))
        except Exception as error:warnings.append('Импорт '+source_name+' не завершён: '+str(error))
        return merge_options(data,artist,title,album_query,contexts,warnings)
    try:
        stage('Проверяем известное из распознавания издание Apple Music')
        contexts.extend(_audio_apple_editions(data,compatible,_request_json))
    except Exception:warnings.append('Apple Music не вернул полные данные известного альбома; поиск по названию продолжен.')
    mb=_musicbrainz_for_final_identity(data)
    mb_artist=mb.get('artist-credit') or []
    from core.naming import canonical_artist_credit_from_items
    recording_ids=[]
    if mb.get('id') and compatible(canonical_artist_credit_from_items(mb_artist),mb.get('title','')):
        recording_ids.append(mb['id'])
    if not recording_ids:
        try:
            recording_ids.extend(_audio_recording_ids(data,compatible))
        except Exception:
            warnings.append('MusicBrainz не ответил на запрос по ISRC; поиск по названию продолжен.')
    if not recording_ids:
        try:
            stage('Ищем запись в MusicBrainz по текущим исполнителю и названию')
            for search_artist, search_title in queries:
                rows=search_musicbrainz(search_artist,search_title,refresh=True)
                recording_ids=list(dict.fromkeys(r['recording_id'] for r in rows if r.get('recording_id')
                    and compatible(r.get('artist',''),r.get('title',''))))[:3]
                if recording_ids:break
        except Exception:warnings.append('Поиск MusicBrainz не завершён из-за ошибки сервиса. Это не означает, что запись отсутствует.')
    for recording_id in recording_ids:
        stage('Получаем издания найденной записи MusicBrainz')
        try:contexts.extend(get_official_release_contexts(recording_id,refresh=True))
        except Exception:warnings.append('MusicBrainz не вернул издания; прежние варианты сохранены.')
    if album_query and not any(edition_matches_query(c,album_query) for c in [*contexts,*verified]):
        from core.recording_credit import roles
        from core.release_credits import remove_primary_features
        search_artist,search_title=queries[0]
        title_without_credit=remove_primary_features(search_title,roles(search_artist,search_title)[1])
        try:
            stage('Ищем запись на указанном издании в MusicBrainz')
            rows=search_musicbrainz(search_artist,title_without_credit,refresh=True,release_title=album_query)
            ids=list(dict.fromkeys(r['recording_id'] for r in rows if r.get('recording_id')
                and r['recording_id'] not in recording_ids and compatible(r.get('artist',''),r.get('title',''))))[:2]
            for recording_id in ids:
                contexts.extend(get_official_release_contexts(recording_id,refresh=True))
        except Exception:
            warnings.append('MusicBrainz не завершил поиск по названию издания.')
    try:
        stage('Ищем издания в Apple Music / iTunes')
        for search_artist, search_title in queries:
            rows=search_itunes(search_artist,search_title,refresh=True)
            rows=[r for r in rows if compatible(r.get('artist',''),r.get('title',''))]
            if rows:break
        ids=list(dict.fromkeys(str(r['collection_id']) for r in rows if r.get('collection_id')))
        if len(ids)>25:warnings.append('Найдено больше 25 изданий; уточните название альбома или сборника для остальных вариантов.')
        albums={}
        if ids:
            payload=_request_json('https://itunes.apple.com/lookup',{'id':','.join(ids[:25]),'country':'US'},'itunes')
            albums={str(a.get('collectionId')):a for a in payload.get('results',[]) if a.get('wrapperType')=='collection'}
        for r in rows:
            a=albums.get(str(r.get('collection_id')))
            if not a:continue # Song.releaseDate is not proof of the album's date/artist.
            contexts.append(apple_context(r,a))
    except Exception:warnings.append('Apple Music / iTunes не ответил; можно повторить поиск или заполнить поля вручную.')
    from ui.editor import release_options
    known=[*contexts,*verified,*(c for c in release_options(data) if matches(c,artist,title))]
    only_compilations=bool(known) and all(
        c.get('primary_type')=='Compilation' or 'Compilation' in (c.get('secondary_types') or []) for c in known)
    if not known or only_compilations or (album_query and not any(edition_matches_query(c,album_query) for c in known)):
        from core.soundcloud_releases import search_editions
        try:
            stage('Проверяем опубликованные издания на SoundCloud')
            contexts.extend(search_editions(artist,title,album_query))
        except Exception:
            warnings.append('SoundCloud не завершил поиск изданий. Можно добавить ссылку на страницу издателя.')
    return merge_options(data,artist,title,album_query,contexts,warnings)


def merge_options(data,artist,title,album_query,contexts,warnings):
    from ui.editor import release_options
    from core.verified_releases import find as verified_editions
    from core.release_credits import matches, recognized_credit, matches_recognized_credit
    recognized = recognized_credit(data, artist, title)
    verified=verified_editions(artist,title)
    contexts=[*contexts,*verified]
    # A different edited identity must not acquire old candidate editions.
    old=release_options(data) if same_track(artist,title,data.get('artist',''),data.get('title','')) else []
    seen=set();result=[]
    for c in [*old,*contexts]:
        c=dict(c)
        # A documented whole-credit variant is scoped to this song/version,
        # never a global fuzzy alias or permission to accept a missing coauthor.
        for known in verified:
            if matches(known,c.get('track_artist_credit') or '',c.get('track_title') or ''):
                aliases=[known.get('track_artist_credit'),*(known.get('track_artist_aliases') or [])]
                credit_aliases=list(dict.fromkeys([*(c.get('track_artist_aliases') or []),*[a for a in aliases if a and a!=c.get('track_artist_credit')]]))
                if credit_aliases:c['track_artist_aliases']=credit_aliases
                if (c.get('source'),c.get('release_id'),c.get('track_id'))==(known.get('source'),known.get('release_id'),known.get('track_id')):
                    for field in ('source_note','evidence','verified_at'):
                        if known.get(field):c.setdefault(field,known[field])
        if c.get('track_title'):
            if c.get('track_artist_credit') or c.get('track_artist_credit_items') or c.get('track_artist_aliases'):
                if not matches(c,artist,title) and not matches_recognized_credit(c,recognized):continue
            elif not same_track(artist,title,artist,c['track_title']):continue
        key=(c.get('source'),c.get('release_id'),c.get('track_id'),c.get('track_number'))
        if key in seen:continue
        seen.add(key);result.append(c)
    if album_query.strip():
        result.sort(key=lambda c:not edition_matches_query(c,album_query))
        if not any(edition_matches_query(c,album_query) for c in result):
            warnings.append('По названию издания совпадений в доступных каталогах не найдено. Другие варианты доступны для выбора.')
    return result,warnings
