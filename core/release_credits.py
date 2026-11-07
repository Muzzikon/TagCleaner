"""Concrete release/track credits outrank local feat. guesses and recordings."""
import re
from core.naming import (artist_roles_from_items,canonical_artist_credit_from_items,
    canonical_entity_list,split_feature_credit,split_credit_entities,canonical_track_title)
from core.matcher import split_artists
from core.duration_policy import duration_requires_review

def key(value):
    return re.sub(r'\s*[:：]\s*',':',' '.join(str(value or '').split()).casefold())

def matches(context,artist,title):
    from core.release_discovery import same_track
    credits=[context.get('track_artist_credit'),canonical_artist_credit_from_items(context.get('track_artist_credit_items')),
             *(context.get('track_artist_aliases') or [])]
    known=[n for i in context.get('track_artist_credit_items') or [] if isinstance(i,dict)
           for n in (i.get('name'),(i.get('artist') or {}).get('name')) if n]
    from core.naming import extract_feature_entities_from_title
    _,role,guest=split_feature_credit(artist)
    featured=split_artists(guest,known_entities=known) if guest and role=='feat.' else []
    hint=extract_feature_entities_from_title(title,known_entities=known)
    if hint.get('role')=='feat.':featured+=hint.get('artists') or []
    if featured:
        expected=parse(context).get('featured') or []
        if expected:
            from core.recording_credit import same_names
            repairs={key(k):v for k,v in (context.get('track_feature_repairs') or {}).items()} if context.get('credit_roles_verified') else {}
            if not same_names([repairs.get(key(n),n) for n in featured],expected):return False
    return any(c and same_track(artist,title,c,context.get('track_title') or '',known_entities=known) for c in credits)

def parse(context,current=False):
    items=context.get('track_artist_credit_items') or []
    primary_aliases=[];aliases=[];entity_names={}
    if items:
        for item in items:
            if isinstance(item,dict):
                historic=item.get('name') or (item.get('artist') or {}).get('name') or ''
                modern_name=(item.get('artist') or {}).get('name') or historic
                for alias in (historic,modern_name):entity_names[key(alias)]=modern_name if current else historic
        modern=[{**i,'name':(i.get('artist') or {}).get('name') or i.get('name')} if isinstance(i,dict) else i for i in items]
        for variant in (items,modern):
            variant_roles=artist_roles_from_items(variant)
            primary_aliases+=variant_roles['primary'];aliases+=variant_roles['primary']+variant_roles['featured']
        if current:items=modern
        roles=artist_roles_from_items(items);exact=True
    else:
        primary,role,guest=split_feature_credit(context.get('track_artist_credit') or '')
        roles={'primary':split_credit_entities(primary),'featured':split_credit_entities(guest) if guest else [],'feature_role':role or 'feat.'}
        exact=bool(guest or context.get('credit_roles_verified'))
    # A storefront's structured artist array can still be a flat list of
    # everyone involved. An explicit feat. in this same track title carries
    # role information that the array alone does not contain.
    from core.naming import extract_feature_entities_from_title
    title_credit=extract_feature_entities_from_title(context.get('track_title') or '',known_entities=roles['primary']+roles['featured']+aliases)
    guests=[entity_names.get(key(n),n) for n in title_credit.get('artists') or []] if title_credit.get('role')=='feat.' else []
    source_conflict=bool(guests and roles['featured'] and
                         {key(n) for n in guests}!={key(n) for n in roles['featured']})
    if guests and not source_conflict:
        guest_keys={key(n) for n in guests}
        remaining=[n for n in roles['primary'] if key(n) not in guest_keys]
        if remaining:
            roles={**roles,'primary':remaining,'featured':list(dict.fromkeys([*roles['featured'],*guests])),
                   'feature_role':title_credit.get('role') or 'feat.'}
            primary_aliases=[n for n in primary_aliases if key(entity_names.get(key(n),n)) not in guest_keys]
            exact=True
    return {**roles,'primary_aliases':primary_aliases or roles['primary'],'aliases':aliases,
            'exact':exact,'context':context,'source':context.get('source') or 'Кредит трека релиза',
            **({'conflict':True,'reason':'В источнике расходятся приглашённые участники в artist credit и Title. Artist и Title сохранены; проверьте кредит вручную.'} if source_conflict else {})}

