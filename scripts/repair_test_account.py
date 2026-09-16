"""仅修复匹配早期已知测试凭据的 admin；默认只读，--apply 才写入。"""
import argparse
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from app.services.auth_service import _verify_password, _hash_password
from app.db import utc_now


def repair(database: Path, *, apply: bool = False) -> dict:
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as source:
        row = source.execute("SELECT id,password_hash FROM users WHERE username='admin'").fetchone()
        matched = bool(row and _verify_password('secret', row[1]))
        if not matched or not apply:
            return {'matched_test_credential':matched, 'changed':False}
        backup = database.with_name(database.name+'.before-account-repair-'+uuid.uuid4().hex+'.db')
        descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
    password = secrets.token_urlsafe(32)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        changed = connection.execute('UPDATE users SET password_hash=?,updated_at=? WHERE id=? AND password_hash=?', (_hash_password(password),utc_now(),row[0],row[1]))
        if changed.rowcount != 1:
            raise RuntimeError('账号已发生变化，未重置密码')
        connection.execute('DELETE FROM user_sessions WHERE user_id=?', (row[0],))
    return {'changed':True, 'backup':str(backup), 'username':'admin', 'password':password}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    print(json.dumps(repair(args.database,apply=args.apply),ensure_ascii=False))
