"""Measure suspect files without trimming them or granting identity confidence."""
from array import array
import math
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from core.cache_utils import atomic_write_json, file_signature, read_json_if_fresh, stable_digest
from core.duration_policy import duration_difference, AUTOMATIC_DURATION_TOLERANCE

CACHE_DIR = Path(__file__).resolve().parent.parent / 'cache' / 'audio_timing'
MEASUREMENT_VERSION = 'strict-pcm-edges-v1'
# -60 dBFS is a description of quiet boundaries, never proof of song identity.
EDGE_THRESHOLD = 32


def _edge_frames(audio, *, reverse=False):
    channels, total = audio.getnchannels(), audio.getnframes()
    position = total if reverse else 0
    count = 0
    while (position > 0 if reverse else position < total):
        start = max(0, position - 4096) if reverse else position
        length = position - start if reverse else min(4096, total - start)
        audio.setpos(start)
        samples = array('h', audio.readframes(length))
        if sys.byteorder != 'little':
            samples.byteswap()
        if len(samples) != length * channels:
            raise RuntimeError('Неполный PCM-блок при проверке длительности.')
        edge = (length - 1) * channels if reverse else 0
        if any(abs(samples[edge + c]) > EDGE_THRESHOLD for c in range(channels)):
            return count
        # Check every sample, but skip the Python frame/channel loop for a
        # wholly quiet block. Preserve the exact inclusive threshold and all
        # channels; the boundary block still gets the precise original scan.
        if samples and min(samples) >= -EDGE_THRESHOLD and max(samples) <= EDGE_THRESHOLD:
            count += length
            position = start if reverse else start + length
            continue
        indexes = range(length - 1, -1, -1) if reverse else range(length)
        for frame in indexes:
            if any(abs(samples[frame * channels + c]) > EDGE_THRESHOLD for c in range(channels)):
                return count
            count += 1
        position = start if reverse else start + length
    return count


def _measure_pcm(path):
    with wave.open(str(path), 'rb') as audio:
        frames, rate = audio.getnframes(), audio.getframerate()
        if audio.getsampwidth() != 2 or rate <= 0 or frames <= 0:
            raise RuntimeError('Декодирование не дало корректного PCM-аудио.')
        leading = _edge_frames(audio)
        trailing = 0 if leading == frames else _edge_frames(audio, reverse=True)
        return {'decoded_seconds': frames / rate,
                'leading_quiet_seconds': leading / rate,
                'trailing_quiet_seconds': trailing / rate,
                'non_edge_seconds': max(0, frames - leading - trailing) / rate,
                'all_quiet': leading == frames, 'quiet_threshold_dbfs': -60}


def measure_audio_timing(file_path):
    file_path = Path(file_path)
    before = file_signature(file_path)
    cache_path = CACHE_DIR / (stable_digest({'file': before, 'version': MEASUREMENT_VERSION}) + '.json')
    try:
        cached = read_json_if_fresh(cache_path, ttl_days=None)
    except OSError:
        cached = None
    if isinstance(cached, dict) and cached.get('status') == 'VERIFIED' and cached.get('version') == MEASUREMENT_VERSION:
        return {**cached, 'cache_hit': True}
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return {'status': 'UNAVAILABLE', 'error': 'ffmpeg не найден; фактическая длительность не проверена.'}
    try:
        with tempfile.TemporaryDirectory(prefix='tagcleaner_timing_') as folder:
            pcm = Path(folder) / 'full.wav'
            # All channels are retained: an antiphase stereo track must not
            # become silence through mono downmixing. Decode to EOF, no seeking.
            result = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-xerror',
                                     '-i', str(file_path), '-map', '0:a:0', '-vn',
                                     '-ar', '16000', '-c:a', 'pcm_s16le', str(pcm)],
                                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
            if result.returncode or result.stderr.strip():
                raise RuntimeError(result.stderr.strip()[:2000] or 'Строгое декодирование завершилось с ошибкой.')
            output = _measure_pcm(pcm)
        if file_signature(file_path) != before:
            raise RuntimeError('Исходный файл изменился во время проверки длительности.')
        output.update(status='VERIFIED', version=MEASUREMENT_VERSION, cache_hit=False)
        try:
            atomic_write_json(cache_path, output)
        except OSError:
            pass
        return output
    except Exception as error:
        return {'status': 'ERROR', 'error': str(error)[:2000]}


def ensure_audio_timing(file_path, data, references=()):
    """One targeted decode per run; failed attempts are retried on the next run."""
    if data.get('audio_timing') is not None:
        return data['audio_timing']
    local = data.get('duration')
    differences = [duration_difference(local, ref) for ref in references]
    needs_check = any(delta is not None and delta > AUTOMATIC_DURATION_TOLERANCE for delta in differences)
    if not needs_check:
        return None
    if not file_path or not Path(file_path).is_file():
        return None
    try:
        output = measure_audio_timing(file_path)
    except Exception as error:
        output = {'status': 'ERROR', 'error': str(error)[:2000]}
    data['audio_timing'] = output
    if output.get('status') == 'VERIFIED':
        decoded = output.get('decoded_seconds')
        if isinstance(decoded, (int, float)) and math.isfinite(decoded) and decoded > 0:
            data['duration_header'] = local
            data['duration'] = decoded
            data['duration_source'] = 'decoded_pcm'
    return output
