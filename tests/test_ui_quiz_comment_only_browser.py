"""TZ-2 B1: a comment-only question (an ``escalate`` with zero options) reaches the real
web UI as a quiz card with no option buttons and the free-answer box, and the owner's
typed answer travels verbatim through the real gateway: the durable quiz block records
no chosen option and the exact text, and the card shows it as the owner's answer.

Only model judgment is a fixture (the scripted stub asks, then finishes); the server,
the worker, the WebSocket ingress and the decision endpoint are the production ones.
"""
import json
import os
import uuid
from pathlib import Path

import pytest

from tests.test_owner_wait_integration import wait_clone as clone_fixture
from tests.test_native_owner_wait_browser import chat_connection
from tests.system_e2e.harness import (
    ArtifactOracle, KeylessIsolatedServer, ScriptedStubModel, keyless_settings,
    wait_durable_result, wait_until, write_settings_file,
)

wait_clone = clone_fixture
pytestmark = [pytest.mark.serial, pytest.mark.browser]

QUESTION = "What deadline should the report state on its cover page?"
ANSWER = "Friday, 3 October — and say it is provisional."


def test_comment_only_question_renders_and_takes_a_free_text_answer(wait_clone, tmp_path):
    from playwright.sync_api import sync_playwright

    root = tmp_path / "instance" / "data"
    root.mkdir(parents=True)
    screenshots = Path(os.environ.get("OUROBOROS_BROWSER_EVIDENCE_OUT") or tmp_path / "screenshots")
    screenshots.mkdir(parents=True, exist_ok=True)
    steps = [
        {"tool": "escalate", "arguments": {"question": QUESTION, "options": [],
            "stake": "The deadline is printed on the cover page.", "wait_for_answer": True}},
        {"final": "The report states the deadline you gave."},
    ]
    with ScriptedStubModel(steps) as stub:
        settings_path = root / "settings.json"
        write_settings_file(settings_path, keyless_settings(stub, OUROBOROS_MAX_WORKERS=1))
        server = KeylessIsolatedServer(wait_clone, root, settings_path)
        server.start(ready_timeout=120)
        oracle = ArtifactOracle(root)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
                try:
                    page.goto(server.base_url, wait_until="domcontentloaded")
                    with chat_connection(server) as ws:
                        message_id = uuid.uuid4().hex
                        ws.send(json.dumps({"type": "chat", "content": "Ask me for the deadline, then wait for my answer.",
                                            "client_message_id": message_id, "chat_id": 1}))
                        task = wait_until(lambda: next((row["task"] for row in oracle.events("task_received")
                            if (row.get("task", {}).get("metadata", {}).get("origin_message_ref") or {}).get("client_message_id") == message_id), None), 90)
                        assert task
                        wait = wait_until(lambda: (block if (block := oracle.task_result(task["id"]).get("owner_wait", {})).get("state") == "waiting" else None), 90)
                        assert wait and wait.get("quiz_id"), wait
                        card = page.locator(f'#chat-messages .chat-quiz-card[data-task-id="{task["id"]}"][data-quiz-id="{wait["quiz_id"]}"]')
                        card.get_by_text(QUESTION, exact=True).wait_for(timeout=30000)
                        card.locator('.chat-quiz-comment').wait_for(timeout=30000)
                        # Zero options: no option button at all, only the free-answer box and its send.
                        assert card.locator('.chat-quiz-option').count() == 0
                        assert card.locator('.chat-quiz-comment').count() == 1
                        assert card.locator('.chat-quiz-send').is_disabled()
                        card.scroll_into_view_if_needed()
                        card.screenshot(animations="disabled", path=str(screenshots / "chromium-comment-only-question.png"))
                        card.locator('.chat-quiz-comment').fill(ANSWER)
                        assert card.locator('.chat-quiz-send').is_enabled()
                        card.locator('.chat-quiz-send').click()
                        result = wait_durable_result(oracle, task["id"], timeout=120)
                        block = result["owner_quiz"][wait["quiz_id"]]
                        assert block.get("answered_index") is None and block["comment"] == ANSWER, block
                        assert result["status"] == "completed", result.get("status")
                        card.locator('.chat-quiz-answer').filter(has_text=f"Owner's answer: {ANSWER}").wait_for(timeout=30000)
                        wait_until(lambda: card.get_attribute("data-state") == "answered" or None, 30)
                        assert card.locator('.chat-quiz-comment').count() == 0  # a settled card takes no second answer
                        card.screenshot(animations="disabled", path=str(screenshots / "chromium-comment-only-answered.png"))
                        (screenshots / "comment-only.json").write_text(json.dumps({
                            "task_id": task["id"], "quiz_id": wait["quiz_id"], "owner_quiz": block}, indent=2))
                finally:
                    browser.close()
        finally:
            server.stop()
