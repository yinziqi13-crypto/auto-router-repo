"""
M2-2 冷却恢复单元测试
覆盖：
  1. record_402() 指数退避 cooldown_until（1h→2h→4h，上限 24h）
  2. reset() 清零 cooldown_until
  3. StateManager.cooldown_scan() 自动恢复过期的 EXHAUSTED
  4. cooldown_scan() 不重置未过期的 EXHAUSTED
  5. cooldown_scan() 异常不抛出（catch 记日志）
不连接实时上游，使用 unittest.mock + pytest
"""

import sys
import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

# 提前 mock aiosqlite（decision.py 顶层 import）
sys.modules["aiosqlite"] = MagicMock()
sys.modules["aiosqlite"].connect = MagicMock()
sys.modules["aiosqlite"].Connection = MagicMock()

from router.models import (
    QuotaState, QuotaStatus,
    COOLDOWN_BASE_HOURS, COOLDOWN_MAX_HOURS,
)
from router.decision import StateManager


# ──────────────────────────────────────────
# record_402() 指数退避测试
# ──────────────────────────────────────────

class TestRecord402Cooldown:
    def test_first_402_sets_1h(self):
        st = QuotaState("tencent_free", "deepseek-v4-flash")
        st.record_402()
        assert st.consecutive_402 == 1
        assert st.cooldown_until is not None
        delta = st.cooldown_until - datetime.utcnow()
        assert timedelta(minutes=59) < delta <= timedelta(hours=1, minutes=1)

    def test_second_402_doubles_to_2h(self):
        st = QuotaState("tencent_free", "deepseek-v4-flash")
        st.record_402()
        first = st.cooldown_until
        st.record_402()
        delta = st.cooldown_until - datetime.utcnow()
        assert timedelta(hours=1, minutes=59) < delta <= timedelta(hours=2, minutes=1)
        assert st.cooldown_until > first  # 第二次的 cooldown 更晚

    def test_third_402_reaches_exhausted_and_4h(self):
        st = QuotaState("tencent_free", "deepseek-v4-flash")
        for _ in range(3):
            st.record_402()
        assert st.status == QuotaStatus.EXHAUSTED
        delta = st.cooldown_until - datetime.utcnow()
        assert timedelta(hours=3, minutes=59) < delta <= timedelta(hours=4, minutes=1)

    def test_many_402_capped_at_24h(self):
        st = QuotaState("tencent_free", "deepseek-v4-flash")
        # 6 次后应达 24h 上限（32h 被 cap）
        for _ in range(8):
            st.record_402()
        delta = st.cooldown_until - datetime.utcnow()
        assert delta <= timedelta(hours=24, minutes=1)
        assert st.status == QuotaStatus.EXHAUSTED

    def test_reset_clears_cooldown(self):
        st = QuotaState("tencent_free", "deepseek-v4-flash")
        st.record_402()
        st.record_402()
        st.record_402()
        assert st.status == QuotaStatus.EXHAUSTED
        st.reset()
        assert st.status == QuotaStatus.AVAILABLE
        assert st.consecutive_402 == 0
        assert st.cooldown_until is None

    def test_constants(self):
        assert COOLDOWN_BASE_HOURS == 1
        assert COOLDOWN_MAX_HOURS == 24


# ──────────────────────────────────────────
# cooldown_scan() 测试
# ──────────────────────────────────────────

class TestCooldownScan:
    @pytest.fixture
    def state_mgr(self):
        sm = StateManager(":memory:")
        sm._conn = None  # 不写真实 DB（_persist 直接 return）
        return sm

    @pytest.mark.asyncio
    async def test_scan_resets_expired_exhausted(self, state_mgr):
        """过期的 EXHAUSTED → cooldown_scan 重置为 AVAILABLE"""
        st = QuotaState(
            "tencent_free", "deepseek-v4-flash",
            status=QuotaStatus.EXHAUSTED, consecutive_402=3,
            cooldown_until=datetime.utcnow() - timedelta(hours=1),  # 已过期
        )
        state_mgr._states[("tencent_free", "deepseek-v4-flash")] = st
        with patch.object(state_mgr, "_persist", new_callable=AsyncMock) as mock_persist:
            await state_mgr.cooldown_scan()
        assert st.status == QuotaStatus.AVAILABLE
        assert st.consecutive_402 == 0
        assert st.cooldown_until is None
        mock_persist.assert_called()

    @pytest.mark.asyncio
    async def test_scan_keeps_future_exhausted(self, state_mgr):
        """未过期的 EXHAUSTED → cooldown_scan 不重置"""
        st = QuotaState(
            "tencent_free", "deepseek-v4-flash",
            status=QuotaStatus.EXHAUSTED, consecutive_402=3,
            cooldown_until=datetime.utcnow() + timedelta(hours=5),  # 未来
        )
        state_mgr._states[("tencent_free", "deepseek-v4-flash")] = st
        await state_mgr.cooldown_scan()
        assert st.status == QuotaStatus.EXHAUSTED  # 保持

    @pytest.mark.asyncio
    async def test_scan_skips_no_cooldown(self, state_mgr):
        """EXHAUSTED 但 cooldown_until 为空 → 不重置（无冷却时间可判断）"""
        st = QuotaState(
            "tencent_free", "deepseek-v4-flash",
            status=QuotaStatus.EXHAUSTED, consecutive_402=3,
            cooldown_until=None,
        )
        state_mgr._states[("tencent_free", "deepseek-v4-flash")] = st
        with patch.object(state_mgr, "_persist", new_callable=AsyncMock) as mock_persist:
            await state_mgr.cooldown_scan()
        assert st.status == QuotaStatus.EXHAUSTED
        mock_persist.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_survives_persist_error(self, state_mgr):
        """_persist 抛异常 → cooldown_scan 不抛（catch 记日志继续）"""
        st = QuotaState(
            "tencent_free", "deepseek-v4-flash",
            status=QuotaStatus.EXHAUSTED, consecutive_402=3,
            cooldown_until=datetime.utcnow() - timedelta(hours=1),
        )
        state_mgr._states[("tencent_free", "deepseek-v4-flash")] = st
        async def _boom(*args, **kwargs):
            raise RuntimeError("db write failed")
        with patch.object(state_mgr, "_persist", new=_boom):
            # 不应抛出
            await state_mgr.cooldown_scan()

    @pytest.mark.asyncio
    async def test_scan_resets_only_expired(self, state_mgr):
        """混合场景：过期 + 未过期 → 只重置过期的"""
        expired = QuotaState(
            "tencent_free", "deepseek-v4-flash",
            status=QuotaStatus.EXHAUSTED, consecutive_402=3,
            cooldown_until=datetime.utcnow() - timedelta(minutes=30),
        )
        future = QuotaState(
            "bailian_free", "deepseek-v4-flash",
            status=QuotaStatus.EXHAUSTED, consecutive_402=3,
            cooldown_until=datetime.utcnow() + timedelta(hours=3),
        )
        state_mgr._states[("tencent_free", "deepseek-v4-flash")] = expired
        state_mgr._states[("bailian_free", "deepseek-v4-flash")] = future
        with patch.object(state_mgr, "_persist", new_callable=AsyncMock):
            await state_mgr.cooldown_scan()
        assert expired.status == QuotaStatus.AVAILABLE
        assert future.status == QuotaStatus.EXHAUSTED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
