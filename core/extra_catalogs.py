"""Optional text discovery. These providers never constitute audio evidence."""
import hashlib
import os
import re
import threading
import time
from pathlib import Path

import requests

from core.cache_utils import atomic_write_json, read_json_if_fresh
from core.singleflight import cached_singleflight

CACHE_DIR = Path(__file__).resolve().parent.parent / 'cache' / 'extra_catalogs_v1'
PROVIDERS = ('discogs', 'lastfm')
_KEYS = {'discogs': 'DISCOGS_TOKEN', 'lastfm': 'LASTFM_API_KEY'}
_URLS = {'discogs': 'https://api.discogs.com', 'lastfm': 'https://ws.audioscrobbler.com/2.0/'}
_locks = {p: threading.RLock() for p in PROVIDERS}
_sessions = {p: requests.Session() for p in PROVIDERS}
_state = {p: {'last': 0.0, 'blocked': None, 'until': 0.0,
              'requests': 0, 'cache_hits': 0, 'errors': 0,
              'last_http_status': None, 'last_api_error': None, 'last_body_kind': None} for p in PROVIDERS}


class CatalogError(RuntimeError):
    """Contains only a safe category, never a request URL or credential."""


def capability():
    return {p: {'configured': bool(os.getenv(_KEYS[p], '').strip()),
                'enabled': bool(os.getenv(_KEYS[p], '').strip()) and
                os.getenv('TAGCLEANER_' + p.upper(), '1').lower().strip() in {'1', 'true', 'yes', 'on'}}
            for p in PROVIDERS}


def checkpoint_signature():
    # Key rotation must retry a previous authentication failure. Only the final
    # digest enters checkpoint keys; tokens never enter reports or raw caches.
    return {p: hashlib.sha256(os.getenv(_KEYS[p], '').strip().encode()).hexdigest()
            for p, value in capability().items() if value['enabled']}


def service_state():
    result = capability()
    for p in PROVIDERS:
        with _locks[p]:
            result[p].update({k: _state[p][k] for k in ('blocked', 'requests', 'cache_hits', 'errors',
                                                      'last_http_status', 'last_api_error', 'last_body_kind')})
    return result


def reset_state():
    for p in PROVIDERS:
        with _locks[p]:
            _state[p].update(last=0.0, blocked=None, until=0.0, requests=0, cache_hits=0, errors=0,
                             last_http_status=None, last_api_error=None, last_body_kind=None)


def _fail(provider, category, cooldown=0, permanent=False):
    state = _state[provider]
    state['errors'] += 1
    if cooldown or permanent:
        state['blocked'] = category
        state['until'] = float('inf') if permanent else time.monotonic() + cooldown
    raise CatalogError(f'{provider}: {category}') from None


