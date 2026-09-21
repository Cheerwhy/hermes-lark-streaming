"""Executable synthetic and revision-pinned cron lanes, without live installation or sends."""

import ast
import asyncio
import logging
import sys
import textwrap
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from hermes_sources import TARGET_REVISION, source_at

from hermes_lark_streaming import patch
from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.patcher import (
    MK_CRON_DELIVER,
    MK_CRON_DELIVER_END,
    PatcherError,
    _remove_block_checked,
)
from hermes_lark_streaming.split_cron import inject_cron

# Keep the relevant upstream statement shapes, while replacing gateway imports
# and I/O with recorders. The original and injected versions are both executable.
SOURCE = textwrap.dedent('''\
def _live_send_text(
    t, text_to_send, route_thread_id, route_metadata, *,
    target_errors, delivery_errors, unverified_targets,
):
    job = t.job
    router = DeliveryRouter(t.config, t.target_adapters)
    events.append(('native_text', t.chat_id, text_to_send, route_thread_id, route_metadata))
    if t.native_failure:
        target_errors.append('native text failed')
        return False, False, None
    return True, False, 'native-message'


def _deliver_via_live_adapter(
    t, cleaned_text, media_files, *, target_errors, delivery_errors, unverified_targets,
):
    job = t.job
    route_thread_id, route_metadata, media_metadata = _live_route_metadata(t)
    delivered = False
    try:
        text_to_send = cleaned_text.strip()
        adapter_ok, timed_out, delivered_message_id = True, False, None
        if not text_to_send and not media_files:
            target_errors.append('empty payload')
            adapter_ok = False
        elif text_to_send:
            adapter_ok, timed_out, delivered_message_id = _live_send_text(
                t, text_to_send, route_thread_id, route_metadata,
                target_errors=target_errors, delivery_errors=delivery_errors,
                unverified_targets=unverified_targets,
            )
        if adapter_ok and not timed_out and media_files:
            _live_send_media(t, media_metadata, media_files, delivery_errors)
        if adapter_ok:
            delivered = True
            _seed_live_delivery_sessions(t, delivered_message_id)
    except Exception as exc:
        target_errors.append(str(exc))
    return delivered


def _standalone_send(t, content, media_files):
    def _send():
        return _send_to_platform(
            t.platform, t.pconfig, t.chat_id, content, thread_id=t.thread_id,
            media_files=media_files)

    def _warned(msg):
        return None, msg

    if t.shutting_down:
        return _warned('shutdown')
    if not content.strip() and not media_files:
        return _warned(f'standalone send skipped (empty text and no media) for {t.where}')
    coro = _send()
    return asyncio.run(coro), None


def _deliver_standalone(t, content, media_files, target_errors, delivery_errors):
    if t.is_relay:
        delivery_errors.extend(target_errors or ['relay unavailable'])
        return
    result, err = _standalone_send(t, content, media_files)
    if err is None and result and result.get('error'):
        err = result['error']
    if err is not None:
        delivery_errors.extend(target_errors + [err])
        return
    delivery_errors.extend(result.get('warnings', []))
    events.append(('mirror', t.chat_id, t.mirror_text, t.thread_id))


def _deliver_result(job, content, adapters=None, loop=None, *, for_failure=False):
    errors, unverified_targets = [], []
    seen = set()
    for t in job['targets']:
        key = (t.platform_name, t.chat_id, t.thread_id)
        if key in seen:
            continue
        seen.add(key)
        target_errors = []
        delivered = t.live_adapter_ready and _deliver_via_live_adapter(
            t, content, job.get('media', []), target_errors=target_errors,
            delivery_errors=errors, unverified_targets=unverified_targets,
        )
        if not delivered:
            _deliver_standalone(t, content, job.get('media', []), target_errors, errors)
    return errors, unverified_targets
''')


def target(**overrides):
    fields = dict(
        job={'id': 'job-1', 'name': 'Digest', 'next_run_at': '2026-09-09'},
        platform_name='feishu', chat_id='chat-1', thread_id=None,
        opened_thread_id=None, is_relay=False, notify_delivery=True,
        in_channel_surface=False, inchannel_continuable=False,
        loop=None, config=None, target_adapters=None,
        native_failure=False, shutting_down=False, live_adapter_ready=True,
        mirror_text='original unwrapped brief', metadata={'job_id': 'job-1', 'notify': True},
        media_error=False, standalone_error=False,
    )
    fields.update(overrides)
    fields['where'] = f"{fields['platform_name']}:{fields['chat_id']}"
    fields['platform'] = fields['platform_name']
    fields['pconfig'] = SimpleNamespace(
        media_error=fields['media_error'], standalone_error=fields['standalone_error'])
    return SimpleNamespace(**fields)


