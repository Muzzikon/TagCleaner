"""Explicit user drafts; recognition plans and checkpoints remain untouched."""
import copy
import json
import os
import re
from datetime import datetime
from pathlib import Path
from core.cache_utils import atomic_write_json, file_signature
from core.tag_writer import WRITABLE_FIELDS, make_operation, is_field_writable_for_path, collect_safe_operations
from core.duration_policy import duration_requires_review

FIELDS = tuple(WRITABLE_FIELDS)
RELEASE_FIELDS = ('album', 'albumartist', 'date', 'tracknumber', 'discnumber')

def compatible_values(values, reference):
    """Extend pre-remixer records without clearing an existing file credit."""
    if not isinstance(values,dict): return None
    if set(values) == set(FIELDS): return dict(values)
    if set(values) == set(FIELDS)-{'remixer'}:
        return {**values, 'remixer':str(reference.get('remixer') or '')}
    return None

def path_key(data):
    # Validated snapshots already contain the canonical absolute path. Avoid
    # reopening the same NTFS path repeatedly while attaching 6000+ records.
    canonical=(data.get('analysis_file_signature') or {}).get('path')
    if canonical and os.path.normcase(os.path.abspath(data['path']))==canonical:
        return canonical
    return os.path.normcase(str(Path(data['path']).resolve()))

def current_values(data):
    return {name: str(data.get(name) or '') for name in FIELDS}


def preferred_spellings(values,rules=None):
    """Apply explicit library choices only to the same literal spelling group."""
    from core.uniformity import group_id,credit_names,credit_replace,name_key
    from core.verified_spellings import field_value
    result=dict(values);chosen=(rules or {}).get('canonical') or {}
    for field in ('artist','albumartist','remixer','album'):
        if field not in result:continue
        value=result.get(field,'')
        context=name_key(result.get('albumartist') or result.get('artist')) if field=='album' else ''
        for part in ([value] if field=='album' else credit_names(value)):
            target=chosen.get(group_id('album' if field=='album' else 'name',part,context))
            if target and name_key(target)==name_key(part):
                value=target if field=='album' else credit_replace(value,part,target)
        result[field]=field_value(field,value)
    return result

def draft_matches_current(data, current=None):
    """An explicit KEEP choice is safe to retain, even with no tag changes."""
    draft = data.get('_ui_draft')
    reference = data if current is None else current
    return (isinstance(draft,dict) and not data.get('_ui_draft_warning')
            and bool(reference.get('analysis_file_signature'))
            and draft.get('signature') == reference['analysis_file_signature']
            and draft.get('values') == current_values(reference))

def release_options(data):
    sources = list(data.get('release_contexts') or []) + list(data.get('_ui_extra_releases') or [])
    selection = data.get('release_context_selection') or {}
    sources += list(selection.get('options') or []) + list(selection.get('equivalent_releases') or [])
    for context in (selection.get('selected'), data.get('selected_release_context')):
        if context: sources.append(context)
    from core.verified_releases import find
    sources.extend(find(data.get('artist') or '',data.get('title') or ''))
    draft_values=(data.get('_ui_draft') or {}).get('values') or {}
    if isinstance(draft_values,dict) and (draft_values.get('artist'),draft_values.get('title'))!=(data.get('artist'),data.get('title')):
        sources.extend(find(draft_values.get('artist') or '',draft_values.get('title') or ''))
    result, seen = [], set()
    for context in sources:
        if not isinstance(context, dict): continue
        key = json.dumps(context, sort_keys=True, ensure_ascii=True, default=str)
        if key not in seen:
            seen.add(key); result.append(context)
    return result

def release_values(data, context):
    # An explicit edition choice uses its facts even if an automatic planner
    # would keep an empty optional field. Never borrow values from another issue.
    from core.release_titles import title as release_title
    from core.album_artist import release_credit
    def number(value,total):
        value=str(value or '')
        return value+('/'+str(total) if value and total and '/' not in value else '')
    return {'album':release_title(context),
            'albumartist':release_credit(context),
            'date':str(context.get('date') or ''),
            'tracknumber':number(context.get('track_position') or context.get('track_number'),context.get('track_total')),
            'discnumber':number(context.get('disc_number'),context.get('disc_total'))}


def release_choice_reason(data,context,values,*,allow_duration_mismatch=False):
    from core.release_credits import matches
    if not matches(context,values.get('artist') or '',values.get('title') or ''):
        from core.local_identity import proposal
        local=proposal(values.get('artist') or '',values.get('title') or '',data.get('file') or data['path'],values.get('remixer') or '')
        if not local or not matches(context,local['values']['artist'],local['values']['title']):
            from core.filename_identity import proposal as filename_proposal,can_use_for_release
            file_candidate=filename_proposal(data.get('file') or data['path'],values.get('remixer') or '',reference_title=values.get('title') or '')
            if not file_candidate or not can_use_for_release(data,values) or not matches(context,file_candidate['values']['artist'],file_candidate['values']['title']):
                if not str(values.get('artist') or '').strip() or not str(values.get('title') or '').strip():
                    return 'Используйте «Заполнить из имени файла» или заполните исполнителя и название вручную. Соответствие этого издания треку пока не подтверждено.'
                return 'Другая запись или состав исполнителей. Совпадения названия и одного участника недостаточно; поля издания не применены.'
    if not allow_duration_mismatch and duration_requires_review(data.get('duration'),context.get('duration')):
        return 'Длительность расходится больше чем на 5 секунд. Проверьте аудио и версию; поля издания не применены.'
    return ''


