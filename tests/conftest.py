"""
conftest.py — 统一处理 aiosqlite mock 污染问题

test_decision.py / test_cooldown.py 在模块顶层 mock 了 sys.modules["aiosqlite"]，
导致 router.db / router.models 在收集期就被导入（带着 MagicMock 引用）。

本 conftest 在 pytest 收集前（conftest 加载时）先把真实 aiosqlite 塞进 sys.modules，
并让所有测试文件共享同一个 mock 策略：需要真实 aiosqlite 的测试通过
`_load_router_modules()` helper 获取 reload 过的模块。
"""
import importlib
import os
import sys

# ── 在 conftest 加载阶段（最早），确保 sys.modules 有真实 aiosqlite ──
if "aiosqlite" in sys.modules and sys.modules["aiosqlite"].__class__.__name__ == "MagicMock":
    # 删除 mock，重新 import 真实版本
    del sys.modules["aiosqlite"]
    import aiosqlite as _real
    sys.modules["aiosqlite"] = _real


def _load_router_modules():
    """
    返回 (db_mod, models_mod)，通过 importlib.reload 确保
    router.db 内部引用的是真实 aiosqlite。
    供 test_stats.py 中需要真实 DB 的测试调用。
    """
    # 确保项目根在 sys.path
    project_root = os.path.join(os.path.dirname(__file__), ".")
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # 再次确认 aiosqlite 是真实版本
    if "aiosqlite" not in sys.modules or sys.modules["aiosqlite"].__class__.__name__ == "MagicMock":
        import aiosqlite as _a
        sys.modules["aiosqlite"] = _a

    import router.db
    db_mod = importlib.reload(router.db)
    import router.models
    models_mod = importlib.reload(router.models)
    return db_mod, models_mod
