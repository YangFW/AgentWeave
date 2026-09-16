"""外部调用配置：资源配置优先于环境默认值，所有等待均有有限上限。"""
import math
import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class CallLimits:
    timeout: float
    max_retries: int = 0
    backoff: float = 0.6


class ModelCallTimeout(TimeoutError):
    pass


class ToolCallTimeout(TimeoutError):
    pass


@asynccontextmanager
async def call_timeout(kind: str, seconds: float):
    """将本调用的总超时转换为可公开识别的错误，不改写内部主动抛出的超时。"""
    deadline = asyncio.timeout(seconds)
    try:
        async with deadline:
            yield
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        error = ModelCallTimeout if kind == 'model' else ToolCallTimeout
        label = {'model': '模型', 'http_tool': 'HTTP 工具', 'mcp': 'MCP'}[kind]
        raise error(f'{label}调用超过 {seconds:g} 秒上限，请检查服务响应或调整调用超时配置') from exc


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} 必须是有限数字') from exc
    if isinstance(value, bool) or not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f'{name} 必须在 {minimum:g} 到 {maximum:g} 之间')
    return number


def call_limits(kind: str, config: Mapping[str, Any]) -> CallLimits:
    defaults = {'model': 90, 'http_tool': 30, 'mcp': 60}
    default = defaults[kind]
    prefix = f'APP_{kind.upper()}'
    timeout = _number(config.get('timeout', os.getenv(prefix + '_TIMEOUT_SECONDS', str(default))), 'timeout', 0.1, 600)
    retries = _number(config.get('max_retries', os.getenv(prefix + '_MAX_RETRIES', '3' if kind == 'model' else '0')), 'max_retries', 0, 5)
    if not retries.is_integer():
        raise ValueError('max_retries 必须是整数')
    if kind != 'model' and retries:
        raise ValueError('HTTP/MCP 工具不自动重试；请确认副作用后显式重试任务')
    backoff = _number(config.get('retry_backoff', os.getenv(prefix + '_RETRY_BACKOFF_SECONDS', '0.6')), 'retry_backoff', 0, 10)
    return CallLimits(timeout, int(retries), backoff)
