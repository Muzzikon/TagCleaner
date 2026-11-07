"""Explain duration discrepancies only against the selected track identity."""
import math

from core.identity_fusion import identities_match
from core.duration_policy import AUTOMATIC_DURATION_TOLERANCE

SEARCH_STAGES = (
    'recognition', 'source_url_identity', 'catalog_identity', 'acrcloud_identity',
    'acrcloud_musicbrainz', 'audd_musicbrainz', 'secondary_recognition',
    'shazam_identity', 'acoustid_multipass',
)


def incomplete_search_stages(data):
    from core.run_state import _stage_result_reusable
    return [key for key in SEARCH_STAGES if not _stage_result_reusable(data.get(key))]


def _seconds(value, scale=1.0):
    try:
        value = float(value) / scale
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def _identity(candidate):
    artists = candidate.get('artists') or []
    return {'artist': candidate.get('artist') or (' & '.join(artists) if isinstance(artists, list) else artists),
            'title': candidate.get('title'), 'recording_id': candidate.get('recording_id') or candidate.get('id')}


def _same_reference(chosen, reference):
    if not chosen.get('title') or not reference.get('title'):
        return False
    from core.versioning import compare_versions
    if compare_versions(chosen['title'], reference['title'])[1]:
        return False
    if chosen.get('recording_id') and chosen['recording_id'] == reference.get('recording_id'):
        return True
    return identities_match(chosen, reference)


