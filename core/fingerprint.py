import json
import os
import subprocess
import shutil
import math
import tempfile
import wave
from pathlib import Path

from core.cache_utils import atomic_write_json, file_signature, read_json_if_fresh, stable_digest


ROOT_DIR = Path(__file__).resolve().parent.parent
FPCALC_PATH = ROOT_DIR / "tools" / "fpcalc.exe"
CACHE_DIR = ROOT_DIR / "cache" / "fingerprints"
DECODE_VALIDATION_VERSION = 'source-duration-v13.9'
DECODE_FIELDS = ("decode_fallback", "decode_partial", "decode_verification_incomplete", "decode_validation", "decoder", "decoded_duration",
                 "reference_duration", "initial_decoder_error", "decode_warnings")


def _cache_path(file_path: Path) -> Path:
    signature = file_signature(file_path)
    return CACHE_DIR / f"{stable_digest(signature)}.json"


def _load_cached_fingerprint(file_path: Path):
    try:
        payload = read_json_if_fresh(_cache_path(file_path), ttl_days=None)
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    # Earlier fallback caches checked only WAV -> fpcalc duration, not whether
    # ffmpeg decoded the complete source. Revalidate just those conversions.
    if payload.get('decode_fallback') and payload.get('decode_validation') != DECODE_VALIDATION_VERSION:
        return None

    duration = payload.get("duration")
    fingerprint = payload.get("fingerprint")
    if duration is None or not fingerprint:
        return None

    result = {
        "duration": duration,
        "fingerprint": fingerprint,
        "cache_hit": True,
    }
    for name in DECODE_FIELDS:
        if name in payload:
            result[name] = payload[name]
    return result


def _save_cached_fingerprint(file_path: Path, data: dict) -> None:
    try:
        atomic_write_json(
            _cache_path(file_path),
            {
                "file": file_signature(file_path),
                "duration": data["duration"],
                "fingerprint": data["fingerprint"],
                **{name: data[name] for name in DECODE_FIELDS
                   if name in data},
            },
        )
    except Exception:
        # Cache failure must never break recognition.
        pass


