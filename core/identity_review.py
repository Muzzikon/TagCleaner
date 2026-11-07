"""Write-side identity objections and transparent manual review context."""
import re
from pathlib import Path


def proposed_identity(data):
    plan = data.get('change_plan') or {}
    fusion = data.get('fallback_fusion') or {}
    candidate = (data.get('best_match') or {}).get('candidate') or {}
    fallback = fusion if fusion.get('status') in {'SAFE', 'REVIEW'} else candidate
    result = {}
    for name in ('artist', 'title'):
        field = plan.get(name) or {}
        value = field.get('proposed') if field.get('status') in {'SAFE', 'REVIEW', 'MANUAL'} else field.get('current')
        result[name] = value or fallback.get(name) or data.get(name) or ''
    if not result['artist']:
        result['artist'] = ' & '.join(candidate.get('artists') or [])
    return result


def _title_for_comparison(title):
    from core.naming import canonical_track_title
    value = canonical_track_title(title)
    # Explicit credits and a bitrate label do not belong to the base title.
    # Ordinary parentheses and every version descriptor remain significant.
    value = re.sub(r'[\(\[]\s*(?:feat\.?|ft\.?|featuring|with|prod\.?\s+by)\s+[^()\[\]]+[\)\]]', '', value, flags=re.I)
    return re.sub(r'[\(\[]\s*\d{2,4}\s*kbps\s*[\)\]]', '', value, flags=re.I).strip()


def filename_identity_conflict(data):
    """Filename is contrary evidence, never proof that its spelling is correct."""
    from core.identity_fusion import _identity_title_score
    from core.versioning import compare_versions, version_descriptors, version_base_title
    from core.naming import extract_feature_entities_from_title
    from core.matcher import split_artists, compact
    from core.artist_aliases import same_artist
    name = data.get('file') or Path(data.get('path') or '').name
    plan = data.get('change_plan') or {}
    if plan.get('identity') in {'REJECT', 'CONFLICT'} and not any(
            (plan.get(key) or {}).get('status') in {'REVIEW', 'MANUAL'}
            and (plan.get(key) or {}).get('proposed') for key in ('artist', 'title')):
        # Blocked current tags are not a proposed identity. Other diagnostics
        # still explain any contrary audio candidates preserved in evidence.
        return None
    parts = re.split(r'\s+[-–—]\s+', Path(name).stem, maxsplit=1)
    target = proposed_identity(data)
    if len(parts) != 2 or not all(target.values()):
        return None
    artist, title = (p.strip() for p in parts)
    if not artist or not title:
        return None
    candidate = (data.get('best_match') or {}).get('candidate') or {}
    known = candidate.get('artists') or []
    if candidate.get('artist_credit_is_flat'):
        # Consensus serializes a whole credit into one string; it does not
        # establish a single artist entity named "A, B & C". Protect only
        # actual entities from the selected recording/release in this path.
        known = []
        mb = data.get('musicbrainz') or {}
        if candidate.get('recording_id') and mb.get('id') == candidate['recording_id']:
            context = data.get('selected_release_context') or (data.get('release_context_selection') or {}).get('selected') or {}
            items = context.get('track_artist_credit_items') or mb.get('artist-credit') or []
            for item in items:
                artist_record = item.get('artist') or {} if isinstance(item, dict) else {}
                if artist_record.get('id'):
                    known.extend(x for x in (item.get('name'), artist_record.get('name')) if x)
    local = split_artists(artist, known_entities=known) + extract_feature_entities_from_title(title, known_entities=known)['artists']
    remote = split_artists(target['artist'], known_entities=known) + extract_feature_entities_from_title(target['title'], known_entities=known)['artists']
    def same(x, y):
        return compact(x) == compact(y) or same_artist(x, y)
    missing = [x for x in local if not any(same(x, y) for y in remote)]
    left, right = _title_for_comparison(title), _title_for_comparison(target['title'])
    version_conflict = compare_versions(left, right)[1]
    score = _identity_title_score(left, right)
    if not version_conflict and version_descriptors(left) and version_descriptors(right):
        score = max(score, _identity_title_score(version_base_title(left), version_base_title(right)))
    if not missing and not version_conflict and score >= 92:
        return None
    return {'filename_identity': {'artist': artist, 'title': title}, 'proposed_identity': target,
            'title_score': score, 'version_conflict': version_conflict,
            'unconfirmed_artists': missing}


def needs_identity_confirmation(data):
    plan = data.get('change_plan') or {}
    return plan.get('identity') in {'REVIEW', 'REJECT', 'CONFLICT'} or any(
        item.get('severity') == 'WARNING' for item in data.get('track_diagnostics') or [])


def review_context_lines(data):
    target = proposed_identity(data)
    lines = ['Проверка песни и версии целиком:',
             f"  файл: {data.get('file') or data.get('path') or '—'}",
             f"  текущие теги: {data.get('artist') or '—'} — {data.get('title') or '—'}",
             f"  предложено: {target['artist'] or '—'} — {target['title'] or '—'}"]
    best = data.get('best_match') or {}
    if best.get('acoustid_score') is not None:
        lines.append(f"  AcoustID score: {best['acoustid_score']} (не вероятность правильного ответа)")
    for key, label in (('acrcloud_identity', 'ACRCloud'), ('shazam_identity', 'Shazam'),
                       ('catalog_identity', 'Каталог'), ('source_url_identity', 'Source URL')):
        value = data.get(key) or {}
        if value:
            lines.append(f"  {label}: {value.get('status') or '—'}")
        if key == 'shazam_identity' and value.get('attempts'):
            attempts = value['attempts']
            found = sum((item.get('recognition') or {}).get('status') == 'FOUND' for item in attempts)
            lines.append(f"  Shazam: ответы FOUND — {found}/{len(attempts)}; "
                         f"согласованных непересекающихся фрагментов — {value.get('agreement_count', 0)}. "
                         "Это не вероятность правильного ответа.")
            for item in attempts:
                result = item.get('recognition') or {}
                sample = item.get('sample') or result.get('sample') or {}
                start = sample.get('start_seconds')
                location = f"с {start} с" if start is not None else 'позиция неизвестна'
                text = f"  Shazam, {location}: {result.get('status') or '—'}"
                if result.get('status') == 'FOUND':
                    text += f" — {result.get('artist') or '—'} — {result.get('title') or '—'}"
                lines.append(text)
    seen = set()
    for warning in data.get('track_diagnostics') or []:
        if warning.get('severity') != 'WARNING':
            continue
        message = warning.get('message') or warning.get('code') or ''
        if message not in seen:
            lines.append('  Требует проверки: ' + message)
            seen.add(message)
        if warning.get('code') == 'LOCAL_IDENTITY_CONFLICT':
            lines.append('  Расхождение с именем файла: ' + str(warning.get('filename_identity')))
        if warning.get('code') == 'DURATION_MISMATCH':
            lines.append(f"  {warning.get('source')}: файл {warning.get('local_seconds')} с, источник {warning.get('reference_seconds')} с")
        for alt in warning.get('alternatives') or []:
            alt = alt.get('candidate') or alt
            artist = alt.get('artist') or ' & '.join(alt.get('artists') or [])
            title = alt.get('title') or ''
            pair = (artist, title)
            if title and pair not in seen:
                lines.append(f'  Альтернатива: {artist or "—"} — {title}')
                seen.add(pair)
    return lines
