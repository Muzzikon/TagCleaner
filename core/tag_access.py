"""The same field mapping for reading, writing and verifying supported containers."""
from mutagen.id3 import ID3, Frames
from core.extra_tags import read_remixer, write_remixer

ID3_FIELDS = {'artist':'TPE1', 'title':'TIT2', 'album':'TALB',
              'albumartist':'TPE2', 'date':'TDRC', 'tracknumber':'TRCK',
              'discnumber':'TPOS', 'genre':'TCON', 'remixer':'TPE4'}
SUPPORTED_EXTENSIONS = {'.mp3','.flac','.ogg','.opus','.m4a','.mp4','.wav','.aiff','.aif'}


def read_values(audio, field):
    if audio is None or audio.tags is None:
        return []
    if isinstance(audio.tags, ID3):
        frame = audio.tags.get(ID3_FIELDS.get(field, field))
        return [str(v) for v in getattr(frame, 'text', [])]
    values = audio.tags.get(field, []) or []
    return [str(v) for v in ([values] if isinstance(values, str) else values)]


def read_value(audio, field):
    if field == 'remixer':
        return read_remixer(audio)
    values = read_values(audio, field)
    return values[0] if values else ''


def write_value(audio, field, value):
    if field == 'remixer':
        write_remixer(audio, value)
    elif isinstance(audio.tags, ID3):
        frame_id = ID3_FIELDS[field]
        audio.tags.delall(frame_id)
        if value:
            audio.tags.add(Frames[frame_id](encoding=3, text=[value]))
    elif value:
        audio[field] = [value]
    elif field in audio:
        del audio[field]
