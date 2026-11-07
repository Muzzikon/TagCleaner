"""Fetch and verify the Windows audio tools on first launch."""

import hashlib
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
AUDIO_TOOLS = (
    (
        'https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip',
        '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba',
        ('ffmpeg.exe', 'ffprobe.exe'),
    ),
    (
        'https://github.com/acoustid/chromaprint/releases/download/v1.6.2/chromaprint-fpcalc-1.6.2-windows-x86_64.zip',
        'b74369726676670af3ab0eadff45f960cf969fdf4a87a971ba37eaf8d530e874',
        ('fpcalc.exe',),
    ),
)


def main():
    target = ROOT / 'runtime' / 'audio'
    target.mkdir(parents=True, exist_ok=True)
    for url, digest, filenames in AUDIO_TOOLS:
        if all((target / name).is_file() for name in filenames):
            continue
        print(f'Downloading {url.rsplit("/", 1)[-1]}...', flush=True)
        with tempfile.TemporaryDirectory(dir=target) as temporary:
            work = Path(temporary)
            archive_path = work / 'download.zip'
            with urlopen(url, timeout=60) as response, archive_path.open('wb') as output:
                shutil.copyfileobj(response, output)
            with archive_path.open('rb') as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() != digest:
                    raise ValueError(f'Download checksum mismatch: {url}')
            with zipfile.ZipFile(archive_path) as archive:
                for name in filenames:
                    member, = (item for item in archive.namelist() if PurePosixPath(item).name == name)
                    with archive.open(member) as source, (work / name).open('wb') as output:
                        shutil.copyfileobj(source, output)
            for name in filenames:
                (work / name).replace(target / name)


if __name__ == '__main__':
    main()
