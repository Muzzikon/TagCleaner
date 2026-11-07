"""Release-scoped Album Artist policy shared by audit, editor and writer.

Remix/Compilation types and a track's collaborators never establish a release
credit. Use the chosen release's credit; the collection template omits explicit
featured guests from Album Artist while retaining genuine joint release artists.
"""
import re
import unicodedata
from core.duration_policy import duration_requires_review
from core.naming import (canonical_album_name,canonical_artist_credit,
    canonical_artist_credit_from_items,split_feature_credit,clean_spaces)

VARIOUS_ID='89ad4ac3-39f7-470e-963a-56509c546377'


def is_various(value):
    return bool(re.fullmatch(r'various(?:\s+artists)?|v\.?\s*a\.?|разные (?:исполнители|артисты)|различные (?:исполнители|артисты)|verschillende artiesten|diverse artiesten',clean_spaces(value),re.I))

def credit_key(value):
    return re.sub(r'\s*[:：]\s*',':',canonical_artist_credit(value).casefold())

def album_key(value):
    text=unicodedata.normalize('NFC',canonical_album_name(value)).casefold()
    return re.sub(r'\s+[-–—]\s+(?:single|ep)$','',text).strip()

def primary_credit(value):
    text=canonical_artist_credit(value)
    # Source tags also put the feature credit in parentheses/brackets.
    # Remove only a complete trailing credit; never leave "Main (" behind.
    text=re.sub(r'\s*[\[(]\s*(?:feat\.?|ft\.?|featuring|with)\s+[^()\[\]]+[\])]\s*$','',text,flags=re.I).strip()
    if re.search(r'[\[(]\s*(?:feat\.?|ft\.?|featuring|with)\s+',text,re.I):return ''
    primary,role,guest=split_feature_credit(text)
    if guest and primary.rstrip().endswith(('(','[')):return ''
    return (primary if guest and role in ('feat.','with') else text).strip()

def release_credit(context):
    items=context.get('album_artist_credit_items') or []
    if len(items)==1 and isinstance(items[0],dict) and (items[0].get('artist') or {}).get('id')==VARIOUS_ID:
        return 'Various Artists'
    # A multi-track release may repeat the same entity in credits for each
    # side (A + B / A + C). Album Artist lists each entity once, preserving
    # its first credited name and order. Distinct IDs remain distinct.
    credit=canonical_artist_credit_from_items(items,unique_entities=True) if items else context.get('album_artist_credit') or ''
    value=primary_credit(credit)
    identified=any(isinstance(i,dict) and (i.get('artist') or {}).get('id') for i in items)
    if not identified and is_various(value):return 'Various Artists'
    return value

def _contexts(data,album):
    from core.release_discovery import same_track
    selection=data.get('release_context_selection') or {}
    chosen=[data.get('_ui_manual_release'),selection.get('selected') if selection.get('status')=='SELECTED' else None,data.get('selected_release_context')]
    others=[*(data.get('release_contexts') or []),*(data.get('_ui_extra_releases') or [])]
    from core.verified_releases import find
    from core.release_titles import same_album
    verified=[c for c in find(data.get('artist') or '',data.get('title') or '') if same_album(c,album)]
    selected_issue=next((c for c in chosen if isinstance(c,dict) and same_album(c,album)),None)
    if selected_issue and selected_issue.get('date'):
        verified=[c for c in verified if c.get('date')==selected_issue['date']]
    # Documented edition credits override a stale flattened catalog result.
    if verified:return verified
    def valid(c,selected=False):
        if not isinstance(c,dict) or not same_album(c,album):return False
        if c.get('track_title'):
            return same_track(data.get('artist') or '',data.get('title') or '',c.get('track_artist_credit') or data.get('artist') or '',c['track_title'])
        return selected and not data.get('_ui_identity_manually_changed')
    selected=[c for c in chosen if valid(c,True) and release_credit(c)]
    return selected[:1] if selected else [c for c in others if valid(c) and release_credit(c)]

