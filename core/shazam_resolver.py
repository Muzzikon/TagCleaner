"""Preserve all Shazam hypotheses; require agreement on separate audio parts."""
from functools import partial
from core.acrcloud_matcher import evaluate_acrcloud_match
from core.shazam_client import audio_windows, recognize_sample, shazam_available


def resolve_shazam_identity(file_path, data, *, recognizer=None, windows=None, refresh_generation=None):
    if recognizer is None and not shazam_available():
        return {'status': 'SKIPPED', 'best': None, 'attempts': [],
                'reason': 'Для дополнительного бесплатного поиска: install_free_audio.cmd на Windows или requirements-free-audio.txt в совместимом Python. TAGCLEANER_SHAZAM=0 отключает источник.'}
    recognizer = recognizer or (partial(recognize_sample, refresh_generation=refresh_generation)
                                if refresh_generation else recognize_sample)
    try:
        windows = audio_windows(file_path) if windows is None else windows
    except Exception as error:
        return {'status': 'ERROR', 'best': None, 'attempts': [],
                'errors': [str(error)], 'verification_incomplete': True}
    if not windows:
        return {'status': 'SKIPPED', 'best': None, 'attempts': [],
                'reason': 'Аудио короче 8 секунд: надёжная дополнительная проверка невозможна.'}
    attempts, found, errors = [], [], []
    for sample in windows:
        try:
            result = recognizer(file_path, sample=sample)
        except Exception as error:
            result = {'status': 'ERROR', 'error': str(error), 'sample': sample}
        attempt = {'sample': sample, 'recognition': result, 'match': None}
        if result.get('status') == 'FOUND':
            attempt['match'] = evaluate_acrcloud_match(file_path, data, result)
            found.append(attempt)
        elif result.get('status') == 'ERROR':
            errors.append(result.get('error') or 'ShazamIO error')
        attempts.append(attempt)
    if not found:
        return {'status': 'ERROR' if errors else 'NOT_FOUND', 'best': None,
                'attempts': attempts, 'errors': errors, 'verification_incomplete': bool(errors)}
    from core.identity_fusion import identities_match
    selected = max(found, key=lambda item: float(item['match'].get('overall_score') or 0))
    best, match = selected['recognition'], selected['match']
    compatible = [item for item in found if identities_match(best, item['recognition'])]
    alternatives = [item['recognition'] for item in found if not identities_match(best, item['recognition'])]
    ambiguous = bool(alternatives)
    # Defensive check even for injected/reused windows: identical or overlapping
    # samples cannot count twice. Service IDs/ISRCs alone cannot resolve versions.
    ends, count = [], 0
    for item in sorted(compatible, key=lambda item: item['sample']['start_seconds']):
        start = float(item['sample']['start_seconds'])
        end = start + float(item['sample']['sample_seconds'])
        if end > start and all(end <= a + .001 or start >= b - .001 for a, b in ends):
            ends.append((start, end))
            count += 1
    local_ok = (not match.get('version_conflict') and match.get('artist_score', 0) >= 90
                and match.get('title_score', 0) >= 95)
    status = ('CONFLICT' if ambiguous or match.get('version_conflict')
              else 'STRONG' if count >= 2 and local_ok and not errors else 'PARTIAL')
    reason = ('Фрагменты Shazam указывают на разные треки/версии; все варианты требуют проверки.' if ambiguous
              else 'Shazam конфликтует с явно указанной локальной версией.' if match.get('version_conflict')
              else f'Shazam: согласованных непересекающихся фрагментов — {count}; для автоматической записи нужно независимое подтверждение.')
    summary_alternatives = [{k: item.get(k) for k in ('artist', 'title', 'shazam_id', 'isrc', 'sample')}
                            for item in alternatives]
    return {'status': status, 'best': best, 'match': match, 'attempts': attempts,
            'agreement_count': count, 'identity_ambiguous': ambiguous,
            'identity_alternatives': summary_alternatives, 'errors': errors,
            'verification_incomplete': bool(errors), 'reason': reason}