def release_duration_warning(data,context):
    if not duration_requires_review(data.get('duration'),context.get('duration')):return ''
    from core.duration_policy import duration_difference
    def stamp(value):
        seconds=round(float(value))
        return f'{seconds//60}:{seconds%60:02d}'
    return (f"Длительность файла {stamp(data['duration'])}, в каталоге {stamp(context['duration'])} "
            f"(разница {duration_difference(data['duration'],context['duration']):.1f} с). "
            'Можно взять теги этого издания вручную. Перед записью проверьте версию и подтвердите выбор; автоматическое распознавание этим не подтверждается.')


def release_check(data,context,values,index):
    # Duration is review evidence, not a veto on an explicit manual edition.
    # Song/version/whole-credit mismatches still cannot supply release facts.
    reason=release_choice_reason(data,context,values,allow_duration_mismatch=True)
    warning=release_duration_warning(data,context) if not reason else ''
    return {'index':index,'selectable':not bool(reason),'reason':reason,
            'requires_duration_review':bool(warning),'warning':warning}


def release_checks(data,values):
    return [release_check(data,c,values,i) for i,c in enumerate(release_options(data))]

def editor_info(data):
    from core.file_renamer import suggested_filename
    from core.version_authors import version_author_options
    from core.editor_identity import identity_proposal
    from core.album_artist import proposal
    from core.verified_releases import warnings
    from core.audit_approval import apply as apply_approval
    from core.filename_identity import proposal as filename_proposal
    accepted=apply_approval(data,{'issues':(data.get('_ui_audit') or {}).get('acknowledged') or [],'fixes':{},'proposals':{}})
    accepted_reasons={i['reason'] for i in accepted.get('acknowledged',[])}
    options = []
    entered=(data.get('_ui_draft') or {}).get('values') or current_values(data)
    for i, context in enumerate(release_options(data)):
        options.append({'context':context, 'values':release_values(data, context),
                        **release_check(data,context,entered,i)})
    return {'current':current_values(data), 'writable':{n:is_field_writable_for_path(data['path'],n) for n in FIELDS},
            'releases':options, 'draft':data.get('_ui_draft'), 'draft_warning':data.get('_ui_draft_warning'),
            'selected_release':data.get('_ui_manual_release'),
            'draft_matches_current':draft_matches_current(data),
            'version_authors':version_author_options(data.get('title')),
            'edition_warnings':[reason for reason in warnings(data) if reason not in accepted_reasons],
            'identity_proposal':identity_proposal(data),
            'filename_identity_proposal':filename_proposal(data.get('file') or data['path'],entered.get('remixer') or data.get('remixer') or '',reference_title=entered.get('title') or ''),
            'albumartist_proposal':proposal(data,(data.get('change_plan',{}).get('albumartist') or {}).get('proposed') or data.get('albumartist') or ''),
            'suggested_filename':suggested_filename(data)}

