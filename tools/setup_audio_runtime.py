"""Fetch and verify the Windows audio tools on first launch."""

import hashlib
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
AUDIO_TOOLS = (
    (
        ('ffmpeg.exe', 'ffprobe.exe'),
        (
            ('https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.7z',
             '4705843ccaaf54257c16ad90f3e952ece33c17df964ecf7bfdbb0f49c7171077'),
            ('https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip',
             '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'),
        ),
    ),
    (
        ('fpcalc.exe',),
        (('https://github.com/acoustid/chromaprint/releases/download/v1.6.2/chromaprint-fpcalc-1.6.2-windows-x86_64.zip',
          'b74369726676670af3ab0eadff45f960cf969fdf4a87a971ba37eaf8d530e874'),),
    ),
)


def find_member(members, filename):
    matches = [member for member in members if PurePosixPath(member).name == filename]
    if len(matches) != 1 or matches[0].startswith('-'):
        raise ValueError(f'Expected one {filename} in the verified archive')
    return matches[0]


def install_tools(target, filenames, url, digest):
    print(f'Downloading {url.rsplit("/", 1)[-1]}...', flush=True)
    with tempfile.TemporaryDirectory(dir=target) as temporary:
        work = Path(temporary)
        archive_path = work / PurePosixPath(url).name
        downloaded = 0
        next_report = 8 * 1024 * 1024
        with urlopen(url, timeout=35) as response, archive_path.open('wb') as output:
            while chunk := response.read(64 * 1024):
                output.write(chunk)
                downloaded += len(chunk)
                if downloaded >= next_report:
                    print(f'  {downloaded // (1024 * 1024)} MiB', flush=True)
                    next_report += 8 * 1024 * 1024
        with archive_path.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != digest:
                raise ValueError(f'Download checksum mismatch: {url}')
        if archive_path.suffix == '.7z':
            members = subprocess.check_output(['tar.exe', '-tf', str(archive_path)], text=True, timeout=60).splitlines()
            for name in filenames:
                member = find_member(members, name)
                with (work / name).open('wb') as output:
                    subprocess.run(['tar.exe', '-xOf', str(archive_path), '--', member],
                                   stdout=output, check=True, timeout=120)
        else:
            with zipfile.ZipFile(archive_path) as archive:
                for name in filenames:
                    member = find_member(archive.namelist(), name)
                    with archive.open(member) as source, (work / name).open('wb') as output:
                        shutil.copyfileobj(source, output)
        for name in filenames:
            (work / name).replace(target / name)


def main():
    target = ROOT / 'runtime' / 'audio'
    target.mkdir(parents=True, exist_ok=True)
    failures = []
    for filenames, sources in AUDIO_TOOLS:
        if all((target / name).is_file() for name in filenames):
            continue
        for url, digest in sources:
            try:
                install_tools(target, filenames, url, digest)
            except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
                print(f'Could not install from {url}: {error}', flush=True)
            else:
                break
        else:
            failures.extend(filenames)
    if failures:
        print('Audio tools unavailable: ' + ', '.join(failures), flush=True)
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