def add_track_diagnostics(data):
    duration = _seconds(data.get('duration'))
    diagnostics = []
    from core.identity_review import filename_identity_conflict
    local_conflict = filename_identity_conflict(data)
    if local_conflict:
        diagnostics.append({'code': 'LOCAL_IDENTITY_CONFLICT', 'severity': 'WARNING',
                            'source': 'filename', **local_conflict,
                            'message': 'Имя файла и предложенные исполнитель/название/версия расходятся. Это не доказывает правильность имени файла; автоматическая замена требует ручного подтверждения песни целиком.'})
    from core.naming import extract_feature_entities_from_title
    from core.identity_review import proposed_identity
    ambiguous_credit = next((hint for title in (data.get('title'), proposed_identity(data).get('title'))
                             if (hint := extract_feature_entities_from_title(title)).get('ambiguous')), None)
    if ambiguous_credit:
        diagnostics.append({'code': 'FEATURE_CREDIT_AMBIGUOUS', 'severity': 'WARNING', 'source': 'caption',
                            'raw_credit': ambiguous_credit.get('raw_credit'),
                            'message': 'После feat. обнаружен неоднозначный заголовок загрузчика с разделителями. Жанр/подпись/ремиксер не добавлены как гости; нужен проверенный текст тегов.'})
    incomplete = incomplete_search_stages(data)
    if incomplete or data.get('_checkpoint_retry_required'):
        diagnostics.append({'code': 'SEARCH_VERIFICATION_INCOMPLETE', 'severity': 'WARNING',
                            'source': 'search', 'stages': incomplete,
                            'message': 'Не все запущенные проверки завершены. Кандидат сохранён; содержательные изменения тегов требуют повторной или ручной проверки.'})
        data['_checkpoint_retry_required'] = True
    if data.get('fingerprint_decode'):
        partial = bool(data['fingerprint_decode'].get('decode_partial'))
        unverified = bool(data['fingerprint_decode'].get('decode_verification_incomplete'))
        diagnostics.append({'code': ('AUDIO_DECODE_PARTIAL' if partial else 'AUDIO_DECODE_UNVERIFIED' if unverified else 'AUDIO_DECODE_RECOVERED'),
                            'severity': 'WARNING' if partial or unverified else 'INFO', 'source': 'ffmpeg_pcm',
                            'decoded_seconds': data['fingerprint_decode'].get('decoded_duration'),
                            'reference_seconds': data['fingerprint_decode'].get('reference_duration'),
                            'decode_warnings': data['fingerprint_decode'].get('decode_warnings'),
                            'initial_error': data['fingerprint_decode'].get('initial_decoder_error'),
                            'message': ('Повреждённые аудиокадры пропущены при восстановлении отпечатка; найденный кандидат и целостность файла требуют проверки.'
                                        if partial else 'Полноту резервного декодирования нельзя сверить с исходной длительностью; найденный кандидат требует проверки.'
                                        if unverified else 'Отпечаток построен после полного резервного декодирования файла в PCM; исходное аудио не изменено.')})
    current = (data.get('best_match') or {}).get('candidate') or {}
    ranked = data.get('ranked_candidates') or []
    original = (ranked[0] if ranked else data.get('best_match') or {})
    fusion = data.get('fallback_fusion') or {}
    chosen = (_identity(fusion) if fusion.get('status') in {'SAFE', 'REVIEW'}
              and fusion.get('title') else _identity(current))
    # Fused best_match is synthetic and has no duration. Recover a compatible
    # original audio candidate, never an unrelated earlier/rejected recording.
    audio = next((item.get('candidate') or {} for item in ranked
                  if _same_reference(chosen, _identity(item.get('candidate') or {}))), None)
    audio = audio or (current if _same_reference(chosen, _identity(current)) else {})
    mb = data.get('musicbrainz') or {}
    credit = mb.get('artist-credit') or []
    mb_artists = [item.get('name') or (item.get('artist') or {}).get('name') or ''
                  for item in credit if isinstance(item, dict)]
    mb_identity = {'artist': ' & '.join(mb_artists), 'title': mb.get('title'), 'recording_id': mb.get('id')}
    catalog_result = data.get('catalog_identity') or {}
    catalog = catalog_result.get('best') or {}
    acr_result = data.get('acrcloud_identity') or {}
    acr = acr_result.get('best') or {}
    shazam_result = data.get('shazam_identity') or {}
    shazam = shazam_result.get('best') or {}
    if duration is None:
        for source, value in [('fpcalc', data.get('fingerprint_duration')),
                              ('audio_sample', (acr.get('sample') or {}).get('source_duration')),
                              ('shazam_sample', (shazam.get('sample') or {}).get('source_duration'))]:
            duration = _seconds(value)
            if duration is not None:
                diagnostics.append({'code': 'LOCAL_DURATION_RECOVERED', 'severity': 'INFO', 'source': source,
                                    'local_seconds': duration,
                                    'message': 'Длительность из тегов/заголовка недоступна; для диагностики использована длительность анализа этого аудиофайла.'})
                break
    references = []
    if audio:
        references.append(('AcoustID', _seconds(audio.get('duration'))))
    if _same_reference(chosen, mb_identity):
        references.append(('MusicBrainz', _seconds(mb.get('length'), 1000)))
    # CONFLICT may have been caused solely by duration. Keep that warning
    # when artist/title/version identify this same track; exclude other songs.
    if catalog_result.get('status') in {'STRONG', 'PARTIAL', 'CONFLICT'} and _same_reference(chosen, _identity(catalog)):
        references.append(('catalog', _seconds(catalog.get('duration'))))
    if acr_result.get('status') in {'STRONG', 'PARTIAL', 'CONFLICT'} and _same_reference(chosen, _identity(acr)):
        references.append(('ACRCloud', _seconds(acr.get('duration_ms'), 1000)))
    from core.audio_timing import ensure_audio_timing
    ensure_audio_timing(data.get('path'), data,
                        [data.get('fingerprint_duration'), *(value for _, value in references)])
    timing = data.get('audio_timing') or {}
    if timing.get('status') == 'VERIFIED':
        duration = _seconds(timing.get('decoded_seconds'))
        diagnostics.append({'code': 'AUDIO_TIMING_VERIFIED', 'severity': 'INFO', 'source': 'ffmpeg_pcm',
                            **{key: timing.get(key) for key in ('decoded_seconds', 'leading_quiet_seconds',
                                                               'trailing_quiet_seconds', 'non_edge_seconds',
                                                               'all_quiet', 'quiet_threshold_dbfs', 'cache_hit')},
                            'message': 'Измерена полная декодированная длительность; края ниже −60 dBFS отмечены отдельно, исходный файл не обрезан.'})
        for source, value in [('Mutagen', data.get('duration_header')), ('fpcalc', data.get('fingerprint_duration'))]:
            header = _seconds(value)
            if header is not None and abs(header - duration) > 1:
                diagnostics.append({'code': 'LOCAL_DURATION_HEADER_MISMATCH', 'severity': 'INFO', 'source': source,
                                    'local_seconds': duration, 'reference_seconds': header,
                                    'delta_seconds': round(duration - header, 2),
                                    'message': 'Длительность заголовка/декодера отличается от измеренного PCM. Это не длина другой песни; для сравнения используется PCM.'})
        if timing.get('all_quiet'):
            diagnostics.append({'code': 'AUDIO_ALL_QUIET', 'severity': 'WARNING', 'source': 'ffmpeg_pcm',
                                'message': 'Всё декодированное аудио ниже −60 dBFS; достоверность распознавания требует проверки.'})
    else:
        references.insert(0, ('fpcalc', _seconds(data.get('fingerprint_duration'))))
        if timing.get('status') in {'ERROR', 'UNAVAILABLE'}:
            diagnostics.append({'code': 'AUDIO_TIMING_UNVERIFIED', 'severity': 'WARNING', 'source': 'ffmpeg_pcm',
                                'message': 'Спорную длительность не удалось проверить: ' + str(timing.get('error') or 'неизвестная ошибка')})
            data['_checkpoint_retry_required'] = True
            reason = 'Не завершена проверка фактической длительности аудио.'
            data['_checkpoint_retry_reasons'] = list(dict.fromkeys([*(data.get('_checkpoint_retry_reasons') or []), reason]))
    for source, remote in references:
        if remote is None or duration is None:
            continue
        delta = duration - remote
        if abs(delta) <= 1:
            continue
        small = abs(delta) <= 5
        message = ('Небольшое расхождение: возможны округление, тишина или другая граница записи.'
                   if small else 'Проверьте обрезку, extended/radio edit или ошибку каталога; причина по длительности не доказана.')
        quiet_explains = (timing.get('status') == 'VERIFIED' and delta > 5
                         and abs((timing.get('non_edge_seconds') or 0) - remote) <= 2
                         and (timing.get('leading_quiet_seconds') or 0) + (timing.get('trailing_quiet_seconds') or 0) >= 1)
        if quiet_explains:
            message = 'Измеренные тихие края объясняют разницу длительности; это не доказывает правильность песни или версии. Автоматическая запись остаётся запрещена.'
        diagnostics.append({'code': 'DURATION_TOLERANCE' if small else 'DURATION_MISMATCH',
                            'severity': 'INFO' if small else 'WARNING', 'source': source,
                            'local_seconds': duration, 'reference_seconds': remote,
                            'delta_seconds': delta, 'quiet_edges_explain_difference': quiet_explains, 'message': message})
    if original.get('identity_ambiguous'):
        resolution = fusion.get('ambiguity_resolution') or {}
        # A resolved ranking tie and contradictory fingerprint passes are
        # separate objections. Do not claim the identity is settled while
        # pass conflicts still prevent writing.
        resolved = bool(resolution.get('resolved')) and not original.get('audio_pass_conflict')
        diagnostics.append({'code': 'IDENTITY_AMBIGUITY_RESOLVED' if resolved else 'IDENTITY_AMBIGUOUS',
                            'severity': 'INFO' if resolved else 'WARNING', 'source': 'AcoustID',
                            'resolution_source': resolution.get('source') if resolved else None,
                            'alternatives': original.get('identity_alternatives') or [],
                            'message': ('Неоднозначность аудио-кандидатов разрешена независимым аудио-подтверждением.'
                                        if resolved else 'Несколько близких аудио-кандидатов разных треков/версий; необходима проверка выбора.')})
    if acr_result.get('identity_ambiguous'):
        diagnostics.append({'code': 'ACR_FRAGMENT_CONFLICT', 'severity': 'WARNING', 'source': 'ACRCloud',
                            'alternatives': acr_result.get('identity_alternatives') or [],
                            'message': 'Аудио-ответы ACRCloud содержат разные треки/версии; все варианты сохранены для проверки.'})
    if acr_result.get('verification_incomplete'):
        diagnostics.append({'code': 'ACR_VERIFICATION_INCOMPLETE', 'severity': 'WARNING', 'source': 'ACRCloud',
                            'message': 'Проверка части фрагментов не завершена из-за ошибки; результат требует повторной проверки.'})
    multipass = data.get('acoustid_multipass') or {}
    if multipass.get('attempts'):
        diagnostics.append({'code': 'ACOUSTID_MULTIPASS', 'severity': 'INFO', 'source': 'AcoustID',
                            'attempts': [{'profile': a.get('profile'), 'status': a.get('status'),
                                          'candidates': len(a.get('candidates') or [])} for a in multipass['attempts']],
                            'message': 'Проверены дополнительные отпечатки; ответы одной базы не считаются независимыми подтверждениями.'})
    if multipass.get('verification_incomplete') or multipass.get('errors'):
        diagnostics.append({'code': 'ACOUSTID_VERIFICATION_INCOMPLETE', 'severity': 'WARNING', 'source': 'AcoustID',
                            'message': 'Часть дополнительных отпечатков не удалось проверить; найденные кандидаты сохранены, проверку нужно повторить.'})
    for attempt in multipass.get('attempts') or []:
        if attempt.get('error_kind') == 'local_audio_decode':
            diagnostics.append({'code': 'AUDIO_FRAGMENT_DECODE_INCOMPLETE', 'severity': 'WARNING',
                                'source': 'ffmpeg_pcm',
                                **{key: attempt.get(key) for key in ('profile', 'source_seconds', 'expected_seconds',
                                                                    'decoded_seconds', 'delta_seconds', 'start_seconds', 'returncode')
                                   if attempt.get(key) is not None},
                                'message': 'Не подтверждена полнота дополнительного аудиофрагмента. Это локальная проверка декодирования, а не ответ сети; причина по одной длине не доказана.'})
    if multipass.get('primary_fingerprint_available') is False and ranked:
        diagnostics.append({'code': 'AUDIO_FINGERPRINT_FRAGMENT_ONLY', 'severity': 'WARNING', 'source': 'AcoustID',
                            'message': 'Основной отпечаток недоступен; кандидат найден по читаемым фрагментам. Целостность файла требует проверки.'})
    if original.get('audio_pass_conflict'):
        diagnostics.append({'code': 'ACOUSTID_PASS_CONFLICT', 'severity': 'WARNING', 'source': 'AcoustID',
                            'alternatives': original.get('identity_alternatives') or [],
                            'message': 'Разные отпечатки AcoustID дали разные треки/версии; все ответы сохранены для проверки.'})
    if shazam_result.get('identity_ambiguous'):
        diagnostics.append({'code': 'SHAZAM_FRAGMENT_CONFLICT', 'severity': 'WARNING', 'source': 'Shazam',
                            'alternatives': shazam_result.get('identity_alternatives') or [],
                            'message': 'Фрагменты Shazam дали разные треки/версии; все варианты требуют проверки.'})
    if shazam_result.get('verification_incomplete'):
        diagnostics.append({'code': 'SHAZAM_VERIFICATION_INCOMPLETE', 'severity': 'WARNING', 'source': 'Shazam',
                            'message': 'Проверка фрагментов Shazam не завершена; найденные кандидаты сохранены для повторной проверки.'})
    for message in fusion.get('review_reasons') or []:
        if (acr_result.get('identity_ambiguous') and message.startswith('ACRCloud распознал разные')) or (
                acr_result.get('verification_incomplete') and message.startswith('Проверка остальных фрагментов ACRCloud')):
            continue
        diagnostics.append({'code': 'IDENTITY_EVIDENCE_CONFLICT', 'severity': 'WARNING',
                            'source': 'consensus', 'message': message})
    data['track_diagnostics'] = diagnostics
    # Repeated finalization must not duplicate warnings or retain a resolved one.
    codes = {'DURATION_MISMATCH', 'IDENTITY_AMBIGUOUS', 'IDENTITY_EVIDENCE_CONFLICT',
             'ACR_FRAGMENT_CONFLICT', 'ACR_VERIFICATION_INCOMPLETE', 'AUDIO_DECODE_PARTIAL', 'AUDIO_DECODE_UNVERIFIED',
             'ACOUSTID_PASS_CONFLICT', 'ACOUSTID_VERIFICATION_INCOMPLETE', 'AUDIO_FINGERPRINT_FRAGMENT_ONLY',
             'SHAZAM_FRAGMENT_CONFLICT', 'SHAZAM_VERIFICATION_INCOMPLETE', 'AUDIO_TIMING_UNVERIFIED', 'AUDIO_ALL_QUIET',
             'SEARCH_VERIFICATION_INCOMPLETE', 'AUDIO_FRAGMENT_DECODE_INCOMPLETE', 'LOCAL_IDENTITY_CONFLICT',
             'FEATURE_CREDIT_AMBIGUOUS'}
    issues = data.setdefault('issues', [])
    issues[:] = [item for item in issues if item.get('code') not in codes]
    for item in diagnostics:
        if item['severity'] == 'WARNING':
            issues.append({'code': item['code'], 'severity': 'WARNING',
                           'message': (f"{item['source']}: Δ {item['delta_seconds']:+.2f} с. "
                                       if 'delta_seconds' in item else f"{item['source']}: ") + item['message']})
    return diagnostics