def editor_diagnostics(data, entered, context=None, rules=None, field=None, use_local_identity=False, apply_release=False, use_filename_identity=False):
    """Recompute read-only suggestions for the current, unsaved editor state."""
    from core.uniformity import audit_records
    from core.release_titles import same_album
    from core.release_credits import matches, authority
    from core.album_artist import proposal
    from core.editor_identity import format_identity
    if not isinstance(entered,dict) or set(entered)!=set(FIELDS):
        raise ValueError('Нужны текущие значения всех полей редактора.')
    if any(not isinstance(v,str) or len(v)>4096 or any(ord(c)<32 for c in v) for v in entered.values()):
        raise ValueError('Некорректные значения редактора.')
    if field is not None and field not in FIELDS:raise ValueError('Неизвестное поле редактора.')
    if type(use_local_identity) is not bool:raise ValueError('Некорректный выбор локальной подсказки.')
    if type(use_filename_identity) is not bool:raise ValueError('Некорректный выбор имени файла.')
    if use_filename_identity and (use_local_identity or apply_release or field is not None):
        raise ValueError('Выберите одно действие редактора.')
    from core.filename_identity import proposal as filename_proposal,can_use_for_release
    filename=filename_proposal(data.get('file') or data['path'],entered['remixer'],reference_title=entered['title'])
    signature=data.get('analysis_file_signature')
    if not signature or file_signature(data['path'])!=signature:
        raise ValueError('Файл изменился. Обновите локальные теги и откройте редактор заново.')
    original=dict(entered)
    if apply_release:
        if context is None:raise ValueError('Выберите конкретное издание.')
        reason=release_choice_reason(data,context,entered,allow_duration_mismatch=True)
        if reason:raise ValueError(reason)
        entered={**entered,**release_values(data,context)}
        # Edition facts change together; case-only spelling already entered
        # by the user does not identify a different edition.
        from core.uniformity import name_key
        for f in ('album','albumartist'):
            if original[f] and name_key(original[f])==name_key(entered[f]):entered[f]=original[f]
        entered=preferred_spellings(entered,rules)
    working=copy.deepcopy(data)
    working.update(entered)
    # Approval validates saved values and source facts itself. Keep it so an
    # unchanged editor cannot immediately suggest undoing its last manual write.
    for key in ('_ui_name_approval','_ui_draft','_ui_draft_warning'):
        working.pop(key,None)
    working['change_plan']={'identity':'REVIEW',**{
        f:{'current':v,'proposed':v,'status':'KEEP'} for f,v in entered.items()}}
    if use_filename_identity:context=None
    if context is not None:
        if not same_album(context,entered['album']):
            raise ValueError('Название альбома изменено. Выберите подходящее издание заново.')
        reason=release_choice_reason(data,context,entered,allow_duration_mismatch=True)
        if reason:raise ValueError(reason)
        working['_ui_editor_release']=copy.deepcopy(context)
        working['_ui_manual_release']=copy.deepcopy(context)
        working['selected_release_context']=copy.deepcopy(context)
        working['release_context_selection']={'status':'SELECTED','selected':copy.deepcopy(context)}
    # A caption + filename may establish a candidate, never authority by
    # themselves. Only a matching whole source credit can corroborate it.
    from core.local_identity import proposal as local_proposal
    candidate=local_proposal(entered['artist'],entered['title'],working.get('file') or working['path'],entered['remixer'])
    if not candidate and filename and can_use_for_release(data,entered):candidate=filename
    identity_input=entered
    if apply_release and filename and can_use_for_release(data,original) and not matches(context,entered['artist'],entered['title']) and matches(context,filename['values']['artist'],filename['values']['title']):
        # The explicit edition choice corroborates the complete filename
        # credit before supplying any missing coauthors or guests.
        identity_input={**entered,**filename['values']}
        working.update(filename['values'])
    if candidate:
        candidate_work={**working,**candidate['values']}
        corroboration=authority(candidate_work,candidate['values']['artist'],candidate['values']['title'],allow_duration_mismatch=context is not None)
        candidate_pair={f:candidate['values'][f] for f in ('artist','title')}
        # A source can confirm the complete song identity while disagreeing
        # with a caption's explicit feat. Keep that pair, not the uploader.
        if corroboration and (not corroboration.get('conflict') or corroboration.get('preserve_identity')==candidate_pair):
            identity_input={**entered,**candidate['values']}
            working.update(candidate['values'])
    working['_ui_audit']=audit_records([working],rules)['entries'][0]
    identity=format_identity(working,identity_input['artist'],identity_input['title'],identity_input['remixer'],allow_duration_mismatch=context is not None)
    if apply_release:
        # Choosing an edition is an explicit choice of its track caption too.
        # Keep conflicting saved feat. roles for review rather than flattening
        # them merely because one catalog returns a list of names.
        selected_credit=authority(working,identity_input['artist'],identity_input['title'],allow_duration_mismatch=True)
        if selected_credit and not selected_credit.get('conflict'):
            from core.naming import canonical_artist_credit_from_items
            source=selected_credit['context']
            source_artist=canonical_artist_credit_from_items(source.get('track_artist_credit_items')) or source.get('track_artist_credit')
            if source_artist and source.get('track_title'):
                identity=format_identity(working,source_artist,source['track_title'],identity_input['remixer'],allow_duration_mismatch=True)
    identity['values']=preferred_spellings(identity['values'],rules)
    from core.audit_approval import reviewed_fields
    reviewed=reviewed_fields(working,rules) if not apply_release else {}
    if any(f in reviewed for f in ('artist','title')):
        for f in ('artist','title'):identity['values'][f]=entered[f]
    if 'remixer' in reviewed:identity['values']['remixer']=entered['remixer']
    # Do not discard a spelling proposal by asking policy about the old value.
    albumartist_candidate=working['_ui_audit'].get('proposals',{}).get('albumartist',entered['albumartist'])
    albumartist=proposal(working,albumartist_candidate,context=context,allow_duration_mismatch=context is not None)
    if 'albumartist' in reviewed:
        albumartist={'value':entered['albumartist'],'basis':'manual_review','automatic':False,
                     'reason':'Ваш ручной выбор сохранён для этого поля и текущих данных источников.'}
    notes=[]
    if context is not None:
        notes.append('Предложения пересчитаны для выбранного издания: '+str(context.get('title') or '')+
                     ' · '+str(context.get('date') or 'дата неизвестна')+' · '+str(context.get('source') or 'источник не указан')+'.')
        warning=release_duration_warning(data,context)
        if warning:notes.append(warning)
    else:notes.append('Предложения пересчитаны для текущих значений редактора.')
    declared=authority(working,identity_input['artist'],identity_input['title'],allow_duration_mismatch=context is not None)
    local=local_proposal(entered['artist'],entered['title'],working.get('file') or working['path'],entered['remixer']) if not declared else None
    if declared and declared.get('conflict'):notes.append(declared['reason'])
    actions={}
    from core.uniformity import multi_fields
    multiple=multi_fields(data)
    for name in FIELDS:
        targets={};reasons=[]
        try:
            if name in working['_ui_audit'].get('proposals',{}):
                applied=diagnostic_proposal(working,name,current_values(working))
                targets=applied['values'];reasons=applied['notes']
        except ValueError as error:reasons=[str(error)]
        if name in ('artist','title','remixer'):
            pair=identity['values']
            if pair.get(name)!=entered[name] or (name in ('artist','title') and any(pair.get(f)!=entered[f] for f in ('artist','title'))):
                targets={f:v for f,v in pair.items() if f in ('artist','title','remixer')}
                reasons=identity['notes']
        if name=='albumartist':
            targets={'albumartist':albumartist['value']} if albumartist['value'] is not None else {}
            reasons=[albumartist['reason']]
        # Older builds could write a catalog correction over a remembered
        # manual spelling. Offer recovery in the editor, never rewrite tags or
        # replace newly typed values automatically.
        previous=(data.get('_ui_name_approval') or {}).get(name)
        recover=(not apply_release and context is None and data.get('_ui_written')
                 and name in ('albumartist','album','remixer') and isinstance(previous,str) and bool(previous)
                 and entered[name]==str(data.get(name) or '') and previous!=entered[name])
        if recover:
            targets={name:previous}
            reasons=['Ранее вы выбрали вручную: '+previous+'. Вернёт этот выбор только в редактор; перед записью проверьте изменение.']
        targets={f:v for f,v in targets.items() if v!=original[f]}
        blocked=any(f in multiple or not is_field_writable_for_path(data['path'],f) for f in targets)
        if blocked:targets={};reasons=['Поле не поддерживает запись или содержит несколько значений. Сохраните участников вручную.']
        actions[name]={'available':bool(targets),'values':targets,
                       'reason':' '.join(dict.fromkeys(reasons)) or 'Для текущих значений нет подтверждённой замены.',
                       **({'source':'manual_history'} if recover and targets else {})}
    if use_local_identity or use_filename_identity:
        chosen_hint=filename if use_filename_identity else local
        if not chosen_hint:raise ValueError('Имя файла не соответствует шаблону «Исполнитель - Название».' if use_filename_identity else 'Локальная подсказка больше не соответствует текущим полям или расходится с кредитом источника.')
        from core.uniformity import multi_fields
        changed={f for f,v in chosen_hint['values'].items() if v!=entered[f]}
        if any(f in multi_fields(data) for f in changed):
            raise ValueError('В тегах несколько значений. Сохраните всех участников вручную.')
        if any(not is_field_writable_for_path(data['path'],f) for f in changed):
            raise ValueError('Запись полей этой подсказки не поддерживается для данного формата.')
        values=chosen_hint['values'];notes.append(chosen_hint['reason'])
    elif apply_release:
        values={**entered,**identity['values']}
        if any(f in multiple or not is_field_writable_for_path(data['path'],f) for f,v in values.items() if v!=original[f]):
            raise ValueError('Поля издания содержат несколько значений или не поддерживают запись. Заполните их вручную.')
        notes.extend(identity['notes'])
    elif field is None:values={}
    else:
        values=actions[field]['values'];notes.append(actions[field]['reason'])
    return {'values':values,'notes':list(dict.fromkeys(notes)),
            'audit':working['_ui_audit'],'identity_proposal':identity,
            'albumartist_proposal':albumartist,'local_identity_proposal':local,
            'filename_identity_proposal':filename,'clear_release':use_filename_identity,
            'actions':actions,'release_checks':release_checks(data,{**entered,**values})}


