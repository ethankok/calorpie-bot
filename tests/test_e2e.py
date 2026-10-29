"""End-to-end handler tests: real handlers + DB, fake Telegram, stubbed Gemini."""
import asyncio
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from calbot.config import Settings
from calbot.estimator import Estimate, EstimatorError, FoodItem, GoalSuggestion
from tests.fakegram import Chat, FakeTelegram, build_test_app


def food(title="Chicken rice", kcal=600, **kw):
    return Estimate(kind="food", title=title,
                    items=[FoodItem(name=title, portion="1 plate", grams=300, kcal=kcal, protein_g=30, carbs_g=70, fat_g=20)],
                    note="", **kw)


class StubEstimator:
    def __init__(self):
        self.calls = []
        self.fail_next = 0
        self.voice_question = False
        self.delay = 0.0
        self.always_correction = False
        self.refine_new_meal = False

    async def estimate(self, *, text="", media=None, notes="", recent_meal=""):
        self.calls.append(("estimate", text, [m.mime_type for m in media or []], notes))
        self.last_recent = recent_meal
        if self.fail_next:
            self.fail_next -= 1
            raise EstimatorError("boom")
        if self.delay:
            await asyncio.sleep(self.delay)
        if (text.startswith("actually") or self.always_correction) and recent_meal:
            return Estimate(kind="correction", title="", note="")
        if text == "boba" and recent_meal:
            return Estimate(kind="new_meal", title="", note="")
        if "?" in text:
            return Estimate(kind="question", title="", note="")
        if not text and media and media[0].mime_type.startswith("audio/") and self.voice_question:
            return Estimate(kind="question", title="", user_said="how much protein today?", note="model reply")
        if text == "nothing":
            return Estimate(kind="not_food", title="", note="")
        if "yesterday" in text:
            return food("Yesterday's soup", 300, yesterday=True)
        if "always" in text:
            return food("Rice", 400, remember="Usually orders less rice")
        return food("Album meal" if len(media or []) > 1 else "Chicken rice", 600 * max(1, len(media or [])))

    async def refine(self, previous, correction, media=None, notes="", original_text=""):
        self.calls.append(("refine", correction, len(media or [])))
        if self.delay:
            await asyncio.sleep(self.delay)
        self.last_refine = {"previous_kcal": previous.kcal, "original_text": original_text}
        if "for breakfast" in correction or self.refine_new_meal:
            return Estimate(kind="new_meal", title="", note="")
        return food(previous.title + " (fixed)", 400)

    async def answer(self, question, context):
        self.calls.append(("answer", question, context))
        return "Try yong tau foo."

    async def suggest_goal(self, text):
        self.calls.append(("suggest_goal", text))
        if "kg" not in text:
            return GoalSuggestion(ok=False, explanation="What's your weight and activity level?")
        return GoalSuggestion(ok=True, goal_kcal=1450, explanation="Math.")


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr("calbot.bot.REMINDER_SETTLE_S", 0)
    settings = Settings(telegram_token="999:TESTTOKEN", gemini_api_key="x", access_code="Pandan",
                        db_path=tmp_path / "t.db", default_tz="Asia/Singapore", gemini_model="m", daily_ai_cap=50)
    tg = FakeTelegram()
    app = build_test_app(settings, tg)
    stub = StubEstimator()
    app.bot_data["estimator"] = stub
    await app.initialize()
    await app.start()
    chat = Chat(app, tg)
    yield app, tg, chat, stub
    await app.stop()
    await app.shutdown()


async def onboard(chat, tg):
    await chat.text("/start")
    await chat.text(" pandan ")
    await chat.press(tg.last().message_id, "1,500")
    await chat.press(tg.last().message_id, "No thanks")


async def test_unauthorized_cannot_log(env):
    app, tg, chat, stub = env
    await chat.photo(b"img")
    assert "access code" in tg.last().text
    assert stub.calls == []
    await chat.text("wrong")
    assert "not the right code" in tg.last().text


