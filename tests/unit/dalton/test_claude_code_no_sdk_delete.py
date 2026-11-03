"""SYNC3 B6 追加验收：无 SDK 环境下 ``DELETE /api/v1/claude-code/token``。

生产镜像不装 ``claude-code`` extra，所以这个接口必须在 SDK 缺失时照常工作：
B4 时它经 ``_changing()`` 顶层导入 SDK 会 500；#652 重构（864627504）后
删除路径只走 Redis 锁和 token 库。本测试在一个新解释器里、导入任何
``src`` 模块之前用 ``sys.modules['claude_agent_sdk'] = None`` 模拟没装
SDK（本套件的其他测试会把运行时连 SDK 一起导入，同一进程测不出），
然后导入整个应用、以已认证用户实际调用路由，断言 204 且删除逻辑
真的执行——不是"任何非 500"都算过。

替身一律 ``create_autospec``，按真实签名；``connection_change`` 是
``@asynccontextmanager`` 包装的 async generator，``create_autospec``
不认它为协程函数，所以它的替身用真实签名 ``inspect.signature().bind()``
（G2 的调用点检查）加调用记录代替。
"""

import json
import pathlib
import subprocess
import sys

# 在导入任何 src 模块之前屏蔽 SDK，再走完整个应用导入 + 一次已认证的
# DELETE。断言失败直接抛出，进程以非零退出；成功打印 RESULT= 一行 JSON。
_INNER = """
import asyncio
import json
import sys

sys.modules['claude_agent_sdk'] = None

import src.server.app  # noqa: F401  无 SDK 导入整个应用，不得报错

import inspect
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, create_autospec, patch

from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.server.app import claude_code as route
from src.server.services.claude_code import checks, flow, saved
from src.server.services.oauth_signin import OAuthRefusal
from src.utils.cache.redis_cache import RedisCacheClient
from tests.conftest import create_test_app

USER = "test-user-123"
TOKEN = "sk-ant-oat01-" + "x" * 40
CONN = object()

# Redis 替身：autospec 实例。enabled/client 是 __init__ 里的实例属性，
# autospec 类上看不到，按真实运行时的样子补上。
cache = create_autospec(RedisCacheClient, instance=True)
cache.enabled = True
cache.client = MagicMock()
cache.acquire_lock.return_value = True

# token 库替身：协程函数按真实签名 autospec。connection_change 是
# @asynccontextmanager 包装的 async generator，create_autospec 不认它
# 为协程，所以替身用真实签名 bind（G2 的调用点检查）加调用记录。
stored_token = create_autospec(saved.stored_token, return_value=TOKEN)
delete_tokens = create_autospec(saved.delete_oauth_tokens, return_value=None)
change_sig = inspect.signature(saved.connection_change)
change_calls: list[tuple[str, str]] = []


@asynccontextmanager
async def connection_change(user_id, provider):
    change_sig.bind(user_id, provider)
    change_calls.append((user_id, provider))
    yield CONN


async def call():
    app = create_test_app(route.router)
    app.add_exception_handler(
        OAuthRefusal, lambda _, exc: JSONResponse({"detail": exc.detail, "error": exc.error}, exc.status_code)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.delete("/api/v1/claude-code/token")


with (
    patch.object(flow, "get_cache_client", return_value=cache),
    patch.object(saved, "get_cache_client", return_value=cache),
    patch.object(checks, "get_cache_client", return_value=cache),
    patch("src.server.services.oauth_signin.require_cache", return_value=cache),
    patch.object(saved, "stored_token", stored_token),
    patch.object(saved, "connection_change", connection_change),
    patch.object(saved, "delete_oauth_tokens", delete_tokens),
):
    response = asyncio.run(call())

assert response.status_code == 204, (response.status_code, response.text)
# 删除逻辑确实走到：token 库为已认证用户删掉了 claude-code 的行。
stored_token.assert_awaited_once_with(USER)
assert change_calls == [(USER, "claude-code")]
delete_tokens.assert_awaited_once_with(USER, "claude-code", conn=CONN)
# 有存量 token，删除后其检查答案也一并清掉。
cache.delete.assert_awaited_once()
assert cache.delete.await_args.args[0].startswith("claude_code:token_check:")

assert sys.modules.get("claude_agent_sdk") is None, "SDK 不应被导入"

print("RESULT=" + json.dumps({
    "status": response.status_code,
    "deleted_for": list(delete_tokens.await_args.args),
}))
"""


def test_delete_token_returns_204_and_reaches_the_delete_without_the_sdk():
    done = subprocess.run(
        [sys.executable, "-c", _INNER],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=pathlib.Path(__file__).resolve().parents[3],
    )
    assert done.returncode == 0, f"subprocess failed:\n{done.stdout}\n{done.stderr}"
    (line,) = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT=")]
    result = json.loads(line[len("RESULT="):])
    assert result == {"status": 204, "deleted_for": ["test-user-123", "claude-code"]}