def decide(data,candidate=None,*,context=None,album=None,allow_duration_mismatch=False):
    # The editor can offer a manual credit for a concrete disputed edition.
    # The default policy used by automatic plans remains strict.
    value=str(data.get('albumartist') or '') if candidate is None else str(candidate)
    album=(data.get('album') or '') if album is None else album
    contexts=[context] if context is not None else _contexts(data,album)
    duration_review=any(duration_requires_review(data.get('duration'),c.get('duration')) for c in contexts)
    if duration_review and not (allow_duration_mismatch and context is not None):
        return {'value':None,'automatic':False,'basis':'recording_conflict','reason':'Длительность файла расходится с выбранной записью. Сначала проверьте версию и издание; Album Artist не заменяется автоматически.'}
    credits={credit_key(release_credit(c)):release_credit(c) for c in contexts if release_credit(c)}
    if len(credits)>1:
        return {'value':None,'automatic':False,'basis':'conflict','reason':'Источники расходятся в Album Artist этого альбома. Выберите конкретное издание; участники отдельного трека не заменяют артиста релиза.'}
    if credits:
        expected=next(iter(credits.values()))
        # Preserve an explicitly chosen spelling/case of the same primary credit.
        primary=primary_credit(value)
        if credit_key(primary)==credit_key(expected) and expected!='Various Artists' and not any(c.get('credit_roles_verified') for c in contexts):expected=primary
        return {'value':expected,'automatic':not duration_review,'basis':'release',
                'reason':('Кредит вручную выбранного издания. Длительность отличается; перед записью подтвердите версию трека.' if duration_review else
                          'Album Artist взят из основного кредита этого релиза. Тип Remix или Compilation сам по себе не означает Various Artists.')}
    primary=primary_credit(value)
    if is_various(primary):
        return {'value':'Various Artists','automatic':True,'basis':'explicit_various','reason':'Уже указанный коллективный кредит приведён к единому написанию Various Artists. Тип сборника сам по себе не назначает этот кредит.'}
    if value.strip() and not primary:
        return {'value':None,'automatic':False,'basis':'invalid_credit','reason':'Кредит Album Artist содержит незавершённый или сложный блок feat. Проверьте имена и скобки вручную.'}
    if primary and credit_key(primary)!=credit_key(value):
        return {'value':primary,'automatic':True,'basis':'explicit_feat','reason':'По шаблону коллекции явный feat./with не входит в Album Artist. Гость остаётся в кредитах трека; настоящие соавторы релиза сохраняются.'}
    from core.credit_style import credit_values
    reduced=credit_values(value,str(data.get('title') or ''))['artist']
    if reduced!=value:
        return {'value':primary_credit(reduced),'automatic':False,'basis':'track_guest','reason':'В Album Artist указан явный гость трека. Возможно, нужен только основной артист; проверьте кредит всего релиза. Совместный альбом может законно включать обоих.'}
    return {'value':primary,'automatic':True,'basis':'unchanged','reason':'Album Artist относится ко всему релизу. Без кредита релиза нельзя удалять соавторов или назначать Various Artists по названию сборника.'}

def proposal(data,candidate,*,context=None,album=None,allow_duration_mismatch=False):
    """Prevent stale diagnostic/name proposals from adding track collaborators."""
    result=decide(data,candidate,context=context,album=album,allow_duration_mismatch=allow_duration_mismatch)
    if result['basis'] in ('unchanged','explicit_feat'):
        saved=primary_credit(data.get('albumartist') or '')
        if credit_key(result['value'])!=credit_key(saved):
            local=decide(data,album=album)
            if local['basis']=='track_guest' and credit_key(local['value'])==credit_key(result['value']):return local
            return {'value':None,'automatic':False,'basis':'unverified','reason':'Предложение не подтверждено кредитом альбома. Выберите издание или введите Album Artist вручную; список исполнителей трека сюда не копируется.'}
    return result
