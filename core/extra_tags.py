"""Portable remixer tag access without changing artist or title credits."""
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4Tags
from mutagen.id3 import ID3, TPE4


def configure_extra_tags():
    # Mutagen calls TPE4 'arranger' by default; Picard's remixer mapping
    # uses the same standard ID3 Interpreter/Remixer/Modifier frame.
    if 'remixer' not in EasyID3.valid_keys:
        EasyID3.RegisterTextKey('remixer', 'TPE4')
    if 'remixer' not in EasyMP4Tags.Get:
        EasyMP4Tags.RegisterFreeformKey('remixer', 'REMIXER')


configure_extra_tags()


def read_remixer(audio):
    if audio is None or audio.tags is None:
        return ''
    if isinstance(audio.tags, ID3):  # WAVE/AIFF do not expose EasyID3.
        frame = audio.tags.get('TPE4')
        values = frame.text if frame is not None else []
    else:
        values = audio.get('remixer', []) or []
    if isinstance(values, str):
        values = [values]
    return '; '.join(str(value) for value in values if str(value))


def write_remixer(audio, value):
    if isinstance(audio.tags, ID3):
        audio.tags.delall('TPE4')
        if value:
            audio.tags.add(TPE4(encoding=3, text=[value]))
    elif value:
        audio['remixer'] = [value]
    elif 'remixer' in audio:
        del audio['remixer']


def hydrate_remixer(data):
    """Older compatible checkpoints did not include this field; read it locally."""
    if 'remixer' in data:
        return
    from mutagen import File
    from core.cache_utils import file_signature
    before = file_signature(data['path'])
    if not data.get('analysis_file_signature') or before != data['analysis_file_signature']:
        raise ValueError('Файл изменился. Перепроверьте трек перед редактированием.')
    value = read_remixer(File(data['path'], easy=True))
    if file_signature(data['path']) != before:
        raise ValueError('Файл изменился во время чтения ремиксера.')
    data['remixer'] = value