def diagnostic_proposal(data, field, entered):
    """Apply final audit values once, keeping coupled Artist/Title roles atomic."""
    from core.uniformity import multi_fields
    if field not in FIELDS or not isinstance(entered,dict) or set(entered)!=set(FIELDS):
        raise ValueError('Нужны текущие значения всех полей редактора.')
    if any(not isinstance(v,str) or len(v)>4096 or any(ord(c)<32 for c in v) for v in entered.values()):
        raise ValueError('Некорректные значения редактора.')
    audit=data.get('_ui_audit') or {}
    if not audit.get('signature') or file_signature(data['path'])!=audit['signature']:
        raise ValueError('Файл изменился после диагностики. Повторите диагностику оформления.')
    proposals=audit.get('proposals') or {}
    if field not in proposals:raise ValueError('Для этого поля нет актуального предложения диагностики.')
    targets={field:proposals[field]}
    issues=audit.get('issues') or []
    if field in ('artist','title'):
        for name in ('artist','title','album'):
            if entered[name] not in (str(data.get(name) or ''),str(proposals.get(name,data.get(name) or ''))):
                raise ValueError('Идентификация или альбом уже изменены в редакторе. Сохраните правки и повторите диагностику.')
        if any(i['code']=='filename_credit' and i['field']==field for i in issues):
            raise ValueError('Кредит только из имени файла не подтверждён источником. Проверьте роли по конкретному изданию и введите их вручную.')
        if any(i['code']=='credit_placement' for i in issues):
            from core.release_credits import authority
            evidence=authority(data,data.get('artist') or '',data.get('title') or '')
            explicit=bool(re.search(r'(?i)\b(?:feat\.?|ft\.?|featuring)\s+',data.get('artist') or ''))
            if (evidence and evidence.get('conflict')) or (not evidence and not explicit):
                raise ValueError('Роли соавторов и гостей не подтверждены. Выберите конкретное издание или укажите роли вручную.')
            other='title' if field=='artist' else 'artist'
            if other in proposals:targets[other]=proposals[other]
    multiple=multi_fields(data)
    for name,value in targets.items():
        if name in multiple:raise ValueError('В теге несколько значений. Сохраните всех участников вручную.')
        if not is_field_writable_for_path(data['path'],name):raise ValueError('Запись поля не поддерживается: '+name)
        if entered[name] not in (str(data.get(name) or ''),str(value)):
            raise ValueError('Поле уже изменено вручную. Сохраните правки и повторите диагностику.')
    if field=='albumartist':
        from core.album_artist import proposal
        if entered['album']!=str(data.get('album') or ''):
            raise ValueError('Альбом изменён. Выберите издание и проверьте Album Artist заново.')
        policy=proposal(data,targets[field])
        if policy['value'] is None:raise ValueError(policy['reason'])
        targets[field]=policy['value']
    reasons=list(dict.fromkeys(i['reason'] for i in issues if i['field'] in targets))
    return {'values':targets,'notes':reasons}


