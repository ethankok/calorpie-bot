"""Telegram handlers and app wiring."""

from __future__ import annotations

import asyncio
import csv
import pathlib
import pickle
from dataclasses import dataclass
from difflib import SequenceMatcher
from collections import defaultdict
import hmac
import io
import logging
import re
import secrets
import time as _time
from datetime import UTC, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    BaseUpdateProcessor,
    PersistenceInput,
    PicklePersistence,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from . import render
from .config import Settings
from .db import DB, Meal, User, log_date
from .estimator import Estimate, Estimator, EstimatorError, MediaPart

log = logging.getLogger(__name__)

ALBUM_WAIT_S = 3.5  # slow mobile uploads can spread an album's photos over a few seconds
STATE_TTL_S = 600  # a pending question (code, goal) expires so stray later messages aren't misread
DUPLICATE_WINDOW = timedelta(minutes=30)
LATE_REPLY_S = 180  # a slower reply starts with an apology
REMINDER_SETTLE_S = 45  # a late recap waits this long so the Telegram backlog is logged first
AI_DEADLINE_S = 90  # give up on one Gemini call, retries included, after this long
RECENT_MEAL_WINDOW = timedelta(minutes=15)  # an unreplied text this soon after a meal may be a correction
GOAL_MIN, GOAL_MAX, GOAL_LOW_WARN = 1000, 4000, 1200  # accepted goal range; below GOAL_LOW_WARN adds a health note
ACK_RE = re.compile(r"(ok+|okay|k|thanks?|thank you|thx|ty|tq|cool|nice|great|noted|alright|yay|haha+|lol|👍|🙏|❤️|😊|🥰)[!. ]*", re.I)
YES_RE = re.compile(r"(y|ya+|yes+|yep|yup|sure|use (it|that)|sounds good|go ahead|ok(ay)? (use|go with) (it|that))[!. ]*", re.I)
MAX_NOTES_LINES = 12
MAX_CODE_ATTEMPTS = 20
GOAL_PRESETS = (1200, 1500, 1800, 2000)
REMINDER_PRESETS = ("20:00", "21:00", "22:00")


# --- concurrency -------------------------------------------------------------


class PerChatUpdateProcessor(BaseUpdateProcessor):
    """Different chats run concurrently, but each chat's updates run one at a time, in order.

    After a restart or network drop, Telegram delivers the backlog at once; without this a re-sent photo slips past
    the duplicate check, /undo can run before the meal it should undo is saved, and a button tap can
    race a correction.
    """

    def __init__(self, locks: dict[int, asyncio.Lock], albums: dict[str, dict]):
        super().__init__(max_concurrent_updates=64)
        self.locks = locks  # also taken by album flushes and reminders, which aren't updates
        self.albums = albums
        self.queues: dict[int, _ChatQueue] = defaultdict(_ChatQueue)

    async def do_process_update(self, update, coroutine) -> None:
        chat = getattr(update, "effective_chat", None)
        if chat is None:
            await coroutine
            return
        q = self.queues[chat.id]
        ticket = q.issued  # tickets are handed out in arrival order and served strictly in that order
        q.issued += 1
        try:
            async with q.cond:
                await q.cond.wait_for(lambda: q.serving == ticket)
            # An album from earlier in the backlog may still be collecting its photos; it must be logged
            # before this later message (e.g. "actually only half" or /undo). Holding the turn while
            # waiting keeps every later update behind this one.
            while (album := _pending_album(self.albums, chat.id, update)) is not None:
                await album["done"].wait()
            async with self.locks[chat.id]:
                await coroutine
        finally:
            async with q.cond:
                if q.serving == ticket:
                    q.serving += 1
                else:  # cancelled before its turn (shutdown): let the queue skip it
                    q.skipped.add(ticket)
                while q.serving in q.skipped:
                    q.skipped.discard(q.serving)
                    q.serving += 1
                q.cond.notify_all()

    def busy(self, chat_id: int) -> bool:
        q = self.queues.get(chat_id)
        pending = any(k.startswith(f"{chat_id}:") for k in self.albums)
        return pending or (q is not None and q.serving < q.issued)

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


class _ChatQueue:
    def __init__(self) -> None:
        self.issued = 0
        self.serving = 0
        self.skipped: set[int] = set()
        self.cond = asyncio.Condition()


def _pending_album(albums: dict[str, dict], chat_id: int, update) -> dict | None:
    """An album still being collected in this chat that this update is not part of."""
    msg = getattr(update, "message", None)
    own = f"{chat_id}:{msg.media_group_id}" if msg is not None and msg.media_group_id else None
    for key, album in albums.items():
        if key.startswith(f"{chat_id}:") and key != own:
            return album
    return None


# --- helpers -----------------------------------------------------------------


def _set_state(ctx: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    ctx.user_data["await"] = (name, _time.time() + STATE_TTL_S)


def _state(ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    value = ctx.user_data.get("await")
    if not value:
        return None
    name, expires = value
    if _time.time() > expires:
        _clear_state(ctx)
        return None
    return name


def _clear_state(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("await", None)
    ctx.user_data.pop("goal_stats_text", None)
    ctx.user_data.pop("suggested_goal", None)


async def _ack(query, text: str | None = None, **kw) -> None:
    """Answer a button tap. Taps queued while the bot was offline are too old to answer; the action itself
    must still happen, so never let that error abort the handler."""
    try:
        await query.answer(text, **kw)
    except (BadRequest, TimedOut) as exc:
        log.info("could not answer callback query: %s", exc)


def _db(ctx: ContextTypes.DEFAULT_TYPE) -> DB:
    return ctx.bot_data["db"]


def _est(ctx: ContextTypes.DEFAULT_TYPE) -> Estimator:
    return ctx.bot_data["estimator"]


def _settings(ctx: ContextTypes.DEFAULT_TYPE) -> Settings:
    return ctx.bot_data["settings"]


async def _safe_edit(target: Message | CallbackQuery, text: str, markup=None) -> None:
    """Edit a message's text, ignoring Telegram's 'message is not modified' error."""
    try:
        if isinstance(target, CallbackQuery):
            await target.edit_message_text(text, reply_markup=markup)
        else:
            await target.edit_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


def _today_kcal(db: DB, user: User) -> float:
    return sum(m.kcal for m in db.meals_on(user.user_id, user.today()))


def _number_in(text: str) -> int | None:
    """The calorie number in a short answer like '1,600', '1600 kcal', 'about 1.6k', '~1500'."""
    t = text.strip().lower().replace(",", "").replace("~", "")
    m = re.fullmatch(r"(?:about|around|maybe|roughly|approx\.?)?\s*(\d+(?:\.\d+)?)\s*(k(?!cal))?\s*(?:k?cals?|calories)?\s*(?:a day|per day|/day|daily)?[.!]?", t)
    if not m:
        return None
    value = float(m.group(1)) * (1000 if m.group(2) else 1)
    return round(value)


def _parse_kcal(text: str) -> int | None:
    n = _number_in(text)
    return n if n is not None and GOAL_MIN <= n <= GOAL_MAX else None


def _parse_hhmm(text: str, evening: bool = True) -> str | None:
    """Accept '21:00', '9pm', '9:30 pm', '2130'. For the evening recap a bare '9' or '9:30' means pm."""
    t = text.strip().lower().replace(".", ":")
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?\s*(am|pm)?", t)
    if not m:
        return None
    h, mins, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if evening and not ampm and 1 <= h <= 11:
        h += 12
    if h > 23 or mins > 59:
        return None
    return f"{h:02d}:{mins:02d}"


async def _get_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> User:
    tg = update.effective_user
    return _db(ctx).upsert_user(tg.id, update.effective_chat.id, tg.first_name)


async def _require_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> User | None:
    """Return the user if authorized; otherwise steer them to the access code and return None."""
    user = await _get_user(update, ctx)
    if user.authorized:
        return user
    _set_state(ctx, "code")
    if update.effective_message:
        await update.effective_message.reply_text("👋 Hi! This is a private bot. Please send the access code to start.")
    return None


def _goal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"{g:,}", callback_data=f"goal:{g}") for g in GOAL_PRESETS],
            [
                InlineKeyboardButton("🧮 Help me pick", callback_data="goal:help"),
                InlineKeyboardButton("No goal", callback_data="goal:none"),
            ],
        ]
    )


