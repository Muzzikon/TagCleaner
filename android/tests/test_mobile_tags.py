import importlib.util
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from mutagen import File
from mutagen.id3 import APIC, TXXX

from core.cover_tags import pictures

ANDROID = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mobile_tags', ANDROID / 'app/src/main/python/mobile_tags.py')
mobile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mobile)


class MobileTags(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.audio = self.root / 'Исполнитель - Трек.wav'
        self.image = self.root / 'picture.jpg'
        self.image.write_bytes(b'new artwork')  # Bitmap validation happens in Documents before this bridge.
        with wave.open(str(self.audio), 'wb') as track:
            track.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            track.writeframes(b'\x00\x00' * 400)
        tagged = File(self.audio)
        tagged.add_tags()
        tagged.tags.add(TXXX(encoding=3, desc='Custom', text=['should survive']))
        tagged.tags.add(APIC(encoding=3, mime='image/jpeg', type=4, desc='Back', data=b'back cover'))
        tagged.save()
        self.original = self.audio.read_bytes()
        self.modified = 1_698_000_000_123_456_700
        os.utime(self.audio, ns=(self.modified, self.modified))
        self.modified = self.audio.stat().st_mtime_ns

    def edit(self, descriptor, signature, updates, artwork=False, output=-1, allow_new_date=False):
        return mobile.edit(descriptor, self.audio.name, signature, json.dumps(updates),
                           str(self.image) if artwork else '', 'image/jpeg', 64, 64,
                           self.root, self.root, 'test-uri', output, allow_new_date=allow_new_date)

    def test_preserves_custom_id3_and_back_cover(self):
        self.assertTrue(mobile.edit_copy(self.audio, {'artist': 'Исполнитель', 'title': '語'},
                                        self.image, 'image/jpeg', 64, 64))
        result = File(self.audio)
        self.assertEqual(result.tags['TXXX:Custom'].text, ['should survive'])
        self.assertEqual(pictures(result), [self.image.read_bytes(), b'back cover'])
        with self.audio.open('rb') as audio:
            tags = json.loads(mobile.inspect(audio.fileno(), self.audio.name))
        self.assertEqual(tags['tags']['title'], '語')
        self.assertTrue(tags['cover'])

    def test_stale_signature_leaves_original_untouched(self):
        with self.audio.open('r+b') as audio:
            with self.assertRaisesRegex(ValueError, 'Файл изменился'):
                self.edit(audio.fileno(), 'stale', {'title': 'updated'})
        self.assertEqual(self.audio.read_bytes(), self.original)
        self.assertFalse(mobile.backup_path(self.root, 'test-uri').exists())

    def test_no_changes_leaves_no_backup(self):
        with self.audio.open('r+b') as audio:
            descriptor = audio.fileno()
            self.assertEqual(self.edit(descriptor, mobile.describe(descriptor), {'title': ''}), 0)
        self.assertEqual(self.audio.read_bytes(), self.original)
        self.assertFalse(mobile.backup_path(self.root, 'test-uri').exists())

    def test_different_write_descriptor_requires_same_contents(self):
        other = self.root / 'other.wav'
        other.write_bytes(self.original[:-1] + b'\x01')
        with self.audio.open('rb') as reader, other.open('r+b') as writer:
            with self.assertRaisesRegex(ValueError, 'другую версию файла'):
                mobile.edit(reader.fileno(), self.audio.name, mobile.describe(reader.fileno()),
                            '{"title":"changed"}', '', '', 0, 0, self.root, self.root, 'test-uri',
                            write_descriptor=writer.fileno())
        self.assertEqual(self.audio.read_bytes(), self.original)
        self.assertFalse(mobile.backup_path(self.root, 'test-uri').exists())

    def test_refuses_new_date_without_explicit_permission(self):
        with patch.object(mobile, 'can_restore_times', return_value=False), self.audio.open('r+b') as audio:
            descriptor = audio.fileno()
            self.assertEqual(self.edit(descriptor, mobile.describe(descriptor), {'title': 'changed'}), 4)
        self.assertEqual(self.audio.read_bytes(), self.original)
        self.assertEqual(self.audio.stat().st_mtime_ns, self.modified)
        self.assertFalse(mobile.backup_path(self.root, 'test-uri').exists())

    def test_allowed_new_date_keeps_original_available_for_restore(self):
        with patch.object(mobile, 'can_restore_times', return_value=False), self.audio.open('r+b') as audio:
            descriptor = audio.fileno()
            self.assertEqual(self.edit(descriptor, mobile.describe(descriptor), {'title': 'changed'},
                                       allow_new_date=True), 5)
            self.assertNotEqual(self.audio.read_bytes(), self.original)
            self.assertEqual(mobile.backup_path(self.root, 'test-uri').read_bytes(), self.original)
            self.assertFalse(mobile.restore(descriptor, self.audio.name, mobile.describe(descriptor),
                                            self.root, self.root, 'test-uri'))
        self.assertEqual(self.audio.read_bytes(), self.original)

    @unittest.skipUnless(os.utime in os.supports_fd, 'descriptor timestamps require Unix')
    def test_commit_and_restore_preserve_mtime(self):
        with self.audio.open('r+b') as audio:
            descriptor = audio.fileno()
            self.assertEqual(self.edit(descriptor, mobile.describe(descriptor), {'title': 'changed'}, True), 1)
            self.assertEqual(self.audio.stat().st_mtime_ns, self.modified)
            self.assertNotEqual(self.audio.read_bytes(), self.original)
            self.assertEqual(mobile.backup_path(self.root, 'test-uri').read_bytes(), self.original)
            mobile.restore(descriptor, self.audio.name, mobile.describe(descriptor), self.root, self.root, 'test-uri')
        self.assertEqual(self.audio.read_bytes(), self.original)
        self.assertEqual(self.audio.stat().st_mtime_ns, self.modified)

    @unittest.skipUnless(os.utime in os.supports_fd, 'descriptor timestamps require Unix')
    def test_commit_failure_restores_original(self):
        write = mobile.write_contents
        failed = False

        def failing_write(descriptor, staged):
            nonlocal failed
            write(descriptor, staged)
            if not failed:
                failed = True
                raise OSError('simulated failure after writing bytes')

        with patch.object(mobile, 'write_contents', failing_write), self.audio.open('r+b') as audio:
            descriptor = audio.fileno()
            with self.assertRaisesRegex(OSError, 'Исходный файл восстановлен'):
                self.edit(descriptor, mobile.describe(descriptor), {'title': 'changed'})
        self.assertEqual(self.audio.read_bytes(), self.original)
        self.assertEqual(self.audio.stat().st_mtime_ns, self.modified)


if __name__ == '__main__':
    unittest.main()
