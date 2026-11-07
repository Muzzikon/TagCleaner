"""One coordinated, read-only Artist / Title / Remixer proposal.

Explicit source roles are usable only for the same song AND recording version.
An extra name in a flat artist list alone never proves a featured role.
"""
import re
from core.credit_style import editor_credit_values,credit_values
from core.naming import (artist_roles_from_items,canonical_artist_credit,
    canonical_artist_credit_from_items,canonical_entity_list,canonical_track_title,
    ensure_feature_credit_in_title,extract_feature_entities_from_title,
    split_feature_credit,split_credit_entities,clean_spaces)
from core.version_authors import version_author_options


def _key(value):
    return re.sub(r'\s*[:：]\s*',':',clean_spaces(value).casefold())


def role_evidence(data,artist,title,*,allow_duration_mismatch=False):
    """Return explicit roles; never infer a guest by subtracting Album Artist."""
    from core.release_credits import authority
    declared=authority(data,artist,title,allow_duration_mismatch=allow_duration_mismatch)
    if declared and (declared.get('conflict') or declared.get('exact')):
        if declared.get('conflict') or not declared.get('featured'):return []
        return [{'source':declared['source'],'primary':canonical_entity_list(declared['primary']),
                 'featured':canonical_entity_list(declared['featured']),'featured_names':declared['featured']}]
    from core.change_planner import _musicbrainz_for_final_identity
    from core.release_discovery import same_track
    from ui.editor import release_options
    sources=[]
    mb=_musicbrainz_for_final_identity(data)
    if mb and (data.get('change_plan') or {}).get('identity') not in ('REJECT','CONFLICT','NO_MATCH'):
        sources.append(('MusicBrainz',canonical_artist_credit_from_items(mb.get('artist-credit')),
                        mb.get('title',''),mb.get('artist-credit') or []))
    for context in release_options(data):
        # An album title or a recording id alone does not establish track roles.
        from core.release_titles import same_album
        if not context.get('track_title') or not same_album(context,data.get('album') or ''):continue
        items=context.get('track_artist_credit_items') or []
        credit=context.get('track_artist_credit') or canonical_artist_credit_from_items(items)
        sources.append((context.get('source') or 'Релиз',credit,context['track_title'],items))
    # A cached formatting proposal is not independent evidence of a guest role.
    result=[]
    for source,source_artist,source_title,items in sources:
        if not source_artist or not source_title or not same_track(artist,title,source_artist,source_title):continue
        roles=artist_roles_from_items(items)
        primary=canonical_entity_list(roles['primary'])
        guests=roles['featured']
        flat_primary,role,guest=split_feature_credit(source_artist)
        if not primary:primary=flat_primary
        if not guests and guest and role=='feat.':guests=[guest];primary=flat_primary
        if not guests:
            hint=extract_feature_entities_from_title(source_title)
            if hint.get('role')=='feat.':guests=hint.get('artists') or []
        # A guest-only edited Artist must not inherit another act's source roles.
        if not primary or not same_track(artist,title,primary,source_title):continue
        # Some catalogs redundantly include remixers in their feat. display.
        # Explicit version authors remain remixers, not newly featured singers.
        remixers={_key(n) for o in version_author_options(title) for n in split_credit_entities(o['author'])}
        guests=[g for g in guests if _key(g) not in remixers]
        if guests:result.append({'source':source,'primary':primary,'featured':canonical_entity_list(guests),'featured_names':guests})
    return result