def runtime(monkeypatch, *, hook=True, patched=True, target_revision=False):
    events = []

    def card(**kwargs):
        events.append(('card', kwargs))
        if isinstance(hook, Exception):
            raise hook
        return hook

    def media(t, metadata, files, errors):
        events.append(('media', t.chat_id, files, metadata))
        if t.media_error:
            errors.append('attachment failed')

    async def standalone(platform, pconfig, chat_id, content, *, thread_id, media_files):
        events.append(('standalone', chat_id, content, media_files, thread_id))
        if pconfig.standalone_error:
            return {'error': 'standalone failed'}
        return {'success': True, 'warnings': ['attachment failed'] if pconfig.media_error else []}

    monkeypatch.setattr(patch, 'on_cron_deliver', card)
    namespace = dict(
        events=events, asyncio=asyncio, logger=logging.getLogger(__name__),
        DeliveryRouter=lambda *_: None,
        _live_route_metadata=lambda t: (t.thread_id, t.metadata, t.metadata),
        _live_send_media=media,
        _seed_live_delivery_sessions=lambda t, mid: events.append(
            ('seed', t.chat_id, t.mirror_text, mid)),
        _send_to_platform=standalone,
    )
    exec(compile(inject_cron(SOURCE) if patched else SOURCE, '<cron-fixture>', 'exec'), namespace)
    if target_revision:
        # Execute the target's actual send lanes; isolate only imports and external I/O.
        monkeypatch.setitem(sys.modules, 'agent.async_utils', SimpleNamespace(safe_schedule_threadsafe=None))
        monkeypatch.setitem(sys.modules, 'gateway.delivery', SimpleNamespace(
            DeliveryRouter=namespace['DeliveryRouter'], DeliveryTarget=None))
        monkeypatch.setitem(sys.modules, 'tools.send_message_tool', SimpleNamespace(_send_to_platform=standalone))
        namespace['_sched'] = SimpleNamespace(_interpreter_shutting_down=lambda: False)
        source = inject_cron(source_at('cron/scheduler_delivery.py', TARGET_REVISION))
        tree = ast.parse(source)
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in (
            '_live_send_text', '_deliver_via_live_adapter', '_standalone_send')]
        module = ast.Module(body=[ast.ImportFrom(
            module='__future__', names=[ast.alias(name='annotations')], level=0), *functions], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, '<target-cron>', 'exec'), namespace)
    return namespace['_deliver_result'], events


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
@pytest.mark.parametrize('terminal_newline', [True, False])
def test_compile_and_exact_removal(newline, terminal_newline):
    source = SOURCE if terminal_newline else SOURCE.rstrip('\n')
    source = source.replace('\n', newline)
    patched = inject_cron(source)
    compile(patched, '<patched>', 'exec')
    assert patched.count(MK_CRON_DELIVER) == 2
    assert patched.count(MK_CRON_DELIVER_END) == 2
    assert _remove_block_checked(patched, MK_CRON_DELIVER, MK_CRON_DELIVER_END) == source


@pytest.mark.parametrize('live', [True, False])
def test_card_keeps_media_mirroring_and_target_dedup(monkeypatch, live):
    run, events = runtime(monkeypatch)
    t = target(live_adapter_ready=live)
    errors, unverified = run({'targets': [t, t], 'media': [('report.pdf', False)]}, ' brief ')
    assert errors == []
    cards = [e for e in events if e[0] == 'card']
    assert len(cards) == 1
    assert cards[0][1] == dict(chat_id='chat-1', content='brief', loop=None,
                              task_name='Digest', run_time='2026-09-09')
    assert not any(e[0] == 'native_text' for e in events)
    if live:
        assert unverified == ['feishu:chat-1']
        assert events[1][0] == 'media'
        assert events[2] == ('seed', 'chat-1', 'original unwrapped brief', None)
    else:
        assert unverified == []
        assert events[1] == ('standalone', 'chat-1', '', [('report.pdf', False)], None)
        assert events[2] == ('mirror', 'chat-1', 'original unwrapped brief', None)