def _reminder_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(_ampm(t), callback_data=f"rem:{t}") for t in REMINDER_PRESETS],
            [InlineKeyboardButton("No thanks", callback_data="rem:off")],
        ]
    )


def _ampm(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{suffix}" if m == 0 else f"{h12}:{m:02d}{suffix}"


# --- onboarding & settings -------------------------------------------------------


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _get_user(update, ctx)
    if not user.authorized:
        _set_state(ctx, "code")
        await update.message.reply_text("👋 Hi! I'm a calorie tracker bot. Please send the access code to start.")
        return
    await update.message.reply_text(
        f"Welcome back{', ' + escape(user.first_name) if user.first_name else ''}! 📸 Send me a photo of what you're eating.\n"
        "Tap /help to see everything I can do."
    )


async def _handle_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user: User, text: str) -> None:
    db = _db(ctx)
    attempts = db.get_user(user.user_id).code_attempts
    if attempts >= MAX_CODE_ATTEMPTS:
        await update.message.reply_text("Too many wrong codes. Ask the person who shared this bot to reset it for you.")
        return
    code = _settings(ctx).access_code
    if not hmac.compare_digest(text.strip().casefold().encode(), code.strip().casefold().encode()):
        db.set_user_field(user.user_id, "code_attempts", attempts + 1)
        await update.message.reply_text("Hmm, that's not the right code. Try again?")
        return
    _db(ctx).set_user_field(user.user_id, "authorized", 1)
    _clear_state(ctx)
    ctx.user_data["onboarding"] = True
    await update.message.reply_text(
        f"🎉 You're in{', ' + escape(user.first_name) if user.first_name else ''}!\n\n"
        "I'll estimate the calories in your meals from photos (or text / voice notes) and keep a running log.\n\n"
        "First: <b>what's your daily calorie goal?</b> Tap one, type a number, or let me help you work it out.",
        reply_markup=_goal_keyboard(),
    )
    _set_state(ctx, "goal")


async def cmd_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_auth(update, ctx)
    if not user:
        return
    arg = " ".join(ctx.args or []).strip()
    if arg.lower() in {"off", "none", "clear"}:
        _db(ctx).set_user_field(user.user_id, "goal_kcal", None)
        await update.message.reply_text("Goal cleared. I'll just track totals.")
        return
    if arg and (n := _parse_kcal(arg)):
        await _set_goal(update.message, ctx, user, n)
        return
    current = f"Your goal is <b>{user.goal_kcal:,} kcal/day</b>.\n\n" if user.goal_kcal else ""
    _set_state(ctx, "goal")
    await update.message.reply_text(
        current + "Pick a daily goal, type a number (e.g. 1650), or tap “Help me pick”.",
        reply_markup=_goal_keyboard(),
    )


async def _set_goal(msg: Message, ctx: ContextTypes.DEFAULT_TYPE, user: User, goal: int | None) -> None:
    db = _db(ctx)
    db.set_user_field(user.user_id, "goal_kcal", goal)
    _clear_state(ctx)
    user = db.get_user(user.user_id)
    text = f"✅ Daily goal set to <b>{goal:,} kcal</b>." if goal else "👍 No goal. I'll just track your totals."
    if goal and goal < GOAL_LOW_WARN:
        text += (
            "\n\n💛 Heads up: that's quite low. Most adults need at least ~1,200 kcal a day to stay healthy, "
            "so consider checking with a doctor or dietitian. You can change it anytime with /goal."
        )
    if goal:
        text += "\n\n" + render.day_line(user, _today_kcal(db, user))
    await msg.reply_text(text)
    if ctx.user_data.pop("onboarding", False):
        await msg.reply_text(
            "Would you like a short recap every evening? (You can change this anytime with /reminder.)",
            reply_markup=_reminder_keyboard(),
        )
        ctx.user_data["onboarding_reminder"] = True


async def _handle_goal_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user: User, text: str, state: str) -> bool:
    """Handle a reply to a goal question. Returns False if the text wasn't about the goal (caller logs it as food)."""
    if n := _parse_kcal(text):
        await _set_goal(update.message, ctx, user, n)
        return True
    if state == "goal" and _number_in(text) is not None:  # out of range: re-ask rather than log it as food
        await update.message.reply_text(
            f"Hmm, please send a daily goal between {GOAL_MIN:,} and {GOAL_MAX:,} kcal, e.g. 1600. (/cancel to skip)"
        )
        return True
    about_stats = re.search(r"\b(kg|cm|lbs?|years?|yo|age|weight|height|male|female|[MF])\b|\d(kg|cm|[mf])\b", text, re.I)
    nums = re.findall(r"\d[\d,]*", text)
    if state == "goal" and not about_stats and len(nums) == 1 and (n := _parse_kcal(nums[0])):
        await _set_goal(update.message, ctx, user, n)  # "1500 please", "I think 1600"
        return True
    if state == "goal" and not about_stats:
        _clear_state(ctx)  # e.g. they typed food after glancing at /goal
        return False
    if not await _check_cap(update.message, ctx, user):
        return True
    await update.effective_chat.send_action(ChatAction.TYPING)
    # Their stats may come over several messages ("female 24 160cm", then "55kg, desk job").
    stats = (ctx.user_data.get("goal_stats_text", "") + "\n" + text).strip()[-1500:]
    ctx.user_data["goal_stats_text"] = stats
    _set_state(ctx, state)  # keep waiting while they answer follow-up questions
    try:
        sug = await _est(ctx).suggest_goal(stats)
    except EstimatorError:
        await update.message.reply_text("Sorry, I couldn't work that out right now. Try typing a number like 1600.")
        return True
    if not sug.about_goal:
        _clear_state(ctx)
        return False
    if not sug.ok or not sug.goal_kcal:
        await update.message.reply_text(escape(render.clip(sug.explanation, 500)) + "\n\n(/cancel to skip)")
        return True
    sug.goal_kcal = max(GOAL_LOW_WARN, min(GOAL_MAX, sug.goal_kcal))
    ctx.user_data["suggested_goal"] = sug.goal_kcal  # so a typed "ok" accepts it
    await update.message.reply_text(
        f"{escape(sug.explanation)}\n\nSuggested goal: <b>{sug.goal_kcal:,} kcal/day</b>",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(f"✅ Use {sug.goal_kcal:,}", callback_data=f"goal:{sug.goal_kcal}"),
                InlineKeyboardButton("Enter my own", callback_data="goal:own"),
            ]]
        ),
    )
    return True


async def cmd_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_auth(update, ctx)
    if not user:
        return
    arg = " ".join(ctx.args or []).strip()
    if arg:
        if arg.lower() in {"off", "stop", "none"}:
            await _set_reminder(update.message, ctx, user, None)
            return
        if t := _parse_hhmm(arg):
            await _set_reminder(update.message, ctx, user, t)
            return
    current = f"Your recap is set for <b>{_ampm(user.reminder_time)}</b>.\n\n" if user.reminder_time else ""
    await update.message.reply_text(
        current + "When should I send your evening recap? (Or type e.g. <code>/reminder 21:30</code>.)",
        reply_markup=_reminder_keyboard(),
    )


async def _set_reminder(msg: Message, ctx: ContextTypes.DEFAULT_TYPE, user: User, hhmm: str | None) -> None:
    _db(ctx).set_user_field(user.user_id, "reminder_time", hhmm)
    user = _db(ctx).get_user(user.user_id)
    schedule_reminder(ctx.application, user)
    if hhmm:
        await msg.reply_text(f"⏰ I'll send you a recap every day at <b>{_ampm(hhmm)}</b>.")
    else:
        await msg.reply_text("🔕 Evening recap turned off.")
    if ctx.user_data.pop("onboarding_reminder", False):
        await msg.reply_text("All set! Here's how I work 👇")
        await msg.reply_text(render.help_text())


# --- reminders -----------------------------------------------------------------


def schedule_reminder(app: Application, user: User) -> None:
    name = f"reminder:{user.user_id}"
    for job in app.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    if not user.reminder_time:
        return
    h, m = map(int, user.reminder_time.split(":"))
    app.job_queue.run_daily(
        send_reminder,
        time=time(h, m, tzinfo=user.zone),
        name=name,
        data=user.user_id,
        chat_id=user.chat_id,
        # If the bot was stalled at reminder time, still send it once it recovers (within 1h) instead of skipping.
        job_kwargs={"misfire_grace_time": 3600, "coalesce": True},
    )