def _check_local_roles(data,artist,title,result):
    """A plain catalog joint list cannot silently erase a saved explicit feat."""
    from core.naming import extract_feature_entities_from_title
    from core.release_discovery import same_track
    saved_artist=data.get('artist') or '';saved_title=data.get('title') or ''
    titles=[title]
    same_saved=same_track(artist,title,saved_artist,saved_title)
    if result.get('conflict'):
        return {'conflict':True,'reason':result['reason'],'preserve_identity':
                {'artist':saved_artist,'title':saved_title} if same_saved else {'artist':artist,'title':title}}
    if result['context'].get('credit_roles_verified'):return result
    if same_saved:titles.append(saved_title)
    known=result['primary']+result['featured']+result.get('aliases',[])
    guest_names=[n for t in titles for hint in [extract_feature_entities_from_title(t,known_entities=known)]
                 if hint.get('role')=='feat.' for n in hint.get('artists') or []]
    if same_saved:
        _,role,guest=split_feature_credit(saved_artist)
        if guest and role=='feat.':guest_names+=split_artists(guest,known_entities=known)
    saved_primary,_,_=split_feature_credit(saved_artist)
    primary_names={key(n) for n in split_artists(saved_primary,known_entities=known)}
    added_primary=any(key(n) not in primary_names for n in result['primary'])
    entered_guests=extract_feature_entities_from_title(title,known_entities=known).get('artists') or []
    if any(key(n) not in {key(k) for k in known} for n in guest_names) and (added_primary or not entered_guests):
        # A spelling mismatch does not prove that a saved guest disappeared.
        # Keep the saved pair instead of applying an already flattened draft.
        return {'conflict':True,'preserve_identity':
                {'artist':saved_artist,'title':saved_title} if same_saved else {'artist':artist,'title':title},
                'reason':'Имя приглашённого участника в тегах не совпадает с источником. Возможна опечатка; Artist и Title сохранены. Проверьте написание и явный кредит выбранного издания.'}
    if any(key(n) in {key(p) for p in result.get('primary_aliases') or result['primary']} for n in guest_names):
        preserve={'artist':saved_artist,'title':saved_title} if same_saved else {'artist':artist,'title':title}
        return {'conflict':True,'preserve_identity':preserve,
                'reason':'Явный feat. расходится со списком основных артистов каталога. Один список имён, даже структурированный, не доказывает смену роли. Artist и Title сохранены; проверьте явный кредит этого издания.'}
    return result

