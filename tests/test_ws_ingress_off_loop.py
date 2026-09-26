"""A held web chat acceptance never stalls the ASGI loop (TZ-1 PR1 review F1).

``ws_endpoint`` hands an owner chat frame to ``bridge.ui_send``. That acceptance
takes the single host's ingress lock and writes the canonical ``chat.jsonl`` row
through a locked durable append BEFORE it enqueues and echoes. A skill delivery
scanning retained chat under that lock, or a slow disk, used to stall the whole
event loop with it: every other HTTP and WebSocket handler waited, Stop and
Panic included. The acceptance now runs off the loop through
``gateway._helpers.run_sync_to_completion`` and keeps its custody: the row, the
queue item and the echo still complete in that order, even when the socket task
is cancelled while the acceptance stands on the lock. A command on another
socket stays an inline queue put; a later frame on this socket waits its turn.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from ouroboros.gateway.state import api_health
from ouroboros.gateway.ws import ws_endpoint
from supervisor import message_bus


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    """A real LocalChatBridge on a pytest drive, registered as THE host bridge."""
    drive = tmp_path / "drive"
    (drive / "logs").mkdir(parents=True)
    bridge = message_bus.LocalChatBridge({})
    monkeypatch.setattr(message_bus, "_BRIDGE", bridge)
    monkeypatch.setattr(message_bus, "DATA_DIR", drive)
    monkeypatch.setattr(message_bus, "load_state", lambda: {})
    bridge.drive = drive
    return bridge


def _chat_frame(client_message_id: str, text: str = "hello") -> str:
    return json.dumps({"type": "chat", "content": text, "client_message_id": client_message_id})


def _row_ids(drive) -> list[str]:
    path = drive / "logs" / "chat.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["client_message_id"]
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _witness_echoes(bridge):
    """Record what the bridge can see at echo time: queue depth and rows on disk."""
    echoes: list[tuple[dict, int, list[str]]] = []
    bridge._broadcast_fn = lambda payload: echoes.append((payload, bridge._inbox.qsize(), _row_ids(bridge.drive)))
    return echoes


def _gate_ui_send(bridge, entered: threading.Event, release: threading.Event | None = None):
    """Flag the moment the socket hands the frame to the bridge (an instance attribute:
    the socket resolves ``bridge.ui_send`` at call time, so the real method still runs)."""
    original = bridge.ui_send

    def ui_send(text, **kwargs):
        entered.set()
        if release is not None:
            assert release.wait(5), "the test never released the held acceptance"
        return original(text, **kwargs)

    bridge.ui_send = ui_send


def test_a_held_ingress_lock_never_stalls_other_handlers(bridge):
    """Consumer regression: with the ingress lock held by another writer, an
    owner chat frame is standing in its acceptance; an unrelated HTTP handler
    and another socket's command frame must still be served meanwhile."""
    held, release, timed_out, entered = (threading.Event() for _ in range(4))
    echoes = _witness_echoes(bridge)
    _gate_ui_send(bridge, entered)

    def hold_ingress():  # a skill delivery mid-scan under the single host's ingress lock
        with message_bus._INGRESS_LOCK:
            held.set()
            if not release.wait(timeout=6.0):
                timed_out.set()

    holder = threading.Thread(target=hold_ingress, name="ingress-holder", daemon=True)
    holder.start()
    assert held.wait(5)
    app = Starlette(routes=[WebSocketRoute("/ws", ws_endpoint), Route("/api/health", api_health)])
    try:
        # One client context = one portal: the sockets and the GET share ONE event loop.
        with TestClient(app) as client, client.websocket_connect("/ws") as owner, \
                client.websocket_connect("/ws") as other:
            owner.send_text(_chat_frame("held-1"))
            assert entered.wait(5), "the chat frame never reached the bridge"
            # The acceptance is standing on the held lock. The loop must still serve
            # an unrelated HTTP handler and another socket's command (the Stop/Panic rail).
            started = time.monotonic()
            response = client.get("/api/health")
            elapsed = time.monotonic() - started
            assert response.status_code == 200
            assert not timed_out.is_set() and elapsed < 3.0, (
                f"the event loop was blocked behind the held ingress lock (health answered after {elapsed:.2f}s)")
            other.send_text(json.dumps({"type": "command", "cmd": "/stop"}))
            deadline = time.monotonic() + 3.0
            while bridge._inbox.qsize() < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert bridge._inbox.qsize() == 1, "a command frame queued behind the held owner ingress"
            assert not timed_out.is_set()
            assert echoes == [] and _row_ids(bridge.drive) == []  # nothing accepted while the lock is held
            release.set()
            holder.join(5)
            deadline = time.monotonic() + 5.0
            while not echoes and time.monotonic() < deadline:
                time.sleep(0.01)
    finally:
        release.set()
    # Custody order survived the off-loop hop: the row was on disk and the item
    # queued (behind the command that never waited) before the echo went out.
    assert [(q, ids) for _payload, q, ids in echoes] == [(2, ["held-1"])], echoes
    assert echoes[0][0]["ingress_accepted"] is True and echoes[0][0]["client_message_id"] == "held-1"
    updates = bridge.get_updates(offset=0, timeout=1)
    assert [u["message"]["text"] for u in updates] == ["/stop", "hello"]
    assert updates[1]["message"]["accepted_source_row"]["client_message_id"] == "held-1"


class _OpenSocket:
    """A socket that delivers its frames, then stays open forever."""

    app = None  # never consulted for a chat frame

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent: list[dict] = []

    async def accept(self):
        return None

    async def receive_text(self):
        if self.frames:
            return self.frames.pop(0)
        await asyncio.Event().wait()

    async def send_text(self, text):
        self.sent.append(json.loads(text))


def test_a_cancelled_socket_task_still_settles_the_held_acceptance(bridge):
    """Cancel the socket task while the acceptance is held: the canonical row,
    the queue item and the echo still complete, in that order, before the
    cancellation is observed — custody is never abandoned mid-write."""
    entered, release = threading.Event(), threading.Event()
    echoes = _witness_echoes(bridge)
    _gate_ui_send(bridge, entered, release)
    socket = _OpenSocket([_chat_frame("cancel-1", "settle me")])

    async def main():
        task = asyncio.create_task(ws_endpoint(socket))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), "the socket task abandoned its held acceptance"
        assert echoes == [] and _row_ids(bridge.drive) == []
        release.set()
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.CancelledError:
            pass
        assert task.cancelled()

    asyncio.run(main())
    assert [(q, ids) for _payload, q, ids in echoes] == [(1, ["cancel-1"])], echoes
    assert echoes[0][0]["ingress_accepted"] is True
    assert socket.sent == []  # no initialization notice: the acceptance succeeded
    update = bridge.get_updates(offset=0, timeout=1)[0]["message"]
    assert update["text"] == "settle me"
    assert update["accepted_source_row"]["client_message_id"] == "cancel-1"


def test_an_acceptance_failure_still_answers_the_socket(bridge):
    """The initialization notice (the socket's only failure reply) survives the
    off-loop hop: a refused canonical write reaches the sender, and nothing is
    echoed or queued."""
    echoes = _witness_echoes(bridge)

    def refuse(*_a, **_k):
        raise OSError("disk refused")

    bridge.ui_send = refuse
    socket = _OpenSocket([_chat_frame("refused-1")])

    async def main():
        task = asyncio.create_task(ws_endpoint(socket))
        deadline = time.monotonic() + 5.0
        while not socket.sent and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert socket.sent and socket.sent[0]["system_type"] == "initialization_notice", socket.sent
    assert echoes == [] and bridge.get_updates(offset=0, timeout=0) == []