def validated_editor_values(values,current):
    """Validate changed date/number fields before Mutagen can touch a file."""
    from core.uniformity import iso_date,number_style
    result=dict(values)
    for field,label,formatter in (('date','Дата',iso_date),('tracknumber','Номер трека',number_style),('discnumber','Номер диска',number_style)):
        if result[field]==current[field]:continue
        try:result[field]=formatter(result[field].strip())
        except ValueError:
            example='YYYY, YYYY-MM или YYYY-MM-DD' if field=='date' else 'N или N/T, например 1/4'
            raise ValueError(f'{label}: недопустимое значение. Используйте {example} или оставьте поле пустым.') from None
    return result


def build_draft(data, command):
    signature = data.get('analysis_file_signature')
    if not signature or file_signature(data['path']) != signature:
        raise ValueError('Файл изменился после анализа или его состояние неизвестно. Перепроверьте этот трек.')
    values = command.get('values')
    if not isinstance(values,dict) or set(values) != set(FIELDS):
        raise ValueError('Нужны значения всех полей редактора.')
    current = current_values(data)
    for name, value in values.items():
        if not isinstance(value,str) or len(value)>4096 or any(ord(c)<32 for c in value):
            raise ValueError('Поле содержит недопустимые символы или слишком длинное значение: '+name)
        value.encode('utf-8', errors='strict')
        if value != current[name] and not is_field_writable_for_path(data['path'],name):
            raise ValueError('Это поле пока не поддерживается для данного формата: '+name)
    values=validated_editor_values(values,current)
    release = None
    index = command.get('release_index')
    if index is not None:
        options = release_options(data)
        if type(index) is not int or not 0 <= index < len(options): raise ValueError('Релиз недоступен. Откройте редактор заново.')
        if command.get('release_confirmed') is not True: raise ValueError('Подтвердите, что выбранный релиз относится к этому аудиофайлу.')
        release = copy.deepcopy(options[index])
        validate_release_choice(data, values, release,allow_duration_mismatch=True)
    return {'values':dict(values), 'signature':signature, 'release':release,
            'release_confirmed':bool(release), 'release_warning':release_duration_warning(data,release) if release else '',
            'saved_at':datetime.now().astimezone().isoformat(timespec='seconds')}


def validate_release_choice(data, values, release,*,allow_duration_mismatch=False):
    """A confirmation cannot attach an unrelated edition to edited identity."""
    from core.release_credits import matches
    from core.release_titles import same_album
    reason=release_choice_reason(data,release,values,allow_duration_mismatch=allow_duration_mismatch)
    if reason:raise ValueError(reason)
    if not same_album(release,values['album']):
        raise ValueError('Альбом отличается от выбранного издания. Выберите подходящее издание или ручное заполнение.')
    expected=release_values(data,release)
    if any(values[f]!=expected[f] for f in ('date','tracknumber','discnumber')):
        raise ValueError('Дата или номера отличаются от выбранного издания. Выберите его заново либо ручное заполнение без издания, чтобы не смешивать выпуски.')

def draft_operations(data):
    draft = data.get('_ui_draft')
    if not draft: return []
    if draft['signature'] != data.get('analysis_file_signature'): raise ValueError('Ручной черновик устарел. Перепроверьте этот трек и откройте редактор заново.')
    values=validated_editor_values(draft['values'],current_values(data))
    operations = []
    for name in FIELDS:
        operation = make_operation(data,name,{'current':current_values(data)[name], 'proposed':values[name], 'status':'MANUAL'})
        if operation:
            operation['source']='user_editor'
            operation['manual_release_id']=(draft.get('release') or {}).get('release_id')
            operations.append(operation)
    return operations

