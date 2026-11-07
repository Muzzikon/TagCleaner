"""Review-only identity/spelling candidates corroborated by local text.

A filename and an upload caption are not catalog verification. Never change
the song, version, collaborator roles or artist styling by title-casing text.
"""
import re

from core.naming import canonical_track_title
from core.uniformity import artist_credit_key,clean


def title_key(value):
    from core.uniformity import title_style
    text=canonical_track_title(title_style(value)[0])
    # A generic trailing VIP has no author named after the song. Catalogs
    # legitimately alternate "Song VIP" and "Song (VIP)".
    text=re.sub(r'\s+VIP\s*$',' (VIP)',text,flags=re.I)
    return text.casefold().replace('[','(').replace(']',')')


def credit_compatible(left,right):
    """Only a missing colon may corroborate local captions, never an alias.

    Windows upload filenames often omit a colon. Do not generalize this to
    arbitrary punctuation, different participants, or a colon in another place.
    """
    a,b=artist_credit_key(left),artist_credit_key(right)
    if a==b:return True
    if sorted((a.count(':'),b.count(':')))!=[0,1]:return False
    return clean(a.replace(':',' '))==clean(b.replace(':',' '))


def credited_spelling(file_artist,other):
    # Preserve the explicit colon from the caption/entered artist rather than
    # copying a filename's Windows workaround back into the music tag.
    if not re.search('[:：]',file_artist) and re.search('[:：]',other):return clean(other)
    return file_artist


def caption_credit_compatible(caption,file_credit):
    """Compare all explicitly named people without assigning their roles.

    An upload caption may say "A feat. B" while its filename says "A & B".
    This is local corroboration only; keep the caption's explicit feat.
    """
    if credit_compatible(caption,file_credit):return True
    from core.naming import split_feature_credit
    from core.recording_credit import roles
    _,left_role,left_guest=split_feature_credit(caption)
    _,right_role,right_guest=split_feature_credit(file_credit)
    if left_role!='feat.' and right_role!='feat.':return False
    if left_role==right_role=='feat.' and not credit_compatible(left_guest,right_guest):return False
    left=roles(caption,'')[1];remaining=list(roles(file_credit,'')[1])
    if not left or len(left)!=len(remaining):return False
    for name in left:
        match=next((n for n in remaining if credit_compatible(name,n)),None)
        if match is None:return False
        remaining.remove(match)
    return True


def proposal(artist,title,filename,remixer=''):
    from core.credit_style import editor_credit_values
    from core.version_authors import version_author_options
    basename=re.split(r'[\\/]',str(filename or ''))[-1]
    stem=re.sub(r'\.(?:mp3|flac|m4a|mp4|ogg|opus|wav|aiff|aif)$','',basename,flags=re.I)
    # An uploader suffix is removable only when it literally repeats Artist
    # and the remaining complete caption agrees with the filename below.
    caption=title.strip().rstrip('|').strip()
    if '|' in caption and clean(caption.rsplit('|',1)[1]).casefold()==clean(artist).casefold():
        caption=caption.rsplit('|',1)[0].strip()
    candidates=[]
    for split in re.finditer(r'\s+[-–—]\s+',stem):
        file_artist=clean(stem[:split.start()]);file_title=clean(stem[split.end():])
        if not file_artist or not file_title:continue
        caption_artists=[clean(caption[:s.start()]) for s in re.finditer(r'\s+[-–—]\s+',caption)
                         if caption_credit_compatible(caption[:s.start()],file_artist)
                         and title_key(caption[s.end():])==title_key(file_title)]
        same_entered=credit_compatible(artist,file_artist)
        missing_artist=not artist.strip() and title_key(caption)==title_key(file_title)
        if not caption_artists and not missing_artist and not (same_entered and title_key(title)==title_key(file_title)):continue
        spelling=credited_spelling(file_artist,caption_artists[0] if caption_artists else artist)
        if caption_artists:
            from core.naming import split_feature_credit
            if split_feature_credit(caption_artists[0])[1]=='feat.':spelling=caption_artists[0]
        values=editor_credit_values(spelling,file_title,filename)
        values['title']=canonical_track_title(values['title'])
        values['remixer']=remixer
        authors=version_author_options(values['title'])
        if not remixer.strip() and len(authors)==1:values['remixer']=authors[0]['author']
        candidates.append((same_entered,values))
    matching=[v for matches,v in candidates if matches]
    choices=matching or [v for _,v in candidates]
    unique={tuple(v.items()):v for v in choices}
    if len(unique)!=1:return None
    values=next(iter(unique.values()))
    if values=={'artist':artist,'title':title,'remixer':remixer}:return None
    return {'values':values,'verified':False,'source':'Title и имя файла',
            'reason':'Исполнитель, трек и версия согласуются в Title и имени файла. '
                     'Имя отделено от названия; явное двоеточие в имени сохранено. '
                     'Локальное написание не подтверждено каталогом. '
                     'Проверьте вариант перед записью. Дата, альбом и номера не меняются.'}