@pytest.mark.parametrize('live', [True, False])
def test_text_only_card_does_not_send_native_text(monkeypatch, live):
    run, events = runtime(monkeypatch)
    assert run({'targets': [target(live_adapter_ready=live)]}, 'brief')[0] == []
    assert [e[0] for e in events] == ['card', 'seed' if live else 'mirror']


@pytest.mark.parametrize('live', [True, False])
@pytest.mark.parametrize('hook', [False, RuntimeError('plugin unavailable')])
def test_card_failure_preserves_native_payload(monkeypatch, live, hook):
    run, events = runtime(monkeypatch, hook=hook)
    errors, _ = run({'targets': [target(live_adapter_ready=live)], 'media': ['file']}, 'brief')
    assert errors == []
    assert events[1][0] == ('native_text' if live else 'standalone')
    assert events[1][2] == 'brief'


@pytest.mark.parametrize('live', [True, False])
@pytest.mark.parametrize('fields', [
    {'platform_name': 'telegram'}, {'is_relay': True},
    {'thread_id': 'topic-1'}, {'thread_id': ''}, {'opened_thread_id': 'new-thread'},
    {'notify_delivery': False},
    {'in_channel_surface': True, 'inchannel_continuable': True},
])
def test_unsupported_route_is_identical_to_native(monkeypatch, live, fields):
    t = target(live_adapter_ready=live, **fields)
    native_run, native_events = runtime(monkeypatch, patched=False)
    expected = native_run({'targets': [t], 'media': ['file']}, 'brief')
    run, events = runtime(monkeypatch)
    assert run({'targets': [t], 'media': ['file']}, 'brief') == expected
    assert events == native_events
    assert not getattr(t, '_hermes_lark_card_attempted', False)


@pytest.mark.parametrize('metadata', [
    {'reply_to_message_id': 'anchor'}, {'thread_id': 'thread'},
    {'scope_id': 'tenant'}, {'future_route': True}, {'notify': False}, 'malformed',
])
def test_unknown_live_metadata_stays_native(monkeypatch, metadata):
    run, events = runtime(monkeypatch)
    t = target(metadata=metadata)
    assert run({'targets': [t]}, 'brief')[0] == []
    assert events[0][0] == 'native_text'
    assert events[-1][-1] == 'native-message'
    assert not getattr(t, '_hermes_lark_card_attempted', False)


@pytest.mark.parametrize('live', [True, False])
def test_attachment_failure_remains_a_delivery_error(monkeypatch, live):
    run, events = runtime(monkeypatch)
    errors, _ = run({'targets': [target(live_adapter_ready=live, media_error=True)],
                     'media': ['file']}, 'brief')
    assert errors == ['attachment failed']
    assert sum(e[0] == 'card' for e in events) == 1


@pytest.mark.parametrize('hook', [False, RuntimeError('uncertain send')])
@pytest.mark.parametrize('media_error', [False, True])
def test_native_live_failure_still_reaches_standalone(monkeypatch, hook, media_error):
    run, events = runtime(monkeypatch, hook=hook)
    errors, _ = run({'targets': [target(native_failure=True, media_error=media_error)],
                     'media': ['file']}, 'brief')
    assert errors == (['attachment failed'] if media_error else [])
    assert [e[0] for e in events] == ['card', 'native_text', 'standalone', 'mirror']
    assert events[2] == ('standalone', 'chat-1', 'brief', ['file'], None)


def test_rejected_live_route_does_not_consume_standalone_attempt(monkeypatch):
    run, events = runtime(monkeypatch)
    t = target(native_failure=True, metadata={'future_route': True})
    assert run({'targets': [t]}, 'brief')[0] == []
    assert [e[0] for e in events] == ['native_text', 'card', 'mirror']