def _request(provider, endpoint, params, *, cache=True):
    """Cache only valid success payloads; serialize and pace each provider."""
    if not capability()[provider]['enabled']:
        raise CatalogError(f'{provider}: disabled_or_missing_key')
    key = repr((endpoint, sorted(params.items())))
    path = CACHE_DIR / (provider + '_' + hashlib.sha256(key.encode()).hexdigest() + '.json')
    with _locks[provider]:
        state = _state[provider]
        cached = read_json_if_fresh(path, ttl_days=30) if cache else None
        if isinstance(cached, dict) and isinstance(cached.get('payload'), dict):
            if not cached.get('empty') or read_json_if_fresh(path, ttl_days=1):
                state['cache_hits'] += 1
                return cached['payload']
        if state['blocked'] and state['until'] > time.monotonic():
            raise CatalogError(f"{provider}: paused_{state['blocked']}")
        state.update(blocked=None, until=0.0)
        delay = 1.1 - (time.monotonic() - state['last'])
        if delay > 0:
            time.sleep(delay)
        query = dict(params)
        headers = {'User-Agent': 'TagCleaner/13.17 (+personal music catalog lookup)', 'Accept': 'application/json'}
        if provider == 'discogs':
            headers['Authorization'] = 'Discogs token=' + os.getenv(_KEYS[provider], '').strip()
        else:
            query.update(api_key=os.getenv(_KEYS[provider], '').strip(), format='json')
        state['requests'] += 1
        try:
            response = _sessions[provider].get(_URLS[provider] + endpoint, params=query,
                                               headers=headers, timeout=(10, 25), allow_redirects=False)
        except requests.RequestException:
            _fail(provider, 'network_error', cooldown=60)
        finally:
            state['last'] = time.monotonic()
        state['last_http_status'] = response.status_code
        state['last_api_error'] = None
        try:
            payload = response.json()
            state['last_body_kind'] = 'json'
        except ValueError:
            payload = None
            state['last_body_kind'] = 'non_json'
        # Last.fm can return a structured API error with HTTP 403. Inspect only
        # its numeric code: never persist raw server text or request URLs.
        if isinstance(payload, dict) and payload.get('error'):
            code = str(payload['error'])
            state['last_api_error'] = int(code) if code.isdigit() else None
            # Invalid parameters concern this request, not provider availability.
            # Keep the error visible and uncached; do not block unrelated tracks.
            request_local = (provider == 'lastfm' and code == '6'
                             and response.status_code == 200)
            _fail(provider, 'api_' + code if code.isdigit() else 'api_error',
                  cooldown=0 if request_local else 60,
                  permanent=code in {'4', '10', '26'})
        if response.status_code in (401, 403):
            _fail(provider, 'http_' + str(response.status_code) + '_access_denied', permanent=True)
        if response.status_code == 429:
            try:
                cooldown = max(60.0, float(response.headers.get('Retry-After', 60)))
            except (TypeError, ValueError):
                cooldown = 60.0
            _fail(provider, 'rate_limit', cooldown=cooldown)
        if response.status_code != 200:
            _fail(provider, 'http_' + str(response.status_code), cooldown=60)
        if state['last_body_kind'] == 'non_json':
            _fail(provider, 'invalid_json', cooldown=60)
        if not isinstance(payload, dict):
            _fail(provider, 'invalid_payload', cooldown=60)
        # A schema error must not be cached as a successful empty search.
        if provider == 'discogs':
            field = 'results' if endpoint == '/database/search' else 'tracklist'
            valid = isinstance(payload.get(field), list) and all(isinstance(x, dict) for x in payload[field])
            empty = not payload.get(field)
        elif params.get('method') == 'track.getInfo':
            track = payload.get('track')
            valid = isinstance(track, dict) and isinstance(track.get('name'), str) and isinstance(track.get('artist'), dict)
            empty = False
        elif params.get('method') == 'track.search':
            root = payload.get('results')
            valid = isinstance(root, dict) and isinstance(root.get('trackmatches'), dict)
            matches = root.get('trackmatches', {}).get('track', []) if valid else []
            valid = valid and isinstance(matches, (list, dict))
            if isinstance(matches, list):
                valid = valid and all(isinstance(x, dict) for x in matches)
            empty = not matches
        else:
            valid = isinstance(payload.get('corrections'), dict)
            correction = payload['corrections'].get('correction') if valid else None
            empty = not correction
            if correction:
                valid = valid and isinstance(correction, (dict, list))
                valid = valid and all(isinstance(x, dict) and isinstance(x.get('track'), dict)
                                      for x in _items(correction))
        if not valid:
            _fail(provider, 'invalid_schema', cooldown=60)
        if cache:
            atomic_write_json(path, {'empty': empty, 'payload': payload})
        return payload


def _items(value):
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []


def _credit(items):
    parts = []
    for item in items or []:
        # Discogs numeric suffix is a database disambiguator, not stage name.
        name = item.get('anv') or re.sub(r' \(\d+\)$', '', item.get('name') or '')
        if not name:
            continue
        parts.append(name)
        parts.append(' ' + (item.get('join') or ',') + ' ')
    return ''.join(parts[:-1]).strip()


def _duration(value):
    if not re.fullmatch(r'\d+:\d{2}(?::\d{2})?', str(value or '')):
        return None
    parts = [int(x) for x in value.split(':')]
    if any(x >= 60 for x in parts[1:]):
        return None
    total = 0
    for part in parts:
        total = total * 60 + part
    return total or None