def apply_identity_write_guard(data):
    """Apply final audio objections to every identity/release field together.

    Called after diagnostics and all recognition layers, before checkpoints,
    export, previews, or writer collection. Proposals are never deleted.
    """
    plan = data.get('change_plan') or {}
    if plan.get('identity') not in {'SAFE', 'REVIEW'}:
        return []
    reasons = []
    for item in data.get('track_diagnostics') or []:
        if item.get('code') == 'DURATION_MISMATCH' and abs(item.get('delta_seconds') or 0) > AUTOMATIC_DURATION_TOLERANCE:
            reasons.append(f"{item['source']}: длительность отличается на {item['delta_seconds']:+.2f} с; версия или границы аудио требуют проверки.")
        elif item.get('code') in {'ACR_FRAGMENT_CONFLICT', 'ACR_VERIFICATION_INCOMPLETE', 'AUDIO_DECODE_PARTIAL', 'AUDIO_DECODE_UNVERIFIED',
                                  'ACOUSTID_PASS_CONFLICT', 'ACOUSTID_VERIFICATION_INCOMPLETE', 'AUDIO_FINGERPRINT_FRAGMENT_ONLY',
                                  'SHAZAM_FRAGMENT_CONFLICT', 'SHAZAM_VERIFICATION_INCOMPLETE', 'IDENTITY_EVIDENCE_CONFLICT',
                                  'AUDIO_TIMING_UNVERIFIED', 'AUDIO_ALL_QUIET', 'SEARCH_VERIFICATION_INCOMPLETE',
                                  'AUDIO_FRAGMENT_DECODE_INCOMPLETE', 'LOCAL_IDENTITY_CONFLICT', 'IDENTITY_AMBIGUOUS',
                                  'FEATURE_CREDIT_AMBIGUOUS'}:
            reasons.append(item['message'])
    reasons = list(dict.fromkeys(reasons))
    if not reasons:
        return []
    plan['identity'] = 'REVIEW'
    data['action_safety'] = 'REVIEW'
    data['automatic_write_review_reasons'] = reasons
    plan['review_reasons'] = list(dict.fromkeys([*(plan.get('review_reasons') or []), *reasons]))
    message = ' '.join(reasons)
    for name in ('artist', 'title', 'album', 'albumartist', 'date', 'tracknumber', 'discnumber'):
        field = plan.get(name) or {}
        if field.get('status') == 'SAFE':
            # Formatting that depends solely on the current value remains
            # possible even without recognition, just as in REJECT plans.
            from core.local_cleanup import analyze_local_cleanup
            cleanup = analyze_local_cleanup(field.get('current'))
            if cleanup['changed'] and cleanup['value'] == field.get('proposed'):
                continue
            field['status'] = 'REVIEW'
            field['reason'] = message
    fusion = data.get('fallback_fusion') or {}
    if fusion.get('status') == 'SAFE':
        fusion['status'] = 'REVIEW'
        fusion['review_reasons'] = list(dict.fromkeys([*(fusion.get('review_reasons') or []), *reasons]))
        fusion['reason'] = ' '.join(fusion['review_reasons'])
    return reasons
