"""Read-only, explicit library formatting rules. No identity/online guesses."""
import copy
import hashlib
import json
import re
import unicodedata
from collections import Counter,defaultdict
from datetime import date
from pathlib import Path

FIELDS=('artist','title','remixer','album','albumartist','date','tracknumber','discnumber')
DEFAULT_RULES={'whitespace':True,'feat':True,'dates_numbers':True,'name_variants':True,
               'version_authors':True,'album_artist':True,'single_album':True,
               'edition_credits':True,'filename':False,'verified_names':True,
               'recheck_confirmed':False,'canonical':{}}
class AuditCancelled(Exception):pass

def validate_rules(values=None):
    values=values or {};rules=copy.deepcopy(DEFAULT_RULES)
    if not isinstance(values,dict) or set(values)-set(rules):raise ValueError('Неизвестные правила диагностики.')
    for key,value in values.items():
        if key=='canonical':
            if not isinstance(value,dict) or len(value)>2000:raise ValueError('Некорректный список написаний.')
            for gid,name in value.items():
                if not isinstance(gid,str) or not re.fullmatch('[a-f0-9]{64}',gid) or not isinstance(name,str) or not name or len(name)>4096 or any(ord(c)<32 for c in name):raise ValueError('Некорректное выбранное написание.')
            rules[key]=dict(value)
        elif type(value) is not bool:raise ValueError('Правила должны быть включены или выключены.')
        else:rules[key]=value
    return rules

def clean(value):return re.sub(r'\s+',' ',unicodedata.normalize('NFC',str(value or ''))).strip()
def name_key(value):return re.sub(r'\s*[:：]\s*',':',clean(value).casefold())
def group_id(kind,value,context=''):
    return hashlib.sha256(json.dumps([kind,name_key(value),context],ensure_ascii=True).encode()).hexdigest()

def feat_style(value):
    # Only explicit credits, not an artist's casing or ordinary words in a title.
    value=re.sub(r'(?i)(\(|\[)\s*(?:feat\.?|ft\.?|featuring)\s+([^\]\)]+)[\]\)]',lambda m:'(feat. '+clean(m[2])+')',value)
    return re.sub(r'(?i)\s+(?:feat\.?|ft\.?|featuring)\s+', ' feat. ',value)

def artist_credit_key(value):
    # Compare explicit collaboration spellings without changing displayed names
    # or guessing that a comma / the word "and" splits a band name.
    from core.naming import canonical_artist_credit
    return name_key(canonical_artist_credit(value))

def title_style(value,artist=''):
    """Format explicit credits; remove a display prefix only if Artist agrees.

    Prefix removal is a manual proposal, since a title can legitimately
    contain a dash. Never interpret an arbitrary dash as Artist - Title.
    """
    from core.naming import split_feature_credit,ensure_feature_credit_in_title,extract_feature_entities_from_title,canonical_entity_list
    text=feat_style(value);structural=False
    # Try each separator, since hyphens can also be part of an artist's name.
    for separator in re.finditer(r'\s+[-–—]\s+',text):
        prefix=text[:separator.start()];rest=text[separator.end():]
        primary,role,guest=split_feature_credit(prefix)
        saved_primary,_,_=split_feature_credit(artist)
        if rest and saved_primary and artist_credit_key(primary)==artist_credit_key(saved_primary):
            if guest and role=='feat.':
                existing=canonical_entity_list(extract_feature_entities_from_title(rest).get('artists'))
                if existing and name_key(existing)!=name_key(guest):continue
                text=ensure_feature_credit_in_title(rest,'feat.',guest)
            elif not guest:text=rest
            else:continue
            structural=True;break
    # Only an unbracketed, explicit trailing feat. is moved. An embedded
    # version credit such as (X Remix feat. Guest) keeps its title structure.
    bare=re.fullmatch(r'(.+?)\s+feat\.\s+([^()\[\]]+?)(\s*(?:\([^()]*\)|\[[^\[\]]*\]))*',text,re.I)
    if bare and '(' not in bare[1] and '[' not in bare[1] and not re.search(r'\s+[-–—|]\s+',bare[2]):
        text=ensure_feature_credit_in_title(text,'feat.',bare[2].strip())
    return text,structural

def iso_date(value):
    if not value:return value
    if re.fullmatch(r'\d{4}',value):
        if 1<=int(value)<=9999:return value
        raise ValueError()
    m=re.fullmatch(r'(\d{4})[-./](\d{1,2})(?:[-./](\d{1,2}))?',value)
    if not m:raise ValueError()
    y,mo,day=int(m[1]),int(m[2]),int(m[3] or 1);date(y,mo,day)
    return f'{y:04}-{mo:02}'+(f'-{day:02}' if m[3] else '')