def has_preview_work(data,*,uniformity=False):
    """Cheap, read-only queue check, independent of persistent checkboxes.

    No filesystem access or copies of recognition evidence for unchanged files.
    Uncertain drafts and write failures must still reach normal preflight.
    """
    if data.get('_ui_draft_warning') or data.get('_ui_write_recheck_required'):return True
    draft=data.get('_ui_draft')
    if draft:
        if not isinstance(draft,dict) or draft.get('signature')!=data.get('analysis_file_signature'):return True
        values=draft.get('values')
        if not isinstance(values,dict) or set(values)!=set(FIELDS):return True
        return values!=current_values(data)
    if uniformity:
        audit=data.get('_ui_audit') or {}
        if not audit.get('signature'):return True
        return any(value!=str(data.get(field) or '') for field,value in (audit.get('fixes') or {}).items())
    return bool(collect_safe_operations([data]))


def prepare_preview(results):
    operations,skipped=[],[]
    for data in results:
        # Every displayed field is an explicit choice, including keeping its current value.
        try:
            if data.get('_ui_draft_warning') or data.get('_ui_write_recheck_required'):
                raise ValueError(data.get('_ui_draft_warning') or 'После ошибки записи нужна перепроверка этого файла.')
            planned=draft_operations(data) if data.get('_ui_draft') else collect_safe_operations([data])
            operations.extend(planned)
            if planned:continue
            reason='Нет изменений для записи. Оставшиеся замечания проверьте в редакторе.'
        except ValueError as error:reason=str(error)
        skipped.append({'path':data['path'],'file':data.get('file') or Path(data['path']).name,'reason':reason})
    return operations,skipped


def preview_operations(results):
    return prepare_preview(results)[0]


def preview_skipped(results, operations):
    """Explain selected files absent from the concrete write plan."""
    included={op['path'] for op in operations}
    absent=[d for d in results if d['path'] not in included]
    reasons={r['path']:r['reason'] for r in prepare_preview(absent)[1]}
    return [{'path':d['path'],'file':d.get('file') or Path(d['path']).name,
             'reason':reasons.get(d['path'],'Нет изменений для записи. Оставшиеся замечания проверьте в редакторе.')} for d in absent]

class DraftStore:
    def __init__(self,path):
        self.path=Path(path)
        self.items={}
        if self.path.exists():
            payload=json.loads(self.path.read_text(encoding='utf-8'))
            if payload.get('schema')!=1 or not isinstance(payload.get('items'),dict): raise ValueError('Не удалось прочитать ручные черновики. Сохраните файл ui_runtime/manual_drafts.json для диагностики.')
            self.items=payload['items']
    def attach(self,data):
        draft=self.items.get(path_key(data))
        if not draft:return
        values = compatible_values(draft.get('values'), data) if isinstance(draft,dict) else None
        if values is None or any(not isinstance(v,str) for v in values.values()):
            data['_ui_draft_warning']='Сохранённый черновик повреждён: заполните редактор заново или сбросьте правки.'
        elif draft.get('signature') != data.get('analysis_file_signature'):
            data['_ui_draft_warning']='Файл изменился: сохранённые правки требуют проверки в редакторе.'
        else:
            expanded = {**draft, 'values':values}
            self.items[path_key(data)] = expanded
            data['_ui_draft']=copy.deepcopy(expanded)
    def save(self,data,draft):
        updated={**self.items,path_key(data):draft}
        atomic_write_json(self.path,{'schema':1,'items':updated})
        self.items=updated;data['_ui_draft']=copy.deepcopy(draft);data.pop('_ui_draft_warning',None)
    def remove(self,data):
        updated=dict(self.items);updated.pop(path_key(data),None)
        atomic_write_json(self.path,{'schema':1,'items':updated})
        self.items=updated;data.pop('_ui_draft',None);data.pop('_ui_draft_warning',None)


