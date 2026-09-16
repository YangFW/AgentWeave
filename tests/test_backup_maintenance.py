import os
import shutil
try:
    import fcntl
except ImportError:
    fcntl = None
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(fcntl is not None and shutil.which('flock'), '维护备份需要 POSIX/flock')
class BackupMaintenanceTests(unittest.TestCase):
    def test_backup_failure_still_restarts_only_previously_running_containers(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = directory / 'bin'
            binary.mkdir()
            log = directory / 'docker.log'
            docker = binary / 'docker'
            docker.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_TEST_LOG"
case "$*" in *"ps -q"*) printf 'aaaa\\nbbbb\\n' ;; esac
exit 0
''')
            docker.chmod(0o755)
            lock_path = directory / 'maintenance.lock'
            environment = {**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH'],'DOCKER_TEST_LOG':str(log),'AGENTNEXUS_DATA_DIR':str(directory/'missing'),'AGENTNEXUS_BACKUP_DIR':str(directory/'backups'),'AGENTNEXUS_MAINTENANCE_LOCK':str(lock_path)}
            args = ['sh',str(root/'scripts/backup-maintenance.sh')]
            denied = subprocess.run(args,env=environment,capture_output=True,text=True)
            self.assertEqual(denied.returncode,2)
            self.assertFalse(log.exists())
            with lock_path.open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
                overlap = subprocess.run([*args,'--maintenance-window-approved'],env=environment,capture_output=True,text=True)
                self.assertEqual(overlap.returncode,1)
                self.assertIn('已有维护备份',overlap.stderr)
                self.assertFalse(log.exists())
            failed = subprocess.run([*args,'--maintenance-window-approved'],env=environment,capture_output=True,text=True)
            self.assertNotEqual(failed.returncode,0)
            commands = log.read_text()
            self.assertIn('stop --time 30 aaaa',commands)
            self.assertIn('stop --time 30 bbbb',commands)
            self.assertIn('start aaaa',commands)
            self.assertIn('start bbbb',commands)
            data = directory/'missing'
            data.mkdir()
            with sqlite3.connect(data/'platform.db') as connection:
                connection.execute('CREATE TABLE test(value TEXT)')
            success = subprocess.run([*args,'--maintenance-window-approved'],env=environment,capture_output=True,text=True)
            self.assertEqual(success.returncode,0,success.stderr)
            self.assertEqual(len(list((directory/'backups').glob('*/manifest.json'))),1)
