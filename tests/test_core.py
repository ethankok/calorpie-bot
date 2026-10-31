from datetime import UTC, datetime, timedelta

import pytest

from calbot import render
from calbot.bot import _parse_hhmm, _parse_kcal
from calbot.db import DB, guess_meal_type
from calbot.estimator import Estimate, FoodItem


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "t.db")
    d.upsert_user(1, 1, "Test")
    d.set_user_field(1, "authorized", 1)
    d.set_user_field(1, "goal_kcal", 1600)
    return d


def est(kcal=600):
    return Estimate(
        kind="food", title="Chicken rice",
        items=[FoodItem(name="rice", portion="1 plate", grams=200, kcal=kcal - 200, protein_g=5, carbs_g=60, fat_g=8),
               FoodItem(name="chicken", portion="100 g", grams=100, kcal=200, protein_g=25, carbs_g=0, fat_g=10)],
        note="Oil varies",
    )


def add(db, when, e=None):
    e = e or est()
    u = db.get_user(1)
    return db.add_meal(u, eaten_at=when, title=e.title, kcal=e.kcal, protein_g=e.protein_g, carbs_g=e.carbs_g,
                       fat_g=e.fat_g, estimate_json=e.model_dump_json(), source="photo", photo_file_ids=["abc"])


@pytest.mark.parametrize("s,v", [("1600", 1600), ("1,600", 1600), ("1650 kcal", 1650), ("about 1.6k", 1600), ("~1500 a day", 1500), ("50", None), ("9999", None), ("abc", None), ("2 eggs", None)])
def test_parse_kcal(s, v):
    assert _parse_kcal(s) == v


@pytest.mark.parametrize("s,v", [("21:00", "21:00"), ("9pm", "21:00"), ("9:30 pm", "21:30"), ("2130", "21:30"),
                                 ("12am", "00:00"), ("25:00", None), ("soon", None)])
def test_parse_hhmm(s, v):
    assert _parse_hhmm(s) == v


def test_local_date_uses_singapore_time(db):
    # 17:30 UTC = 01:30 on the 28th in Singapore: a late supper, counted towards the 27th (day starts 4am)
    mid = add(db, datetime(2026, 10, 27, 17, 30, tzinfo=UTC))
    m = db.get_meal(mid, 1)
    assert str(m.local_date) == "2026-10-27"
    assert m.meal_type == "supper"  # 600 kcal; a small bite at that hour would be a snack
    # 20:30 UTC = 04:30 on the 28th: a new day
    mid = add(db, datetime(2026, 10, 27, 20, 30, tzinfo=UTC))
    assert str(db.get_meal(mid, 1).local_date) == "2026-10-28"


def test_meal_type_guess():
    assert guess_meal_type(datetime(2026, 1, 1, 8, 0)) == "breakfast"
    assert guess_meal_type(datetime(2026, 1, 1, 12, 30)) == "lunch"
    assert guess_meal_type(datetime(2026, 1, 1, 19, 0)) == "dinner"
    assert guess_meal_type(datetime(2026, 1, 1, 16, 0)) == "snack"
    assert guess_meal_type(datetime(2026, 1, 1, 23, 30), kcal=1474) == "supper"
    assert guess_meal_type(datetime(2026, 1, 1, 22, 20), kcal=510) == "supper"
    assert guess_meal_type(datetime(2026, 1, 1, 23, 0), kcal=94) == "snack"
    assert guess_meal_type(datetime(2026, 1, 1, 1, 30), kcal=600) == "supper"


def test_totals_and_delete(db):
    now = datetime.now(UTC)
    a = add(db, now)
    add(db, now)
    u = db.get_user(1)
    assert sum(m.kcal for m in db.meals_on(1, u.today())) == 1200
    db.update_meal(a, deleted=1)
    assert len(db.meals_on(1, u.today())) == 1
    week = db.daily_totals(1, u.today() - timedelta(days=6), u.today())
    assert len(week) == 7 and week[u.today()]["kcal"] == 600


def test_user_isolation(db):
    mid = add(db, datetime.now(UTC))
    db.upsert_user(2, 2, "Other")
    assert db.get_meal(mid, 2) is None


def test_render_card_and_keyboard(db):
    mid = add(db, datetime.now(UTC))
    u = db.get_user(1)
    m = db.get_meal(mid, 1)
    text = render.meal_card(m, est(), u, 600, logged_for=m.local_date)
    assert "600" in text and "1,000 left" in text and "Oil varies" in text
    kb = render.meal_keyboard(m)
    for row in kb.inline_keyboard:
        for b in row:
            assert len(b.callback_data.encode()) <= 64
    assert any(b.text.startswith("✓") for b in kb.inline_keyboard[0])


def test_html_escaped(db):
    e = est()
    e.title = "<b>bad</b> & co"
    u = db.get_user(1)
    mid = add(db, datetime.now(UTC), e)
    text = render.meal_card(db.get_meal(mid, 1), e, u, 0)
    assert "<b>bad</b>" not in text and "&lt;b&gt;" in text


def test_summary_and_week(db):
    add(db, datetime.now(UTC))
    u = db.get_user(1)
    meals = db.meals_on(1, u.today())
    s = render.day_summary(u, u.today(), meals)
    assert "Total:" in s
    totals = db.daily_totals(1, u.today() - timedelta(days=6), u.today())
    assert render.week_chart(u, totals)[:4] == b"\x89PNG"
    assert "Average" in render.week_text(u, totals)


def test_over_goal(db):
    u = db.get_user(1)
    assert "200 over" in render.day_line(u, 1800)


