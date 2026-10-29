"""Drive the real bot (real Gemini, fake Telegram) through a full user journey and print the transcript.

Usage: PYTHONPATH=. uv run python tests/e2e_scenario.py (needs the Gemini key; calls the real API)
"""
import asyncio, os, pathlib, re, tempfile
from dataclasses import replace

from calbot import config
from tests.fakegram import Chat, FakeTelegram, build_test_app

FIX = pathlib.Path(__file__).parent / "fixtures"


def show(tg, since):
    for m in tg.bot_messages():
        if m.message_id > since:
            kb = " | ".join(b["text"] for b in m.buttons())
            body = re.sub(r"<[^>]+>", "", m.text)
            print(f"  [bot #{m.message_id}{' photo' if m.photo else ''}{' doc' if m.document else ''}] {body}")
            if kb:
                print(f"      buttons: {kb}")


async def main():
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "999:TESTTOKEN")
    os.environ.setdefault("ACCESS_CODE", "pandan")
    tmp = tempfile.mkdtemp()
    settings = replace(config.load(), db_path=pathlib.Path(tmp) / "e2e.db", access_code="pandan")
    tg = FakeTelegram()
    app = build_test_app(settings, tg)
    await app.initialize()
    await app.post_init(app)
    await app.start()
    chat = Chat(app, tg)
    mark = 0

    async def step(label, coro):
        nonlocal mark
        print(f"\n>>> {label}")
        await coro
        show(tg, mark)
        mark = max(tg.messages)

    await step("user: hi (unauthorised)", chat.text("hi"))
    await step("user: wrong code", chat.text("banana"))
    await step("user: right code", chat.text("Pandan"))
    await step("tap Help me pick", chat.press(tg.last().message_id, "Help me pick"))
    await step("user: stats", chat.text("female 22, 158cm, 52kg, office job, gym 2x a week, want to lose a bit"))
    await step("tap Use suggestion", chat.press(tg.last().message_id, "Use"))
    await step("tap 9pm reminder", chat.press(tg.last().message_id, "9pm"))
    await step("photo: chicken rice", chat.photo((FIX / "chicken_rice.jpg").read_bytes()))
    card = tg.last().message_id
    await step("tap ½ portion", chat.press(card, "½"))
    await step("tap meal type cycle", chat.press(card, "↻"))
    await step("photo with caption: laksa, shared", chat.photo((FIX / "laksa.jpg").read_bytes(), caption="shared with my bf, I had half"))
    laksa = tg.last().message_id
    await step("reply to laksa card with correction", chat.text("actually no tau pok, and I didn't drink the gravy", reply_to=laksa))
    await step("text log", chat.text("2 kaya toast and kopi c siew dai"))
    await step("voice log", chat.voice((FIX / "voice.ogg").read_bytes()))
    await step("album: burger + salad", asyncio.gather(
        chat.photo((FIX / "burger.jpg").read_bytes(), caption="dinner", media_group_id="g1"),
        chat.photo((FIX / "salad.jpg").read_bytes(), media_group_id="g1")))
    await asyncio.sleep(16)  # album debounce (3.5s) plus the real Gemini call
    show(tg, mark); mark = max(tg.messages)
    await step("non-food question", chat.text("what's a good dinner if I have 400 kcal left?"))
    await step("/today", chat.text("/today"))
    await step("delete from summary", chat.press(tg.last().message_id, "🗑"))
    await step("/undo", chat.text("/undo"))
    # /undo puts Restore on the struck-through card itself, not on its reply
    undone = next(m for m in reversed(tg.bot_messages()) if any("Restore" in b["text"] for b in m.buttons()))
    await step("restore", chat.press(undone.message_id, "Restore"))
    await step("/week", chat.text("/week"))
    await step("/notes", chat.text("/notes"))
    await step("/export", chat.text("/export"))
    await step("sticker", chat.sticker())
    await step("unknown command", chat.text("/foo"))
    print("\n>>> reminder job (forced)")
    job = app.job_queue.get_jobs_by_name("reminder:42")[0]
    await job.run(app)
    show(tg, mark)
    print("\nAPI calls:", {m for m, _ in tg.calls})
    await app.stop(); await app.shutdown()

asyncio.run(main())
