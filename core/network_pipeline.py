from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor


def _env_workers(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def pipeline_worker_count() -> int:
    # Coordinator threads mostly wait for network. They do not decode audio.
    return _env_workers("TAGCLEANER_PIPELINE_WORKERS", 12, 2, 32)


def source_worker_count() -> int:
    return _env_workers("TAGCLEANER_SOURCE_WORKERS", 6, 1, 12)


def catalog_worker_count() -> int:
    return _env_workers("TAGCLEANER_CATALOG_WORKERS", 4, 1, 8)


def acrcloud_worker_count() -> int:
    # ACRCloud also extracts short ffmpeg samples, so keep this intentionally low.
    return _env_workers("TAGCLEANER_ACRCLOUD_WORKERS", 2, 1, 4)


def musicbrainz_prefetch_worker_count() -> int:
    # MusicBrainz HTTP itself is serialized by musicbrainz_client's rate limiter.
    # A few workers are still useful because cache hits do not need the network.
    return _env_workers("TAGCLEANER_MB_PREFETCH_WORKERS", 3, 1, 6)


class NetworkPipeline:
    """Provider-aware executors used only to overlap independent work.

    This class deliberately does not change recognition decisions. It only
    schedules the same callables earlier/concurrently. Provider-specific rate
    limits stay inside the provider clients.
    """

    def __init__(self):
        self.coordinator = ThreadPoolExecutor(
            max_workers=pipeline_worker_count(),
            thread_name_prefix="tagcleaner-pipe",
        )
        self.source = ThreadPoolExecutor(
            max_workers=source_worker_count(),
            thread_name_prefix="tagcleaner-source",
        )
        self.catalog = ThreadPoolExecutor(
            max_workers=catalog_worker_count(),
            thread_name_prefix="tagcleaner-catalog",
        )
        self.acrcloud = ThreadPoolExecutor(
            max_workers=acrcloud_worker_count(),
            thread_name_prefix="tagcleaner-acr",
        )
        self.shazam = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tagcleaner-shazam')
        self.musicbrainz = ThreadPoolExecutor(
            max_workers=musicbrainz_prefetch_worker_count(),
            thread_name_prefix="tagcleaner-mb",
        )

        self._singleflight_lock = threading.RLock()
        self._mb_recording_futures = {}
        self._mb_release_futures = {}

    def submit_file(self, fn, *args, **kwargs):
        return self.coordinator.submit(fn, *args, **kwargs)

    def submit_source(self, fn, *args, **kwargs):
        return self.source.submit(fn, *args, **kwargs)

    def submit_catalog(self, fn, *args, **kwargs):
        return self.catalog.submit(fn, *args, **kwargs)

    def submit_acrcloud(self, fn, *args, **kwargs):
        return self.acrcloud.submit(fn, *args, **kwargs)

    def submit_shazam(self, fn, *args, **kwargs):
        return self.shazam.submit(fn, *args, **kwargs)

    def _singleflight(self, bucket, key, fn, *args, **kwargs):
        if not key:
            return None
        text_key = str(key)
        with self._singleflight_lock:
            future = bucket.get(text_key)
            if future is not None and not future.cancelled():
                return future
            future = self.musicbrainz.submit(fn, *args, **kwargs)
            bucket[text_key] = future

            # Do not pin a transiently failed MusicBrainz Future for the whole
            # run. The current callers still receive the same exception, but a
            # later file with the same MBID may retry after the provider cooldown
            # and succeed without requiring a full application restart.
            def _drop_failed(done_future):
                try:
                    failed = done_future.cancelled() or done_future.exception() is not None
                except Exception:
                    failed = True
                if not failed:
                    return
                with self._singleflight_lock:
                    if bucket.get(text_key) is done_future:
                        bucket.pop(text_key, None)

            future.add_done_callback(_drop_failed)
            return future

    def submit_mb_recording(self, recording_id, fn):
        return self._singleflight(
            self._mb_recording_futures,
            recording_id,
            fn,
            recording_id,
        )

    def submit_mb_release_bundle(self, recording_id, fn):
        return self._singleflight(
            self._mb_release_futures,
            recording_id,
            fn,
            recording_id,
        )

    def shutdown(self, *, cancel_pending=False, wait=True):
        # On interruption cancel queued work before joining. In-flight HTTP
        # calls still obey their normal timeouts; Python threads cannot be killed.
        for executor in (self.coordinator, self.source, self.catalog, self.acrcloud, self.shazam, self.musicbrainz):
            executor.shutdown(wait=wait, cancel_futures=cancel_pending)

    def settings_text(self) -> str:
        return (
            f"pipeline={pipeline_worker_count()}, "
            f"source={source_worker_count()}, "
            f"catalog={catalog_worker_count()}, "
            f"acr={acrcloud_worker_count()}, "
            f"shazam=1, "
            f"mb-prefetch={musicbrainz_prefetch_worker_count()}"
        )