async def test_empty_food_estimate_is_retried():
    from types import SimpleNamespace
    from calbot.estimator import Estimate, Estimator, FoodItem

    e = Estimator.__new__(Estimator)
    e.config = None
    good = Estimate(kind="food", title="Rice",
                    items=[FoodItem(name="rice", portion="1 bowl", grams=200, kcal=250, protein_g=5, carbs_g=55, fat_g=1)])
    replies = [SimpleNamespace(parsed=Estimate(kind="food", title="Rice", items=[]), text=""), SimpleNamespace(parsed=good, text="")]

    async def fake_generate(parts, config):
        return replies.pop(0)

    e._generate = fake_generate
    assert (await e._call([])).kcal == 250


async def test_two_empty_estimates_fail_as_empty_not_network():
    from types import SimpleNamespace
    from calbot.estimator import Estimate, Estimator, EstimatorError

    e = Estimator.__new__(Estimator)
    e.config = None

    async def fake_generate(parts, config):
        return SimpleNamespace(parsed=Estimate(kind="food", title="Eggs", items=[]), text="")

    e._generate = fake_generate
    with pytest.raises(EstimatorError) as err:
        await e._call([])
    assert err.value.reason == "empty"


def test_items_required_in_gemini_schema():
    # Left optional, Gemini skipped items in about 1 in 6 photo estimates (measured 2026-10-31).
    from google import genai
    from google.genai import _transformers

    schema = _transformers.t_schema(genai.Client(api_key="x")._api_client, Estimate)
    assert {"kind", "title", "items"} <= set(schema.required)
    assert Estimate(kind="not_food", title="").items == []  # still optional in Python and in stored JSON


def test_last_activity_is_latest_meal_or_join(db):
    joined = db.last_activity(1)
    assert datetime.now(UTC) - joined < timedelta(minutes=1)
    db.conn.execute("UPDATE users SET created_at = '2026-01-01T00:00:00+00:00' WHERE user_id = 1")
    assert db.last_activity(1) == datetime(2026, 1, 1, tzinfo=UTC)
    meal_id = add(db, datetime.now(UTC))
    db.update_meal(meal_id, deleted=1)  # a deleted meal still shows they were using the bot
    assert datetime.now(UTC) - db.last_activity(1) < timedelta(minutes=1)


async def test_refine_correction_kind_counts_as_food():
    from calbot.estimator import Estimate, Estimator, FoodItem

    e = Estimator.__new__(Estimator)
    fixed = Estimate(kind="correction", title="Chicken",
                     items=[FoodItem(name="chicken", portion="1", grams=150, kcal=250, protein_g=30, carbs_g=0, fat_g=12)])

    async def fake_call(parts):
        return fixed

    e._call = fake_call
    assert (await e.refine(fixed, "no rice")).kind == "food"


def test_card_shows_one_number_and_reads_old_estimates():
    from calbot import render
    from calbot.db import Meal, User
    from calbot.estimator import Estimate
    from datetime import UTC, date, datetime

    # Meals saved before the range and confidence label were dropped still carry those fields.
    old_json = ('{"kind":"food","title":"Rice","items":[{"name":"rice","portion":"1","grams":200,"kcal":600,'
                '"protein_g":5,"carbs_g":55,"fat_g":1}],"confidence":"high","kcal_low":500,"kcal_high":700,"note":""}')
    est = Estimate.model_validate_json(old_json)
    meal = Meal(1, 1, datetime.now(UTC), date.today(), "lunch", "Rice", 600, 5, 55, 1, old_json, 1.0,
                "text", [], None, None)
    user = User(1, 1, "Test", True, 1500, "Asia/Singapore", None, "")
    text = render.meal_card(meal, est, user, 600)
    assert "≈ 600 kcal</b>\n" in text
    assert "estimate" not in text and "500–" not in text and "💡" not in text
    asked = render.meal_card(meal, est.model_copy(update={"note": "Did you drink the gravy?"}), user, 600)
    assert "💡 <i>Did you drink the gravy?</i>" in asked


async def test_zero_kcal_drink_is_accepted():
    from types import SimpleNamespace
    from calbot.estimator import Estimate, Estimator, FoodItem

    e = Estimator.__new__(Estimator)
    e.config = None
    water = Estimate(kind="food", title="Water", items=[FoodItem(name="water", portion="1 glass", grams=250, kcal=0,
                                                                 protein_g=0, carbs_g=0, fat_g=0)])

    async def fake_generate(parts, config):
        return SimpleNamespace(parsed=water, text="")

    e._generate = fake_generate
    assert (await e._call([])).title == "Water"


def test_corrupt_state_file_is_set_aside(tmp_path):
    from calbot.bot import _usable_state_file
    f = tmp_path / "calbot.state.pickle"
    f.write_bytes(b"\x80\x04truncated")
    assert _usable_state_file(f) == f and not f.exists()
    assert list(tmp_path.glob("calbot.state.corrupt-*"))


def test_log_times_in_configured_timezone(monkeypatch):
    import logging
    from zoneinfo import ZoneInfo

    from calbot.__main__ import log_in_timezone

    monkeypatch.setattr(logging.Formatter, "converter", logging.Formatter.converter)  # restored after the test
    log_in_timezone(ZoneInfo("Asia/Singapore"))
    rec = logging.makeLogRecord({"msg": "hi"})
    rec.created = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    assert logging.Formatter("%(asctime)s %(message)s").format(rec).startswith("2026-01-01 08:00:00")