def number_style(value):
    if not value:return value
    m=re.fullmatch(r'(\d+)\s*(?:/\s*(\d+))?',value)
    if not m:raise ValueError()
    n=int(m[1]);total=int(m[2]) if m[2] else None
    if n<1 or (total is not None and (total<1 or n>total)):raise ValueError()
    return str(n)+(('/'+str(total)) if total is not None else '')

_RAW_FIELDS={'tpe1':'artist','artist':'artist','©art':'artist','tpe2':'albumartist','albumartist':'albumartist','aart':'albumartist','tit2':'title','title':'title','©nam':'title','talb':'album','album':'album','©alb':'album','tdrc':'date','tyer':'date','date':'date','year':'date','©day':'date','trck':'tracknumber','tracknumber':'tracknumber','tpos':'discnumber','discnumber':'discnumber','tpe4':'remixer','remixer':'remixer','----:com.apple.itunes:remixer':'remixer'}
_CREDIT_SEPARATOR=re.compile(r'(\s*;\s*|\s+(?:feat\.?|ft\.?|featuring)\s+)',re.I)
def credit_names(value):return [clean(p) for p in _CREDIT_SEPARATOR.split(str(value or ''))[::2] if clean(p)]
def credit_replace(value,name,replacement):
    parts=_CREDIT_SEPARATOR.split(value)
    for i in range(0,len(parts),2):
        if name_key(parts[i])==name_key(name):
            lead=re.match(r'^\s*',parts[i])[0];trail=re.search(r'\s*$',parts[i])[0]
            parts[i]=lead+replacement+trail
    return ''.join(parts)

def known_spellings(records):
    result=defaultdict(set)
    for d in records:
        for field in ('artist','albumartist','remixer','album'):
            value=clean(d.get(field))
            if value:
                context=name_key(d.get('albumartist') or d.get('artist')) if field=='album' else ''
                for part in ([value] if field=='album' else credit_names(value)):
                    result[group_id('album' if field=='album' else 'name',part,context)].add(part)
    return result

def multi_fields(data):
    result={}
    for key,values in (data.get('raw_tags') or {}).items():
        field=_RAW_FIELDS.get(key.casefold())
        if field and isinstance(values,list) and len(values)>1:result[field]=values
    return result

