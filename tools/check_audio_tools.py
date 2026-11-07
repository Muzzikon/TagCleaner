"""Read-only diagnosis of public executables/dependencies, without credentials."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    print('Python:', sys.version.split()[0])
    print('Исполняемый Python:', sys.executable)
    missing = []
    for name in ('fpcalc', 'ffmpeg', 'ffprobe'):
        bundled = ROOT / 'tools' / (name + '.exe')
        path = str(bundled) if name == 'fpcalc' and os.name == 'nt' and bundled.is_file() else shutil.which(name)
        if not path and name == 'ffprobe':
            ffmpeg = shutil.which('ffmpeg')
            if ffmpeg:
                sibling = Path(ffmpeg).with_name('ffprobe.exe' if Path(ffmpeg).suffix == '.exe' else 'ffprobe')
                path = str(sibling) if sibling.is_file() else None
        if not path:
            print(name + ': НЕ НАЙДЕН')
            missing.append(name)
            continue
        try:
            result = subprocess.run([path, '-version'], capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=15)
            first = (result.stdout or result.stderr).splitlines()
            print(f'{name}: {path}; {first[0] if first else "нет ответа"}')
            if result.returncode:
                missing.append(name)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f'{name}: не запускается ({type(error).__name__})')
            missing.append(name)
    from core.shazam_client import shazam_capability
    print('ShazamIO:', shazam_capability())
    if os.name == 'nt' and sys.version_info[:2] >= (3, 13):
        print('Для готового Windows-пакета ShazamIO: install_free_audio.cmd создаёт отдельное окружение Python 3.12.')
    if shazam_capability() not in {'disabled', 'not-installed'}:
        try:
            from shazamio import Shazam
            print('ShazamIO/native: импорт успешен')
        except (ImportError, OSError) as error:
            print('ShazamIO/native: не загружается (' + type(error).__name__ + ')')
    print('FFmpeg / ffprobe: https://ffmpeg.org/download.html')
    print('Chromaprint / fpcalc: https://acoustid.org/chromaprint')
    print('Дополнительный источник: https://github.com/shazamio/ShazamIO')
    return 1 if missing else 0


if __name__ == '__main__':
    raise SystemExit(main())