def refresh_after_write(data, outcome, drafts):
    """Refresh only written files. Discard preceding proposals, never reuse stale signatures."""
    if not outcome.get('success'):
        data['analysis_file_signature'] = None
        data['_ui_write_recheck_required'] = True
        data['_ui_draft_warning'] = 'Запись не завершена успешно. Перепроверьте только этот трек перед следующей записью.'
        return
    from core.scanner import scan_file
    from core.audit_approval import before_write,after_write
    review=before_write(data,outcome)
    original_plan = data.get('change_plan') or {}
    saved_draft = copy.deepcopy(data.get('_ui_draft'))
    fresh = scan_file(data['path'])
    changed_identity = any(
        op.get('status') == 'MANUAL' and op['field'] in ('artist','title','remixer')
        and str(op['proposed']) != str((original_plan.get(op['field']) or {}).get('proposed') or data.get(op['field']) or '')
        for op in outcome.get('operations', [])
    )
    data.update(fresh)
    names={f:v for f,v in (data.get('_ui_name_approval') or {}).items() if str(fresh.get(f) or '')==v}
    names.update({f:v for f,v in review['manual'].items()
                  if f in ('artist','albumartist','remixer','album') and str(fresh.get(f) or '')==v})
    if names:data['_ui_name_approval']=names
    else:data.pop('_ui_name_approval',None)
    data['change_plan'] = {
        'identity': 'REVIEW' if changed_identity else original_plan.get('identity','REVIEW'),
        **{name:{'current':str(fresh.get(name) or ''), 'proposed':str(fresh.get(name) or ''),
                 'status':'KEEP', 'recognition_proposed':(original_plan.get(name) or {}).get('proposed'),
                 'reason':'Теги перечитаны после записи. Старое предложение больше не используется.'} for name in FIELDS}
    }
    data['_ui_written'] = True
    data['_ui_completion_kind'] = 'write'
    data.pop('_ui_write_recheck_required',None)
    data.pop('_ui_draft_warning',None)
    confirmed_release = (saved_draft or {}).get('release') if (saved_draft or {}).get('release_confirmed') else None
    if confirmed_release:
        data['_ui_manual_release']=copy.deepcopy(confirmed_release)
        data['selected_release_context']=copy.deepcopy(confirmed_release)
        data['release_context_selection']={'status':'SELECTED','selected':copy.deepcopy(confirmed_release)}
    elif saved_draft and (changed_identity or any(op.get('field') in RELEASE_FIELDS for op in outcome.get('operations',[]))):
        data.pop('_ui_manual_release',None)
        data['selected_release_context']=None;data['release_context_selection']={}
    if changed_identity:
        # A different identity cannot inherit the previous candidate's release options.
        data.pop('_ui_extra_releases',None)
        data['release_contexts'] = [confirmed_release] if confirmed_release else []
        data['release_context_selection'] = {}
        data['selected_release_context'] = confirmed_release
        data['_ui_identity_manually_changed'] = True
    if data.get('_ui_draft'):
        if drafts is not None: drafts.remove(data)
        else: data.pop('_ui_draft',None)
    after_write(data,review)


def mark_completed(data, drafts, written_store, confirmed_release=None):
    """Record the user's approval of saved tags without rewriting the audio."""
    from core.scanner import scan_file
    if drafts is None or written_store is None:
        raise ValueError('Не удалось открыть файл отметок или черновиков. Подтверждение не сохранено.')
    if data.get('_ui_draft_warning') or data.get('_ui_write_recheck_required'):
        raise ValueError('Сначала проверьте предупреждение этого трека в редакторе.')
    fresh = scan_file(data['path'])
    if not data.get('analysis_file_signature') or fresh['analysis_file_signature'] != data['analysis_file_signature']:
        raise ValueError('Файл изменился после анализа. Перепроверьте этот трек.')
    if confirmed_release:
        validate_release_choice(fresh,current_values(fresh),confirmed_release,allow_duration_mismatch=True)
    if data.get('_ui_draft') and not draft_matches_current(data, fresh):
        raise ValueError('Есть незаписанные изменения. Сначала запишите теги через предпросмотр.')
    updated = copy.deepcopy(data)
    # Supply explicit current identity choices so conflicting recognition
    # candidates cannot lend their release metadata to a manual approval.
    outcome = {'success':True, 'operations':[
        {'status':'MANUAL', 'field':name, 'proposed':current_values(fresh)[name]}
        for name in ('artist','title')]}
    refresh_after_write(updated, outcome, None)
    updated['_ui_completion_kind'] = 'manual'
    if confirmed_release:
        updated['_ui_manual_release']=copy.deepcopy(confirmed_release)
        updated['selected_release_context']=copy.deepcopy(confirmed_release)
        updated['release_context_selection']={'status':'SELECTED','selected':copy.deepcopy(confirmed_release)}
    from core.audit_approval import capture
    audit=data.get('_ui_audit') or {}
    issues=[*(audit.get('issues') or []),*(audit.get('acknowledged') or [])] if audit.get('signature')==fresh['analysis_file_signature'] else []
    updated['_ui_audit_approval']=capture(updated,issues)
    for name in FIELDS:
        updated['change_plan'][name]['reason'] = 'Текущие теги подтверждены пользователем; аудиофайл не перезаписывался.'
    if file_signature(data['path']) != fresh['analysis_file_signature']:
        raise ValueError('Файл изменился во время подтверждения. Отметка не сохранена.')
    # Commit the approval before removing an otherwise harmless no-op draft.
    written_store.save_batch([updated])
    warnings = []
    try: drafts.remove(updated)
    except Exception as error: warnings.append('Отметка сохранена, но совпадающий черновик не очищен: '+str(error))
    data.clear();data.update(updated)
    return warnings