def audit_records(records,rules=None,*,reuse=None,changed_paths=None,previous_groups=None):
    rules=validate_rules(rules);buckets=defaultdict(Counter);where={}
    for d in records:
        for field in ('artist','albumartist','remixer','album'):
            value=clean(d.get(field))
            if not value:continue
            context=name_key(d.get('albumartist') or d.get('artist')) if field=='album' else ''
            for part in ([value] if field=='album' else credit_names(value)):
                gid=group_id('album' if field=='album' else 'name',part,context)
                buckets[gid][part]+=1;where.setdefault((d['path'],field),[]).append((gid,part))
    groups=[];group_map={}
    if rules['name_variants']:
        for gid,counts in buckets.items():
            chosen=rules['canonical'].get(gid)
            if len(counts)<2 and (not chosen or chosen in counts):continue
            variants=sorted(counts,key=lambda v:(-counts[v],v.casefold(),v))
            group={'id':gid,'variants':[{'value':v,'count':counts[v]} for v in variants],
                   'suggested':variants[0],'chosen':chosen,'label':variants[0]}
            groups.append(group);group_map[gid]=group
    groups.sort(key=lambda g:g['label'].casefold())
    # Rebuild cheap collection-wide spelling groups, but reuse unchanged
    # per-track diagnostics after a single saved/approved file. If a group
    # changes, recheck every member affected by that change as well.
    previous_group_map={g['id']:g for g in (previous_groups or [])}
    group_changes={gid for gid in set(previous_group_map)|set(group_map)
                   if previous_group_map.get(gid)!=group_map.get(gid)}
    dirty=None if changed_paths is None else set(changed_paths)
    entries=[]
    for data in records:
        if reuse is not None and dirty is not None and data['path'] not in dirty and data['path'] in reuse and not any(
            gid in group_changes for field in ('artist','albumartist','remixer','album')
            for gid,_ in where.get((data['path'],field),[])):
            entries.append(reuse[data['path']]);continue
        issues=[];fixes={};proposals={};multiple=multi_fields(data);current={f:str(data.get(f) or '') for f in FIELDS}
        def issue(code,field,reason,proposed=None,automatic=False,group=None):
            from core.tag_writer import is_field_writable_for_path
            if automatic and not is_field_writable_for_path(data['path'],field):
                automatic=False;reason+=' Запись этого поля для данного формата пока недоступна.'
            if automatic and field in multiple:
                automatic=False;proposed=None;reason+=' В теге несколько значений; сохраните всех участников вручную.'
            issues.append({'code':code,'field':field,'current':'; '.join(str(v) for v in multiple[field]) if field in multiple else current.get(field,data.get('file','')),
                           'proposed':proposed,'automatic':automatic,'reason':reason,**({'group':group} if group else {})})
            if automatic and proposed is not None:fixes[field]=proposed
            if proposed is not None:proposals[field]=proposed
        if data.get('_audit_stale'):
            issue('stale','file','Файл изменился после диагностики. Повторите проверку, чтобы увидеть актуальные теги.')
        elif data.get('_audit_error'):
            issue('unreadable','file','Не удалось прочитать файл: '+data['_audit_error'])
        else:
            from core.credit_style import credit_values
            role_artist=clean(current['artist']) if rules['whitespace'] else current['artist']
            role_title=clean(current['title']) if rules['whitespace'] else current['title']
            styled_role_title,structural_title=title_style(role_title,role_artist) if rules['feat'] else (role_title,False)
            roles=credit_values(role_artist,styled_role_title) if rules['feat'] else current
            from core.release_credits import authority,apply as apply_declared
            declared=authority(data,role_artist,styled_role_title) if rules['feat'] else None
            if declared and declared.get('conflict'):
                roles={'artist':role_artist,'title':styled_role_title}
            elif declared and (declared.get('exact') or not re.search(r'(?i)\b(?:feat\.?|ft\.?|featuring)\s+',styled_role_title)):
                roles=apply_declared(role_artist,styled_role_title,declared)
            # A flat collaboration plus a possibly stale Title feat. is a
            # suggestion for review, not proof that the second artist is a guest.
            explicit_artist_feat=bool(re.search(r'(?i)\b(?:feat\.?|ft\.?|featuring)\s+',role_artist))
            role_auto=not structural_title and explicit_artist_feat and not declared
            for field,value in current.items():
                if any((ord(c)<32 and not c.isspace()) or c in '\u200b\ufeff' for c in value):
                    issue('invisible',field,'Непечатаемый или невидимый символ. Удалите его вручную в редакторе.');continue
                target=clean(value) if rules['whitespace'] else value
                if rules['whitespace'] and target!=value:issue('whitespace',field,'Лишние пробелы или разная Unicode-запись.',target,True)
                if rules['feat'] and field in ('title','artist','albumartist'):
                    featured,structural=title_style(target,current['artist']) if field=='title' else (feat_style(target),False)
                    if featured!=target:
                        issue('title_structure' if structural else 'feat',field,
                              'В Title попали исполнитель и название. Подтвердите отдельно название и feat.' if structural else 'Разное оформление feat.; принято «feat.» и «(feat. …)».',featured,not structural)
                        target=featured
                    if field in ('artist','title') and roles[field]!=current[field] and roles[field]!=target:
                        target=roles[field]
                        reason=('Кредит конкретной записи/издания сохраняет всех основных соавторов; feat. и ремиксеры имеют отдельные роли.'
                                if declared and declared.get('exact') else 'Проверьте роль участника: Title содержит feat., но плоский список артистов сам по себе не отличает гостя от соавтора.')
                        issue('credit_placement',field,reason,target,role_auto)
                if rules['dates_numbers'] and target and field in ('date','tracknumber','discnumber'):
                    try:
                        formatted=iso_date(target) if field=='date' else number_style(target)
                        if formatted!=target:issue('format',field,'Единый формат даты или номера.',formatted,True);target=formatted
                    except ValueError:issue('invalid_format',field,'Проверьте значение вручную: дата YYYY, YYYY-MM или YYYY-MM-DD; номер N либо N/T.')
                for gid,part in where.get((data['path'],field),[]):
                    group=group_map.get(gid)
                    if not group:continue
                    if (not rules['recheck_confirmed'] or data.get('_audit_confirmed_after_recheck')) and (data.get('_ui_name_approval') or {}).get(field) == value:continue
                    chosen=group['chosen']
                    proposed=credit_replace(target,part,chosen) if chosen and field!='album' else chosen
                    if chosen and target!=proposed:
                        issue('name_variant',field,'Написание отличается от выбранного вами.',proposed,True,gid);target=proposed
                    elif not chosen:
                        issue('name_variant',field,'В коллекции есть разные написания. Укажите своё в редакторе или подтвердите текущее. Ручной выбор запоминается для этого трека.',None,False,gid)
                if rules['verified_names']:
                    from core.verified_spellings import field_value
                    verified=field_value(field,target)
                    if verified!=target:
                        issue('verified_name',field,"Официальное написание Hi I'm Ghost без запятой: hiimghostsound.com; Firepower Records.",verified,True)
                        target=verified
                if field in proposals:proposals[field]=target
            for field in ('artist','title'):
                if not clean(current[field]):issue('missing',field,'Обязательный тег не заполнен; нужно ввести вручную.')
            if rules['version_authors'] and not clean(current['remixer']):
                from core.version_authors import version_author_options
                options=version_author_options(current['title'])
                if options:
                    names=list(dict.fromkeys(o['author'] for o in options))
                    issue('version_author','remixer','В названии указан автор версии, а тег ремиксера пуст. Подтвердите имя вручную.','; '.join(names))
            if rules['feat'] and not re.search(r'(?i)\b(?:feat|ft|featuring)\b',current['title']):
                from core.naming import ensure_feature_credit_in_title
                stem=Path(data['path']).stem;match=re.search(r'(?i)\((?:feat\.?|ft\.?|featuring)\s+([^)]*)\)',stem)
                if match:
                    base=re.sub(r'(?i)\s*\((?:feat\.?|ft\.?|featuring)\s+[^)]*\)','',stem)
                    base=base.split(' - ',1)[-1]
                    if name_key(base)==name_key(current['title']) and not declared:issue('filename_credit','title','В имени файла есть feat., которого нет в теге. Имя файла требует ручного подтверждения.',ensure_feature_credit_in_title(proposals.get('title',current['title']),'feat.',clean(match[1])))
            if rules['filename'] and current['artist'] and current['title']:
                from core.file_renamer import suggested_filename
                expected=suggested_filename(data)
                if expected!=Path(data['path']).name:issue('filename','file','Имя файла отличается от шаблона «Исполнитель - Название». Переименуйте отдельно по сохранённым тегам.',expected)
            if rules['single_album']:
                from core.release_titles import decide as single_album
                # Semantic cleanup must refine, not undo, the chosen spelling.
                policy=single_album({**data,'album':proposals.get('album',current['album'])})
                if policy['value']!=proposals.get('album',current['album']):
                    issue('single_album','album',policy['reason'],policy['value'],policy['automatic'])
            if rules['edition_credits']:
                from core.verified_releases import warnings
                for reason in warnings(data):issue('edition_identity','file',reason)
            # Apply release semantics LAST: a casing/feat-style proposal must
            # never resurrect a guest or copy a track collaborator into TPE2.
            if rules['album_artist']:
                from core.album_artist import decide
                target=proposals.get('albumartist',current['albumartist'])
                policy=decide(data,target)
                proposed=policy['value']
                needs_issue=proposed is None or proposed!=target or policy['basis']=='track_guest'
                if needs_issue:
                    if 'albumartist' in multiple:
                        fixes.pop('albumartist',None);proposals.pop('albumartist',None)
                        for item in issues:
                            if item['field']=='albumartist':item.update(proposed=None,automatic=False)
                        issue('album_artist','albumartist',policy['reason']+' В теге несколько значений; сохраните их вручную.')
                    else:
                        if not policy['automatic']:
                            fixes.pop('albumartist',None)
                            for item in issues:
                                if item['field']=='albumartist':item['automatic']=False
                        issue('album_artist','albumartist',policy['reason'],proposed,policy['automatic'])
                        if proposed is None:
                            proposals.pop('albumartist',None)
                            for item in issues:
                                if item['field']=='albumartist':item['proposed']=None
        # Never apply an intermediate auto-fix when the final value of this
        # field requires manual structural confirmation.
        for field,value in proposals.items():
            if field in fixes and fixes[field]!=value:fixes.pop(field)
            for item in issues:
                if item['field']==field and item['proposed'] is not None:item['proposed']=value
        if 'artist' in fixes and roles.get('title')!=current['title'] and 'title' not in fixes:
            fixes.pop('artist',None)
            for item in issues:
                if item['code']=='credit_placement':
                    item['automatic']=False;item['reason']+=' Сначала подтвердите Artist и Title вместе в редакторе.'
        from core.audit_approval import apply as apply_approval
        entry={'path':data['path'],'signature':data.get('analysis_file_signature'),
               'issues':issues,'fixes':fixes,'proposals':proposals,'checked':True}
        entries.append(entry if rules['recheck_confirmed'] and not data.get('_audit_confirmed_after_recheck') else apply_approval(data,entry))
    summary={'checked':len(records),'deviations':sum(bool(e['issues']) for e in entries),
             'automatic':sum(bool(e['fixes']) for e in entries),'manual':sum(any(not i['automatic'] for i in e['issues']) for e in entries),
             'errors':sum(bool(d.get('_audit_error')) for d in records)}
    return {'entries':entries,'groups':groups,'summary':summary,'rules':rules}


def read_collection(files,progress=None,cancel=None):
    from core.scanner import scan_file
    records=[]
    for i,path in enumerate(files):
        if cancel and cancel():raise AuditCancelled()
        if progress:progress(i,len(files),str(path))
        try:records.append(scan_file(path))
        except Exception as error:records.append({'path':str(path),'file':Path(path).name,'_audit_error':str(error)})
    if progress:progress(len(files),len(files),'')
    return records
