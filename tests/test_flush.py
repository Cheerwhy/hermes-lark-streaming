"""streaming.flush 测试 — FlushController 节流、刷新、完成逻辑."""

from __future__ import annotations

import asyncio
import time

import pytest

from hermes_lark_streaming.streaming.flush import (
    BATCH_AFTER_GAP_MS,
    LONG_GAP_MS,
    FlushController,
)


def _make_async(**kwargs: object) -> FlushController:
    """必须在 async 测试内调用，以捕获正确的事件循环."""
    return FlushController(**kwargs)  # type: ignore[arg-type]


class TestConstruction:
    def test_explicit_loop_is_used_directly(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            ctrl = FlushController(loop=loop)
            assert ctrl._loop is loop
        finally:
            loop.close()

    @pytest.mark.asyncio
    async def test_without_loop_uses_running_loop(self) -> None:
        ctrl = FlushController()
        assert ctrl._loop is asyncio.get_running_loop()

    def test_without_loop_in_sync_context_raises(self) -> None:
        with pytest.raises(RuntimeError):
            FlushController()


class TestScheduleUpdate:
    @pytest.mark.asyncio
    async def test_first_call_schedules_delayed_flush(self) -> None:
        ctrl = _make_async()
        ctrl.set_card_message_ready(True)
        flushed = asyncio.Event()

        async def do_flush() -> None:
            flushed.set()

        # 第一次调用: elapsed ≈ 0 < throttle_ms → 延迟路径
        ctrl.schedule_update(do_flush)
        await asyncio.sleep(0.05)
        assert not flushed.is_set()  # 还没触发
        await asyncio.sleep(0.10)
        assert flushed.is_set()  # 节流窗口过后触发

    @pytest.mark.asyncio
    async def test_immediate_path_taken_after_elapsed(self) -> None:
        ctrl = _make_async(throttle_ms=0.05)  # 50ms
        ctrl.set_card_message_ready(True)
        count = 0

        async def do_flush() -> None:
            nonlocal count
            count += 1

        # 等待节流窗口过去
        await asyncio.sleep(0.08)
        ctrl.schedule_update(do_flush)
        # 无 pending timer 说明走了立即路径（via call_soon）
        assert ctrl._pending_timer is None
        # call_soon → create_task 管道需要时间传播
        await asyncio.sleep(0.2)
        assert count == 1

    @pytest.mark.asyncio
    async def test_no_flush_when_not_ready(self) -> None:
        ctrl = _make_async()
        # card_message_ready 为 False
        count = 0

        async def do_flush() -> None:
            nonlocal count
            count += 1

        ctrl.schedule_update(do_flush)
        await asyncio.sleep(0.05)
        assert count == 0

    @pytest.mark.asyncio
    async def test_no_flush_after_completed(self) -> None:
        ctrl = _make_async()
        ctrl.set_card_message_ready(True)
        ctrl.mark_completed()
        count = 0

        async def do_flush() -> None:
            nonlocal count
            count += 1

        ctrl.schedule_update(do_flush)
        await asyncio.sleep(0.05)
        assert count == 0

    @pytest.mark.asyncio
    async def test_long_gap_schedules_delayed_batch(self) -> None:
        ctrl = _make_async()
        ctrl.set_card_message_ready(True)
        count = 0

        async def do_flush() -> None:
            nonlocal count
            count += 1

        # 模拟长时间空闲
        ctrl._last_update_time = time.monotonic() - LONG_GAP_MS - 1.0
        ctrl.schedule_update(do_flush)
        # 不应立即刷新
        await asyncio.sleep(0.01)
        assert count == 0
        # 延迟批量刷新后触发
        await asyncio.sleep(BATCH_AFTER_GAP_MS + 0.05)
        assert count == 1


class TestFlushNow:
    @pytest.mark.asyncio
    async def test_flush_now_executes_immediately(self) -> None:
        ctrl = _make_async()
        ctrl.set_card_message_ready(True)
        count = 0

        async def do_flush() -> None:
            nonlocal count
            count += 1

        await ctrl.flush_now(do_flush)
        assert count == 1

    @pytest.mark.asyncio
    async def test_flush_now_skips_when_completed(self) -> None:
        ctrl = _make_async()
        ctrl.set_card_message_ready(True)
        ctrl.mark_completed()
        count = 0

        async def do_flush() -> None:
            nonlocal count
            count += 1

        await ctrl.flush_now(do_flush)
        assert count == 0

    @pytest.mark.asyncio
    async def test_flush_now_skips_when_not_ready(self) -> None:
        ctrl = _make_async()
        count = 0

        async def do_flush() -> None:
            nonlocal count
            count += 1

        await ctrl.flush_now(do_flush)
        assert count == 0

    @pytest.mark.asyncio
    async def test_flush_now_cancels_pending_timer(self) -> None:
        ctrl = _make_async(throttle_ms=500.0)
        ctrl.set_card_message_ready(True)
        count = 0

        async def do_flush() -> None:
            nonlocal count
            count += 1

        # 构造一个 pending timer 的场景
        ctrl._last_update_time = time.monotonic() - 0.05
        ctrl.schedule_update(do_flush)
        assert ctrl._pending_timer is not None
        await ctrl.flush_now(do_flush)
        assert ctrl._pending_timer is None


class TestWaitForFlush:
    @pytest.mark.asyncio
    async def test_returns_immediately_when_idle(self) -> None:
        ctrl = _make_async()
        ctrl.set_card_message_ready(True)
        await ctrl.wait_for_flush()  # 不应挂起

    @pytest.mark.asyncio
    async def test_waits_for_flush_to_complete(self) -> None:
        ctrl = _make_async()
        ctrl.set_card_message_ready(True)
        flush_event = asyncio.Event()
        count = 0

        async def slow_flush() -> None:
            nonlocal count
            count += 1
            await flush_event.wait()

        # 启动一个阻塞的 flush
        _task = asyncio.create_task(ctrl._do_flush(slow_flush))
        await asyncio.sleep(0.02)
        assert ctrl._flush_in_progress

        # wait_for_flush 应阻塞
        waiter = asyncio.create_task(ctrl.wait_for_flush())
        await asyncio.sleep(0.02)
        assert not waiter.done()

        # 释放 flush
        flush_event.set()
        await asyncio.sleep(0.02)
        assert waiter.done()


class TestMarkCompleted:
    @pytest.mark.asyncio
    async def test_prevents_future_flushes(self) -> None:
        ctrl = _make_async()
        ctrl.set_card_message_ready(True)
        ctrl.mark_completed()
        count = 0

        async def do_flush() -> None:
            nonlocal count
            count += 1

        await ctrl.flush_now(do_flush)
        assert count == 0

    @pytest.mark.asyncio
    async def test_resolves_waiters(self) -> None:
        ctrl = _make_async()
        ctrl.set_card_message_ready(True)
        flush_event = asyncio.Event()

        async def blocking_flush() -> None:
            await flush_event.wait()

        _task = asyncio.create_task(ctrl._do_flush(blocking_flush))
        await asyncio.sleep(0.02)

        waiter = asyncio.create_task(ctrl.wait_for_flush())
        await asyncio.sleep(0.02)
        assert not waiter.done()

        ctrl.mark_completed()
        await asyncio.sleep(0.02)
        assert waiter.done()


class TestReflush:
    @pytest.mark.asyncio
    async def test_needs_reflush_during_active_flush(self) -> None:
        ctrl = _make_async()
        ctrl.set_card_message_ready(True)
        flush_event = asyncio.Event()
        count = 0

        async def slow_flush() -> None:
            nonlocal count
            count += 1
            if count == 1:
                await flush_event.wait()

        # 启动一个慢 flush
        task = asyncio.create_task(ctrl._do_flush(slow_flush))
        await asyncio.sleep(0.02)
        assert ctrl._flush_in_progress

        # 再次尝试 flush → 应设置 reflush 标志
        asyncio.create_task(ctrl._do_flush(slow_flush))
        await asyncio.sleep(0.02)
        assert ctrl._needs_reflush

        # 释放第一个 flush
        flush_event.set()
        await task
        await asyncio.sleep(0.02)
        # reflush 应自动触发
        assert count >= 2


class TestSetCardMessageReady:
    @pytest.mark.asyncio
    async def test_enables_flushing(self) -> None:
        ctrl = _make_async()
        count = 0

        async def do_flush() -> None:
            nonlocal count
            count += 1

        ctrl.schedule_update(do_flush)
        await asyncio.sleep(0.05)
        assert count == 0

        ctrl.set_card_message_ready(True)
        # 等待节流窗口过去
        await asyncio.sleep(0.15)
        ctrl.schedule_update(do_flush)
        await asyncio.sleep(0.05)
        assert count == 1

    @pytest.mark.asyncio
    async def test_initializes_timestamp(self) -> None:
        ctrl = _make_async()
        assert ctrl.last_update_time == 0.0
        ctrl.set_card_message_ready(True)
        assert ctrl.last_update_time > 0.0


class TestFlushException:
    @pytest.mark.asyncio
    async def test_exception_does_not_break_controller(self) -> None:
        ctrl = _make_async()
        ctrl.set_card_message_ready(True)
        count = 0

        async def failing_flush() -> None:
            nonlocal count
            count += 1
            if count == 1:
                raise RuntimeError("test error")

        await ctrl._do_flush(failing_flush)
        assert count == 1
        assert not ctrl._flush_in_progress

    @pytest.mark.asyncio
    async def test_exception_still_updates_timestamp(self) -> None:
        ctrl = _make_async()
        ctrl.set_card_message_ready(True)
        before = ctrl.last_update_time

        async def failing_flush() -> None:
            raise RuntimeError("test")

        await ctrl._do_flush(failing_flush)
        assert ctrl.last_update_time >= before


class TestSetThrottle:
    @pytest.mark.asyncio
    async def test_updates_throttle(self) -> None:
        ctrl = _make_async()
        ctrl.set_throttle(500.0)
        assert ctrl.throttle_ms == 500.0
