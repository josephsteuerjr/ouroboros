"""Answering an Ouroboros quiz card from Telegram (#472).

The host's ``chat.quiz`` event carries the card identity (``task_id``,
``quiz_id``). The owner's button tap or reply is relayed to the SAME decision
ingress the web card uses — Host Service ``POST /chat/decision`` →
``task_decision.answer_decision`` — so the answer is idempotent per
``request_id`` (``tg:<update_id>``), first answer wins, and a late answer is
accepted exactly as it is for the browser card: the host records it and delivers
it into the card's chat as an ordinary owner message, and the toast says which
of those happened. The only state kept here maps a
short callback token and the sent message to that identity: Telegram caps
``callback_data`` at 64 bytes, too short for the ids themselves. A card also
remembers its settled lifecycle state, so the host's ``chat.quiz_state`` facts
edit it forward only. Nothing here parses the owner's words; a reply is
delivered verbatim as their own answer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .telegram_state import _read_json_file, _state_file

_QUIZ_STATE_FILE = "quiz_state.json"
_MAX_REMEMBERED = 50
_CALLBACK_PREFIX = "qz:"
_BUTTON_LABEL_MAX = 40
_ANSWER_ECHO_MAX = 200

HostPost = Callable[[Any, str, Dict[str, Any]], Awaitable[Tuple[int, Dict[str, Any]]]]

_TEXTS = {
    "en": {
        "hint": "Tap an option, or reply to this message with your own answer.",
        "hint_open": "Reply to this message with your answer.",
        "recorded": "✅ Answer delivered to the task.",
        "late_delivered": "✅ The task had already finished — your answer was delivered to the chat.",
        "late_recorded": "✅ Answer recorded. The task had already finished and this card has no chat to deliver it to.",
        "already": "This question was already answered.",
        "expired": "This question has expired — the task moved on.",
        "gone": "This question is no longer known to Ouroboros.",
        "failed": "Could not deliver the answer (HTTP {status}). Try again.",
        "answered_line": "Answered: {answer}",
        "answered_plain": "Answered.",
        "resumed": "The task continued; an answer is still accepted.",
        "expired_terminal": "The task finished; a late answer is accepted as your message.",
        "superseded": "Replaced by a newer question.",
    },
    "ru": {
        "hint": "Нажмите вариант или ответьте на это сообщение своим текстом.",
        "hint_open": "Ответьте на это сообщение своим текстом.",
        "recorded": "✅ Ответ передан задаче.",
        "late_delivered": "✅ Задача уже завершилась — ответ доставлен в чат.",
        "late_recorded": "✅ Ответ записан. Задача уже завершилась, а доставлять его в чат некуда.",
        "already": "На этот вопрос уже отвечали.",
        "expired": "Вопрос устарел — задача уже двинулась дальше.",
        "gone": "Этот вопрос Ouroboros больше не знает.",
        "failed": "Не удалось передать ответ (HTTP {status}). Попробуйте ещё раз.",
        "answered_line": "Ответ: {answer}",
        "answered_plain": "Ответ получен.",
        "resumed": "Задача продолжила работу; ответ всё ещё принимается.",
        "expired_terminal": "Задача завершилась; поздний ответ придёт как ваше сообщение.",
        "superseded": "Вопрос заменён более новым.",
    },
}

# A remembered card's lifecycle only moves forward, as on the web card: a closed
# wait or an expiry never reopens a settled card, and nothing downgrades an answer.
_LIFECYCLE_RANK = {"open": 0, "expired_terminal": 1, "superseded": 2, "answered": 3}


def _texts(lang: str) -> Dict[str, str]:
    return _TEXTS["ru" if lang == "ru" else "en"]


def hint(lang: str) -> str:
    return _texts(lang)["hint"]


def hint_open(lang: str) -> str:
    return _texts(lang)["hint_open"]


def mint_token(task_id: str, quiz_id: str) -> str:
    """Short stable token for ``callback_data`` (Telegram's 64-byte cap)."""
    return hashlib.sha256(f"{task_id}:{quiz_id}".encode("utf-8")).hexdigest()[:12]


def quiz_keyboard(token: str, labels: List[str]) -> List[List[dict]]:
    """One button row per option; ``callback_data`` = ``qz:<token>:<index>``."""
    return [
        [{"text": f"{index}. {label}"[:_BUTTON_LABEL_MAX],
          "callback_data": f"{_CALLBACK_PREFIX}{token}:{index - 1}"}]
        for index, label in enumerate(labels, 1)
    ]


def render_quiz_text(question: str, labels: List[str], stake: str, assumption: str,
                     *, wait_for_answer: bool = False) -> str:
    lines = [f"Question: {question}"]
    if stake:
        lines.append(f"At stake: {stake}")
    lines.extend(f"{index}. {label}" for index, label in enumerate(labels, 1))
    if wait_for_answer:
        lines.append("Waiting for your answer; Stop and the task deadline still apply.")
    elif assumption:
        lines.append(f"Continuing meanwhile: {assumption}")
    return "\n".join(lines)


def _load(api) -> Dict[str, Any]:
    data = _read_json_file(_state_file(api, _QUIZ_STATE_FILE))
    quizzes = data.get("quizzes") if isinstance(data, dict) else None
    return {"quizzes": dict(quizzes) if isinstance(quizzes, dict) else {}}


def remember_quiz(api, token: str, record: Dict[str, Any]) -> None:
    """Bounded token → card mapping (the newest ``_MAX_REMEMBERED`` cards)."""
    data = _load(api)
    quizzes = data["quizzes"]
    quizzes.pop(token, None)
    quizzes[token] = dict(record)
    for stale in list(quizzes)[:-_MAX_REMEMBERED]:
        quizzes.pop(stale, None)
    path = _state_file(api, _QUIZ_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def quiz_for_token(api, token: str) -> Optional[Dict[str, Any]]:
    record = _load(api)["quizzes"].get(str(token or ""))
    return dict(record) if isinstance(record, dict) else None


def remember_state(api, token: str, record: Dict[str, Any], state: str) -> None:
    """Persist a settled lifecycle state on the card: the no-rollback evidence."""
    if _LIFECYCLE_RANK.get(state, 0) and str(record.get("state") or "") != state:
        remember_quiz(api, token, {**record, "state": state})


def quiz_for_message(api, chat_id: int, message_id: int) -> Optional[Dict[str, Any]]:
    """The card sent as ``message_id`` in ``chat_id`` (for reply-to answers)."""
    if not message_id:
        return None
    for record in _load(api)["quizzes"].values():
        if (isinstance(record, dict)
                and int(record.get("chat_id") or 0) == int(chat_id)
                and int(record.get("message_id") or 0) == int(message_id)):
            return dict(record)
    return None


async def _deliver(
    api, post: HostPost, record: Dict[str, Any], *,
    option_index: Optional[int], comment: str, update_id: int,
) -> Tuple[int, Dict[str, Any]]:
    body: Dict[str, Any] = {
        "request_id": f"tg:{int(update_id)}",
        "decision_id": f"quiz:{record.get('task_id')}:{record.get('quiz_id')}",
    }
    if option_index is not None:
        body["option_index"] = int(option_index)
    if comment:
        body["comment"] = comment
    return await post(api, "/chat/decision", body)


def _outcome_text(status: int, payload: Dict[str, Any], lang: str) -> str:
    texts = _texts(lang)
    if status < 400:
        if payload.get("answered_after_terminal") is True:
            # The card outlived its task: the answer became an owner message in
            # the card's chat, unless that chat has no owner turn to start.
            return texts["late_delivered" if payload.get("forwarded") else "late_recorded"]
        return texts["recorded"]
    if status == 404:
        return texts["gone"]
    if status == 409:
        answered = payload.get("answered_index") is not None or str(payload.get("state") or "") == "answered"
        return texts["already"] if answered else texts["expired"]
    return texts["failed"].format(status=status)


def _echo(answer: str) -> str:
    return answer if len(answer) <= _ANSWER_ECHO_MAX else answer[:_ANSWER_ECHO_MAX] + "…"


def _answered_text(record: Dict[str, Any], answer: str, lang: str) -> str:
    texts = _texts(lang)
    line = texts["answered_line"].format(answer=answer) if answer else texts["answered_plain"]
    return f"{record.get('text') or ''}\n{line}"


def lifecycle_edit(
    record: Dict[str, Any], event: Dict[str, Any], lang: str,
) -> Optional[Tuple[str, List[List[dict]]]]:
    """The (text, keyboard) edit a host ``chat.quiz_state`` fact asks of a sent card.

    ``None`` when the fact changes nothing here: a state this card cannot show, an
    ``open`` that does not close a wait, or a fact older than the card's own state.
    The card mirrors the web one (``web/modules/question_presentation.js``): an
    answer settles it on the recorded option and/or the owner's own words; a closed
    wait drops the waiting line; an expired card stays answerable, because a late
    answer is still accepted as the owner's message (В17a=A); a superseded card is
    a read-only record. An answer is always re-applied — the edit is idempotent.
    """
    state = str(event.get("state") or "")
    if state not in _LIFECYCLE_RANK or (state == "open" and event.get("wait_for_answer") is not False):
        return None
    if _LIFECYCLE_RANK[state] < _LIFECYCLE_RANK.get(str(record.get("state") or "open"), 0):
        return None
    texts = _texts(lang)
    base = str(record.get("text") or "")
    if state == "answered":
        options = list(record.get("options") or [])
        index = event.get("answered_index")
        parts = []
        if isinstance(index, int) and not isinstance(index, bool):
            parts.append(f"{index + 1}. {options[index]}" if 0 <= index < len(options) else f"{index + 1}.")
        if str(event.get("comment") or ""):
            parts.append(_echo(str(event["comment"])))
        return _answered_text(record, " — ".join(parts), lang), []
    if state == "superseded":
        return f"{base}\n{texts['superseded']}", []
    status = texts["resumed" if state == "open" else "expired_terminal"]
    labels = [str(label) for label in record.get("options") or []]
    if not labels:
        return f"{base}\n{status}\n{texts['hint_open']}", []
    token = mint_token(str(record.get("task_id") or ""), str(record.get("quiz_id") or ""))
    return f"{base}\n{status}\n{texts['hint']}", quiz_keyboard(token, labels)


def lifecycle_target(
    api, event: Dict[str, Any], lang: str,
) -> Optional[Tuple[int, int, str, List[List[dict]]]]:
    """``(chat_id, message_id, text, keyboard)`` for a card sent here, else ``None``.

    A card never sent to Telegram has nothing to edit. A settled state is
    remembered before the edit: it is the host's fact, whatever the edit does.
    """
    task_id = str(event.get("task_id") or "").strip()
    quiz_id = str(event.get("quiz_id") or "").strip()
    token = mint_token(task_id, quiz_id)
    record = quiz_for_token(api, token) if task_id and quiz_id else None
    message_id = int((record or {}).get("message_id") or 0)
    edit = lifecycle_edit(record, event, lang) if record and message_id else None
    if record is None or edit is None:
        return None
    remember_state(api, token, record, str(event.get("state") or ""))
    return int(record.get("chat_id") or 0), message_id, edit[0], edit[1]


async def _mark_answered(api, client, record: Dict[str, Any], answer: str, lang: str) -> None:
    token = mint_token(str(record.get("task_id") or ""), str(record.get("quiz_id") or ""))
    remember_state(api, token, record, "answered")
    message_id = int(record.get("message_id") or 0)
    if not message_id:
        return
    await client.edit_message_text_with_inline_keyboard(
        int(record.get("chat_id") or 0), message_id, _answered_text(record, answer, lang), [], parse_mode="",
    )


async def answer_from_callback(
    api, client, cb_data: str, *, cb_id: str, update_id: int, lang: str, post: HostPost,
) -> None:
    """A tapped option → the decision ingress; toast the honest outcome."""
    parts = str(cb_data or "").split(":")
    record = quiz_for_token(api, parts[1]) if len(parts) == 3 else None
    try:
        index = int(parts[2]) if len(parts) == 3 else -1
    except ValueError:
        index = -1
    options = list((record or {}).get("options") or [])
    if record is None or not 0 <= index < len(options):
        await client.answer_callback_query(cb_id, text=_texts(lang)["gone"])
        return
    status, payload = await _deliver(api, post, record, option_index=index, comment="", update_id=update_id)
    await client.answer_callback_query(cb_id, text=_outcome_text(status, payload, lang))
    recorded = payload.get("answered_index")
    if status < 400 or (status == 409 and isinstance(recorded, int)):
        # Settle the card on the RECORDED option (a first-wins loser learns the winner).
        chosen = recorded if isinstance(recorded, int) and 0 <= recorded < len(options) else index
        await _mark_answered(api, client, record, f"{chosen + 1}. {options[chosen]}", lang)


async def answer_from_reply(
    api, client, record: Dict[str, Any], answer_text: str, *,
    chat_id: int, update_id: int, lang: str, post: HostPost,
) -> None:
    """A reply to the card → the owner's own verbatim answer (comment-only)."""
    status, payload = await _deliver(api, post, record, option_index=None, comment=answer_text, update_id=update_id)
    await client.send_message(chat_id, _outcome_text(status, payload, lang))
    if status < 400:
        await _mark_answered(api, client, record, _echo(answer_text), lang)