@pytest.mark.parametrize('target_revision', [False, True])
@pytest.mark.parametrize('live', [False, True])
def test_pending_controller_timeout_is_not_retried_across_lanes(monkeypatch, target_revision, live):
    run, events = runtime(monkeypatch, target_revision=target_revision)
    pending = []
    sent = []

    async def send(chat_id, content, **kwargs):
        sent.append((chat_id, content))

    controller = SimpleNamespace(enabled=True, _do_cron_deliver=send,
                                 _on_bg_task_done=StreamCardController._on_bg_task_done)
    loop = SimpleNamespace(is_running=lambda: True, is_closed=lambda: False)

    def schedule(coro, target_loop):
        assert target_loop is loop
        future = Future()
        # Exercise the real pending-future TimeoutError without waiting 30 seconds.
        def wait(timeout=None):
            if timeout is None:
                return Future.result(future)
            assert timeout == 30
            return Future.result(future, timeout=0)
        monkeypatch.setattr(future, 'result', wait)
        pending.append((coro, future))
        return future

    monkeypatch.setattr(asyncio, 'run_coroutine_threadsafe', schedule)
    monkeypatch.setattr(patch, 'on_cron_deliver', lambda **kwargs:
                        StreamCardController.on_cron_deliver(controller, **kwargs))
    try:
        targets = [target(loop=loop, native_failure=True, live_adapter_ready=live),
                   target(loop=loop, native_failure=True, live_adapter_ready=live, chat_id='chat-2')]
        expected_unverified = ['feishu:chat-1', 'feishu:chat-2'] if live else []
        assert run({'targets': targets, 'media': ['file']}, 'brief') == ([], expected_unverified)
        assert len(pending) == 2
        # Hermes creates a new _TargetDelivery even for the same chat on a later run.
        later = target(loop=loop, native_failure=True, live_adapter_ready=live, job={'id': 'job-2'})
        assert run({'targets': [later], 'media': ['file']}, 'brief') == ([], ['feishu:chat-1'] if live else [])
        assert len(pending) == 3
        assert all(not future.done() for _, future in pending)
        assert not any(e[0] == 'native_text' for e in events)
        if live:
            assert not any(e[0] == 'standalone' for e in events)
            assert len([e for e in events if e[0] == 'media']) == 3
        else:
            assert [e for e in events if e[0] == 'standalone'] == [
                ('standalone', chat_id, '', ['file'], None) for chat_id in ('chat-1', 'chat-2', 'chat-1')
            ]
        # The old uncertain sends can still complete; the guard does not cancel them.
        for coro, future in pending:
            future.set_result(asyncio.run(coro))
        assert sent == [('chat-1', 'brief'), ('chat-2', 'brief'), ('chat-1', 'brief')]
    finally:
        for coro, _ in pending:
            coro.close()


def test_different_threads_are_not_deduplicated_by_chat(monkeypatch):
    run, events = runtime(monkeypatch)
    assert run({'targets': [target(), target(thread_id='a'), target(thread_id='b')]}, 'brief')[0] == []
    assert sum(e[0] == 'card' for e in events) == 1
    assert [e[3] for e in events if e[0] == 'native_text'] == ['a', 'b']


@pytest.mark.parametrize('live', [True, False])
@pytest.mark.parametrize('media', [[], ['file']])
def test_empty_text_does_not_create_card(monkeypatch, live, media):
    run, events = runtime(monkeypatch)
    errors, _ = run({'targets': [target(live_adapter_ready=live)], 'media': media}, '  ')
    assert bool(errors) is (not media)
    assert not any(e[0] == 'card' for e in events)


def test_standalone_shutdown_guard_precedes_card(monkeypatch):
    run, events = runtime(monkeypatch)
    assert run({'targets': [target(live_adapter_ready=False, shutting_down=True)]}, 'brief')[0]
    assert events == []


@pytest.mark.parametrize('source', [
    SOURCE + '\ndef _live_send_text():\n    pass\n',
    SOURCE.replace('router = DeliveryRouter(t.config, t.target_adapters)', 'router = None'),
    SOURCE.replace('coro = _send()', 'coro = _send()\n    coro = _send()'),
    SOURCE.replace('router = DeliveryRouter(t.config, t.target_adapters)',
                   'def decoy():\n        router = DeliveryRouter(t.config, t.target_adapters)'),
    SOURCE.replace('_seed_live_delivery_sessions(t, delivered_message_id)', 'pass'),
    SOURCE.replace('_live_send_media(t, media_metadata, media_files, delivery_errors)', 'pass'),
    SOURCE.replace('t, content, media_files):', 't, new_content, media_files):'),
    SOURCE + '\nreturn\n',
    'def broken(:',
])
def test_drift_and_nested_decoys_fail_closed(source):
    with pytest.raises(PatcherError):
        inject_cron(source)


def test_existing_markers_require_parent_cleanup():
    with pytest.raises(PatcherError, match='markers'):
        inject_cron(inject_cron(SOURCE))
