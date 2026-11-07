"""Streaming UTF-8 report and compact stage journal, independent of Rich's RAM buffer."""
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class RunJournal:
    def __init__(self, directory, terminal, sync_interval=2.0):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        self.report_path = directory / f"TagCleaner_report_{stamp}.txt"
        self.events_path = directory / f"TagCleaner_stages_{stamp}.jsonl"
        self.terminal = terminal
        self.encoding = getattr(terminal, "encoding", "utf-8") or "utf-8"
        self._report = self.report_path.open("w", encoding="utf-8", newline="\n")
        self._events = self.events_path.open("w", encoding="utf-8", newline="\n")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._closed = False
        self._dirty = False
        self._thread = threading.Thread(target=self._sync_loop, args=(sync_interval,), daemon=True)
        self._thread.start()
        self.stage(None, "run_started")
        self.sync()

    def isatty(self):
        return bool(getattr(self.terminal, "isatty", lambda: False)())

    def write(self, value):
        with self._lock:
            if not self._closed:
                self._report.write(_ANSI.sub("", str(value)).replace("\r", ""))
                self._report.flush()
                self._dirty = True
            self.terminal.write(value)
        return len(value)

    def flush(self):
        with self._lock:
            if not self._closed:
                self._report.flush()
            self.terminal.flush()

    def stage(self, file, stage, **details):
        event = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "path": str(file) if file is not None else None, "stage": stage, **details}
        with self._lock:
            if self._closed:
                return
            self._events.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            self._events.flush()
            label = Path(str(file)).name if file is not None else "RUN"
            summary = json.dumps(details, ensure_ascii=False, default=str) if details else ""
            self._report.write(f"\n[{event['time']}] {stage}: {label} {summary}\n")
            self._report.flush()
            self._dirty = True

    def sync(self):
        with self._lock:
            if not self._closed and self._dirty:
                for handle in (self._report, self._events):
                    handle.flush()
                    os.fsync(handle.fileno())
                self._dirty = False

    def _sync_loop(self, interval):
        while not self._stop.wait(interval):
            try:
                self.sync()
            except OSError as error:
                # Make a disk-full/report error visible without killing audio
                # workers. The next sync and final close still try again.
                self.terminal.write(f"\nОшибка сохранения отчёта: {error}\n")
                self.terminal.flush()

    def close(self, status):
        self.stage(None, status)
        self._stop.set()
        self._thread.join(timeout=3.0)
        with self._lock:
            self.sync()
            self._report.close()
            self._events.close()
            self._closed = True
