"""模型请求预算：上下文定位任务，SQLite 原子累计已尝试的请求。"""
from contextvars import ContextVar
from contextlib import closing
import os

from app import db

SCHEMA = 'CREATE TABLE IF NOT EXISTS model_call_budget(task_id TEXT PRIMARY KEY,calls INTEGER NOT NULL DEFAULT 0)'
_owner = ContextVar('model_budget_owner', default=None)


class ModelBudgetExceeded(RuntimeError):
    pass


def bind(task_id: str):
    return _owner.set(_owner.get() or task_id)


def reset(token) -> None:
    _owner.reset(token)


def reserve_call() -> None:
    task_id = _owner.get()
    if task_id is None:
        return
    limit = int(os.getenv('APP_MAX_MODEL_CALLS', '32'))
    if not 1 <= limit <= 10000:
        raise ValueError('APP_MAX_MODEL_CALLS 必须在 1 到 10000 之间')
    with closing(db.get_conn()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT OR IGNORE INTO model_call_budget(task_id) VALUES(?)', (task_id,))
        updated = conn.execute('UPDATE model_call_budget SET calls=calls+1 WHERE task_id=? AND calls<?', (task_id,limit))
        if updated.rowcount != 1:
            raise ModelBudgetExceeded('任务已达到模型调用次数上限')