async def send_reminder(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(ctx)
    user = db.get_user(ctx.job.data)
    if not user or not user.authorized or not user.reminder_time:
        return
    h, m = map(int, user.reminder_time.split(":"))
    due = datetime.now(user.zone).replace(hour=h, minute=m, second=0, microsecond=0)
    if due > datetime.now(user.zone) + timedelta(minutes=1):
        due -= timedelta(days=1)  # a 23:30 recap running after midnight
    late = datetime.now(user.zone) - due > timedelta(minutes=2)
    if late:
        # Running late (e.g. after a restart): let Telegram's backlog (e.g. a dinner photo) arrive and be logged first.
        await asyncio.sleep(REMINDER_SETTLE_S)
        processor = ctx.application.update_processor
        for _ in range(120):  # then wait (up to 2 min) until the chat's queued messages have all been handled
            if not hasattr(processor, "busy") or not processor.busy(user.chat_id):
                break
            await asyncio.sleep(1)
    async with ctx.bot_data["chat_locks"][user.chat_id]:
        # A late recap outside 07:00-23:00 arrives without a notification sound.
        quiet = late and not (time(7) <= datetime.now(user.zone).time() < time(23))
        await _send_recap(ctx, db.get_user(user.user_id) or user, quiet=quiet)


async def _send_recap(ctx: ContextTypes.DEFAULT_TYPE, user: User, quiet: bool) -> None:
    db = _db(ctx)
    today = user.today()
    meals = db.meals_on(user.user_id, today)
    if not meals:
        text = "🌙 Evening check-in: nothing logged today yet. What did you eat? Send a photo or just type it 📸"
    else:
        text = render.day_summary(user, today, meals, title="🌙 <b>Your day so far</b>")
        last_local = max(m.eaten_at for m in meals).astimezone(user.zone)
        now_local = datetime.now(user.zone)
        if now_local - last_local > timedelta(hours=5):
            text += f"\n\nNothing logged since {last_local:%-I:%M%p}. Anything to add?"
        elif user.goal_kcal:
            left = user.goal_kcal - sum(m.kcal for m in meals)
            if left > 300:
                text += f"\n\nYou still have room for ~{render.fmt_kcal(left)} kcal if you're hungry."
    await ctx.bot.send_message(user.chat_id, text, disable_notification=quiet)


# --- logging meals -----------------------------------------------------------------


def _over_cap(ctx: ContextTypes.DEFAULT_TYPE, user: User) -> bool:
    db, cap = _db(ctx), _settings(ctx).daily_ai_cap
    return db.usage(user.user_id, user.today()) >= cap or db.total_usage(user.today()) >= cap * 2


async def _check_cap(msg: Message, ctx: ContextTypes.DEFAULT_TYPE, user: User) -> bool:
    db, settings = _db(ctx), _settings(ctx)
    calls = db.bump_usage(user.user_id, user.today())
    # The global cap protects the API budget even if the access code leaks to strangers.
    if calls > settings.daily_ai_cap or db.total_usage(user.today()) > settings.daily_ai_cap * 2:
        await msg.reply_text("😅 I've hit today's limit for AI estimates. Try again tomorrow!")
        return False
    return True


async def _download(ctx: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    f = await ctx.bot.get_file(file_id)
    return bytes(await f.download_as_bytearray())


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image/heic"
    return "image/jpeg"


def _remember(db: DB, user: User, fact: str) -> str:
    """Save a habit; returns the fact if it was new (so the card can mention it), else ''."""
    fact = render.clip(fact.strip().rstrip("."), 150)
    if not fact or fact.lower() in user.notes.lower():
        return ""
    lines = [ln for ln in user.notes.splitlines() if ln.strip()] + [f"- {fact}"]
    db.set_user_field(user.user_id, "notes", "\n".join(lines[-MAX_NOTES_LINES:]))
    return fact


async def log_meal(
    ctx: ContextTypes.DEFAULT_TYPE,
    user: User,
    trigger: Message,
    *,
    source: str,
    file_ids: list[tuple[str, str]],
    text: str = "",
    dedupe_key: str | None = None,
    manual_retry: bool = False,
    sent_at: datetime | None = None,
    recent: Meal | None = None,
    allow_correction: bool = True,
    own_message_ids: list[int] | None = None,
    replaces: Meal | None = None,
) -> None:
    """Estimate and save a meal. file_ids holds (telegram_file_id, mime_type) pairs.
    sent_at is when the user sent it (defaults to trigger.date; a retry's trigger is the bot's own message)."""
    db = _db(ctx)
    sent_at = sent_at or trigger.date
    _prune_tokens(ctx)
    if dedupe_key and (dup := db.recent_duplicate(user.user_id, dedupe_key, DUPLICATE_WINDOW)):
        await trigger.reply_text(
            f"👆 I already logged this photo as <b>{escape(dup.title)}</b> ({render.fmt_kcal(dup.kcal)} kcal), "
            "so I won't count it twice."
        )
        return
    if not await _check_cap(trigger, ctx, user):
        return
    waiting = {"photo": "🔍 Looking at your food…", "voice": "🎧 Listening…"}.get(source, "🤔 Thinking…")
    placeholder = await trigger.reply_text(waiting)
    await trigger.chat.send_action(ChatAction.TYPING)
    # For typed/spoken messages shortly after a meal, let the model spot "actually only half" etc.
    # recent: a meal this may be correcting (passed in for a reply to an old Fix prompt).
    # allow_correction=False when refine already decided this is a separate meal; otherwise the two
    # calls could disagree forever and burn the daily AI budget.
    if not allow_correction:
        recent = None
    elif recent is None and source != "photo":
        last = db.last_meal(user.user_id)
        if last and timedelta(0) <= sent_at - last.eaten_at <= RECENT_MEAL_WINDOW:
            recent = last
    try:
        media = [MediaPart(await _download(ctx, fid), mime) for fid, mime in file_ids]
        est = await asyncio.wait_for(
            _est(ctx).estimate(text=text, media=media, notes=user.notes, recent_meal=_describe(recent) if recent else ""),
            AI_DEADLINE_S,  # retries inside the estimator must not freeze the chat for minutes
        )
        if est.kind == "new_meal" and not est.items and recent:
            # Shown the previous meal, the model only classified; ask again for the new food itself.
            db.bump_usage(user.user_id, user.today())
            est = await asyncio.wait_for(
                _est(ctx).estimate(text=text, media=media, notes=user.notes), AI_DEADLINE_S
            )
        if est.kind == "new_meal":
            est.kind = "food"
    except Exception as exc:  # EstimatorError or Telegram download failure
        log.exception("estimate failed: %s", exc)
        await _report_failure(ctx, exc)
        token = secrets.token_hex(4)
        ctx.user_data.setdefault("retry", {})[token] = {
            "source": source, "file_ids": file_ids, "text": text, "dedupe_key": dedupe_key, "sent_at": sent_at,
            "own_message_ids": sorted({*(own_message_ids or []), *([trigger.message_id] if not trigger.from_user.is_bot else [])}),
            "replaces_id": replaces.id if replaces else None,
            "recent_id": recent.id if recent and allow_correction else None,
        }
        await _safe_edit(
            placeholder,
            _failure_text(exc),
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Try again", callback_data=f"retry:{token}")]]),
        )
        return
    ctx.bot_data["failures"] = 0

    if est.kind == "correction":
        if recent:
            db.bump_usage(user.user_id, user.today())  # the refine is another Gemini call
            await refine_meal(ctx, user, recent, trigger, text or est.user_said, placeholder=placeholder)
        else:
            await _safe_edit(
                placeholder,
                "✏️ To change a meal, swipe left on its card (or tap ✏️ Fix) and tell me what's different.",
            )
        return

    own_ids = sorted({*(own_message_ids or []), *([trigger.message_id] if not trigger.from_user.is_bot else [])})
    save = {"source": source, "file_ids": file_ids, "text": text, "dedupe_key": dedupe_key,
            "sent_at": sent_at, "own_message_ids": own_ids}
    if est.kind == "question" and est.items and source == "photo":
        est.kind = "food"  # a photo of a meal captioned "is this healthy?": log it; the note answers
    if est.kind != "food" or not est.items:
        if est.kind == "question" and source != "photo":
            question = text or est.user_said
            if est.items and est.note:
                reply = est.note  # already answered alongside the estimate; keeps answer and button consistent
            else:
                if question:
                    db.bump_usage(user.user_id, user.today())  # answering is a second Gemini call
                reply = await _answer(ctx, user, question) if question else est.note
            keyboard = None
            echoes_recent = recent is not None and SequenceMatcher(
                None, (est.title or "").lower(), recent.title.lower()
            ).ratio() > 0.75
            if est.items and not echoes_recent:  # "is this too oily?" about the meal just logged: no button
                # "how many calories in a curry puff?" names food they may not have eaten: never log it
                # silently, but make logging it one tap if they did.
                token = secrets.token_hex(4)
                ctx.user_data.setdefault("pending_log", {})[token] = {**save, "est": est.model_dump_json()}
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
                    f"➕ Log {render.clip(est.title or est.items[0].name, 30)} ({render.fmt_kcal(est.kcal)} kcal)",
                    callback_data=f"logq:{token}",
                )]])
            note = "\n\n" + NOT_LOGGED_NOTE if keyboard else ""
            await _safe_edit(placeholder, escape(render.clip(reply, 1500)) + note, keyboard)
            return
        fallback = {
            "not_food": "🤔 I don't see any food or drink in that. Send a photo of your meal, or describe it in words.",
            "unclear": "🤔 I can't tell what that is. Could you describe it in a few words?",
        }.get(est.kind, "🤔 I'm not sure what to log. Describe the food, e.g. “bowl of laksa”.")
        await _safe_edit(placeholder, escape(render.clip(est.note, 500)) if est.note else fallback)
        return

    await _save_meal(ctx, user, est, placeholder, manual_retry=manual_retry, replaces=replaces, **save)


