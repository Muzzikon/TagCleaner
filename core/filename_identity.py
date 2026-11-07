"""An explicit editor input from the user's Artist - Title filenames.

Independent of saved tags and recognition. Parsing is a reviewable user action,
not proof of an official release or an automatic write-side identity decision.
"""
import re

from core.credit_style import editor_credit_values
from core.naming import canonical_artist_credit,canonical_track_title
from core.version_authors import version_author_options


def proposal(filename,remixer='',*,reference_title=''):
    basename=re.split(r'[\\/]',str(filename or ''))[-1]
    stem=re.sub(r'\.(?:mp3|flac|m4a|mp4|ogg|opus|wav|aiff|aif)$','',basename,flags=re.I)
    parts=re.split(r'\s+[-–—]\s+',stem,maxsplit=1)
    if len(parts)!=2 or not all(p.strip() for p in parts):return None
    if len(stem)>4096 or any(ord(c)<32 for c in stem):return None
    artist,title=parts
    # Reverse our Windows-safe question mark when explicitly filling tags.
    # Underscores are ambiguous; only retain '?' already present in matching
    # metadata, never invent punctuation from an underscore-only filename.
    artist,title=artist.replace('\uff1f','?'),title.replace('\uff1f','?')
    values=editor_credit_values(artist.strip(),title.strip(),basename)
    values['artist']=canonical_artist_credit(values['artist'])
    values['title']=canonical_track_title(values['title'])
    reference=canonical_track_title(str(reference_title or ''))
    if '?' in reference and values['title']==reference.replace('?','_'):
        values['title']=reference
    if not values['artist'] or not values['title']:return None
    authors=version_author_options(values['title'])
    values['remixer']='; '.join(o['author'] for o in authors) if authors else str(remixer or '')
    return {'values':values,'verified':False,'source':'Имя файла',
            'reason':'Вариант разобран из имени файла по шаблону «Исполнитель - Название». '
                     'Все соавторы, явный feat. и обозначение версии сохранены. '
                     'Это заполнение черновика, а не подтверждение каталога. Альбом, дата и номера не меняются.'}


def can_use_for_release(data,entered):
    """A filename may bootstrap saved/incomplete tags, not override new input."""
    return (not str(entered.get('artist') or '').strip() or not str(entered.get('title') or '').strip()
            or all(str(entered.get(f) or '').strip()==str(data.get(f) or '').strip() for f in ('artist','title')))
