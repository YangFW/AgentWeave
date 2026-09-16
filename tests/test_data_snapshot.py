import sqlite3
import json
import tempfile
import unittest
from pathlib import Path

from scripts.data_snapshot import backup, restore, verify


class DataSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        connection = sqlite3.connect(self.data / 'platform.db')
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE example (value TEXT)')
        connection.execute("INSERT INTO example VALUES ('committed in WAL')")
        connection.commit()
        for name in ('uploads', 'artifacts'):
            (self.data / name).mkdir()
            (self.data / name / 'test.txt').write_text(name, encoding='utf-8')
        (self.data / '.secret_key').write_bytes(b'test-only-key')
        (self.data / '.env.runtime').write_text('APP_PPTX_GENERATOR=python\n')
        self.snapshot = self.root / 'snapshot'

    def test_wal_database_files_and_key_roundtrip(self):
        backup(self.data, self.snapshot, quiesced=True)
        verify(self.snapshot)
        destination = self.root / 'restored'
        restore(self.snapshot, destination)
        with sqlite3.connect(destination / 'platform.db') as connection:
            self.assertEqual(connection.execute('SELECT value FROM example').fetchone()[0], 'committed in WAL')
        for name in ('uploads/test.txt', 'artifacts/test.txt', '.secret_key', '.env.runtime'):
            self.assertEqual((destination / name).read_bytes(), (self.data / name).read_bytes())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)

    def test_refuses_existing_target_without_changing_it(self):
        backup(self.data, self.snapshot, quiesced=True)
        with self.assertRaises(FileExistsError):
            restore(self.snapshot, self.data)
        self.assertEqual((self.data / 'uploads/test.txt').read_text(), 'uploads')

    def test_corrupt_or_missing_file_prevents_restore(self):
        backup(self.data, self.snapshot, quiesced=True)
        (self.snapshot / 'artifacts/test.txt').write_text('corrupt')
        with self.assertRaises(ValueError):
            restore(self.snapshot, self.root / 'restored')
        self.assertFalse((self.root / 'restored').exists())
        (self.snapshot / 'artifacts/test.txt').unlink()
        with self.assertRaises(ValueError):
            verify(self.snapshot)

    def test_rejects_symlinks_and_unconfirmed_snapshot(self):
        with self.assertRaises(ValueError):
            backup(self.data, self.snapshot, quiesced=False)
        (self.data / 'uploads/link').symlink_to(self.data / '.secret_key')
        with self.assertRaises(ValueError):
            backup(self.data, self.snapshot, quiesced=True)
        self.assertFalse(self.snapshot.exists())

    def test_restore_rejects_linked_parent_before_writing(self):
        backup(self.data, self.snapshot, quiesced=True)
        outside = self.root/'outside'
        outside.mkdir()
        linked = self.root/'linked'
        linked.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):
            restore(self.snapshot,linked/'restored')
        self.assertFalse((outside/'restored').exists())

    def test_invalid_manifest_and_nested_destination_fail_closed(self):
        backup(self.data, self.snapshot, quiesced=True)
        with self.assertRaises(ValueError):
            restore(self.snapshot,self.snapshot/'nested')
        self.assertFalse((self.snapshot/'nested').exists())
        (self.snapshot/'manifest.json').write_text(json.dumps([]))
        with self.assertRaises(ValueError):
            restore(self.snapshot,self.root/'restored')
        self.assertFalse((self.root/'restored').exists())
