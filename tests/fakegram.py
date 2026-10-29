"""In-process fake of the Telegram Bot API, for end-to-end tests of the real handlers.

It plugs into python-telegram-bot as a custom BaseRequest, records every API call the bot makes,
keeps a model of the chat (message ids, text, keyboards) and serves uploaded files back on download.
"""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import dataclass, field

from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest, RequestData

BOT_ID = 999


@dataclass
class ChatMessage:
    message_id: int
    from_bot: bool
    text: str = ""
    keyboard: list[list[dict]] | None = None
    photo: bool = False
    document: bool = False
    deleted: bool = False
    date: int = field(default_factory=lambda: int(time.time()))

    def buttons(self) -> list[dict]:
        return [b for row in (self.keyboard or []) for b in row]

    def button(self, label_substring: str) -> dict:
        for b in self.buttons():
            if label_substring in b["text"]:
                return b
        raise KeyError(f"No button containing {label_substring!r}; have {[b['text'] for b in self.buttons()]}")


@dataclass
class FakeTelegram(BaseRequest):
    calls: list[tuple[str, dict]] = field(default_factory=list)
    messages: dict[int, ChatMessage] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    callback_answers: list[dict] = field(default_factory=list)
    stale_queries: bool = False  # answerCallbackQuery fails as for taps delivered after the bot was offline
    fail_next: dict[str, int] = field(default_factory=dict)  # method -> how many upcoming calls answer 502
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _update_ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    # --- BaseRequest interface ---------------------------------------------------

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self) -> float:
        return 5.0

    async def do_request(self, url, method, request_data: RequestData | None = None, **_kw):
        if "/file/bot" in url:
            return 200, self.files[url.rsplit("/", 1)[-1]]
        api_method = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        if request_data and request_data.contains_files:
            params["_files"] = list((request_data.multipart_data or {}).keys())
        self.calls.append((api_method, params))
        if api_method == "answerCallbackQuery" and self.stale_queries:
            return 400, json.dumps({"ok": False, "error_code": 400, "description":
                                    "Bad Request: query is too old and response timeout expired or query id is invalid"}).encode()
        if self.fail_next.get(api_method):
            self.fail_next[api_method] -= 1
            return 502, json.dumps({"ok": False, "error_code": 502, "description": "Bad Gateway"}).encode()
        result = self._dispatch(api_method, params)
        return 200, json.dumps({"ok": True, "result": result}).encode()

    # --- API emulation ----------------------------------------------------------------

    def _dispatch(self, m: str, p: dict):
        if m == "getMe":
            return {"id": BOT_ID, "is_bot": True, "first_name": "CalBot", "username": "test_cal_bot",
                    "can_join_groups": False, "can_read_all_group_messages": False, "supports_inline_queries": False}
        if m in {"setMyCommands", "deleteWebhook", "sendChatAction", "deleteMyCommands"}:
            return True
        if m == "answerCallbackQuery":
            self.callback_answers.append(p)
            return True
        if m in {"sendMessage", "sendPhoto", "sendDocument"}:
            mid = next(self._ids)
            msg = ChatMessage(mid, True, p.get("text") or p.get("caption") or "", _kb(p.get("reply_markup")),
                              photo=m == "sendPhoto", document=m == "sendDocument")
            self.messages[mid] = msg
            return self._message_json(msg, p.get("chat_id"))
        if m == "editMessageText":
            msg = self.messages[int(p["message_id"])]
            msg.text = p["text"]
            msg.keyboard = _kb(p.get("reply_markup"))
            return self._message_json(msg, p.get("chat_id"))
        if m == "editMessageReplyMarkup":
            msg = self.messages[int(p["message_id"])]
            msg.keyboard = _kb(p.get("reply_markup"))
            return self._message_json(msg, p.get("chat_id"))
        if m == "deleteMessage":
            self.messages[int(p["message_id"])].deleted = True
            return True
        if m == "getFile":
            fid = p["file_id"]
            return {"file_id": fid, "file_unique_id": fid, "file_size": len(self.files[fid]), "file_path": fid}
        if m == "getUpdates":
            return []
        raise NotImplementedError(m)

    def _message_json(self, msg: ChatMessage, chat_id) -> dict:
        d = {"message_id": msg.message_id, "date": msg.date, "chat": {"id": int(chat_id or 1), "type": "private"},
             "from": {"id": BOT_ID, "is_bot": True, "first_name": "CalBot"}, "text": msg.text}
        if msg.keyboard:
            d["reply_markup"] = {"inline_keyboard": msg.keyboard}
        return d

    # --- helpers for tests ---------------------------------------------------------------

    def bot_messages(self) -> list[ChatMessage]:
        return [m for m in self.messages.values() if m.from_bot and not m.deleted]

    def last(self) -> ChatMessage:
        return self.bot_messages()[-1]


