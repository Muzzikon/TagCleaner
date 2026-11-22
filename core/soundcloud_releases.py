"""Import public release facts from a SoundCloud set with a matching track."""
import json
import re
from datetime import date
from urllib.parse import urlparse

from requests import RequestException

from core.cover_urls import edition_source_url


def _set_data(text):
    payload=re.search(r'window\.__sc_hydration\s*=\s*(\[.*?\])\s*;?\s*</script>',text,re.S)
    if not payload:
        raise ValueError('SoundCloud не вернул список треков издания.')
    data=json.loads(payload[1])
    return next((item['data'] for item in data if isinstance(item,dict)
                 and item.get('hydratable')=='playlist' and isinstance(item.get('data'),dict)),{})


def _edition_date(value):
    try:return date.fromisoformat(str(value or '')[:10]).isoformat()
    except ValueError:return ''


def contexts(data,url,artist,title):
    from core.cover_pages import _identity, _soundcloud_artwork
    from core.release_discovery import same_track

    caption=str(data.get('title') or '').strip()
    album_artist,album=_identity(caption)
    label=str(data.get('label_name') or '').strip()
    release_date=_edition_date(data.get('release_date'))
    kinds={'album':'Album','ep':'EP','single':'Single','compilation':'Compilation'}
    kind=kinds.get(str(data.get('set_type') or '').casefold(),'')
    if not kind:
        match=re.search(r'\b(EP|Single|Album)\s*$',album,re.I)
        if match:kind=kinds[match[1].casefold()]
    # Some label EPs are published as ordinary sets. Require both an explicit
    # release designation and label/date metadata when the album flag is absent.
    if not caption or not (data.get('is_album') or (kind and label and release_date)):
        raise ValueError('Страница SoundCloud не указывает данные издания. Нужна страница альбома или EP от издателя.')
    tracks=data.get('tracks') or []
    if not isinstance(tracks,list):raise ValueError('SoundCloud вернул некорректный список треков.')
    total=data.get('track_count')
    complete=type(total) is int and total==len(tracks) and all(isinstance(t,dict) and t.get('id') for t in tracks)
    result=[]
    for number,track in enumerate(tracks,1):
        if not isinstance(track,dict) or not track.get('title'):continue
        credit,track_title=_identity(track['title'])
        if label and track_title.casefold().endswith(f' [{label}]'.casefold()):
            track_title=track_title[:-(len(label)+3)].rstrip()
        if not credit:
            publisher=track.get('publisher_metadata') or {}
            credit=str(publisher.get('artist') or '').strip()
        if not credit or not same_track(artist,title,credit,track_title):continue
        row={'source':'soundcloud','source_url':url,'release_id':'soundcloud:'+str(data.get('id') or url),
            'track_id':str(track.get('id') or ''),'title':album,'primary_type':kind or 'Album',
            'album_artist_credit':album_artist,'date':release_date,'track_title':track_title,'track_artist_credit':credit,
            'track_number':str(number) if complete else '', 'track_total':total if complete else None,
            'duration':track['duration']/1000 if isinstance(track.get('duration'),(int,float)) and track['duration']>0 else None,
            'source_note':'Дата из метаданных этого издания в SoundCloud. Номер — позиция в списке треков.'}
        artwork=str(data.get('artwork_url') or '')
        if _soundcloud_artwork(artwork):row['image_url']=artwork
        result.append(row)
    return result


def import_release(url,artist,title):
    from core.cover_pages import _soundcloud_text

    url=edition_source_url(url)
    if urlparse(url).hostname!='soundcloud.com':
        raise ValueError('Нужна ссылка на издание SoundCloud.')
    result=contexts(_set_data(_soundcloud_text(url)),url,artist,title)
    if not result:raise ValueError('На странице SoundCloud не найден трек с совпадающими исполнителями и версией.')
    return result


def search_editions(artist,title,album_query=''):
    from core.cover_pages import _SearchPage, _soundcloud_text, soundcloud_enabled
    from core.recording_credit import roles

    if not soundcloud_enabled():return []
    primary,_=roles(artist,title)
    if not primary:return []
    query=' '.join(filter(None,(primary[0],album_query)))
    page=_SearchPage(_soundcloud_text('https://soundcloud.com/search/sets',{'q':query}),sets=True)
    if not page.has_results_markup:
        raise ValueError('SoundCloud не вернул результаты поиска.')
    # Prefer artist-titled release sets over mixes and fan playlists. Validate
    # the actual release metadata and exact track on each fetched page.
    rows=list(dict(page.rows).items())
    def priority(row):
        _,caption=row
        return (bool(album_query) and album_query.casefold() not in caption.casefold(),
                primary[0].casefold() not in caption.casefold(),
                not bool(re.search(r'\b(?:EP|Album|Single)\b',caption,re.I)))
    rows.sort(key=priority)
    result=[];failure=None
    for url,_ in rows[:3]:
        try:result.extend(import_release(url,artist,title))
        except ValueError:continue
        except RequestException as error:
            if error.response is not None and error.response.status_code in (404,410):continue
            failure=error
            if error.response is not None and error.response.status_code==429:break
    if failure is not None and not result:raise failure
    return result


def artwork_candidates(data,artist,title):
    from core.cover_pages import _soundcloud_artwork
    from core.release_credits import matches

    rows=[]
    editions=[*(data.get('_ui_extra_releases') or []),data.get('selected_release_context') or {}]
    for edition in editions:
        image=edition.get('image_url') or ''
        if edition.get('source')!='soundcloud' or not _soundcloud_artwork(image) or not matches(edition,artist,title):
            continue
        original=re.sub(r'-[a-zA-Z0-9]+(\.(?:jpg|jpeg|png))$',r'-original\1',image)
        rows.append({'image_url':original,'image_fallbacks':[image],'source':'SoundCloud',
            'source_url':edition.get('source_url'),'release':edition['title'],'release_id':edition['release_id'],
            'release_type':edition.get('primary_type'),'date':edition.get('date'),'artist':edition['track_artist_credit'],
            'title':edition['track_title'],'duration':edition.get('duration')})
    return rows