def authority(data,artist,title,selection=None,*,allow_duration_mismatch=False):
    # Only the manual editor opts in, and only for an actual chosen edition.
    # Cached search results and every automatic caller retain the duration veto.
    from core.album_artist import album_key
    from core.verified_releases import find
    from core.release_titles import same_album
    explicit_selection=bool(selection and selection.get('status')=='SELECTED')
    selection=selection or data.get('release_context_selection') or {}
    chosen=next((c for c in (data.get('_ui_editor_release'),data.get('_ui_manual_release'),
        selection.get('selected') if selection.get('status')=='SELECTED' else None,data.get('selected_release_context'))
        if isinstance(c,dict) and matches(c,artist,title) and (explicit_selection or same_album(c,data.get('album') or ''))),None)
    album=(chosen or {}).get('title') or data.get('album') or ''
    # Source-documented overrides are scoped to this edition, never to an
    # artist pair globally. A 2015 guest may be a 2025 co-primary artist.
    verified=[c for c in find(artist,title) if c.get('credit_roles_verified') and same_album(c,album)]
    if chosen and chosen.get('date'):verified=[c for c in verified if c.get('date')==chosen['date']]
    candidates=verified or ([chosen] if chosen else [])
    if not candidates:
        candidates=[c for c in [*(data.get('release_contexts') or []),*(data.get('_ui_extra_releases') or [])]
                    if isinstance(c,dict) and matches(c,artist,title) and same_album(c,album)]
    current=data.get('_artist_name_preference')=='current'
    parsed=[parse(c,current) for c in candidates]
    parsed=[r for r in parsed if r['primary']]
    if any(r.get('conflict') for r in parsed):
        return _check_local_roles(data,artist,title,next(r for r in parsed if r.get('conflict')))
    signatures={(tuple(key(n) for n in r['primary']),tuple(key(n) for n in r['featured'])) for r in parsed}
    if len(signatures)>1:return {'conflict':True,'reason':'Роли или порядок участников в источниках расходятся. Выберите конкретное издание; порядок соавторов и гостей не определяется по первому результату поиска.'}
    spellings={(tuple(r['primary']),tuple(r['featured'])) for r in parsed}
    if len(spellings)>1:return {'conflict':True,'reason':'Источники расходятся в написании или регистре имён. Выберите конкретное издание; написание не определяется по первому результату или большинству файлов.'}
    if parsed:
        result=parsed[0]
        duration=data.get('duration');expected=result['context'].get('duration')
        if duration_requires_review(duration,expected) and not (allow_duration_mismatch and chosen is not None):
            return {'conflict':True,'reason':'Длительность файла не соответствует выбранной записи. Кредиты не применены; проверьте версию.'}
        if result['exact']:return _check_local_roles(data,artist,title,result)
        flat=result
    else:flat=None
    # Recording roles can preserve a missing collaborator, but only when
    # there is no concrete release credit for the same file identity.
    from core.change_planner import _musicbrainz_for_final_identity
    mb=_musicbrainz_for_final_identity(data)
    c={'source':'MusicBrainz Recording','track_title':mb.get('title'),
       'track_artist_credit_items':mb.get('artist-credit') or []}
    if (data.get('change_plan') or {}).get('identity') not in ('REJECT','CONFLICT','NO_MATCH') and c['track_artist_credit_items'] and matches(c,artist,title):
        structured=parse(c,current)
        # A flat storefront list has no role information. A matching structured
        # credit can distinguish its names, but cannot discard extra artists.
        if not flat or {key(n) for n in flat['primary']+flat['featured']}=={key(n) for n in structured['primary']+structured['featured']}:
            return _check_local_roles(data,artist,title,structured)
    return _check_local_roles(data,artist,title,flat) if flat else None

def remove_primary_features(title,names):
    """Remove only explicit feat. names now credited as primary on this edition."""
    protected=set(key(n) for n in names)
    def block(m):
        inner=m[2];feat=re.search(r'(?i)(?:^|\s)(?:feat\.?|ft\.?|featuring|with)\s+(.+)$',inner)
        if not feat:return m[0]
        guests=split_artists(feat[1],known_entities=names)
        kept=[n for n in guests if key(n) not in protected]
        if len(kept)==len(guests):return m[0]
        prefix=inner[:feat.start()].strip()
        value=(prefix+' ' if prefix else '')+('feat. '+canonical_entity_list(kept) if kept else '')
        return m[1]+value+m[3] if value else ''
    result=re.sub(r'([\[(])([^()\[\]]+)([\])])',block,title)
    bare=re.search(r'(?i)\s+(?:feat\.?|ft\.?|featuring)\s+([^()\[\]]+)$',result)
    if bare:
        guests=split_artists(bare[1],known_entities=names);kept=[n for n in guests if key(n) not in protected]
        if len(kept)!=len(guests):result=result[:bare.start()]+(' (feat. '+canonical_entity_list(kept)+')' if kept else '')
    return canonical_track_title(' '.join(result.split()))

