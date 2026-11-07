import json
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

    with LAST_SELECTION_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )


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
    ):
        return [], []

    existing_files = []
    missing_files = []

    for value in data.get(
        "files",
        [],
    ):
        path = Path(value)

        if path.is_file():
            existing_files.append(path)
        else:
            missing_files.append(path)

    return (
        existing_files,
        missing_files,
    )