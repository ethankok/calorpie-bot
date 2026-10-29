"""Message text, keyboards and charts. Pure functions so they are easy to test."""

from __future__ import annotations

import io
from datetime import date
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .db import MEAL_TYPES, Meal, User
from .estimator import Estimate

PORTIONS = (0.5, 1.0, 1.5, 2.0)
MEAL_EMOJI = {"breakfast": "🍳", "lunch": "🍱", "dinner": "🍽", "supper": "🌙", "snack": "🍪"}


def clip(text: str, limit: int) -> str:
    """Keep model-written strings short so a chatty answer can't break Telegram's 4096-char limit."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def fmt_kcal(x: float) -> str:
    return f"{round(x):,}"


def portion_label(p: float) -> str:
    return {0.5: "½", 1.0: "1×", 1.5: "1½", 2.0: "2×"}.get(p, f"{p:g}×")


def progress_bar(eaten: float, goal: int, width: int = 10) -> str:
    frac = eaten / goal if goal else 0
    filled = min(width, round(frac * width))
    return "▓" * filled + "░" * (width - filled)


def day_line(user: User, eaten: float) -> str:
    """'Today: 1,250 / 1,600 kcal · 350 left' with a bar, or just the total if no goal."""
    if not user.goal_kcal:
        return f"<b>Today:</b> {fmt_kcal(eaten)} kcal"
    left = user.goal_kcal - eaten
    status = f"{fmt_kcal(left)} left" if left >= 0 else f"{fmt_kcal(-left)} over"
    return (
        f"<b>Today:</b> {fmt_kcal(eaten)} / {fmt_kcal(user.goal_kcal)} kcal · {status}\n"
        f"{progress_bar(eaten, user.goal_kcal)}"
    )


def meal_card(
    meal: Meal,
    est: Estimate,
    user: User,
    today_kcal: float,
    *,
    logged_for: date | None = None,
    remembered: str = "",
) -> str:
    p = meal.portion
    lines = [
        f"{MEAL_EMOJI.get(meal.meal_type, '🍽')} <b>{escape(clip(meal.title, 80))}</b> · {meal.meal_type}"
        + (f" · {portion_label(p)} portion" if p != 1.0 else ""),
        # No range or confidence label: the model rated nearly everything "high", and calorie apps show one
        # number. Uncertainty shows up as the 💡 question when one detail would change the total.
        f"<b>≈ {fmt_kcal(meal.kcal)} kcal</b>",
        f"Protein {meal.protein_g:.0f}g · Carbs {meal.carbs_g:.0f}g · Fat {meal.fat_g:.0f}g",
        "",
    ]
    for item in est.items[:8]:
        lines.append(
            f"• {escape(clip(item.name, 60))} <i>({escape(clip(item.portion, 40))})</i> — {fmt_kcal(item.kcal * p)}"
        )
    if len(est.items) > 8:
        rest = sum(i.kcal for i in est.items[8:]) * p
        lines.append(f"• + {len(est.items) - 8} more — {fmt_kcal(rest)}")
    if est.note:
        lines += ["", f"💡 <i>{escape(clip(est.note, 300))}</i>"]
    if remembered:
        lines += ["", f"🧠 I'll remember: <i>{escape(clip(remembered, 150))}</i> (see /notes)"]
    lines.append("")
    if logged_for and logged_for != user.today():
        lines.append(f"📅 Logged for {logged_for:%a %d %b}")
    else:
        lines.append(day_line(user, today_kcal))
    return "\n".join(lines)


def meal_keyboard(meal: Meal) -> InlineKeyboardMarkup:
    portion_row = [
        InlineKeyboardButton(
            ("✓ " if abs(meal.portion - p) < 1e-6 else "") + portion_label(p),
            callback_data=f"p:{meal.id}:{p:g}",
        )
        for p in PORTIONS
    ]
    action_row = [
        InlineKeyboardButton("✏️ Fix", callback_data=f"fix:{meal.id}"),
        InlineKeyboardButton(f"{MEAL_EMOJI.get(meal.meal_type, '')} {meal.meal_type.title()} ↻", callback_data=f"mt:{meal.id}"),
        InlineKeyboardButton("🗑 Delete", callback_data=f"del:{meal.id}"),
    ]
    return InlineKeyboardMarkup([portion_row, action_row])


def deleted_keyboard(meal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Restore", callback_data=f"undel:{meal_id}")]])


def next_meal_type(current: str) -> str:
    i = MEAL_TYPES.index(current) if current in MEAL_TYPES else -1
    return MEAL_TYPES[(i + 1) % len(MEAL_TYPES)]


def day_summary(user: User, day: date, meals: list[Meal], *, title: str | None = None) -> str:
    total = sum(m.kcal for m in meals)
    head = title or (f"📊 <b>Today</b> ({day:%a %d %b})" if day == user.today() else f"📊 <b>{day:%a %d %b}</b>")
    if not meals:
        return f"{head}\n\nNothing logged yet. Send a photo of your food to start!"
    lines = [head, ""]
    for mt in MEAL_TYPES:
        group = [m for m in meals if m.meal_type == mt]
        if not group:
            continue
        lines.append(f"{MEAL_EMOJI[mt]} <b>{mt.title()}</b> — {fmt_kcal(sum(m.kcal for m in group))}")
        for m in group:
            t = m.eaten_at.astimezone(user.zone).strftime("%H:%M")
            lines.append(f"   {t} {escape(clip(m.title, 60))} · {fmt_kcal(m.kcal)}")
    p = sum(m.protein_g for m in meals)
    c = sum(m.carbs_g for m in meals)
    f = sum(m.fat_g for m in meals)
    lines += ["", f"Protein {p:.0f}g · Carbs {c:.0f}g · Fat {f:.0f}g", day_line(user, total).replace("<b>Today:</b>", "<b>Total:</b>")]
    return "\n".join(lines)


def summary_keyboard(meals: list[Meal], restore: Meal | None = None) -> InlineKeyboardMarkup | None:
    """One delete button per entry (newest last). 'sdel' makes the handler refresh the summary in place;
    after a delete, a Restore button for that meal comes first so a mis-tap is one tap to undo."""
    rows = []
    if restore:
        rows.append([InlineKeyboardButton(f"↩️ Restore {clip(restore.title, 24)}", callback_data=f"sundel:{restore.id}")])
    buttons = [
        InlineKeyboardButton(f"🗑 {clip(m.title, 18)}", callback_data=f"sdel:{m.id}") for m in meals[-8:]
    ]
    rows += [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows) if rows else None


def week_text(user: User, totals: dict[date, dict[str, float]]) -> str:
    logged = [v for v in totals.values() if v["n"]]
    lines = ["📈 <b>Last 7 days</b>", ""]
    for d, v in totals.items():
        mark = ""
        if user.goal_kcal and v["n"]:
            # Only tick days that look fully logged; a 400 kcal day is probably missing meals, not a win.
            mark = " ✅" if user.goal_kcal * 0.6 <= v["kcal"] <= user.goal_kcal * 1.05 else ""
        lines.append(f"<code>{d:%a %d}</code>  {fmt_kcal(v['kcal']) if v['n'] else '—':>6}{mark}")
    if logged:
        avg = sum(v["kcal"] for v in logged) / len(logged)
        lines += ["", f"Average on logged days: <b>{fmt_kcal(avg)} kcal</b> ({len(logged)}/7 days logged)"]
        avg_p = sum(v["protein_g"] for v in logged) / len(logged)
        lines.append(f"Avg protein: {avg_p:.0f}g/day")
        if user.goal_kcal:
            lines.append(f"Goal: {fmt_kcal(user.goal_kcal)} kcal/day")
    else:
        lines += ["", "No meals logged this week yet."]
    return "\n".join(lines)


def week_chart(user: User, totals: dict[date, dict[str, float]]) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    days = list(totals)
    kcal = [totals[d]["kcal"] for d in days]
    goal = user.goal_kcal
    colors = ["#4C9F70" if (not goal or k <= goal * 1.05) else "#E9B44C" for k in kcal]
    fig, ax = plt.subplots(figsize=(7, 3.6), dpi=150)
    bars = ax.bar([d.strftime("%a\n%d") for d in days], kcal, color=colors, width=0.6)
    for b, k in zip(bars, kcal):
        if k:
            ax.text(b.get_x() + b.get_width() / 2, k, f"{round(k):,}", ha="center", va="bottom", fontsize=8)
    if goal:
        ax.axhline(goal, color="#555", linestyle="--", linewidth=1)
        ax.text(len(days) - 0.5, goal, f" goal {goal:,}", va="bottom", ha="right", fontsize=8, color="#555")
    ax.set_ylabel("kcal")
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_ylim(0, max([*kcal, goal or 0, 500]) * 1.15)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def help_text() -> str:
    return (
        "<b>How to use me</b>\n\n"
        "📸 <b>Send a photo</b> of your food (add a caption like “half rice” or “shared with 2” if useful). "
        "Several photos at once count as one meal.\n"
        "✍️ Or <b>type</b> it: “2 eggs, kaya toast, kopi c”\n"
        "🎙 Or send a <b>voice note</b> describing it.\n\n"
        "Every meal is saved automatically. Under each one you can:\n"
        "• tap ½ / 1½ / 2× to change the portion\n"
        "• tap ✏️ Fix (or just <i>reply</i> to it) to correct it, e.g. “no rice”, “it was teh peng”\n"
        "• tap the 🍱 meal button to switch breakfast → lunch → dinner → supper → snack\n"
        "• tap 🗑 Delete\n\n"
        "<b>Commands</b>\n"
        "/today – today's meals and total\n"
        "/yesterday – yesterday's meals\n"
        "/week – last 7 days with a chart\n"
        "/goal – set your daily calorie goal\n"
        "/reminder – evening recap on/off\n"
        "/undo – delete the last meal\n"
        "/notes – what I've learned about your habits\n"
        "/export – download everything as a CSV\n"
        "/cancel – stop whatever I was asking you\n\n"
        "You can also ask me things like “what should I eat for dinner with 500 kcal left?”\n\n"
        "<i>Numbers are estimates (usually within ~20%). Sauces, oil and sugar are the hardest to see, "
        "so mention them if you know.</i>"
    )
