"""停机维护期间的完整数据快照；恢复只写新目录，绝不覆盖运行目录。"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def reject_linked_path(path: Path) -> None:
    absolute = path.absolute()
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise ValueError('数据路径及其父目录不能是符号链接，请使用实际目录路径')


def files_under(root: Path) -> list[Path]:
    if root.is_symlink():
        raise ValueError('拒绝符号链接目录')
    found = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('快照仅支持普通文件和目录')
        if path.is_file():
            found.append(path)
    return found


def check_database(path: Path) -> None:
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('数据库完整性检查失败')


def backup(source: Path, target: Path, *, quiesced: bool) -> None:
    if not quiesced:
        raise ValueError('请先停止 API 和 Worker，再传入 --quiesced')
    reject_linked_path(source)
    reject_linked_path(target)
    if source.is_symlink():
        raise ValueError('拒绝符号链接数据目录')
    source = source.resolve()
    target = target.absolute()
    if target.resolve().is_relative_to(source):
        raise ValueError('备份目录不能位于数据目录中')
    if not (source / 'platform.db').is_file():
        raise ValueError('数据目录缺少 platform.db')
    # 在写入前验证目录，拒绝链接到数据目录之外的文件。
    selected = [source / 'platform.db']
    for name in ('uploads', 'artifacts', '.secret_key', '.env.runtime'):
        path = source / name
        if path.is_symlink():
            raise ValueError('拒绝符号链接')
        if path.is_dir():
            selected.extend(files_under(path))
        elif path.is_file():
            selected.append(path)
    if (source / 'platform.db').is_symlink():
        raise ValueError('拒绝数据库符号链接')
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    with closing(sqlite3.connect((source / 'platform.db').as_uri() + '?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(target / 'platform.db')) as snapshot:
            original.backup(snapshot)
    for path in selected[1:]:
        destination = target / path.relative_to(source)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        destination.chmod(0o600)
    (target / 'platform.db').chmod(0o600)
    for name in ('uploads', 'artifacts'):
        (target / name).mkdir(mode=0o700, exist_ok=True)
    check_database(target / 'platform.db')
    manifest = {
        'version': 1,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source_data_dir': str(source),
        'files': {str(path.relative_to(target)): digest(path) for path in files_under(target)},
    }
    # 最后生成清单；中途失败的目录没有清单，不能作为有效快照恢复。
    (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (target / 'manifest.json').chmod(0o600)


def verify(source: Path) -> dict:
    reject_linked_path(source)
    files = files_under(source)
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('不支持的快照清单')
    actual = {str(path.relative_to(source)): path for path in files if path.name != 'manifest.json' or path.parent != source}
    if set(actual) != set(manifest['files']) or 'platform.db' not in actual:
        raise ValueError('快照文件缺失或含额外文件')
    for name, path in actual.items():
        if digest(path) != manifest['files'][name]:
            raise ValueError('快照文件校验失败')
    check_database(source / 'platform.db')
    return manifest


def restore(source: Path, target: Path) -> None:
    reject_linked_path(target)
    if target.resolve().is_relative_to(source.resolve()):
        raise ValueError('恢复目录不能位于快照内')
    verify(source)
    # 包括空目录在内，已有目标一律拒绝；保留原始运行数据以便回滚。
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    for path in files_under(source):
        if path == source / 'manifest.json':
            continue
        destination = target / path.relative_to(source)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        destination.chmod(0o600)
    for name in ('uploads', 'artifacts'):
        (target / name).mkdir(mode=0o700, exist_ok=True)
    check_database(target / 'platform.db')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('backup', 'verify', 'restore'))
    parser.add_argument('source', type=Path)
    parser.add_argument('target', type=Path, nargs='?')
    parser.add_argument('--quiesced', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'verify':
            verify(args.source)
        elif args.target is None:
            parser.error('必须指定新目标目录')
        elif args.action == 'backup':
            backup(args.source, args.target, quiesced=args.quiesced)
        else:
            restore(args.source, args.target)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'快照操作失败：{exc}\n')
    print(args.target if args.target else '快照校验通过')


if __name__ == '__main__':
    main()
