"""URL policy shared with the frozen UI; no parser or network dependencies."""
import re
from urllib.parse import urlparse,urlunparse

# Provider-owned domain families cover regional/numbered CDN nodes, including
# redirects. Shared CDN operators (Fastly/Akamai) need a Last.fm tenant name.
ARTWORK_DOMAINS = ('coverartarchive.org', 'archive.org', 'mzstatic.com',
                   'dzcdn.net', 'deezer.com', 'discogs.com', 'last.fm', 'sndcdn.com', 'bcbits.com')
_LASTFM_CDN = re.compile(r'(?:[a-z0-9-]+\.)*lastfm(?:-img[0-9]*)?\.freetls\.fastly\.net\Z|lastfm-img[0-9]*\.akamaized\.net\Z')


def _allowed_image_url(url):
    try:
        parsed=urlparse(url)
        host=(parsed.hostname or '').lower()
        return bool(parsed.scheme=='https' and parsed.port in (None,443) and not parsed.username and not parsed.password and (
            any(host==domain or host.endswith('.'+domain) for domain in ARTWORK_DOMAINS) or
            _LASTFM_CDN.fullmatch(host)))
    except (ValueError,TypeError,AttributeError):
        return False



def bandcamp_source_url(value):
    p=urlparse(str(value).strip())
    if (p.scheme!='https' or not re.fullmatch(r'[a-z0-9-]+\.bandcamp\.com',p.hostname or '')
            or p.username or p.password or p.port not in (None,443)
            or not re.fullmatch(r'/(album|track)/[a-zA-Z0-9_-]+/?',p.path)):
        raise ValueError('Нужна HTTPS-ссылка на страницу альбома или трека Bandcamp.')
    return urlunparse(('https',p.hostname,p.path.rstrip('/'),'','',''))


def source_url(value):
    try:
        p=urlparse(str(value).strip())
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443):raise ValueError()
        host=(p.hostname or '').lower()
        if host in ('soundcloud.com','www.soundcloud.com','m.soundcloud.com'):
            if not re.fullmatch(r'/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+/?',p.path):raise ValueError()
            if p.path.split('/')[1].lower() in ('search','discover','charts','you','stations','tags','pages'):raise ValueError()
            if p.path.rstrip('/').split('/')[-1].lower() in ('sets','tracks','albums','reposts','likes','popular-tracks'):raise ValueError()
            return urlunparse(('https','soundcloud.com',p.path.rstrip('/'),'','',''))
        return bandcamp_source_url(value)
    except (ValueError,TypeError,AttributeError):
        raise ValueError('Нужна HTTPS-ссылка на трек SoundCloud или страницу трека/альбома Bandcamp.') from None
