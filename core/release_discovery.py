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

def apple_context(row,album):
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
        'disc_number':raw.get('discNumber'),'disc_total':discs,'country':'US',
        **({'duration':float(raw['trackTimeMillis'])/1000} if raw.get('trackTimeMillis') else {})}


def _audio_apple_editions(data,compatible,request):
    """Resolve a known Shazam album ID, without another audio upload or search."""
    identity=data.get('shazam_identity') or {};best=identity.get('best') or {}
    if identity.get('status')!='STRONG' or identity.get('identity_ambiguous') or identity.get('verification_incomplete'):return []
    if not compatible(best.get('artist') or '',best.get('title') or ''):return []
    track=(best.get('raw') or {}).get('track') or {}
    album_id=str(track.get('albumadamid') or '')
    if not re.fullmatch(r'[1-9][0-9]{0,17}',album_id):return []
    payload=request('https://itunes.apple.com/lookup',{'id':album_id,'entity':'song','limit':200,'country':'US'},'itunes')
    if not isinstance(payload,dict) or not isinstance(payload.get('results'),list) or payload.get('errorMessage'):
        raise ValueError('Неполный ответ Apple Music по известному альбому.')
    rows=payload['results']
    if any(not isinstance(r,dict) for r in rows):raise ValueError('Некорректный состав ответа Apple Music.')
    album=next((r for r in rows if r.get('wrapperType')=='collection' and str(r.get('collectionId'))==album_id),None)
    if not album:raise ValueError('Известный альбом недоступен в выбранной витрине Apple Music.')
    result=[]
    for raw in rows:
        if raw.get('kind')!='song' or str(raw.get('collectionId'))!=album_id:continue
        if not compatible(raw.get('artistName') or '',raw.get('trackName') or ''):continue
        row={'raw':raw,'collection_id':album_id,'track_id':raw.get('trackId'),
             'title':raw.get('trackName') or '', 'artist':raw.get('artistName') or '',
             'track_number':raw.get('trackNumber'),'track_count':raw.get('trackCount')}
        result.append(apple_context(row,album))
    return result


def discover(data,artist,title,album_query='',source_url='',progress=None):
    from core.catalog_search_resolver import search_itunes,search_musicbrainz,_request_json
    from core.musicbrainz_client import get_official_release_contexts
    from core.change_planner import _musicbrainz_for_final_identity
    from ui.editor import release_options
    contexts=[];warnings=[]
    from core.verified_releases import find as verified_editions
    from core.release_credits import matches
    verified=verified_editions(artist,title)
    def compatible(other_artist,other_title):
        return same_track(artist,title,other_artist,other_title) or any(matches(c,other_artist,other_title) for c in verified)
    search_artists=list(dict.fromkeys([artist,*[c.get('track_artist_credit') for c in verified if c.get('track_artist_credit')]]))[:2]
    def stage(message):
        if progress:progress(message)
    if source_url:
        # An explicit source should not wait for unrelated catalog searches.
        from core.bandcamp_releases import import_release
        stage('Читаем выбранную страницу Bandcamp')
        try:contexts.extend(import_release(source_url,artist,title))
        except Exception as error:warnings.append('Импорт Bandcamp не завершён: '+str(error))
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
    else:
        try:
            stage('Ищем запись в MusicBrainz по текущим исполнителю и названию')
            for search_artist in search_artists:
                rows=search_musicbrainz(search_artist,title,refresh=True)
                recording_ids=list(dict.fromkeys(r['recording_id'] for r in rows if r.get('recording_id')
                    and compatible(r.get('artist',''),r.get('title',''))))[:3]
                if recording_ids:break
        except Exception:warnings.append('Поиск MusicBrainz не завершён из-за ошибки сервиса. Это не означает, что запись отсутствует.')
    for recording_id in recording_ids:
        stage('Получаем издания найденной записи MusicBrainz')
        try:contexts.extend(get_official_release_contexts(recording_id,refresh=True))
        except Exception:warnings.append('MusicBrainz не вернул издания; прежние варианты сохранены.')
    try:
        # Album text is not part of a song title. Always discover the actual
        # recording first; use the optional album text only to rank choices.
        stage('Ищем издания в Apple Music / iTunes')
        for search_artist in search_artists:
            rows=search_itunes(search_artist,title,refresh=True)
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
    return merge_options(data,artist,title,album_query,contexts,warnings)


def merge_options(data,artist,title,album_query,contexts,warnings):
    from ui.editor import release_options
    from core.verified_releases import find as verified_editions
    from core.release_credits import matches
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
                if not matches(c,artist,title):continue
            elif not same_track(artist,title,artist,c['track_title']):continue
        key=(c.get('source'),c.get('release_id'),c.get('track_id'),c.get('track_number'))
        if key in seen:continue
        seen.add(key);result.append(c)
    if album_query.strip():
        query=normalize_text(album_query)
        def edition_text(c):return normalize_text(' '.join([c.get('title',''),*c.get('search_aliases',[])]))
        result.sort(key=lambda c:query not in edition_text(c))
        if not any(query in edition_text(c) for c in result):
            warnings.append('Совпадений по названию сборника в доступных источниках нет. Остальные издания сохранены; это не означает, что сборника не существует.')
    return result,warnings