def format_identity(data,artist,title,remixer='',filename='',featured_choice='',*,allow_duration_mismatch=False):
    filename=filename or data.get('file') or data.get('path') or ''
    values=editor_credit_values(artist,title,filename)
    values['title']=canonical_track_title(values['title'])
    notes=[];role_choices=[];evidence=role_evidence(data,values['artist'],values['title'],allow_duration_mismatch=allow_duration_mismatch)
    choices={_key(e['featured']) for e in evidence}
    explicit=extract_feature_entities_from_title(values['title'])
    current_guest=canonical_entity_list(explicit.get('artists')) if explicit.get('role')=='feat.' else ''
    from core.release_credits import authority,apply as apply_declared
    declared=authority(data,values['artist'],values['title'],allow_duration_mismatch=allow_duration_mismatch)
    if featured_choice:
        if current_guest and _key(current_guest)!=_key(featured_choice):
            raise ValueError('В Title уже указан другой feat. Проверьте кредит вручную.')
        remaining=credit_values(values['artist'],'Track (feat. '+featured_choice+')')['artist']
        if remaining==values['artist']:raise ValueError('Выберите участника, явно указанного в Artist; основной исполнитель должен остаться.')
        values['artist']=remaining
        values['title']=ensure_feature_credit_in_title(values['title'],'feat.',featured_choice)
        notes.append('Роль feat. указана вами: '+featured_choice+'.')
    elif declared and declared.get('conflict'):
        preserved=declared.get('preserve_identity') or {'artist':artist,'title':title}
        values['artist']=preserved['artist']
        values['title']=canonical_track_title(preserved['title'])
        notes.append(declared['reason'])
    elif declared and (declared['exact'] or not current_guest):
        values=apply_declared(values['artist'],values['title'],declared)
        notes.append('Кредит этой записи/издания: основные — '+canonical_entity_list(declared['primary'])+
                     '; приглашённые — '+(canonical_entity_list(declared['featured']) or 'не указаны')+'. Источник: '+declared['source']+'.')
    elif len(choices)>1:
        notes.append('Источники расходятся в роли feat. Проверьте участников вручную; новый гость не добавлен.')
    elif evidence:
        guest=evidence[0]['featured']
        if current_guest and _key(current_guest)!=_key(guest):
            notes.append('Указанный в Title гость отличается от источника. Ваш кредит сохранён; проверьте его вручную.')
        else:
            values['title']=ensure_feature_credit_in_title(values['title'],'feat.',guest)
            values=credit_values(values['artist'],values['title'])
            # Structured entities remain atomic (including a band's internal
            # ampersand). Remove each explicit guest even if the flat Artist
            # uses different punctuation for a multiple-guest credit.
            for name in evidence[0]['featured_names']:
                values['artist']=credit_values(values['artist'],'Track (feat. '+name+')')['artist']
            values['artist']=canonical_artist_credit(values['artist'],references=[e['primary'] for e in evidence])
            notes.append('Роль feat. подтверждена для этой версии: '+guest+'.')
    elif not current_guest:
        from core.release_discovery import same_track
        saved_artist=data.get('artist') or ''
        saved_parts={_key(n) for n in split_credit_entities(saved_artist)}
        entered_parts={_key(n) for n in split_credit_entities(values['artist'])}
        if (saved_parts and saved_parts<entered_parts
                and same_track(values['artist'],values['title'],saved_artist,data.get('title') or '')):
            notes.append('Источник добавил участников без указания роли. По одному списку имён нельзя определить feat.; проверьте их роли вручную.')
            role_choices=[n for n in split_credit_entities(values['artist']) if _key(n) not in saved_parts]
    # The explicit formatting action also normalizes x / ×, and commas only
    # when another representation corroborates the complete artist list.
    references=[]
    stem=re.split(r'[\\/]',filename)[-1]
    if ' - ' in stem:references.append(stem.split(' - ',1)[0])
    values['artist']=canonical_artist_credit(values['artist'],references=references)
    values['remixer']=remixer
    if declared and not declared.get('conflict') and 'remixer_credit' in declared['context'] and declared['context'].get('credit_roles_verified') and remixer==str(data.get('remixer') or ''):
        values['remixer']=str(declared['context']['remixer_credit'] or '')
        if remixer.strip() and not values['remixer']:
            notes.append('В проверенном кредите этой записи отдельный ремиксер не указан. Поле очищено в черновике; проверьте его перед записью.')
        remixer=values['remixer']
    authors=version_author_options(values['title'])
    if not remixer.strip() and len(authors)==1:values['remixer']=authors[0]['author']
    elif not remixer.strip() and len(authors)>1:
        notes.append('В названии несколько кредитов версии. Выберите автора кнопкой «Из названия».')
    elif remixer.strip() and authors:
        entered={_key(n) for n in split_credit_entities(remixer)}
        named={_key(n) for o in authors for n in split_credit_entities(o['author'])}
        if entered!=named:notes.append('Заполненный автор версии отличается от указанного в Title. Значение сохранено; проверьте его вручную.')
    if not declared and not evidence:
        notes.append('Проверено только оформление введённых значений. Исполнитель, название и регистр не подтверждены каталогом.')
    return {'values':values,'notes':notes,'role_choices':role_choices}


def identity_proposal(data):
    """Reformat old cached flat proposals without re-running audio recognition."""
    plan=data.get('change_plan') or {}
    def proposed(field):
        p=plan.get(field) or {}
        return str(p.get('proposed') if p.get('proposed') is not None else data.get(field) or '')
    artist,title=proposed('artist'),proposed('title')
    from core.release_credits import authority,remove_primary_features
    declared=authority(data,artist,title)
    if not declared or not declared.get('exact'):
        saved=data.get('artist') or ''
        primary,role,guest=split_feature_credit(saved)
        saved_names=split_credit_entities(saved) if not guest else []
        introduced=extract_feature_entities_from_title(title).get('artists') or []
        if any(_key(n) in {_key(s) for s in saved_names} for n in introduced):
            artist=saved
            title=remove_primary_features(title,saved_names)
    return format_identity(data,artist,title,str(data.get('remixer') or ''),
                           data.get('file') or data.get('path') or '')