def _kb(markup) -> list[list[dict]] | None:
    if not markup:
        return None
    if isinstance(markup, str):
        markup = json.loads(markup)
    return markup.get("inline_keyboard")


class Chat:
    """A simulated private chat between one user and the bot."""

    def __init__(self, app: Application, tg: FakeTelegram, user_id: int = 42, first_name: str = "Test"):
        self.app, self.tg, self.user_id, self.first_name = app, tg, user_id, first_name

    def _base(self, **extra) -> dict:
        mid = next(self.tg._ids)
        self.tg.messages[mid] = ChatMessage(mid, False, extra.get("text", ""))
        msg = {"message_id": mid, "date": int(time.time()), "chat": {"id": self.user_id, "type": "private"},
               "from": {"id": self.user_id, "is_bot": False, "first_name": self.first_name}}
        msg.update(extra)
        return msg

    async def _push(self, payload: dict) -> None:
        update = Update.de_json({"update_id": next(self.tg._update_ids), **payload}, self.app.bot)
        await self.app.process_update(update)

    async def text(self, text: str, reply_to: int | None = None) -> None:
        extra = {"text": text}
        if text.startswith("/"):
            cmd = text.split()[0]
            extra["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
        if reply_to:
            extra["reply_to_message"] = {"message_id": reply_to, "date": self.tg.messages[reply_to].date,
                                         "chat": {"id": self.user_id, "type": "private"},
                                         "from": {"id": BOT_ID, "is_bot": True, "first_name": "CalBot"},
                                         "text": self.tg.messages[reply_to].text}
        await self._push({"message": self._base(**extra)})

    async def photo(self, data: bytes, caption: str = "", media_group_id: str | None = None) -> None:
        fid = f"photo{len(self.tg.files)}"
        self.tg.files[fid] = data
        extra = {"photo": [{"file_id": fid, "file_unique_id": fid, "width": 1280, "height": 960}]}
        if caption:
            extra["caption"] = caption
        if media_group_id:
            extra["media_group_id"] = media_group_id
        await self._push({"message": self._base(**extra)})

    async def voice(self, data: bytes, mime: str = "audio/ogg", duration: int = 5) -> None:
        fid = f"voice{len(self.tg.files)}"
        self.tg.files[fid] = data
        extra = {"voice": {"file_id": fid, "file_unique_id": fid, "duration": duration, "mime_type": mime}}
        await self._push({"message": self._base(**extra)})

    async def sticker(self) -> None:
        extra = {"sticker": {"file_id": "stk", "file_unique_id": "stk", "type": "regular", "width": 512,
                             "height": 512, "is_animated": False, "is_video": False}}
        await self._push({"message": self._base(**extra)})

    async def press(self, message_id: int, label_substring: str) -> None:
        msg = self.tg.messages[message_id]
        button = msg.button(label_substring)
        await self._push({"callback_query": {
            "id": str(next(self.tg._update_ids)), "chat_instance": "ci",
            "from": {"id": self.user_id, "is_bot": False, "first_name": self.first_name},
            "message": self.tg._message_json(msg, self.user_id), "data": button["callback_data"],
        }})


def build_test_app(settings, tg: FakeTelegram) -> Application:
    """build_app with the network swapped for the fake."""
    from calbot import bot as botmod

    original = botmod.ApplicationBuilder

    class Builder(original):  # type: ignore[misc, valid-type]
        def build(self):
            self.request(tg).get_updates_request(tg)
            return original.build(self)

    botmod.ApplicationBuilder = Builder
    try:
        return botmod.build_app(settings)
    finally:
        botmod.ApplicationBuilder = original
