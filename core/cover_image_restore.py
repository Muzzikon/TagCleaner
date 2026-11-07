"""Restore one evicted choice using the installed audio Python environment."""
import json
import hashlib
import sys
from pathlib import Path


def main():
    try:
        from core.covers import load_candidate_image, queued_image
        candidate=json.loads(sys.stdin.read(65536))
        if not isinstance(candidate,dict):raise ValueError('Некорректный вариант обложки.')
        root = Path(__file__).resolve().parents[1]
        queued_path = candidate.get('queued_path')
        blob = queued_image(root, queued_path) if queued_path else None
        if blob is None or hashlib.sha256(blob).hexdigest() != candidate.get('id'):
            blob = load_candidate_image(root, candidate)
        sys.stdout.buffer.write(blob)
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__=='__main__':sys.exit(main())
