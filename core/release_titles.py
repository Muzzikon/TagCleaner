"""The single's Album is a release title, not a display Artist - Title credit."""
import re
from core.naming import canonical_album_name,canonical_track_title

def is_single(context):
    kind=str(context.get('primary_type') or context.get('type') or '').casefold()
    return kind=='single' or (not kind and bool(re.search(r'\s[-–—]\sSingle$',context.get('title') or '',re.I)))

def without_feature(value):
    text=canonical_album_name(value)
    # Preserve remix/version blocks; remove only the explicit artist-credit part.
    def block(m):
        inner=re.sub(r'(?i)(?:^|\s)(?:feat\.?|ft\.?|featuring)\s+.+$','',m[2]).strip()
        return m[1]+inner+m[3] if inner else ''
    text=re.sub(r'([\[(])([^()\[\]]+)([\])])',block,text)
    text=re.sub(r'(?i)\s+(?:feat\.?|ft\.?|featuring)\s+[^()\[\]]+(?=[(\[]|$)',' ',text)
    return ' '.join(text.split())

def title(context):
    raw=canonical_album_name(context.get('title') or '')
    if not is_single(context):return raw
    # - Single is a storefront type marker; the record's title remains intact.
    raw=re.sub(r'\s[-–—]\sSingle$','',raw,flags=re.I)
    return without_feature(raw)

def same_album(context,album):
    from core.album_artist import album_key
    wanted=album_key(without_feature(album) if is_single(context) else album)
    aliases=context.get('release_title_aliases') or [] if context.get('credit_roles_verified') else []
    return bool(wanted and wanted in {album_key(n) for n in [title(context),*aliases]})

def decide(data):
    from core.album_artist import album_key
    current=str(data.get('album') or '')
    contexts=[data.get('_ui_manual_release'),data.get('selected_release_context'),(data.get('release_context_selection') or {}).get('selected'),
              *(data.get('release_contexts') or []),*(data.get('_ui_extra_releases') or [])]
    from core.release_credits import matches
    selected=[c for c in contexts if isinstance(c,dict) and is_single(c)
              and album_key(without_feature(current))==album_key(without_feature(c.get('title') or ''))
              and (not c.get('track_title') or matches(c,data.get('artist') or '',data.get('title') or ''))]
    values={title(c) for c in selected}
    if len(values)==1:
        expected=next(iter(values))
        # Catalog case is not a different edition. Keep the entered spelling
        # while removing only actual storefront / featured-credit decoration.
        local=without_feature(re.sub(r'\s[-–—]\sSingle$','',current,flags=re.I))
        if local and local.casefold()==expected.casefold():expected=local
        return {'value':expected,'automatic':True,'reason':'Название сингла в Album не включает служебный feat.; обозначение версии сохраняется.'}
    clean=without_feature(re.sub(r'\s[-–—]\sSingle$','',current,flags=re.I))
    track=without_feature(canonical_track_title(data.get('title') or ''))
    if clean!=current and clean and album_key(clean)==album_key(track) and (str(data.get('tracknumber') or '')=='1/1' or re.search(r'\s[-–—]\sSingle$',current,re.I)):
        return {'value':clean,'automatic':False,'reason':'Похоже на название сингла с feat. Проверьте тип и название релиза; гость относится к кредитам, не к Album.'}
    return {'value':current,'automatic':True,'reason':'Название Album сохраняется по выбранному релизу.'}
