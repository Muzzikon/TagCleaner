"""Comparison of track identities with conservative upload caption handling."""
import re

# Exact, bracketed labels only. Never strip a Remix, VIP, Live or other version.
CAPTION = re.compile(r'\s*[\[(](?:(?:official\s+)?(?:music\s+video|video|audio|lyric\s+video)|melodic\s+dubstep)[\])]\s*$',re.I)

def comparison_title(artist,title):
    from core.uniformity import title_style
    from core.naming import canonical_track_title
    title,_=title_style(str(title or ''),artist)
    return canonical_track_title(CAPTION.sub('',title).strip())