async def _save_meal(
    ctx: ContextTypes.DEFAULT_TYPE,
    user: User,
    est: Estimate,
    placeholder: Message,
    *,
    source: str,
    file_ids: list[tuple[str, str]],
    text: str,
    dedupe_key: str | None,
    sent_at: datetime,
    own_message_ids: list[int],
    manual_retry: bool = False,
    replaces: Meal | None = None,
) -> None:
    """Store an estimated meal and turn the placeholder into its card.
    replaces: a meal this new photo stands in for; it keeps that meal's time and type and is deleted
    only once the new one is safely saved."""
    db = _db(ctx)
    title = est.title or est.items[0].name
    if manual_retry and not replaces and (twin := _relogged_since(db, user, title, sent_at)):
        # e.g. a photo failed, they re-sent it from the gallery, then hours later tapped the old Try again
        await _safe_edit(
            placeholder,
            f"👍 You already logged that again as <b>{escape(render.clip(twin.title, 60))}</b> "
            f"({render.fmt_kcal(twin.kcal)} kcal), so I won't count it twice.",
        )
        return
    eaten_at = sent_at.astimezone(UTC)
    if replaces:
        eaten_at = replaces.eaten_at
    elif est.yesterday and not _in_night_overlap(eaten_at, user):
        eaten_at -= timedelta(days=1)
    meal_id = db.add_meal(
        user,
        eaten_at=eaten_at,
        title=title,
        kcal=est.kcal,
        protein_g=est.protein_g,
        carbs_g=est.carbs_g,
        fat_g=est.fat_g,
        estimate_json=est.model_dump_json(),
        source=source,
        photo_file_ids=[fid for fid, mime in file_ids if mime.startswith("image/")],
        user_text=text or None,
        meal_type=replaces.meal_type if replaces else None if est.meal_type_hint == "none" else est.meal_type_hint,
        dedupe_key=dedupe_key,
    )
    if replaces:
        db.update_meal(replaces.id, deleted=1)
        if replaces.bot_message_id:
            try:
                await ctx.bot.edit_message_text(
                    f"🔄 Replaced <s>{escape(replaces.title)}</s> ({render.fmt_kcal(replaces.kcal)} kcal) with your new photo.",
                    chat_id=placeholder.chat_id, message_id=replaces.bot_message_id,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        "↩️ Undo replace", callback_data=f"unrep:{replaces.id}:{meal_id}")]]),
                )
            except TelegramError:  # cosmetic; the change is already saved
                pass
    db.update_meal(meal_id, bot_message_id=placeholder.message_id)
    # The user's own message(s) too, so swiping-to-reply on their photo corrects this meal.
    for mid in own_message_ids:
        db.link_message(user.user_id, mid, meal_id)
    remembered = _remember(db, user, est.remember) if est.remember else ""
    meal = db.get_meal(meal_id, user.user_id)
    late = not manual_retry and (datetime.now(UTC) - sent_at).total_seconds() > LATE_REPLY_S
    prefix = "⏳ <i>Sorry for the slow reply, I was offline. Logged at the time you sent it.</i>\n\n" if late else ""
    # After a slow reply they may have retyped the meal 20+ min later, so look further back then.
    if twin := _likely_repeat(db, user, meal, timedelta(minutes=60 if late else 10)):
        # They may have re-sent or retyped the same meal (e.g. while waiting for a slow reply).
        prefix += (
            f"🔁 <i>Looks like a repeat of <b>{escape(render.clip(twin.title, 50))}</b> "
            f"(sent {twin.eaten_at.astimezone(user.zone):%-I:%M%p}). If so, tap 🗑 Delete below.</i>\n\n"
        )
    await _show_card(placeholder, lambda: prefix + _card(db, user, meal, est, remembered), meal)


