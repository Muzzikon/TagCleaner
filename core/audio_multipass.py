"""Bounded additional AcoustID lookups; one provider never becomes several votes."""
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from core.cache_utils import atomic_write_json, file_signature, read_json_if_fresh, stable_digest
from core.fingerprint import FPCALC_PATH, _pcm_duration, _run_fpcalc
from core.acoustid_client import lookup_fingerprint, extract_candidates
from core.matcher import rank_candidates, action_safety, _same_candidate_identity

CACHE_DIR = Path(__file__).resolve().parent.parent / 'cache' / 'fingerprint_variants'
PROFILES = (('prefix_60', 0.0, 60.0), ('skip_intro_5', 5.0, 120.0), ('prefix_180', 0.0, 180.0))


class VariantDecodeError(RuntimeError):
    """Keep measured facts, without treating a short fragment as a valid hit."""
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = {'error_kind': 'local_audio_decode', **details}


def _variant_cache_path(file_path, source_duration, profile):
    key = stable_digest({'file': file_signature(file_path), 'profile': profile,
                         'source_duration': source_duration, 'version': 1})
    return CACHE_DIR / (key + '.json')


def generate_variant(file_path, source_duration, profile):
    file_path = Path(file_path)
    name, start, length = profile
    expected = min(length, source_duration - start)
    if expected < 30:
        return {'status': 'SKIPPED', 'profile': name, 'reason': 'Недостаточно аудио для дополнительного отпечатка.'}
    path = _variant_cache_path(file_path, source_duration, profile)
    try:
        cached = read_json_if_fresh(path, ttl_days=None)
    except OSError:
        cached = None
    if isinstance(cached, dict) and cached.get('fingerprint'):
        return {**cached, 'cache_hit': True}
    ffmpeg = shutil.which('ffmpeg')
    executable = str(FPCALC_PATH) if os.name == 'nt' and FPCALC_PATH.is_file() else shutil.which('fpcalc')
    if not ffmpeg or not executable:
        raise RuntimeError('Для дополнительных отпечатков нужны ffmpeg и fpcalc.')
    with tempfile.TemporaryDirectory(prefix='tagcleaner_variant_') as folder:
        wav = Path(folder) / 'sample.wav'
        command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-xerror', '-ss', str(start),
                   '-i', str(file_path), '-t', str(length), '-map', '0:a:0', '-vn', '-c:a', 'pcm_s16le', str(wav)]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
        if result.returncode or result.stderr.strip():
            raise VariantDecodeError(result.stderr.strip() or 'Не удалось декодировать дополнительный фрагмент.',
                                     source_seconds=source_duration, expected_seconds=expected,
                                     start_seconds=start, returncode=result.returncode)
        duration = _pcm_duration(wav)
        if abs(duration - expected) > 2:
            raise VariantDecodeError('Длительность дополнительного фрагмента отличается от ожидаемой; полнота не подтверждена.',
                                     source_seconds=source_duration, expected_seconds=expected,
                                     decoded_seconds=duration, delta_seconds=duration - expected,
                                     start_seconds=start)
        # fpcalc's ordinary limit is 120 seconds. For the 180-second prefix
        # use explicit -length 180; do not silently fingerprint the same 120s.
        if length > 120:
            run = subprocess.run([executable, '-json', '-length', str(length), str(wav)],
                                 capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
            if run.returncode:
                raise RuntimeError(run.stderr.strip() or 'Ошибка дополнительного fpcalc.')
            import json
            raw = json.loads(run.stdout)
            fingerprint = raw.get('fingerprint')
            measured = float(raw.get('duration') or 0)
            if not fingerprint or not math.isfinite(measured) or abs(measured - duration) > 2:
                raise RuntimeError('Некорректный дополнительный отпечаток.')
        else:
            raw = _run_fpcalc(executable, wav)
            if abs(float(raw['duration']) - duration) > 2:
                raise RuntimeError('fpcalc не подтвердил длительность дополнительного фрагмента.')
            fingerprint = raw['fingerprint']
    output = {'status': 'FOUND', 'profile': name, 'start_seconds': start,
              'sample_seconds': round(duration, 3), 'duration': source_duration,
              'fingerprint': fingerprint, 'cache_hit': False}
    try:
        atomic_write_json(path, output)
    except OSError:
        pass
    return output


def additional_lookup(file_path, data, primary_fingerprint, candidates, *, variant_generator=None, lookup=None):
    variant_generator = variant_generator or generate_variant
    lookup = lookup or lookup_fingerprint
    ranked = rank_candidates(data, candidates)
    primary_safe = bool(ranked and action_safety(ranked[0]) == 'SAFE')
    if not Path(file_path).exists():
        return candidates, {'status': 'SKIPPED', 'reason': 'Исходное аудио недоступно.', 'attempts': []}
    try:
        duration = float((data.get('audio_timing') or {}).get('decoded_seconds')
                         or (primary_fingerprint or {}).get('reference_duration')
                         or (primary_fingerprint or {}).get('duration') or data.get('duration') or 0)
        if not math.isfinite(duration) or duration <= 0:
            from core.acrcloud_client import _audio_duration
            duration = _audio_duration(Path(file_path))
    except Exception as error:
        return candidates, {'status': 'ERROR', 'attempts': [], 'errors': [str(error)]}
    # A richer metadata cache may make the primary match SAFE on recheck.
    # Do not discard previously generated audio passes (and their conflicts)
    # merely because that early-exit condition now succeeds.
    if primary_safe:
        try:
            had_previous_passes = any(_variant_cache_path(file_path, duration, p).exists() for p in PROFILES)
        except OSError as error:
            return candidates, {'status': 'ERROR', 'attempts': [], 'errors': [str(error)]}
        if not had_previous_passes:
            return candidates, {'status': 'SKIPPED', 'reason': 'Первичный отпечаток дал однозначное SAFE-сопоставление.', 'attempts': []}
    attempts, errors = [], []
    merged = {}
    for c in candidates:
        key = (c.get('recording_id'), c.get('title'), tuple(c.get('artists') or []))
        merged[key] = {**c, 'primary_audio_score': c.get('score'), 'audio_passes': ['primary']}
    seen = {str((primary_fingerprint or {}).get('fingerprint') or '')}
    for profile in PROFILES:
        entry = {'profile': profile[0]}
        attempts.append(entry)
        try:
            variant = variant_generator(file_path, duration, profile)
            entry.update({key: variant.get(key) for key in ('status', 'start_seconds', 'sample_seconds', 'cache_hit', 'reason')})
            if variant.get('status') == 'SKIPPED':
                continue
            fingerprint = variant['fingerprint']
            if fingerprint in seen:
                entry.update(status='SKIPPED', reason='Отпечаток совпадает с уже проверенным; повтор не считается подтверждением.')
                continue
            seen.add(fingerprint)
            # The API needs the WHOLE source duration, never the WAV fragment
            # duration. The fragment is only another query of the same audio.
            found = extract_candidates(lookup(duration, fingerprint))
            entry.update(status='FOUND' if found else 'NOT_FOUND', candidates=found)
            for c in found:
                key = (c.get('recording_id'), c.get('title'), tuple(c.get('artists') or []))
                prior = merged.get(key)
                if prior:
                    prior['score'] = max(prior['score'], c['score'])
                    prior['audio_passes'].append(profile[0])
                else:
                    merged[key] = {**c, 'primary_audio_score': None, 'audio_passes': [profile[0]]}
        except Exception as error:
            entry.update(status='ERROR', error=str(error))
            if isinstance(error, VariantDecodeError):
                entry.update(error.details)
            errors.append(str(error))
    for c in merged.values():
        primary_score = float(c.get('primary_audio_score') or 0)
        c['supplemental_audio_only'] = (c.get('primary_audio_score') is None
                                       or primary_score < .90 and float(c.get('score') or 0) > primary_score)
    output = list(merged.values())
    reranked = rank_candidates(data, output)
    if reranked:
        best = reranked[0]['candidate']
        contrary = [c for c in output if float(c.get('score') or 0) >= .90
                    and not _same_candidate_identity(best, c)]
        if contrary:
            for c in output:
                c['audio_pass_conflict'] = True
                # Snapshots, not references back into merged candidates:
                # otherwise the contrary candidates refer to one another and
                # JSON stage/plan export fails with a circular reference.
                others = contrary if c is best else [best, *contrary]
                c['audio_pass_alternatives'] = [
                    {key: value for key, value in other.items() if key != 'audio_pass_alternatives'}
                    for other in others if not _same_candidate_identity(c, other)]
    return output, {'status': 'FOUND' if output else 'ERROR' if errors else 'NOT_FOUND',
                    'attempts': attempts, 'errors': errors,
                    'verification_incomplete': bool(errors),
                    'primary_fingerprint_available': bool(primary_fingerprint),
                    'independent_provider': False}
