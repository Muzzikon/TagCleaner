import json
from core.cache_utils import atomic_write_json
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent

CONFIG_DIR = ROOT_DIR / "config"

LAST_SELECTION_FILE = (
    CONFIG_DIR
    / "last_selection.json"
)


def save_last_selection(files):
    CONFIG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    data = {
        "files": [
            str(
                Path(file).resolve()
            )
            for file in files
        ]
    }

    atomic_write_json(LAST_SELECTION_FILE, data)


def load_last_selection():
    if not LAST_SELECTION_FILE.exists():
        return [], []

    try:
        with LAST_SELECTION_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

    except (
        json.JSONDecodeError,
        OSError,
        UnicodeError,
    ):
        return [], []

    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return [], []

    existing_files = []
    missing_files = []

    for value in data.get(
        "files",
        [],
    ):
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value)
        try:
            exists = path.is_file()
        except (OSError, ValueError):
            exists = False

        if exists:
            existing_files.append(path)
        else:
            missing_files.append(path)

    return (
        existing_files,
        missing_files,
    )