def _run_fpcalc(executable, file_path):
    result = subprocess.run([executable, "-json", str(file_path)], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=300)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Не удалось построить аудиоотпечаток.")
    data = json.loads(result.stdout)
    duration = float(data.get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0 or not data.get("fingerprint"):
        raise RuntimeError("fpcalc вернул пустой/некорректный отпечаток или длительность.")
    return {"duration": data["duration"], "fingerprint": data["fingerprint"], "cache_hit": False}


def _probe_duration(file_path, ffmpeg):
    probe = shutil.which('ffprobe')
    if not probe:
        sibling = Path(ffmpeg).with_name('ffprobe.exe' if Path(ffmpeg).suffix.lower() == '.exe' else 'ffprobe')
        probe = str(sibling) if sibling.is_file() else None
    if not probe:
        raise RuntimeError('ffprobe не найден; полноту восстановления проверить нельзя')
    result = subprocess.run([probe, '-v', 'error', '-show_entries',
                             'format=duration:stream=codec_type,duration', '-of', 'json', str(file_path)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'ffprobe не смог прочитать длительность')
    payload = json.loads(result.stdout)
    audio = [s for s in payload.get('streams') or [] if s.get('codec_type') == 'audio']
    if not audio:
        raise RuntimeError('ffprobe не обнаружил аудиопоток')
    for value in (audio[0].get('duration'), (payload.get('format') or {}).get('duration')):
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    raise RuntimeError('Длительность повреждённого файла неизвестна; полноту восстановления проверить нельзя')


def _decoded_fingerprint(executable, file_path, original_error):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(f"{original_error}; резервный декодер ffmpeg не найден")
    with tempfile.TemporaryDirectory(prefix="tagcleaner_fp_") as directory:
        decoded = Path(directory) / "audio.wav"
        # No trimming, loudness normalization or speed changes. -xerror alone
        # can still return zero after an early stop; verify the source length.
        result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-xerror",
                                 "-i", str(file_path), "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", str(decoded)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        partial = False
        reference_duration = None
        decode_warnings = None
        verification_error = None
        try:
            reference_duration = _probe_duration(file_path, ffmpeg)
        except Exception as error:
            verification_error = str(error)
        strict_error = result.stderr.strip() or ('ffmpeg не смог декодировать аудио' if result.returncode else None)
        duration = None
        if not result.returncode:
            try:
                duration = _pcm_duration(decoded)
            except Exception as error:
                strict_error = str(error)
            if duration is not None and reference_duration is not None and abs(duration - reference_duration) > 5:
                strict_error = (strict_error or '') + ' Декодированная длительность отличается от исходной больше чем на 5 с.'
        if strict_error:
            try:
                if reference_duration is None:
                    raise RuntimeError(verification_error)
                # Attempt full decoding without stopping at the first damaged
                # frame. At most 1% decode failures are accepted. No trimming,
                # ignored errors, speed changes or rewriting of the source.
                result = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                                         '-max_error_rate', '0.01', '-i', str(file_path),
                                         '-map', '0:a:0', '-vn', '-c:a', 'pcm_s16le', str(decoded)],
                                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
                if result.returncode:
                    raise RuntimeError(result.stderr.strip() or 'слишком много повреждённых кадров')
                partial = True
                duration = _pcm_duration(decoded)
                decode_warnings = (strict_error + '\n' + result.stderr.strip())[:2000]
            except Exception as error:
                raise RuntimeError(f'{strict_error}; ограниченное восстановление: {error}') from error
        if partial and abs(duration - reference_duration) > 5:
            raise RuntimeError('Восстановление потеряло больше 5 с аудио; отпечаток не принят.')
        output = _run_fpcalc(executable, decoded)
        if abs(float(output["duration"]) - duration) > 5:
            raise RuntimeError("fpcalc не обработал полную декодированную длительность; отпечаток не принят.")
        output.update(duration=round(duration, 3), decode_fallback=True, decoder="ffmpeg_pcm",
                      decoded_duration=round(duration, 3), initial_decoder_error=str(original_error),
                      decode_validation=DECODE_VALIDATION_VERSION)
        if reference_duration is not None:
            output['reference_duration'] = round(reference_duration, 3)
        elif verification_error:
            output.update(decode_verification_incomplete=True, decode_warnings=verification_error)
        if partial:
            output.update(decode_partial=True, reference_duration=round(reference_duration, 3),
                          decode_warnings=decode_warnings)
        return output


def _pcm_duration(file_path):
    with wave.open(str(file_path), 'rb') as audio:
        duration = audio.getnframes() / audio.getframerate()
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError('Резервное декодирование не дало аудио')
    return duration


def generate_fingerprint(file_path, *, use_cache=True):
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"Аудиофайл не найден: {file_path}"
        )

    if use_cache:
        cached = _load_cached_fingerprint(file_path)
        if cached is not None:
            return cached

    executable = (str(FPCALC_PATH) if os.name == "nt" and FPCALC_PATH.exists()
                  else shutil.which("fpcalc"))
    if not executable:
        raise FileNotFoundError(f"fpcalc не найден: {FPCALC_PATH} или PATH")

    try:
        output = _run_fpcalc(executable, file_path)
    except Exception as error:
        try:
            output = _decoded_fingerprint(executable, file_path, error)
        except Exception as fallback_error:
            raise RuntimeError(f"fpcalc: {error}; резервное декодирование: {fallback_error}") from fallback_error

    if use_cache:
        _save_cached_fingerprint(file_path, output)

    return output


def fingerprint_worker_count() -> int:
    raw = (os.getenv("TAGCLEANER_FINGERPRINT_WORKERS") or "2").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 2
    return max(1, min(4, value))