async def test_onboarding_and_photo_log(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    assert "How to use me" in tg.last().text
    await chat.photo(b"img", caption="half rice")
    card = tg.last()
    assert "Chicken rice" in card.text and "600" in card.text and "900 left" in card.text
    assert stub.calls[-1][1] == "half rice" and stub.calls[-1][2] == ["image/jpeg"]
    # portion buttons
    await chat.press(card.message_id, "2×")
    assert "1,200" in tg.messages[card.message_id].text and "300 left" in tg.messages[card.message_id].text
    assert tg.messages[card.message_id].button("✓").get("text") == "✓ 2×"
    # meal type cycle
    before = tg.messages[card.message_id].button("↻")["text"]
    await chat.press(card.message_id, "↻")
    assert tg.messages[card.message_id].button("↻")["text"] != before


async def test_fix_via_button_and_reply(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    card = tg.last().message_id
    await chat.press(card, "Fix")
    prompt = tg.last()
    assert "What should I change" in prompt.text
    await chat.text("no rice", reply_to=prompt.message_id)
    assert stub.calls[-1] == ("refine", "no rice", 1)
    new_card = tg.last()
    assert "Updated: 600 → <b>400 kcal" in new_card.text
    assert "corrected below" in tg.messages[card].text and not tg.messages[card].keyboard
    # replying to the new card refines again
    await chat.text("bigger plate", reply_to=new_card.message_id)
    assert stub.calls[-1][0] == "refine"
    # replying to the retired (struck-through) card still corrects the same meal
    await chat.text("kopi", reply_to=card)
    assert stub.calls[-1][0] == "refine"
    with app.bot_data["db"].conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 1


async def test_stale_fix_does_not_hijack_next_meal(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    await chat.press(tg.last().message_id, "Fix")
    await chat.text("2 eggs and toast")  # typed normally, not as a reply to the Fix prompt
    assert stub.calls[-1][0] == "estimate"
    with app.bot_data["db"].conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 2


async def test_correction_keeps_portion_and_caption(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img", caption="shared, I had half")
    card = tg.last().message_id
    await chat.press(card, "½")
    await chat.text("no chilli", reply_to=card)
    assert stub.last_refine == {"previous_kcal": 300, "original_text": "shared, I had half"}


async def test_food_after_goal_command_is_logged(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("/goal")
    await chat.text("chicken rice and kopi")
    assert "Chicken rice" in tg.last().text and tg.last().button("Fix")


async def test_duplicate_photo_not_logged_twice(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    tg.files["same"] = b"img"
    for _ in range(2):
        await chat._push({"message": chat._base(photo=[{"file_id": "same", "file_unique_id": "U1", "width": 1, "height": 1}])})
    assert "already logged" in tg.last().text
    assert len([c for c in stub.calls if c[0] == "estimate"]) == 1


async def test_voice_question_uses_transcript(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    stub.voice_question = True
    await chat.voice(b"ogg")
    assert stub.calls[-1][:2] == ("answer", "how much protein today?")


async def test_summary_delete_offers_restore(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("a")
    await chat.text("/today")
    summary = tg.last().message_id
    await chat.press(summary, "🗑")
    assert "Nothing logged" in tg.messages[summary].text
    await chat.press(summary, "Restore")
    assert "Chicken rice" in tg.messages[summary].text


async def test_low_goal_warns(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("/goal 1000")
    assert "quite low" in tg.last().text


async def test_text_voice_question_notfood(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("2 eggs")
    assert "Chicken rice" in tg.last().text
    await chat.voice(b"ogg")
    assert stub.calls[-1][2] == ["audio/ogg"]
    await chat.text("what should I eat?")
    assert tg.last().text == "Try yong tau foo."
    assert "Eaten today: 1200" in stub.calls[-1][2]
    await chat.text("nothing")
    assert "don't see any food" in tg.last().text


async def test_album_is_one_meal(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"a", caption="dinner", media_group_id="g")
    await chat.photo(b"b", media_group_id="g")
    await asyncio.sleep(4.0)
    est_calls = [c for c in stub.calls if c[0] == "estimate"]
    assert len(est_calls) == 1 and est_calls[0][2] == ["image/jpeg", "image/jpeg"] and est_calls[0][1] == "dinner"
    assert "Album meal" in tg.last().text


async def test_yesterday_and_today_views(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("soup yesterday")
    if datetime.now(ZoneInfo("Asia/Singapore")).hour < 4:
        # 00:00-04:00 still counts as the previous day, so "yesterday" is the day being logged
        assert "Logged for" not in tg.last().text
        await chat.text("/today")
        assert "Yesterday&#x27;s soup" in tg.last().text
        return
    assert "Logged for" in tg.last().text
    await chat.text("/yesterday")
    assert "Yesterday&#x27;s soup" in tg.last().text
    await chat.text("/today")
    assert "Nothing logged yet" in tg.last().text


async def test_delete_restore_undo_and_summary_delete(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("a")
    card = tg.last().message_id
    await chat.press(card, "Delete")
    assert "Deleted" in tg.messages[card].text
    await chat.press(card, "Restore")
    assert "Chicken rice" in tg.messages[card].text and tg.messages[card].button("Fix")
    await chat.text("/undo")
    assert "Deleted" in tg.last().text
    await chat.text("/undo")
    assert "Nothing to undo" in tg.last().text
    await chat.text("b")
    await chat.text("c")
    await chat.text("/today")
    summary = tg.last()
    await chat.press(summary.message_id, "🗑")
    assert tg.messages[summary.message_id].text.count(" · 600") == 1


async def test_retry_after_failure(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    stub.fail_next = 1
    await chat.photo(b"img")
    assert "couldn't analyse" in tg.last().text
    await chat.press(tg.last().message_id, "Try again")
    assert "Chicken rice" in tg.last().text


async def test_habit_memory_and_notes(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("I always ask for less rice")
    await chat.text("/notes")
    assert "Usually orders less rice" in tg.last().text
    await chat.text("x")
    assert stub.calls[-1][3].endswith("Usually orders less rice")
    await chat.text("/notes clear")
    await chat.text("/notes")
    assert "haven't learned" in tg.last().text


async def test_goal_and_reminder_commands(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("/goal 1700")
    assert "1,700" in tg.last().text
    await chat.text("/goal")
    await chat.press(tg.last().message_id, "Help me pick")
    await chat.text("I'm 22F 158cm 52kg")
    await chat.press(tg.last().message_id, "Use 1,450")
    assert "1,450" in tg.last().text
    await chat.text("/reminder 9:30pm")
    assert "9:30pm" in tg.last().text
    assert app.job_queue.get_jobs_by_name("reminder:42")
    await chat.text("/reminder off")
    await asyncio.sleep(0.05)
    assert not [j for j in app.job_queue.get_jobs_by_name("reminder:42") if j.enabled and not j.removed]


async def test_reminder_message(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("/reminder 21:00")
    job = app.job_queue.get_jobs_by_name("reminder:42")[0]
    await job.run(app)
    assert "nothing logged today" in tg.last().text
    await chat.text("a")
    await job.run(app)
    assert "Your day so far" in tg.last().text


async def test_week_and_export(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("a")
    await chat.text("/week")
    assert tg.last().photo and "Last 7 days" in tg.last().text
    await chat.text("/export")
    assert tg.last().document


async def test_daily_cap(env):
    app, tg, chat, stub = env
    app.bot_data["settings"] = replace(app.bot_data["settings"], daily_ai_cap=2)
    await onboard(chat, tg)
    await chat.text("a")
    await chat.text("b")
    await chat.text("c")
    assert "limit" in tg.last().text


async def _backlog(app, tg, chat, payloads):
    """Deliver several updates at once through the app's update processor, as after a restart or network drop."""
    from telegram import Update

    updates = [Update.de_json({"update_id": next(tg._update_ids), **p}, app.bot) for p in payloads]
    await asyncio.gather(*(app.update_processor.process_update(u, app.process_update(u)) for u in updates))


async def test_backlog_is_processed_in_order_per_chat(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    stub.delay = 0.2
    tg.files["dup"] = b"img"
    photo = {"photo": [{"file_id": "dup", "file_unique_id": "dup", "width": 10, "height": 10}]}
    # the same photo sent twice, then /undo: one meal is logged, then undone
    await _backlog(app, tg, chat, [
        {"message": chat._base(**photo)},
        {"message": chat._base(**photo)},
        {"message": chat._base(text="/undo", entities=[{"type": "bot_command", "offset": 0, "length": 5}])},
    ])
    with app.bot_data["db"].conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 0


async def test_refine_of_meal_deleted_meanwhile(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    card = tg.last().message_id
    stub.delay = 0.2
    db = app.bot_data["db"]
    meal = db.last_meal(chat.user_id)
    task = asyncio.create_task(chat.text("no rice", reply_to=card))
    await asyncio.sleep(0.05)
    db.update_meal(meal.id, deleted=1)
    await task
    assert "deleted" in tg.last().text
    with db.conn as c:
        assert c.execute("SELECT title FROM meals WHERE id = ?", (meal.id,)).fetchone()[0] == "Chicken rice"


async def test_unreplied_correction_updates_last_meal(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    await chat.text("actually only half the rice")
    assert "Chicken rice" in stub.last_recent
    assert stub.calls[-1][0] == "refine"
    with app.bot_data["db"].conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 1


async def test_goal_number_variants(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("/goal")
    await chat.text("1,600")
    assert app.bot_data["db"].get_user(chat.user_id).goal_kcal == 1600
    await chat.text("/goal")
    await chat.text("600")  # out of range: re-ask, never log as food
    assert "between" in tg.last().text
    assert not [c for c in stub.calls if c[0] == "estimate"]


async def test_short_ack_costs_no_ai_call(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("thanks!")
    assert stub.calls == []


async def test_failure_wording_by_reason(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)

    async def quota(**_kw):
        raise EstimatorError("429", reason="quota")

    stub.estimate = quota
    await chat.text("2 eggs")
    assert "limit" in tg.last().text and tg.last().button("Try again")


async def test_album_job_never_misfires(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"a", media_group_id="g1")
    job = app.job_queue.jobs()[-1]
    assert job.job.misfire_grace_time is None


async def test_stale_button_taps_still_work(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    card = tg.last().message_id
    tg.stale_queries = True
    await chat.press(card, "½")
    assert "300" in tg.messages[card].text
    assert not any("Oops" in m.text for m in tg.bot_messages())


async def test_retry_keeps_original_send_time(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    stub.fail_next = 1
    import time as t
    real = t.time
    t.time = lambda: real() - 10 * 3600  # sent 10h ago (fakegram stamps messages with time.time())
    try:
        await chat.text("laksa")
    finally:
        t.time = real
    await chat.press(tg.last().message_id, "Try again")
    meal = app.bot_data["db"].last_meal(chat.user_id)
    from datetime import UTC, datetime
    assert (datetime.now(UTC) - meal.eaten_at).total_seconds() > 9 * 3600


async def test_goal_stats_over_two_messages(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("/goal")
    await chat.press(tg.last().message_id, "Help me")
    await chat.text("female 24 160cm")
    assert "weight" in tg.last().text
    await chat.text("55kg, desk job")
    assert "female 24 160cm" in stub.calls[-1][1] and "55kg" in stub.calls[-1][1]
    assert "1,450" in tg.last().text


async def test_ack_reply_to_card_is_free(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    n = len(stub.calls)
    await chat.text("thanks", reply_to=tg.last().message_id)
    assert len(stub.calls) == n


def test_reminder_bare_hour_is_evening():
    from calbot.bot import _parse_hhmm
    assert _parse_hhmm("9:30") == "21:30" and _parse_hhmm("9") == "21:00"
    assert _parse_hhmm("8am") == "08:00" and _parse_hhmm("21:00") == "21:00"


def _shift_clock(hours):
    """Make fakegram stamp the next messages as sent `hours` ago."""
    import time as t
    real = t.time
    t.time = lambda: real() - hours * 3600
    return lambda: setattr(t, "time", real)


async def test_ignored_fix_prompt_does_not_overwrite_meal(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    restore = _shift_clock(3)
    try:
        await chat.photo(b"img")
        await chat.press(tg.last().message_id, "Fix")
        prompt = tg.last().message_id
    finally:
        restore()
    await chat.text("2 eggs and kaya toast", reply_to=prompt)  # ForceReply kept the draft attached
    assert stub.calls[-1][0] == "estimate"
    with app.bot_data["db"].conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 2


async def test_refine_that_is_really_a_new_meal(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    await chat.text("2 eggs for breakfast", reply_to=tg.last().message_id)
    with app.bot_data["db"].conn as c:
        titles = [r[0] for r in c.execute("SELECT title FROM meals WHERE deleted = 0")]
    assert titles == ["Chicken rice", "Chicken rice"]  # stub names every text meal "Chicken rice"
    assert stub.calls[-1][0] == "estimate"


async def test_reply_to_deleted_card(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    card = tg.last().message_id
    await chat.press(card, "Delete")
    n = len(stub.calls)
    await chat.text("no rice", reply_to=card)
    assert "Restore" in tg.last().text and len(stub.calls) == n


async def test_album_in_backlog_is_logged_before_later_messages(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    tg.files["a1"], tg.files["a2"] = b"1", b"2"

    def ph(fid):
        return {"photo": [{"file_id": fid, "file_unique_id": fid, "width": 9, "height": 9}], "media_group_id": "g9"}

    await _backlog(app, tg, chat, [
        {"message": chat._base(**ph("a1"))},
        {"message": chat._base(**ph("a2"))},
        {"message": chat._base(text="actually only half")},
    ])
    kinds = [c[0] for c in stub.calls]
    assert kinds == ["estimate", "estimate", "refine"]
    assert stub.calls[0][2] == ["image/jpeg", "image/jpeg"]


async def test_backlog_repeat_is_flagged(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    restore = _shift_clock(2)
    try:
        await chat.text("chicken rice")
        await chat.text("chicken rice!!")
    finally:
        restore()
    assert "Looks like a repeat" in tg.last().text


async def test_slow_ai_call_gives_up_with_retry(env, monkeypatch):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    monkeypatch.setattr("calbot.bot.AI_DEADLINE_S", 0.1)
    stub.delay = 1
    await chat.text("laksa")
    assert tg.last().button("Try again")


async def test_estimate_and_refine_disagreeing_cannot_loop(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    stub.always_correction, stub.refine_new_meal = True, True
    n = len(stub.calls)
    await chat.text("2 kaya toast")
    assert [c[0] for c in stub.calls[n:]] == ["estimate", "refine", "estimate"]
    with app.bot_data["db"].conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 2


async def test_late_answer_to_fix_prompt_still_corrects(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    restore = _shift_clock(1)
    try:
        await chat.photo(b"img")
        await chat.press(tg.last().message_id, "Fix")
        prompt = tg.last().message_id
    finally:
        restore()
    await chat.text("actually no rice", reply_to=prompt)
    assert "Chicken rice" in stub.last_recent
    assert stub.calls[-1][0] == "refine"


async def test_goal_stats_bare_number_goes_to_ai(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.text("/goal")
    await chat.press(tg.last().message_id, "Help me")
    await chat.text("24")
    assert "between" not in tg.last().text
    assert stub.calls[-1] == ("suggest_goal", "24")


async def test_new_meal_kind_from_estimate_is_logged(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    await chat.text("boba")
    with app.bot_data["db"].conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 2


async def test_messages_after_album_keep_arrival_order(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    stub.delay = 0.3
    tg.files["b1"] = b"1"
    first = chat._base(photo=[{"file_id": "b1", "file_unique_id": "b1", "width": 9, "height": 9}], media_group_id="g7")
    t1, t2 = chat._base(text="first thing"), chat._base(text="second thing")
    from telegram import Update

    async def push(p):
        u = Update.de_json({"update_id": next(tg._update_ids), "message": p}, app.bot)
        await app.update_processor.process_update(u, app.process_update(u))

    tasks = [asyncio.create_task(push(first))]
    await asyncio.sleep(0.05)
    tasks.append(asyncio.create_task(push(t1)))
    await asyncio.sleep(3.6)  # album flush starts; T2 arrives while it is being estimated
    tasks.append(asyncio.create_task(push(t2)))
    await asyncio.gather(*tasks)
    texts = [c[1] for c in stub.calls if c[0] == "estimate"]
    assert texts == ["", "first thing", "second thing"]


async def test_edited_message_gets_a_hint(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    from telegram import Update

    edited = chat._base(text="what's my goal", edit_date=1)  # not a meal message: stay quiet
    await app.process_update(Update.de_json({"update_id": next(tg._update_ids), "edited_message": edited}, app.bot))
    assert "How to use me" in tg.last().text
    await chat.text("chicken rice")
    meal_msg = max(m for m, v in tg.messages.items() if not v.from_bot)
    edited = {**chat._base(text="chicken rice, half", edit_date=1), "message_id": meal_msg}
    await app.process_update(Update.de_json({"update_id": next(tg._update_ids), "edited_message": edited}, app.bot))
    assert "edit" in tg.last().text and len(stub.calls) == 1


async def test_reply_to_her_own_photo_corrects_that_meal(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"lunch")
    lunch_photo = max(m for m, v in tg.messages.items() if not v.from_bot)
    lunch = app.bot_data["db"].last_meal(chat.user_id)
    await chat.text("teh peng")
    await chat.text("only ate half", reply_to=lunch_photo)
    assert stub.calls[-1][0] == "refine" and stub.calls[-1][2] == 1  # refined with the lunch photo
    assert "(fixed)" in app.bot_data["db"].get_meal(lunch.id, chat.user_id).title


async def test_tap_on_retired_card_updates_live_card(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    old_card = tg.last().message_id
    await chat.text("no rice", reply_to=old_card)
    new_card = tg.last().message_id
    tg.messages[old_card].keyboard = [[{"text": "½", "callback_data": tg.messages[new_card].button("½")["callback_data"]}]]
    await chat.press(old_card, "½")
    assert "corrected below" in tg.messages[old_card].text
    assert "200" in tg.messages[new_card].text


async def test_food_question_answers_with_log_button(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)

    async def est(**_kw):
        e = food("Chicken rice", 600)
        e.kind = "question"
        e.note = "Yes, fine for lunch."
        return e

    stub.estimate = est
    await chat.text("how many calories in chicken rice?")
    answer = tg.last()
    assert "fine for lunch" in answer.text and not any(c[0] == "answer" for c in stub.calls)  # answered, not logged
    db = app.bot_data["db"]
    assert db.last_meal(chat.user_id) is None
    await chat.press(answer.message_id, "Log Chicken rice")
    assert db.last_meal(chat.user_id).title == "Chicken rice"
    assert "600" in tg.last().text


async def test_retry_skipped_when_she_retyped_it(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    stub.fail_next = 1
    await chat.text("fishball noodles")
    failed = tg.last().message_id
    await chat.text("fishball noodles")
    n = len(stub.calls)
    await chat.press(failed, "Try again")
    assert "already logged" in tg.messages[failed].text and len(stub.calls) == n


def test_image_mime_sniffing():
    from calbot.bot import _image_mime
    assert _image_mime(b"\x89PNG\r\n") == "image/png"
    assert _image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8") == "image/webp"
    assert _image_mime(b"\xff\xd8\xff") == "image/jpeg"


async def test_photo_reply_to_card_can_replace_meal(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"blurry")
    card = tg.last().message_id
    db = app.bot_data["db"]
    first = db.last_meal(chat.user_id)
    tg.files["clear"] = b"clear"
    await chat._push({"message": chat._base(
        photo=[{"file_id": "clear", "file_unique_id": "clear", "width": 9, "height": 9}],
        reply_to_message={"message_id": card, "date": tg.messages[card].date, "chat": {"id": chat.user_id, "type": "private"},
                          "from": {"id": 999, "is_bot": True, "first_name": "CalBot"}, "text": tg.messages[card].text},
    )})
    assert "instead of" in tg.last().text
    await chat.press(tg.last().message_id, "Replace")
    with db.conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 1
    assert db.get_meal(first.id, chat.user_id) is None
    assert "Replaced" in tg.messages[card].text
    new = db.last_meal(chat.user_id)
    await chat.press(card, "Undo replace")  # a mis-tapped Replace: back to the original, counted once
    with db.conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 1
    assert db.get_meal(first.id, chat.user_id) and db.get_meal(new.id, chat.user_id) is None
    assert tg.messages[card].button("Fix") and "Kept the original" in tg.messages[new.bot_message_id].text


async def test_retry_links_her_photo(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    stub.fail_next = 1
    await chat.photo(b"img")
    her_photo = max(m for m, v in tg.messages.items() if not v.from_bot)
    await chat.press(tg.last().message_id, "Try again")
    await chat.text("no rice", reply_to=her_photo)
    assert stub.calls[-1][0] == "refine"


async def test_refine_to_nothing_offers_delete(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    card = tg.last().message_id

    async def refine(*a, **kw):
        return Estimate(kind="not_food", title="", note="")

    stub.refine = refine
    await chat.text("I didn't eat this", reply_to=card)
    reply = tg.last()
    assert "Remove" in reply.text
    await chat.press(reply.message_id, "Delete it")
    assert app.bot_data["db"].last_meal(chat.user_id) is None


async def test_photo_with_question_caption_is_logged(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)

    async def est(**_kw):
        e = food("Nasi lemak", 750)
        e.kind, e.note = "question", "Fairly rich; go easy on the sambal."
        return e

    stub.estimate = est
    await chat.photo(b"img", caption="is this healthy?")
    assert app.bot_data["db"].last_meal(chat.user_id).title == "Nasi lemak"
    assert "go easy" in tg.last().text


async def _photo_reply(chat, tg, card, fid):
    tg.files[fid] = fid.encode()
    await chat._push({"message": chat._base(
        photo=[{"file_id": fid, "file_unique_id": fid, "width": 9, "height": 9}],
        reply_to_message={"message_id": card, "date": tg.messages[card].date, "chat": {"id": chat.user_id, "type": "private"},
                          "from": {"id": 999, "is_bot": True, "first_name": "CalBot"}, "text": tg.messages[card].text},
    )})


async def test_replace_keeps_old_meal_if_new_estimate_fails(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"blurry")
    card = tg.last().message_id
    await _photo_reply(chat, tg, card, "clear1")
    stub.fail_next = 1
    await chat.press(tg.last().message_id, "Replace")
    assert app.bot_data["db"].last_meal(chat.user_id) is not None
    assert "Replaced" not in tg.messages[card].text


async def test_replace_keeps_time_and_type(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    restore = _shift_clock(14)
    try:
        await chat.photo(b"dinner")
    finally:
        restore()
    db = app.bot_data["db"]
    old = db.last_meal(chat.user_id)
    card = tg.last().message_id
    await _photo_reply(chat, tg, card, "clear2")
    await chat.press(tg.last().message_id, "Replace")
    new = db.last_meal(chat.user_id)
    assert new.id != old.id and new.eaten_at == old.eaten_at and new.meal_type == old.meal_type


async def test_question_reply_to_card_does_not_offer_delete(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    card = tg.last().message_id

    async def refine(*a, **kw):
        return Estimate(kind="question", title="", note="About 30g protein.")

    stub.refine = refine
    await chat.text("how much protein is this?", reply_to=card)
    assert "30g" in tg.last().text and not tg.last().keyboard


async def test_photo_reply_to_deleted_card_is_logged(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"bad")
    card = tg.last().message_id
    await chat.press(card, "Delete")
    await _photo_reply(chat, tg, card, "better")
    assert app.bot_data["db"].last_meal(chat.user_id) is not None


async def test_undo_strikes_through_card(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    await chat.photo(b"img")
    card = tg.last().message_id
    await chat.text("/undo")
    assert "<s>" in tg.messages[card].text and tg.messages[card].button("Restore")


async def test_double_tap_on_try_again(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    stub.fail_next = 1
    await chat.text("laksa")
    failed = tg.last().message_id
    await chat.press(failed, "Try again")
    await chat.press(failed, "Try again")
    assert tg.callback_answers[-1].get("text") == "Already done 👍"
    with app.bot_data["db"].conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 1


async def test_backlog_retype_20_min_later_is_flagged(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    restore = _shift_clock(1)
    try:
        await chat.text("chicken rice")
    finally:
        restore()
    restore = _shift_clock(0.6)
    try:
        await chat.text("chicken rice please")
    finally:
        restore()
    assert "Looks like a repeat" in tg.last().text


async def test_log_it_survives_a_telegram_hiccup(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)

    async def est(**_kw):
        e = food("Chicken rice", 600)
        e.kind = "question"
        return e

    stub.estimate = est
    await chat.text("how many calories in chicken rice?")
    answer = tg.last().message_id
    tg.fail_next["sendMessage"] = 1  # the "📝 Logging it…" send fails
    await chat.press(answer, "Log Chicken rice")
    db = app.bot_data["db"]
    assert db.last_meal(chat.user_id) is None
    await chat.press(answer, "Log Chicken rice")  # the button still works, not "Already done"
    assert db.last_meal(chat.user_id).title == "Chicken rice"
    with db.conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 1


async def test_retry_survives_failed_delete(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    stub.fail_next = 1
    await chat.text("laksa")
    failed = tg.last().message_id
    tg.fail_next["deleteMessage"] = 1
    await chat.press(failed, "Try again")
    with app.bot_data["db"].conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 1


async def test_typed_ok_accepts_suggested_goal(env):
    app, tg, chat, stub = env
    await chat.text("/start")
    await chat.text(chat.app.bot_data["settings"].access_code)
    await chat.press(tg.last().message_id, "Help me pick")
    await chat.text("I'm 22F 158cm 52kg")
    assert "Suggested goal" in tg.last().text
    await chat.text("ok")
    assert app.bot_data["db"].get_user(chat.user_id).goal_kcal == 1450
    assert "evening" in tg.last().text  # onboarding carries on to the reminder question


async def test_old_photo_retry_after_resend_is_not_double_logged(env):
    app, tg, chat, stub = env
    await onboard(chat, tg)
    stub.fail_next = 1
    restore = _shift_clock(0.5)
    try:
        await chat.photo(b"first")
    finally:
        restore()
    failed = tg.last().message_id
    await chat.photo(b"resent from gallery")  # new file_unique_id, so the photo dedupe can't see it
    await chat.press(failed, "Try again")
    assert "won't count it twice" in tg.last().text
    with app.bot_data["db"].conn as c:
        assert c.execute("SELECT COUNT(*) FROM meals WHERE deleted = 0").fetchone()[0] == 1


async def test_goal_typed_in_a_sentence(env):
    app, tg, chat, stub = env
    await chat.text("/start")
    await chat.text(chat.app.bot_data["settings"].access_code)
    await chat.text("I think 1600 please")
    assert app.bot_data["db"].get_user(chat.user_id).goal_kcal == 1600
