import os
import shutil
import subprocess
import tempfile

from pathlib import Path

import requests
from core.dns_recovery import DNSRecovery, is_dns_error

from dotenv import load_dotenv
from mutagen import File


AUDD_API_URL = "https://api.audd.io/"
_dns_recovery = DNSRecovery('api.audd.io')


def audd_dns_stats():
    return _dns_recovery.stats()

# AudD сам рекомендует присылать
# не больше примерно 12 секунд.
#
# Берём 11 секунд с небольшим запасом.
SAMPLE_DURATION = 11.0


def _audio_duration(
    file_path,
):
    """
    Получает длительность файла
    без декодирования всего аудио.
    """

    try:
        audio = File(
            file_path
        )

        if (
            audio is None
            or audio.info is None
        ):
            return None

        duration = float(
            audio.info.length
        )

        if duration <= 0:
            return None

        return duration

    except Exception:
        return None


def _choose_sample_start(
    duration,
):
    """
    Выбираем не самое начало трека.

    Для электронной музыки интро часто
    плохо подходит для распознавания.

    Поэтому примерно 30% трека —
    достаточно разумная первая точка.
    """

    if (
        duration is None
        or duration
        <= SAMPLE_DURATION
    ):
        return 0.0

    start = (
        duration * 0.30
    )

    max_start = max(
        0.0,
        duration
        - SAMPLE_DURATION
        - 0.25,
    )

    return min(
        start,
        max_start,
    )


def _find_ffmpeg():
    """
    Ищет ffmpeg в PATH.
    """

    return shutil.which(
        "ffmpeg"
    )


def _create_audd_sample(
    file_path,
    output_path,
):
    """
    Создаёт короткий MP3-фрагмент
    для AudD.

    Возвращает информацию о том,
    откуда был взят фрагмент.
    """

    ffmpeg = (
        _find_ffmpeg()
    )

    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg не найден в PATH."
        )

    duration = (
        _audio_duration(
            file_path
        )
    )

    start = (
        _choose_sample_start(
            duration
        )
    )

    command = [
        ffmpeg,

        "-hide_banner",
        "-loglevel",
        "error",

        "-y",

        # Быстрый seek.
        "-ss",
        str(start),

        "-i",
        str(file_path),

        "-t",
        str(
            SAMPLE_DURATION
        ),

        # Видео/обложка нам не нужны.
        "-vn",

        # Для fingerprint recognition
        # стерео и высокий bitrate
        # совершенно не обязательны.
        "-ac",
        "1",

        "-ar",
        "44100",

        "-b:a",
        "96k",

        str(output_path),
    ]

    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    if process.returncode != 0:
        error = (
            process.stderr.strip()
            or (
                "Неизвестная ошибка "
                "ffmpeg."
            )
        )

        raise RuntimeError(
            error
        )

    output_path = Path(
        output_path
    )

    if (
        not output_path.exists()
        or output_path.stat().st_size
        <= 0
    ):
        raise RuntimeError(
            "ffmpeg не создал "
            "аудиофрагмент."
        )

    return {
        "source_duration": (
            duration
        ),

        "sample_start": (
            start
        ),

        "sample_duration": (
            SAMPLE_DURATION
        ),

        "sample_size": (
            output_path.stat().st_size
        ),
    }


def _extract_isrc(
    result,
):
    spotify = (
        result.get("spotify")
        or {}
    )

    spotify_ids = (
        spotify.get(
            "external_ids"
        )
        or {}
    )

    spotify_isrc = (
        spotify_ids.get(
            "isrc"
        )
    )

    if spotify_isrc:
        return spotify_isrc

    apple_music = (
        result.get(
            "apple_music"
        )
        or {}
    )

    return apple_music.get(
        "isrc"
    )


def _extract_spotify_data(
    result,
):
    spotify = (
        result.get("spotify")
        or {}
    )

    album = (
        spotify.get("album")
        or {}
    )

    artists = []

    for artist in (
        spotify.get(
            "artists"
        )
        or []
    ):
        name = (
            artist.get("name")
        )

        if name:
            artists.append(
                name
            )

    return {
        "track_id": (
            spotify.get("id")
        ),

        "track_url": (
            (
                spotify.get(
                    "external_urls"
                )
                or {}
            ).get(
                "spotify"
            )
        ),

        "artists": artists,

        "album_id": (
            album.get("id")
        ),

        "album": (
            album.get("name")
        ),

        "album_url": (
            (
                album.get(
                    "external_urls"
                )
                or {}
            ).get(
                "spotify"
            )
        ),

        "release_date": (
            album.get(
                "release_date"
            )
        ),

        "track_number": (
            spotify.get(
                "track_number"
            )
        ),

        "disc_number": (
            spotify.get(
                "disc_number"
            )
        ),

        "total_tracks": (
            album.get(
                "total_tracks"
            )
        ),
    }