def _tracks(items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get('sub_tracks'):
            for child in _tracks(item['sub_tracks']):
                yield {**child, 'artists': child.get('artists') or item.get('artists')}
        elif item.get('type_', 'track') == 'track' and item.get('title'):
            yield item


@cached_singleflight(lambda artist, title: ('discogs', artist, title))
def search_discogs(artist, title):
    if not title or not capability()['discogs']['enabled']:
        return {'results': [], 'errors': [], 'status': 'DISABLED'}
    candidates, errors = [], []
    limited = False
    try:
        params = {'type': 'release', 'track': title, 'per_page': 10, 'page': 1}
        if artist:
            params['artist'] = artist
        found = _request('discogs', '/database/search', params)
        limited = int((found.get('pagination') or {}).get('pages') or 1) > 1
        seen = set()
        for item in found['results']:
            release_id = item.get('id')
            if not str(release_id).isdigit() or release_id in seen:
                continue
            seen.add(release_id)
            try:
                release = _request('discogs', '/releases/' + str(release_id), {})
                for track in _tracks(release['tracklist']):
                    credit = _credit(track.get('artists') or release.get('artists'))
                    if not credit or credit.casefold() == 'various':
                        continue
                    candidates.append({'provider': 'discogs', 'discovery_only': True,
                        'track_id': f"{release_id}:{track.get('position', '')}:{track['title']}",
                        'artist': credit, 'title': track['title'], 'album': release.get('title'),
                        'duration': _duration(track.get('duration')), 'release_date': release.get('released'),
                        'release_id': str(release_id), 'track_position': track.get('position'),
                        'url': f'https://www.discogs.com/release/{release_id}',
                        'release_choice_required': True, 'cover_choice_required': True,
                        'artist_credits': track.get('artists') or release.get('artists') or []})
            except CatalogError as error:
                errors.append(str(error))
                break
    except (CatalogError, ValueError, TypeError, AttributeError, KeyError) as error:
        errors.append(str(error) if isinstance(error, CatalogError) else 'discogs: invalid_payload')
    return {'results': candidates, 'errors': errors, 'search_limited': limited}


@cached_singleflight(lambda artist, title: ('lastfm', artist, title))
def search_lastfm(artist, title):
    if not title or not capability()['lastfm']['enabled']:
        return {'results': [], 'errors': [], 'status': 'DISABLED'}
    candidates, errors = [], []
    methods = ['track.search', 'track.getCorrection'] if artist else ['track.search']
    for method in methods:
        try:
            params = {'method': method, 'track': title}
            if artist:
                params['artist'] = artist
            if method == 'track.search':
                params['limit'] = 20
            payload = _request('lastfm', '', params)
            if method == 'track.search':
                tracks = _items(payload['results']['trackmatches'].get('track'))
            else:
                tracks = [x.get('track') or {} for x in _items(payload['corrections'].get('correction'))]
            for track in tracks:
                credit = track.get('artist')
                if isinstance(credit, dict):
                    credit = credit.get('name')
                if not isinstance(credit, str) or not track.get('name'):
                    continue
                candidates.append({'provider': 'lastfm', 'discovery_only': True,
                    'artist': credit, 'title': track['name'], 'duration': None,
                    'url': track.get('url'), 'discovery_method': method})
        except (CatalogError, TypeError, KeyError, AttributeError) as error:
            errors.append(str(error) if isinstance(error, CatalogError) else 'lastfm: invalid_payload')
    return {'results': candidates, 'errors': errors}


def check_connections():
    """Two small read-only authenticated queries; no audio or tag writes."""
    result = capability()
    for provider in PROVIDERS:
        if not result[provider]['enabled']:
            result[provider]['status'] = 'DISABLED_OR_MISSING_KEY'
            continue
        try:
            if provider == 'discogs':
                _request(provider, '/database/search', {'artist': 'The Beatles', 'type': 'release', 'per_page': 1}, cache=False)
            else:
                _request(provider, '', {'method': 'track.search', 'artist': 'The Beatles', 'track': 'Yesterday', 'limit': 1}, cache=False)
            result[provider]['status'] = 'OK'
        except CatalogError as error:
            result[provider]['status'] = str(error)
    state = service_state()
    for provider in PROVIDERS:
        result[provider].update({key: state[provider][key] for key in
                                ('last_http_status', 'last_api_error', 'last_body_kind')})
    return result
