"""Restore one evicted choice using the installed audio Python environment."""
import json
import hashlib
import sys
from pathlib import Path


def main():
    try:
        from core.covers import load_candidate_image, queued_image, image_info
        candidate=json.loads(sys.stdin.read(65536))
        if not isinstance(candidate,dict):raise ValueError('Некорректный вариант обложки.')
        root = Path(__file__).resolve().parents[1]
        queued_path = candidate.get('queued_path')
        blob = queued_image(root, queued_path) if queued_path else None
        if blob is None or hashlib.sha256(blob).hexdigest() != candidate.get('id'):
            blob = load_candidate_image(root, candidate, allow_changed=candidate.get('allow_changed') is True)
        if candidate.get('metadata') is True:
            info = image_info(blob)
            print(json.dumps({k:info[k] for k in ('width','height')}), file=sys.stderr)
        sys.stdout.buffer.write(blob)
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__=='__main__':sys.exit(main())