def preserve_spelling(names,artist,title=''):
    """Catalog case alone is not a reason to replace an existing artist name."""
    # split_artists restores protected entities using the catalog spelling,
    # which is useful for matching but loses the entered case. Read literal
    # whole entities here, including atomic band names with an internal &.
    sources=[artist]
    feature=re.search(r'(?i)(?:^|\s|[\[(])(?:feat\.?|ft\.?|featuring|with)\s+([^()\[\]]+)',title)
    if feature:sources.append(feature[1])
    separator=r'\s*(?:&|,|;|×|\bx\b|\b(?:feat\.?|ft\.?|featuring|with)\s+)\s*'
    result=[]
    for name in names:
        pattern=re.compile(r'(?:^|'+separator+r')(?P<name>'+re.escape(name)+r')(?=$|'+separator+r')',re.I)
        match=next((m for raw in sources if (m:=pattern.search(raw))),None)
        result.append(match['name'] if match else name)
    return result

def apply(artist,title,roles):
    from core.naming import ensure_feature_credit_in_title
    from core.version_authors import version_author_options
    if not roles or roles.get('conflict'):return {'artist':artist,'title':title}
    # Only edition-scoped, documented spelling repairs are permitted here.
    # Never fuzzy-match unknown guests or replace a person's name globally.
    repairs=roles['context'].get('track_feature_repairs') or {}
    if roles['context'].get('credit_roles_verified') and repairs:
        from core.title_identity import comparison_title
        title=comparison_title(artist,title)
        for wrong,right in repairs.items():
            title=re.sub(r'(?i)((?:feat\.?|ft\.?|featuring)\s+)'+re.escape(wrong)+r'(?=\s*[\])]|$)',lambda m:m[1]+right,title)
            artist=re.sub(r'(?i)(^|\s+&\s+|,\s*|;\s*)'+re.escape(wrong)+r'(?=$|\s+&\s+|,|;)',lambda m:m[1]+right,artist)
    verified=roles['context'].get('credit_roles_verified')
    primary=list(roles['primary']) if verified else preserve_spelling(roles['primary'],artist,title)
    featured=list(roles['featured']) if verified else preserve_spelling(roles['featured'],artist,title)
    known=primary+featured+roles.get('aliases',[])
    from core.recording_credit import source_names
    entered=source_names(artist,title,known)
    remixers={key(n) for o in version_author_options(title) for n in split_credit_entities(o['author'])}
    # Extra manually entered names remain visible for review. Explicit remix
    # authors are separate unless this release actually credits them primary.
    from core.naming import extract_feature_entities_from_title
    _,_,flat_guest=split_feature_credit(artist)
    explicit_guests=split_artists(flat_guest,known_entities=known) if flat_guest else []
    explicit_guests+=extract_feature_entities_from_title(title,known_entities=known).get('artists') or []
    extras=[n for n in entered if key(n) not in {key(k) for k in known+explicit_guests} and key(n) not in remixers]
    result_title=remove_primary_features(title,roles.get('primary_aliases') or primary) if roles['exact'] else title
    if verified and featured:
        # The generic formatter intentionally preserves existing bracketed
        # feat. blocks. Here a documented edition can also correct their case
        # and guest order, without moving an embedded remix credit.
        expected={key(n) for n in featured}
        def verified_feature(match):
            entered_names=split_artists(match[2],known_entities=known)
            if {key(n) for n in entered_names}!=expected:return match[0]
            return match[1]+canonical_entity_list(featured)
        result_title=re.sub(r'(?i)(\b(?:feat\.?|ft\.?|featuring|with)\s+)([^()\[\]]+)',verified_feature,result_title)
    if featured:result_title=ensure_feature_credit_in_title(result_title,roles.get('feature_role') or 'feat.',canonical_entity_list(featured))
    return {'artist':canonical_entity_list([*primary,*extras]),'title':result_title}