class WrittenStore:
    """Remember verified writes and user-approved saved tags, bound to file state."""
    def __init__(self, path):
        self.path = Path(path)
        self.items = {}
        self.approval_migrations=False
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            if payload.get('schema') != 1 or not isinstance(payload.get('items'), dict):
                raise ValueError('Некорректный файл отметок записи: ui_runtime/written_tracks.json')
            self.items = payload['items']

    def attach(self, data):
        record = self.items.get(path_key(data))
        if not isinstance(record, dict): return
        if not data.get('analysis_file_signature') or record.get('signature') != data['analysis_file_signature']: return
        values = compatible_values(record.get('values'), data)
        if values != current_values(data): return
        record = {**record, 'values':values}
        self.items[path_key(data)] = record
        data['_ui_written'] = True
        data['_ui_completion_kind'] = record.get('completion_kind','write')
        data['_ui_written_at'] = record.get('written_at')
        approval = record.get('name_approval')
        if not isinstance(approval, dict) and record.get('completion_kind') == 'manual':
            approval = {f:str(data.get(f) or '') for f in ('artist','albumartist','remixer','album')}
        if isinstance(approval, dict): data['_ui_name_approval'] = dict(approval)
        if isinstance(record.get('manual_release'),dict):
            data['_ui_manual_release']=copy.deepcopy(record['manual_release'])
            data['selected_release_context']=copy.deepcopy(record['manual_release'])
            data['release_context_selection']={'status':'SELECTED','selected':copy.deepcopy(record['manual_release'])}
        # A saved manual correction must not inherit the previous candidate's release.
        if record.get('manual_identity_changed'):
            data['release_contexts'] = [record['release']] if record.get('release') else []
            data['release_context_selection'] = {}
            data['selected_release_context'] = record.get('release')
            data['_ui_identity_manually_changed'] = True
        if isinstance(record.get('audit_approval'),dict):
            data['_ui_audit_approval']=copy.deepcopy(record['audit_approval'])
        elif record.get('completion_kind')=='manual':
            from core.audit_approval import capture
            data['_ui_audit_approval']=capture(data,legacy=True)
            self.items[path_key(data)]['audit_approval']=copy.deepcopy(data['_ui_audit_approval'])
            self.approval_migrations=True
        plan = data.setdefault('change_plan', {})
        for name in FIELDS:
            old = plan.get(name) or {}
            plan[name] = {'current':str(data.get(name) or ''), 'proposed':str(data.get(name) or ''),
                          'recognition_proposed':old.get('proposed'), 'status':'KEEP',
                          'reason':('Текущие теги подтверждены пользователем.' if record.get('completion_kind')=='manual' else 'Теги уже записаны и совпадают с сохранённым результатом.')}

    def save_batch(self, results):
        updated = dict(self.items)
        stamp = datetime.now().astimezone().isoformat(timespec='seconds')
        for data in results:
            if not data.get('_ui_written') or not data.get('analysis_file_signature') or data.get('_ui_write_recheck_required') or data.get('_ui_draft_warning'): continue
            updated[path_key(data)] = {'signature':data['analysis_file_signature'], 'values':current_values(data),
                'completion_kind':data.get('_ui_completion_kind','write'),
                'name_approval':data.get('_ui_name_approval'),
                'audit_approval':data.get('_ui_audit_approval'),
                'manual_release':data.get('_ui_manual_release'),
                'written_at':stamp, 'manual_identity_changed':bool(data.get('_ui_identity_manually_changed')),
                'release':data.get('selected_release_context') if data.get('_ui_identity_manually_changed') else None}
        atomic_write_json(self.path, {'schema':1, 'items':updated})
        self.items = updated
        for data in results:
            record=updated.get(path_key(data))
            if record and record.get('signature')==data.get('analysis_file_signature'):data['_ui_written_at']=record['written_at']

    def flush_approval_migrations(self):
        if self.approval_migrations:
            atomic_write_json(self.path,{'schema':1,'items':self.items})
            self.approval_migrations=False

    def recover_from_plans(self, results, reports_dir):
        """One-time migration: only verified writes with matching post-write signatures/tags."""
        if self.path.exists(): return 0
        by_path = {path_key(data):data for data in results}
        recovered = {}
        for report in sorted(Path(reports_dir).glob('TagCleaner_plan_*.json')):
            try:
                # Large full-analysis exports are not needed for this UI write migration.
                if report.stat().st_size > 16 * 1024 * 1024: continue
                payload = json.loads(report.read_text(encoding='utf-8'))
                verified = {os.path.normcase(str(Path(r['path']).resolve())):r for r in payload.get('write_results',[])
                            if r.get('success') is True and r.get('verified') is True and r.get('dry_run') is False}
                for item in payload.get('files',[]):
                    key = os.path.normcase(str(Path(item['path']).resolve()))
                    data = by_path.get(key)
                    if data is None or key not in verified: continue
                    if not data.get('analysis_file_signature') or item.get('analysis_file_signature') != data['analysis_file_signature']: continue
                    values = current_values(data)
                    if any(not isinstance(item.get(n),dict) or item[n].get('status')!='KEEP' or str(item[n].get('current') or '')!=values[n] for n in FIELDS if n!='remixer' or n in item): continue
                    changed = any(op.get('status')=='MANUAL' and op.get('field') in ('artist','title')
                                  and str(op.get('proposed') or '') != str(item[op['field']].get('recognition_proposed') or '')
                                  for op in verified[key].get('operations',[]))
                    data['_ui_written'] = True
                    if changed:
                        data['_ui_identity_manually_changed'] = True
                        data['selected_release_context'] = item.get('selected_release_context')
                    recovered[key]=data
            except (OSError,ValueError,TypeError,KeyError,AttributeError):
                continue
        self.save_batch(list(recovered.values()))
        return len(recovered)
