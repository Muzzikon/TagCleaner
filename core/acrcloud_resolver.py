from __future__ import annotations

from core.acrcloud_client import recognize_with_acrcloud
from core.acrcloud_matcher import evaluate_acrcloud_match


DEFAULT_SAMPLE_POSITIONS = (0.20, 0.50, 0.75)


def _audio_score(attempt):
    try:
        return float((attempt.get("recognition") or {}).get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _same_audio_identity(left, right):
    from core.identity_fusion import identities_match
    # ISRC alone cannot erase an explicit difference between versions.
    return identities_match(left.get("recognition") or {}, right.get("recognition") or {})


def _found_result(attempts, selected, status, errors):
    hypotheses = [item for item in attempts if item.get("match")]
    # A provider can return several competing recordings for one fragment.
    # They are evidence too; taking only candidates[0] hides a tied version.
    for attempt in attempts:
        for candidate in (attempt.get("recognition") or {}).get("candidates") or []:
            if candidate.get("artist") and candidate.get("title"):
                hypotheses.append({"recognition": candidate})
    high = [item for item in hypotheses if _audio_score(item) >= 90.0]
    ambiguous = any(not _same_audio_identity(left, right)
                    for index, left in enumerate(high) for right in high[index + 1:])
    # A lower-score result that matches the filename cannot dismiss a strong
    # contrary audio match from an earlier/later part of the same file.
    ambiguous = ambiguous or any(not _same_audio_identity(selected, item) for item in high)
    reason = (selected.get("match") or {}).get("reason")
    if ambiguous:
        status = "CONFLICT"
        reason = "Аудио-ответы ACRCloud содержат разные треки или версии; требуется ручная проверка."
    alternatives = []
    seen = set()
    for item in hypotheses:
        if _same_audio_identity(selected, item):
            continue
        candidate = item['recognition']
        key = (candidate.get('artist'), candidate.get('title'), candidate.get('isrc'), candidate.get('score'))
        if key in seen:
            continue
        seen.add(key)
        # Raw payloads and complete per-fragment lists stay in attempts.
        # The summary must not duplicate large raw responses in TXT warnings.
        alternatives.append({name: candidate.get(name) for name in (
            'artist', 'title', 'score', 'isrc', 'acrid', 'duration_ms', 'album', 'sample')})
    return {
        "status": status,
        "best": selected.get("recognition"),
        "match": selected.get("match"),
        "attempts": attempts,
        "identity_ambiguous": ambiguous,
        "identity_alternatives": alternatives,
        "verification_incomplete": bool(errors),
        "reason": reason,
        "errors": errors,
    }


def resolve_acrcloud_identity(
    file_path,
    data,
    *,
    recognizer=None,
    sample_positions=DEFAULT_SAMPLE_POSITIONS,
):
    """Check every requested audio position before trusting the identity."""

    recognizer = recognizer or recognize_with_acrcloud
    attempts = []
    strong = []
    partials = []
    conflicts = []
    errors = []

    for position in sample_positions:
        try:
            result = recognizer(file_path, sample_position=position)
        except Exception as error:
            # Preserve earlier positive results if another fragment fails.
            result = {"status": "ERROR", "error": str(error)}
        status = result.get("status") or "ERROR"

        attempt = {
            "position": position,
            "recognition": result,
            "match": None,
        }

        if status == "FOUND":
            match = evaluate_acrcloud_match(file_path, data, result)
            attempt["match"] = match
            attempts.append(attempt)

            match_status = match.get("status") or "CONFLICT"

            if match_status == "STRONG":
                strong.append(attempt)
            elif match_status == "PARTIAL":
                partials.append(attempt)
            else:
                conflicts.append(attempt)

            # An apparently correct first hit may recognize a shared intro,
            # sample, original, or one portion of a mix. Check the rest too.
            continue

        attempts.append(attempt)

        if status == "ERROR":
            errors.append(result.get("error") or "ACRCloud error")

    if strong:
        selected = max(strong, key=lambda item: (_audio_score(item),
                       float((item.get("match") or {}).get("overall_score") or 0.0)))
        return _found_result(attempts, selected, "STRONG", errors)

    if partials:
        best_attempt = max(
            partials,
            key=lambda item: float((item.get("match") or {}).get("overall_score") or 0.0),
        )
        return _found_result(attempts, best_attempt, "PARTIAL", errors)

    if conflicts:
        best_attempt = max(
            conflicts,
            key=lambda item: (_audio_score(item), float((item.get("match") or {}).get("overall_score") or 0.0)),
        )
        return _found_result(attempts, best_attempt, "CONFLICT", errors)

    if errors and len(errors) == len(attempts):
        return {
            "status": "ERROR",
            "best": None,
            "match": None,
            "attempts": attempts,
            "reason": errors[0],
            "errors": errors,
            "verification_incomplete": True,
        }

    return {
        "status": "NOT_FOUND",
        "best": None,
        "match": None,
        "attempts": attempts,
        "reason": ("ACRCloud не нашёл запись в успешно проверенных частях; остальные проверки завершились ошибкой."
                   if errors else "ACRCloud не нашёл запись ни в одной из проверенных частей трека."),
        "errors": errors,
        "verification_incomplete": bool(errors),
    }
