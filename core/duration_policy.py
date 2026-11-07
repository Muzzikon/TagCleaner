"""Shared tolerance for automatic identity writes, not a discovery filter."""
import math

# Small offsets can result from leading/trailing silence or rounding. Larger
# offsets are unresolved version/boundary evidence, not proof of a wrong song.
AUTOMATIC_DURATION_TOLERANCE = 5.0


def duration_difference(local, reference):
    try:
        local, reference = float(local), float(reference)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(x) and x > 0 for x in (local, reference)):
        return None
    return abs(local - reference)


def duration_requires_review(local, reference):
    difference = duration_difference(local, reference)
    return difference is not None and difference > AUTOMATIC_DURATION_TOLERANCE