def _prune_tokens(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Button state (Try again / Log it / Replace) older than 3 days is never going to be tapped."""
    cutoff = datetime.now(UTC) - timedelta(days=3)
    for bucket in ("retry", "pending_log", "photo_reply"):
        jobs = ctx.user_data.get(bucket, {})
        for token in [t for t, job in jobs.items() if job.get("sent_at") and job["sent_at"] < cutoff]:
            del jobs[token]


def _already_relogged(db: DB, user: User, job: dict) -> Meal | None:
    """For a failed text log: a meal they sent later with (nearly) the same words."""
    if job["source"] != "text" or not job["text"]:
        return None
    sent = job["sent_at"]
    days = {log_date(sent.astimezone(user.zone)), user.today()}
    for day in days:
        for m in db.meals_on(user.user_id, day):
            if sent <= m.eaten_at <= sent + timedelta(hours=1) and m.user_text and SequenceMatcher(
                None, m.user_text.lower(), job["text"].lower()
            ).ratio() > 0.8:
                return m
    return None


def _relogged_since(db: DB, user: User, title: str, sent: datetime) -> Meal | None:
    """A meal with a near-identical title that they sent in the 3 hours after `sent`."""
    for day in {log_date(sent.astimezone(user.zone)), user.today()}:
        for m in db.meals_on(user.user_id, day):
            if sent < m.eaten_at <= sent + timedelta(hours=3) and SequenceMatcher(
                None, m.title.lower(), title.lower()
            ).ratio() > 0.75:
                return m
    return None


def _likely_repeat(db: DB, user: User, meal: Meal, window: timedelta = timedelta(minutes=10)) -> Meal | None:
    """Another meal sent within `window` with a near-identical title (a re-send while they waited)."""
    for other in db.meals_on(user.user_id, meal.local_date):
        if other.id == meal.id or abs(other.eaten_at - meal.eaten_at) > window:
            continue
        same_name = SequenceMatcher(None, other.title.lower(), meal.title.lower()).ratio() > 0.75
        if same_name:
            return other
    return None


def _moved_to_yesterday(meal: Meal, est: Estimate, user: User) -> dict:
    """Fields to update when a correction says 'this was yesterday' about a meal logged for today."""
    if not est.yesterday or meal.local_date != user.today() or _in_night_overlap(meal.eaten_at, user):
        return {}
    moved = meal.eaten_at - timedelta(days=1)
    return {"eaten_at": moved.isoformat(timespec="seconds"), "local_date": (meal.local_date - timedelta(days=1)).isoformat()}


def _in_night_overlap(when: datetime, user: User) -> bool:
    """00:00-04:00 still belongs to the previous log day, so 'yesterday' then means that same log day."""
    return when.astimezone(user.zone).hour < 4


def _describe(meal: Meal) -> str:
    est = Estimate.model_validate_json(meal.estimate_json).scaled(meal.portion)
    items = ", ".join(f"{i.name} ({i.portion})" for i in est.items[:8])
    # So a typed "yes, all of it" makes sense; other notes are answers or tips, not questions.
    asked = f' (the card asked the user: "{est.note}")' if "?" in est.note else ""
    return f'"{meal.title}", {round(meal.kcal)} kcal: {items}{asked}'


def _failure_text(exc: Exception) -> str:
    reason = getattr(exc, "reason", "network")
    if reason == "quota":
        return "😓 The AI service is busy or its limit was reached. Try again in a little while."
    if reason == "rejected":
        return "😓 I couldn't process that one. Try another photo, or describe the food in words."
    return "😓 Sorry, I couldn't analyse that just now (connection hiccup)."


async def _report_failure(ctx: ContextTypes.DEFAULT_TYPE, exc: Exception) -> None:
    """After a few failures in a row, DM the owner (if configured) so a dead key or spent budget gets noticed."""
    ctx.bot_data["failures"] = ctx.bot_data.get("failures", 0) + 1
    owner = _settings(ctx).owner_chat_id
    if owner and ctx.bot_data["failures"] == 3:
        try:
            await ctx.bot.send_message(
                owner,
                f"⚠️ Calorpie: 3 Gemini failures in a row. Last error ({getattr(exc, 'reason', '?')}): "
                f"{escape(render.clip(str(exc), 300))}",
            )
        except Exception:
            log.exception("could not alert owner")


async def _show_card(placeholder: Message, build_text, meal: Meal) -> None:
    """Put the meal card on the placeholder. The meal is already saved, so never fail loudly here
    (not even while building the text): a generic 'try again' would make them resend and log it twice."""
    try:
        await _safe_edit(placeholder, build_text(), render.meal_keyboard(meal))
    except Exception:
        log.exception("could not render meal card %s", meal.id)
        try:
            await _safe_edit(
                placeholder,
                f"✅ Logged {escape(render.clip(meal.title, 80))}: about {render.fmt_kcal(meal.kcal)} kcal. "
                "(See /today for details.)",
                render.meal_keyboard(meal),
            )
        except Exception:
            log.exception("fallback card failed too")


def _card(db: DB, user: User, meal: Meal, est: Estimate | None = None, remembered: str = "") -> str:
    est = est or Estimate.model_validate_json(meal.estimate_json)
    return render.meal_card(
        meal, est, user, _today_kcal(db, user), logged_for=meal.local_date, remembered=remembered
    )


async def _answer(ctx: ContextTypes.DEFAULT_TYPE, user: User, question: str) -> str:
    db = _db(ctx)
    today = user.today()
    meals = db.meals_on(user.user_id, today)
    week = db.daily_totals(user.user_id, today - timedelta(days=6), today)
    logged = [v["kcal"] for v in week.values() if v["n"]]
    eaten = sum(m.kcal for m in meals)
    context = "\n".join(
        [
            f"Local time: {datetime.now(user.zone):%A %H:%M}",
            f"Daily goal: {user.goal_kcal or 'not set'} kcal",
            f"Eaten today: {eaten:.0f} kcal"
            + (f" ({user.goal_kcal - eaten:.0f} left)" if user.goal_kcal else ""),
            "Today's meals: " + ("; ".join(f"{m.meal_type}: {m.title} {m.kcal:.0f} kcal" for m in meals) or "none"),
            f"7-day average on logged days: {sum(logged) / len(logged):.0f} kcal" if logged else "No meals in the last 7 days",
            f"Known habits: {user.notes or 'none'}",
        ]
    )
    try:
        return await _est(ctx).answer(question, context)
    except EstimatorError:
        return "Sorry, I couldn't answer that right now. Try again in a bit."


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_auth(update, ctx)
    if not user:
        return
    msg = update.message
    _clear_state(ctx)  # a new photo cancels any pending goal question
    if msg.photo:
        fid, uid, mime = msg.photo[-1].file_id, msg.photo[-1].file_unique_id, "image/jpeg"
    else:  # image sent as a file
        fid, uid, mime = msg.document.file_id, msg.document.file_unique_id, msg.document.mime_type or "image/jpeg"

    if not msg.media_group_id:
        target = await _reply_target(ctx, user, msg)
        if target == "handled":
            return
        if isinstance(target, Meal):
            # A photo sent in reply to a meal (e.g. a clearer shot after tapping Fix): ask, don't guess.
            token = secrets.token_hex(4)
            ctx.user_data.setdefault("photo_reply", {})[token] = {
                "source": "photo", "file_ids": [(fid, mime)], "text": msg.caption or "", "dedupe_key": uid,
                "sent_at": msg.date, "own_message_ids": [msg.message_id], "replaces": target.id,
            }
            title = escape(render.clip(target.title, 40))
            await msg.reply_text(
                f"Is this photo instead of <b>{title}</b>, or something else you ate?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Replace it", callback_data=f"phrep:{token}"),
                    InlineKeyboardButton("➕ Something else", callback_data=f"phnew:{token}"),
                ]]),
            )
            return

    if msg.media_group_id:
        albums = ctx.bot_data["albums"]
        key = f"{msg.chat_id}:{msg.media_group_id}"
        album = albums.get(key)
        if album is None:
            album = albums[key] = {
                "user_id": user.user_id, "msg": msg, "files": [], "uids": [], "caption": "", "done": asyncio.Event(), "msg_ids": [],
            }
            ctx.job_queue.run_once(
                _flush_album, ALBUM_WAIT_S, data=key, user_id=user.user_id, chat_id=msg.chat_id,
                job_kwargs={"misfire_grace_time": None},  # run however late; never drop
            )
        album["files"].append((fid, mime))
        album["uids"].append(uid)
        album["msg_ids"].append(msg.message_id)
        if msg.caption:
            album["caption"] = msg.caption
        return

    await log_meal(ctx, user, msg, source="photo", file_ids=[(fid, mime)], text=msg.caption or "", dedupe_key=uid)


async def _flush_album(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    albums = ctx.bot_data["albums"]
    album = albums.get(ctx.job.data)
    if not album:
        return
    try:
        user = _db(ctx).get_user(album["user_id"])
        async with ctx.bot_data["chat_locks"][album["msg"].chat_id]:
            albums.pop(ctx.job.data, None)
            if len(album["files"]) > 6:
                await album["msg"].reply_text(
                    f"📸 I can look at up to 6 photos at once, so I used the first 6 of {len(album['files'])}. "
                    "Send the rest separately if they show other food."
                )
            await log_meal(
                ctx, user, album["msg"], source="photo", file_ids=album["files"][:6], text=album["caption"],
                dedupe_key=",".join(sorted(album["uids"])), own_message_ids=album["msg_ids"],
            )
    except TelegramError:
        log.exception("Album failed")
        try:
            await ctx.bot.send_message(album["msg"].chat_id, "😓 Sorry, I couldn't process those photos. Please send them again.")
        except TelegramError:
            pass
    finally:
        albums.pop(ctx.job.data, None)
        album["done"].set()


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_auth(update, ctx)
    if not user:
        return
    msg = update.message
    media = msg.voice or msg.audio
    duration = media.duration
    seconds = duration.total_seconds() if hasattr(duration, "total_seconds") else (duration or 0)
    if seconds > 120:
        await msg.reply_text("That voice note is a bit long. Keep it under 2 minutes please 🙏")
        return
    mime = media.mime_type or "audio/ogg"
    target = await _reply_target(ctx, user, msg)
    if target == "handled":
        return
    _clear_state(ctx)
    # A voice reply to a meal is transcribed with that meal as context: a correction refines it.
    await log_meal(
        ctx, user, msg, source="voice", file_ids=[(media.file_id, mime)], text=msg.caption or "",
        recent=target.meal if isinstance(target, StaleFix) else target if isinstance(target, Meal) else None,
    )


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    text = msg.text.strip()
    user = await _get_user(update, ctx)

    if not user.authorized:
        # Unauthorised users only ever get the code prompt, so any text is a code attempt.
        await _handle_code(update, ctx, user, text)
        return

    if (
        _state(ctx) in {"goal", "goal_stats"}
        and (suggested := ctx.user_data.get("suggested_goal"))
        and (ACK_RE.fullmatch(text) or YES_RE.fullmatch(text))
    ):
        await _set_goal(msg, ctx, user, suggested)
        return

    if ACK_RE.fullmatch(text):  # "thanks", "ok", 👍 (even as a reply to a card): no need to spend an AI call
        await msg.reply_text("😊")
        return

    # Correction: a reply to a meal card, to an older version of it, or to the Fix prompt.
    # (Deliberately reply-based, not a remembered "fixing" state: a stale state could overwrite
    # a different meal hours later.)
    target = await _reply_target(ctx, user, msg)
    if target == "handled":
        return
    if isinstance(target, Meal):
        _clear_state(ctx)
        await refine_meal(ctx, user, target, msg, text)
        return
    if isinstance(target, StaleFix):  # let the model decide: late correction of that meal, or new food
        _clear_state(ctx)
        await log_meal(ctx, user, msg, source="text", file_ids=[], text=text, recent=target.meal)
        return

    state = _state(ctx)
    if state in {"goal", "goal_stats"}:
        if await _handle_goal_text(update, ctx, user, text, state):
            return

    await log_meal(ctx, user, msg, source="text", file_ids=[], text=text)


FIX_PROMPT = "What should I change about"
NOT_LOGGED_NOTE = "<i>(Not logged. Tap below if you ate it.)</i>"


@dataclass
class StaleFix:
    """A reply to an old Fix prompt: maybe a late correction, maybe new food typed into a leftover draft."""

    meal: Meal


FIX_PROMPT_TTL = timedelta(minutes=15)  # past this, a reply to a Fix prompt may be new food, so the model decides


async def _reply_target(ctx: ContextTypes.DEFAULT_TYPE, user: User, msg: Message) -> Meal | StaleFix | str | None:
    """The meal a message corrects, if it replies to a meal card (or an older version, or a Fix prompt).
    Returns "handled" when the reply was answered here, None when the message should be logged as new food.

    Replying to a card takes a deliberate swipe, so it always counts. A Fix prompt is different: its
    ForceReply attaches the *next* message to it, so if they ignored the prompt, food typed hours later
    would arrive as a "correction" and overwrite the wrong meal. Only fresh Fix prompts count."""
    reply = msg.reply_to_message
    if not reply or not reply.from_user:
        return None
    from_bot = reply.from_user.id == ctx.bot.id
    if not from_bot and reply.from_user.id != user.user_id:
        return None
    db = _db(ctx)
    # Cards, older versions of cards, Fix prompts, and the user's own messages that logged the meal.
    meal = db.meal_by_bot_message(user.user_id, reply.message_id, include_deleted=True)
    if meal is None:
        return None
    if from_bot and (reply.text or "").startswith(FIX_PROMPT) and msg.date - reply.date > FIX_PROMPT_TTL:
        return StaleFix(meal)
    if meal.deleted:
        if msg.photo or msg.document:
            return None  # a fresh photo after deleting a bad estimate: just log it
        await msg.reply_text(
            f"<b>{escape(render.clip(meal.title, 60))}</b> was deleted. Tap ↩️ Restore (or ↩️ Undo replace) on it first if you want to change it."
        )
        return "handled"
    return meal


async def refine_meal(
    ctx: ContextTypes.DEFAULT_TYPE,
    user: User,
    meal: Meal,
    msg: Message,
    correction: str,
    placeholder: Message | None = None,
) -> None:
    db = _db(ctx)
    if placeholder is None:
        if not await _check_cap(msg, ctx, user):
            return
        placeholder = await msg.reply_text("✏️ Updating…")
    else:
        await _safe_edit(placeholder, f"✏️ Updating {escape(render.clip(meal.title, 60))}…")
    try:
        media = [MediaPart(data, _image_mime(data)) for data in [await _download(ctx, fid) for fid in meal.photo_file_ids]]
        # Refine what they currently see (portion applied) so a ½ tap isn't silently undone.
        previous = Estimate.model_validate_json(meal.estimate_json).scaled(meal.portion)
        est = await asyncio.wait_for(
            _est(ctx).refine(previous, correction, media=media, notes=user.notes, original_text=meal.user_text or ""),
            AI_DEADLINE_S,
        )
    except Exception as exc:
        log.exception("refine failed: %s", exc)
        await _report_failure(ctx, exc)
        await _safe_edit(placeholder, _failure_text(exc) + "\n\nReply to the meal again to retry the correction.")
        return
    ctx.bot_data["failures"] = 0
    if db.get_meal(meal.id, user.user_id) is None:  # deleted while we were waiting on Gemini
        await _safe_edit(placeholder, "That meal was deleted, so I didn't change anything.")
        return
    if est.kind == "new_meal":
        await _safe_edit(placeholder, f"That sounds like a separate meal, so I'll log it on its own instead of changing {escape(render.clip(meal.title, 60))}.")
        await log_meal(ctx, user, msg, source="text", file_ids=[], text=correction, allow_correction=False)
        return
    if est.kind == "question":  # "how much protein is this?" as a reply: answer, change nothing
        await _safe_edit(placeholder, escape(render.clip(est.note or "I'm not sure, sorry.", 1000)))
        return
    if est.kind != "food" or not est.items:
        await _safe_edit(
            placeholder,
            escape(render.clip(est.note, 400) if est.note else "That would leave nothing to log.")
            + f"\n\nRemove <b>{escape(render.clip(meal.title, 60))}</b> instead?",
            InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Delete it", callback_data=f"del:{meal.id}")]]),
        )
        return

    old_kcal = meal.kcal
    db.update_meal(
        meal.id,
        title=est.title or meal.title,
        kcal=round(est.kcal),
        protein_g=round(est.protein_g, 1),
        carbs_g=round(est.carbs_g, 1),
        fat_g=round(est.fat_g, 1),
        estimate_json=est.model_dump_json(),
        portion=1.0,
        bot_message_id=placeholder.message_id,
        **({"meal_type": est.meal_type_hint} if est.meal_type_hint != "none" else {}),
        **_moved_to_yesterday(meal, est, user),
    )
    remembered = _remember(db, user, est.remember) if est.remember else ""
    # Retire the old card so only the newest version has buttons; replies to it still reach this meal.
    if meal.bot_message_id:
        db.link_message(user.user_id, meal.bot_message_id, meal.id)
        try:
            await ctx.bot.edit_message_text(
                f"↪️ <s>{escape(meal.title)} · {render.fmt_kcal(old_kcal)} kcal</s> (corrected below)",
                chat_id=msg.chat_id,
                message_id=meal.bot_message_id,
            )
        except TelegramError:  # cosmetic; the change is already saved
            pass
    meal = db.get_meal(meal.id, user.user_id)
    header = f"✅ Updated: {render.fmt_kcal(old_kcal)} → <b>{render.fmt_kcal(meal.kcal)} kcal</b>\n\n"
    await _show_card(placeholder, lambda: header + _card(db, user, meal, est, remembered), meal)


# --- callbacks ---------------------------------------------------------------------


TOKEN_BUCKETS = {"logq": "pending_log", "phrep": "photo_reply", "phnew": "photo_reply", "retry": "retry"}


async def _run_token_action(ctx: ContextTypes.DEFAULT_TYPE, query: CallbackQuery, user: User, kind: str, job: dict) -> None:
    """Carry out a Log it / Replace / Something else / Try again tap. Raises only before a meal is saved."""
    db = _db(ctx)
    if kind == "logq":
        await _ack(query, "Logging…")
        est = Estimate.model_validate_json(job.pop("est"))
        est.kind = "food"
        answer = await query.message.reply_text("📝 Logging it…")
        logged = query.message.text_html.replace(NOT_LOGGED_NOTE, "<i>✅ Logged below.</i>")
        await _safe_edit(query, logged)
        await _save_meal(ctx, user, est, answer, manual_retry=True, **job)
    elif kind in {"phrep", "phnew"}:
        await _ack(query)
        await _drop_buttons(query)
        old_id = job.pop("replaces")
        old = db.get_meal(old_id, user.user_id) if kind == "phrep" else None
        await log_meal(ctx, user, query.message, manual_retry=True, allow_correction=False, replaces=old, **job)
    else:  # retry
        if twin := _already_relogged(db, user, job):
            await _ack(query)
            await _safe_edit(query, f"👍 You already logged that again as <b>{escape(render.clip(twin.title, 60))}</b>.")
            return
        await _ack(query, "Retrying…")
        try:
            await query.message.delete()
        except TelegramError:  # older than 48h can't be deleted, or a timeout: just drop the button
            await _drop_buttons(query)
        rid = job.pop("replaces_id", None)
        recent_id = job.pop("recent_id", None)
        await log_meal(
            ctx, user, query.message.reply_to_message or query.message, manual_retry=True,
            replaces=db.get_meal(rid, user.user_id) if rid else None,
            recent=db.get_meal(recent_id, user.user_id) if recent_id else None, **job,
        )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    db = _db(ctx)
    user = db.get_user(query.from_user.id)
    if not user or not user.authorized:
        await _ack(query, "Please send the access code first.")
        return
    kind, _, rest = query.data.partition(":")

    if kind == "goal":
        await _ack(query)
        await _drop_buttons(query)
        if rest == "help":
            _clear_state(ctx)
            _set_state(ctx, "goal_stats")
            await query.message.reply_text(
                "Tell me a bit about yourself in one message, e.g.\n"
                "<i>“female, 24, 160 cm, 55 kg, desk job + gym twice a week, want to lose a little”</i>"
            )
        elif rest == "own":
            _set_state(ctx, "goal")
            await query.message.reply_text("OK! Type your daily goal as a number, e.g. 1650.")
        elif rest == "none":
            await _set_goal(query.message, ctx, user, None)
        else:
            goal = int(rest)
            if GOAL_MIN <= goal <= GOAL_MAX:
                await _set_goal(query.message, ctx, user, goal)
        return

    if kind == "rem":
        await _ack(query)
        await _drop_buttons(query)
        await _set_reminder(query.message, ctx, user, None if rest == "off" else rest)
        return

    if kind in TOKEN_BUCKETS:
        bucket = TOKEN_BUCKETS[kind]
        job = ctx.user_data.get(bucket, {}).pop(rest, None)
        done = ctx.user_data.setdefault("done_tokens", [])
        if not job:
            if rest in done:  # a double tap: the first one already worked
                await _ack(query, "Already done 👍")
            else:
                await _ack(query, "That one expired. Please send it again.", show_alert=True)
            return
        saved = dict(job)
        if kind != "logq" and _over_cap(ctx, user):  # these need Gemini; keep the button for tomorrow
            ctx.user_data.setdefault(bucket, {})[rest] = saved
            await _ack(query, "😅 I've hit today's limit for AI estimates. Tap this again tomorrow!", show_alert=True)
            return
        try:
            await _run_token_action(ctx, query, user, kind, job)
        except Exception:
            # A Telegram hiccup before the meal was saved: keep the button usable for another tap.
            ctx.user_data.setdefault(bucket, {})[rest] = saved
            raise
        done.append(rest)
        del done[:-30]
        return

    meal_id_s, _, arg = rest.partition(":")
    try:
        meal_id = int(meal_id_s)
    except ValueError:
        await _ack(query)
        return

    if kind == "sundel":
        db.conn.execute("UPDATE meals SET deleted = 0 WHERE id = ? AND user_id = ?", (meal_id, user.user_id))
        db.conn.commit()
        meal = db.get_meal(meal_id, user.user_id)
        await _ack(query, "Restored")
        if meal:
            meals = db.meals_on(user.user_id, meal.local_date)
            await _safe_edit(query, render.day_summary(user, meal.local_date, meals), render.summary_keyboard(meals))
        return

    if kind == "unrep":  # put the old meal back and drop its replacement, so nothing is counted twice
        new = db.get_meal(int(arg), user.user_id) if arg.isdigit() else None
        db.conn.execute("UPDATE meals SET deleted = 0 WHERE id = ? AND user_id = ?", (meal_id, user.user_id))
        db.conn.commit()
        old = db.get_meal(meal_id, user.user_id)
        if not old:
            await _ack(query, "Couldn't undo that one.")
            return
        if new:
            db.update_meal(new.id, deleted=1)
            if new.bot_message_id:
                try:
                    await ctx.bot.edit_message_text(
                        f"↩️ Kept the original instead of <s>{escape(new.title)}</s>.",
                        chat_id=query.message.chat_id, message_id=new.bot_message_id,
                    )
                except TelegramError:
                    pass
        db.update_meal(meal_id, bot_message_id=query.message.message_id)
        await _ack(query, "Undone")
        await _safe_edit(query, _card(db, user, old), render.meal_keyboard(old))
        return

    if kind == "undel":
        db.conn.execute("UPDATE meals SET deleted = 0 WHERE id = ? AND user_id = ?", (meal_id, user.user_id))
        db.conn.commit()
        meal = db.get_meal(meal_id, user.user_id)
        if not meal:
            await _ack(query, "Couldn't restore that one.")
            return
        if meal.bot_message_id and meal.bot_message_id != query.message.message_id:
            db.link_message(user.user_id, meal.bot_message_id, meal_id)
        db.update_meal(meal_id, bot_message_id=query.message.message_id)
        await _ack(query, "Restored")
        await _safe_edit(query, _card(db, user, meal), render.meal_keyboard(meal))
        return

    meal = db.get_meal(meal_id, user.user_id)
    if not meal:
        await _ack(query, "That meal was already deleted.")
        return

    if kind in {"del", "sdel"}:
        db.update_meal(meal_id, deleted=1)
        await _ack(query, "Deleted")
        if kind == "sdel":  # pressed from a /today-style summary: refresh it in place, offering Restore
            meals = db.meals_on(user.user_id, meal.local_date)
            await _safe_edit(
                query, render.day_summary(user, meal.local_date, meals), render.summary_keyboard(meals, restore=meal)
            )
        else:
            if meal.bot_message_id and meal.bot_message_id != query.message.message_id:
                try:  # tapped on a correction prompt: the meal's own card must not look live
                    await ctx.bot.edit_message_text(
                        f"🗑 Deleted <s>{escape(meal.title)}</s> ({render.fmt_kcal(meal.kcal)} kcal)",
                        chat_id=query.message.chat_id, message_id=meal.bot_message_id,
                    )
                except TelegramError:
                    pass
            await _safe_edit(
                query,
                f"🗑 Deleted <s>{escape(meal.title)}</s> ({render.fmt_kcal(meal.kcal)} kcal)\n\n"
                + render.day_line(user, _today_kcal(db, user)),
                render.deleted_keyboard(meal_id),
            )
        return

    if kind == "p":
        portion = float(arg)
        base = Estimate.model_validate_json(meal.estimate_json)
        db.update_meal(
            meal_id,
            portion=portion,
            kcal=round(base.kcal * portion),
            protein_g=round(base.protein_g * portion, 1),
            carbs_g=round(base.carbs_g * portion, 1),
            fat_g=round(base.fat_g * portion, 1),
        )
        await _ack(query, f"{render.portion_label(portion)} portion")
    elif kind == "mt":
        new_type = render.next_meal_type(meal.meal_type)
        db.update_meal(meal_id, meal_type=new_type)
        await _ack(query, new_type.title())
    elif kind == "fix":
        await _ack(query)
        prompt = await query.message.reply_text(
            f"{FIX_PROMPT} <b>{escape(meal.title)}</b>?\n"
            "<i>e.g. “no rice”, “it was a large”, “drink was teh peng not teh”, “I only ate half”</i>",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Describe the correction"),
        )
        db.link_message(user.user_id, prompt.message_id, meal_id)
        return
    else:
        await _ack(query)
        return

    meal = db.get_meal(meal_id, user.user_id)
    if meal.bot_message_id and meal.bot_message_id != query.message.message_id:
        # Tapped a card that a correction already replaced: update the live card, keep the old one retired.
        try:
            await ctx.bot.edit_message_text(
                _card(db, user, meal), chat_id=query.message.chat_id, message_id=meal.bot_message_id,
                reply_markup=render.meal_keyboard(meal),
            )
        except TelegramError:  # cosmetic; the change is already saved
            pass
        return
    await _safe_edit(query, _card(db, user, meal), render.meal_keyboard(meal))


async def _drop_buttons(query: CallbackQuery) -> None:
    try:
        await query.edit_message_reply_markup(None)
    except BadRequest:  # double tap: buttons already gone
        pass


# --- views ---------------------------------------------------------------------------


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await _require_auth(update, ctx):
        await update.message.reply_text(render.help_text())


async def _show_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE, days_ago: int) -> None:
    user = await _require_auth(update, ctx)
    if not user:
        return
    day = user.today() - timedelta(days=days_ago)
    meals = _db(ctx).meals_on(user.user_id, day)
    await update.message.reply_text(render.day_summary(user, day, meals), reply_markup=render.summary_keyboard(meals))


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_day(update, ctx, 0)


async def cmd_yesterday(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_day(update, ctx, 1)


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_auth(update, ctx)
    if not user:
        return
    today = user.today()
    totals = _db(ctx).daily_totals(user.user_id, today - timedelta(days=6), today)
    await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
    png = render.week_chart(user, totals)
    await update.message.reply_photo(png, caption=render.week_text(user, totals))


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_auth(update, ctx)
    if not user:
        return
    db = _db(ctx)
    meal = db.last_meal(user.user_id)
    if not meal:
        await update.message.reply_text("Nothing to undo.")
        return
    db.update_meal(meal.id, deleted=1)
    if meal.bot_message_id:
        try:
            await ctx.bot.edit_message_text(
                f"🗑 Deleted <s>{escape(meal.title)}</s> ({render.fmt_kcal(meal.kcal)} kcal)",
                chat_id=update.effective_chat.id, message_id=meal.bot_message_id,
                reply_markup=render.deleted_keyboard(meal.id),
            )
        except TelegramError:  # cosmetic; the change is already saved
            pass
    await update.message.reply_text(  # Restore lives on the struck-through card itself
        f"🗑 Deleted <s>{escape(meal.title)}</s> ({render.fmt_kcal(meal.kcal)} kcal). Tap ↩️ Restore on its card to undo.\n\n"
        + render.day_line(user, _today_kcal(db, user)),
    )


async def cmd_notes(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_auth(update, ctx)
    if not user:
        return
    if (ctx.args or [""])[0].lower() == "clear":
        _db(ctx).set_user_field(user.user_id, "notes", "")
        await update.message.reply_text("🧹 Cleared everything I'd learned about your habits.")
        return
    if not user.notes:
        await update.message.reply_text(
            "I haven't learned any habits yet. When you correct a meal (e.g. “I always ask for less rice”), "
            "I'll remember it for next time."
        )
        return
    await update.message.reply_text(
        f"🧠 <b>What I remember about your eating habits</b>\n{escape(user.notes)}\n\n"
        "Send <code>/notes clear</code> to forget all of these."
    )


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_auth(update, ctx)
    if not user:
        return
    meals = _db(ctx).all_meals(user.user_id)
    if not meals:
        await update.message.reply_text("Nothing to export yet.")
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "time", "meal", "food", "kcal", "protein_g", "carbs_g", "fat_g", "portion", "source", "your_note"])
    for m in meals:
        local = m.eaten_at.astimezone(user.zone)
        w.writerow([m.local_date, f"{local:%H:%M}", m.meal_type, m.title, round(m.kcal), m.protein_g, m.carbs_g,
                    m.fat_g, m.portion, m.source, m.user_text or ""])
    data = buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens it correctly
    await update.message.reply_document(data, filename=f"calories-{user.today()}.csv", caption=f"{len(meals)} meals")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_state(ctx)
    await update.message.reply_text("OK, cancelled.")


async def on_other(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await _require_auth(update, ctx):
        await update.message.reply_text("I can read photos, text and voice notes. 📸 Send a picture of your food!")


async def on_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Editing a sent message doesn't change what was logged; say so instead of staying silent."""
    db = _db(ctx)
    user = db.get_user(update.effective_user.id)
    if not user or not user.authorized:
        return
    if not db.meal_by_bot_message(user.user_id, update.edited_message.message_id):
        return  # not a message that logged a meal (a goal answer, a question...)
    await update.edited_message.reply_text(
        "✏️ I saw your edit, but edits don't change what I logged. "
        "Swipe left on the meal card (or tap ✏️ Fix) and tell me what's different."
    )


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update is None and isinstance(ctx.error, NetworkError):
        # Polling while the network is down: PTB retries by itself, so one line is enough.
        log.warning("Network error while polling: %s", ctx.error)
        return
    log.error("Unhandled error", exc_info=ctx.error)
    if isinstance(ctx.error, BadRequest) and "query" in str(ctx.error).lower():
        return  # a stale button tap; nothing they need to hear about
    if isinstance(update, Update) and update.effective_chat:
        try:
            await ctx.bot.send_message(update.effective_chat.id, "😓 Oops, something went wrong. Please try again.")
        except Exception:
            pass


# --- wiring --------------------------------------------------------------------------

COMMANDS = [
    ("today", "Today's meals and total"),
    ("week", "Last 7 days with a chart"),
    ("yesterday", "Yesterday's meals"),
    ("undo", "Delete the last meal"),
    ("goal", "Set your daily calorie goal"),
    ("reminder", "Evening recap time / off"),
    ("notes", "Habits I've learned"),
    ("export", "Download your log as CSV"),
    ("help", "How to use this bot"),
]


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    db: DB = app.bot_data["db"]
    for user in db.users_with_reminders():
        schedule_reminder(app, user)
    settings: Settings = app.bot_data["settings"]
    app.job_queue.run_daily(
        _backup, time=time(4, 30, tzinfo=ZoneInfo(settings.default_tz)), name="backup",
        job_kwargs={"misfire_grace_time": 20 * 3600, "coalesce": True},
    )
    me = await app.bot.get_me()
    log.info("Bot @%s ready", me.username)


async def _backup(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(ctx)
    path = db.backup(db.path.parent / "backups")
    log.info("Backed up database to %s", path)


def _usable_state_file(path: pathlib.Path) -> pathlib.Path:
    """A half-written state file (crash or container kill mid-save) would crash every start; set it aside instead."""
    if path.exists():
        try:
            with path.open("rb") as f:
                pickle.load(f)
        except Exception:
            log.warning("state file unreadable; moving it aside")
            path.rename(path.with_suffix(f".corrupt-{int(_time.time())}"))
    return path


def build_app(settings: Settings) -> Application:
    chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
    albums: dict[str, dict] = {}
    app = (
        ApplicationBuilder()
        .token(settings.telegram_token)
        .defaults(
            Defaults(
                parse_mode=ParseMode.HTML,
                tzinfo=ZoneInfo(settings.default_tz),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        )
        .concurrent_updates(PerChatUpdateProcessor(chat_locks, albums))
        # Keep per-user state (Try again / Log it / Replace tokens, pending questions) across restarts.
        .persistence(PicklePersistence(
            _usable_state_file(pathlib.Path(settings.db_path).with_suffix(".state.pickle")),
            store_data=PersistenceInput(bot_data=False, chat_data=False, user_data=True, callback_data=False),
            update_interval=30,
        ))
        .post_init(_post_init)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["chat_locks"] = chat_locks
    app.bot_data["albums"] = albums
    app.bot_data["db"] = DB(settings.db_path)
    app.bot_data["estimator"] = Estimator(settings.gemini_api_key, settings.gemini_model)

    private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.UpdateType.EDITED_MESSAGE, on_edit))
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("help", cmd_help, filters=private))
    app.add_handler(CommandHandler("today", cmd_today, filters=private))
    app.add_handler(CommandHandler("yesterday", cmd_yesterday, filters=private))
    app.add_handler(CommandHandler("week", cmd_week, filters=private))
    app.add_handler(CommandHandler("goal", cmd_goal, filters=private))
    app.add_handler(CommandHandler("reminder", cmd_reminder, filters=private))
    app.add_handler(CommandHandler("undo", cmd_undo, filters=private))
    app.add_handler(CommandHandler("notes", cmd_notes, filters=private))
    app.add_handler(CommandHandler("export", cmd_export, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))
    app.add_handler(MessageHandler(private & (filters.PHOTO | filters.Document.IMAGE), on_photo))
    app.add_handler(MessageHandler(private & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(private & filters.COMMAND, cmd_help))
    app.add_handler(MessageHandler(private & ~filters.StatusUpdate.ALL, on_other))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    return app
