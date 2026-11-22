# SPDX-License-Identifier: GPL-2.0-or-later
"""Container-specific artwork access shared by the desktop and Android apps."""
import base64

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover


def pictures(audio):
    if audio is None:
        raise ValueError('Не удалось прочитать аудиофайл.')
    if isinstance(audio, FLAC):
        return [p.data for p in sorted(audio.pictures, key=lambda p: p.type != 3) if p.data]
    tags = audio.tags
    if tags is None:
        return []
    if isinstance(tags, ID3):
        return [p.data for p in sorted(tags.getall('APIC'), key=lambda p: p.type != 3) if p.data]
    if isinstance(audio, MP4):
        return [bytes(p) for p in tags.get('covr', []) if p]
    decoded = [Picture(base64.b64decode(p)) for p in tags.get('metadata_block_picture', [])]
    result = [p.data for p in sorted(decoded, key=lambda p: p.type != 3)]
    result.extend(base64.b64decode(p) for p in tags.get('coverart', []))
    return [p for p in result if p]


def set_front_cover(audio, blob, mime, width, height):
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if isinstance(tags, ID3):
        for old in tags.getall('APIC'):
            if old.type in (0, 3):
                del tags[old.HashKey]
        description = 'TagCleaner front cover'
        while 'APIC:' + description in tags:
            description += ' '
        tags.add(APIC(encoding=3, mime=mime, type=3, desc=description, data=blob))
        return
    if isinstance(audio, MP4):
        image_format = MP4Cover.FORMAT_JPEG if mime == 'image/jpeg' else MP4Cover.FORMAT_PNG
        tags['covr'] = [MP4Cover(blob, imageformat=image_format), *tags.get('covr', [])[1:]]
        return
    picture = Picture()
    picture.type = 3
    picture.mime = mime
    picture.width = width
    picture.height = height
    picture.data = blob
    if isinstance(audio, FLAC):
        retained = [old for old in audio.pictures if old.type not in (0, 3)]
        audio.clear_pictures()
        for old in retained:
            audio.add_picture(old)
        audio.add_picture(picture)
        return
    retained = [value for value in tags.get('metadata_block_picture', [])
                if Picture(base64.b64decode(value)).type not in (0, 3)]
    for key in ('coverart', 'coverartmime'):
        if key in tags:
            del tags[key]
    tags['metadata_block_picture'] = [base64.b64encode(picture.write()).decode('ascii'), *retained]