def _extract_apple_data(
    result,
):
    apple = (
        result.get(
            "apple_music"
        )
        or {}
    )

    return {
        "id": (
            apple.get("id")
        ),

        "url": (
            apple.get("url")
        ),

        "artist": (
            apple.get(
                "artistName"
            )
        ),

        "title": (
            apple.get("name")
        ),

        "album": (
            apple.get(
                "albumName"
            )
        ),

        "release_date": (
            apple.get(
                "releaseDate"
            )
        ),

        "track_number": (
            apple.get(
                "trackNumber"
            )
        ),

        "isrc": (
            apple.get("isrc")
        ),
    }


def recognize_with_audd(
    file_path,
    *,
    api_token=None,
    timeout=45,
):
    """
    Независимое распознавание
    через AudD.

    В AudD отправляется НЕ весь трек,
    а короткий локально созданный
    фрагмент.

    Результат пока не даёт права
    на SAFE-write.
    """

    load_dotenv()

    path = Path(
        file_path
    )

    if not path.exists():
        return {
            "provider": "audd",
            "status": "ERROR",
            "error": (
                "Файл не найден."
            ),
        }

    if not path.is_file():
        return {
            "provider": "audd",
            "status": "ERROR",
            "error": (
                "Путь не является "
                "файлом."
            ),
        }

    token = (
        api_token
        or os.getenv(
            "AUDD_API_TOKEN"
        )
        or "test"
    )

    sample_info = None

    dns = _dns_recovery.deferred()
    if dns:
        return {**dns, 'provider': 'audd'}

    try:
        with tempfile.TemporaryDirectory(
            prefix="tagcleaner_audd_"
        ) as temp_dir:
            sample_path = (
                Path(temp_dir)
                / "sample.mp3"
            )

            sample_info = (
                _create_audd_sample(
                    path,
                    sample_path,
                )
            )

            with sample_path.open(
                "rb"
            ) as audio_file:
                response = (
                    requests.post(
                        AUDD_API_URL,

                        data={
                            "api_token": (
                                token
                            ),

                            "return": (
                                "apple_music,"
                                "spotify"
                            ),
                        },

                        files={
                            "file": (
                                "sample.mp3",
                                audio_file,
                                "audio/mpeg",
                            )
                        },

                        timeout=timeout,
                    )
                )

        response.raise_for_status()
        _dns_recovery.succeeded()

        payload = (
            response.json()
        )

    except requests.RequestException as exc:
        if is_dns_error(exc):
            _dns_recovery.failed()
        return {
            "provider": "audd",
            "status": "ERROR",
            "error": str(exc),
            "sample": sample_info,
        }

    except ValueError as exc:
        return {
            "provider": "audd",
            "status": "ERROR",
            "error": (
                "AudD вернул "
                "некорректный JSON: "
                f"{exc}"
            ),
            "sample": sample_info,
        }

    except Exception as exc:
        return {
            "provider": "audd",
            "status": "ERROR",
            "error": str(exc),
            "sample": sample_info,
        }

    if (
        payload.get("status")
        != "success"
    ):
        return {
            "provider": "audd",
            "status": "ERROR",
            "error": (
                payload.get("error")
                or payload
            ),
            "sample": sample_info,
        }

    result = (
        payload.get("result")
    )

    if not result:
        return {
            "provider": "audd",
            "status": "NOT_FOUND",
            "sample": sample_info,
            "raw": payload,
        }

    spotify = (
        _extract_spotify_data(
            result
        )
    )

    apple_music = (
        _extract_apple_data(
            result
        )
    )

    return {
        "provider": "audd",
        "status": "FOUND",

        "artist": (
            result.get("artist")
        ),

        "title": (
            result.get("title")
        ),

        "album": (
            result.get("album")
        ),

        "release_date": (
            result.get(
                "release_date"
            )
        ),

        "label": (
            result.get("label")
        ),

        "timecode": (
            result.get(
                "timecode"
            )
        ),

        "song_link": (
            result.get(
                "song_link"
            )
        ),

        "isrc": (
            _extract_isrc(
                result
            )
        ),

        "spotify": spotify,

        "apple_music": (
            apple_music
        ),

        # Полезно потом для отладки:
        # какой кусок реально ушёл
        # на распознавание.
        "sample": sample_info,

        "raw": result,
    }
