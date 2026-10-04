"""前端「保存存档」请求归属回归测试的 pytest 封装。

用例本体在 tests/test_archive_save_ownership.js：用最小 DOM 桩在 Node 虚拟机中加载
真实的 app/static/app.js，驱动「保存存档 / 重新导入 / 恢复存档」的真实事件处理器，
覆盖首次保存预读取迟到（成功 / 失败）与换稿后再次保存的场景。
这里通过 node --test 运行它；环境没有 Node 时跳过（后端接口测试不受影响）。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 运行前端单测")
def test_archive_save_ownership_frontend():
    r = subprocess.run(
        ["node", "--test", str(ROOT / "tests" / "test_archive_save_ownership.js")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
