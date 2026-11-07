"""Place explicit featured credits in Title without guessing collaborator roles."""
import re
from core.naming import split_feature_credit,ensure_feature_credit_in_title

def credit_values(artist,title):
    primary,role,guest=split_feature_credit(artist)
    result_artist=artist;result_title=title
    if primary and guest and role=='feat.':
        # Do not overwrite a different, already explicit title guest.
        existing=re.search(r'(?:[\[(]\s*|\s)(?:feat\.?|ft\.?|featuring)\s+([^\])]+)',title,re.I)
        if not existing or existing[1].strip().casefold()==guest.casefold():
            result_artist=primary;result_title=ensure_feature_credit_in_title(title,'feat.',guest)
    match=re.search(r'(?:[\[(]\s*|\s)(?:feat\.?|ft\.?|featuring)\s+([^\])]+)',result_title,re.I)
    if match:
        guest=match[1].strip()
        # Whole literal credit only. Never substring-match Ann inside Anna,
        # split a band name at &, or guess that an ordinary coauthor is a guest.
        sep=r'\s+(?:&|x|×)\s+|\s*;\s*|,\s+'
        pattern=re.compile(r'(^|'+sep+r')'+re.escape(guest)+r'(?=$|'+sep+r')',re.I)
        m=pattern.search(result_artist)
        if m:
            before=result_artist[:m.start()];after=result_artist[m.end():]
            candidate=(before+after if before else re.sub(r'^(?:'+sep+r')','',after)).strip()
            if candidate:result_artist=candidate
    return {'artist':result_artist,'title':result_title}


def editor_credit_values(artist,title,filename=''):
    """An explicit editor action formats the *currently entered* identity.

    Saved tags can contain an uploader instead of the artist. Do not reuse a
    cached diagnostic based on those tags after the user has corrected Artist.
    A filename only corroborates removal of an exact upload-label suffix; it
    never supplies a different song, guest, artist or recording version.
    """
    from core.uniformity import title_style,artist_credit_key,name_key,clean
    from core.naming import canonical_track_title
    artist=clean(artist)
    formatted,_=title_style(clean(title),artist)
    # A source caption is sometimes retained after the bare feat. is formatted.
    # Keep meaningful Live/Remix/Edit/etc. suffixes, even when absent in filename.
    from core.title_identity import CAPTION
    candidate=CAPTION.sub('',formatted).strip()
    if candidate and candidate!=formatted and filename:
        stem=re.sub(r'\.[^.\\/]+$','',re.split(r'[\\/]',filename)[-1])
        for separator in re.finditer(r'\s+[-–—]\s+',stem):
            prefix=stem[:separator.start()];rest=stem[separator.end():]
            primary,_,_=split_feature_credit(artist)
            file_primary,_,_=split_feature_credit(prefix)
            if not primary or artist_credit_key(primary)!=artist_credit_key(file_primary):continue
            file_title,_=title_style(rest,artist)
            if name_key(canonical_track_title(file_title))==name_key(canonical_track_title(candidate)):
                formatted=candidate;break
    return credit_values(artist,formatted)
