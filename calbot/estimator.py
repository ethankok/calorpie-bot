"""Food -> calorie/macro estimation with Gemini (vision, text and voice)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MODEL = "gemini-3.8-flash"  # keep in sync with the GEMINI_MODEL default in config.py


class FoodItem(BaseModel):
    name: str = Field(description="Short dish/component name, e.g. 'steamed chicken', 'white rice'")
    portion: str = Field(description="Human portion, e.g. '1 plate', '1 cup (200 g)', '2 pieces'")
    grams: float = Field(description="Estimated edible weight in grams (ml for drinks)")
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float


class Estimate(BaseModel):
    kind: Literal["food", "not_food", "question", "unclear", "correction", "new_meal"] = Field(
        description=(
            "food: the input describes or shows something eaten/drunk to log. "
            "correction: the message adjusts the PREVIOUS logged meal (only possible when one is given) "
            "rather than describing new food, e.g. 'actually only half', 'no rice', 'it was teh not kopi'. "
            "not_food: a photo with no food or drink in it. "
            "question: the user is asking something rather than logging food (if they describe food they ate "
            "AND ask about it, e.g. 'had chicken rice, is that ok?', use food and answer briefly in note). "
            "unclear: food is present but too ambiguous to estimate at all. "
            "new_meal: only when correcting a meal: the message is not a change to that meal but describes a "
            "different, separate meal or snack (e.g. correcting chicken rice with '2 eggs and kaya toast for breakfast')."
        )
    )
    title: str = Field(description="2-6 word summary of the meal, e.g. 'Chicken rice + iced Milo'. Empty if not food.")
    items: list[FoodItem] = Field(default_factory=list)
    meal_type_hint: Literal["breakfast", "lunch", "dinner", "supper", "snack", "none"] = Field(
        "none", description="Only if the user explicitly says which meal this was; otherwise 'none'."
    )
    yesterday: bool = Field(False, description="True only if the user explicitly says they ate this yesterday.")
    remember: str = Field(
        "",
        description=(
            "Only when the user EXPLICITLY states a recurring habit with words like 'always', 'usually', "
            "'never', 'I normally' (e.g. 'I always ask for less rice' -> 'Usually orders less rice'). "
            "A one-off correction about this meal is NOT a habit. When in doubt, leave empty."
        ),
    )
    user_said: str = Field(
        "", description="For audio input only: a short transcription of what the user said. Otherwise empty."
    )
    note: str = Field(
        "",
        description=(
            "For food: usually empty. Ask one short, friendly question only if the food is too vague to pin down "
            "(e.g. just 'noodles') or one detail would swing the total by 150+ kcal (e.g. how much of a rich soup or "
            "gravy was drunk, fried vs steamed, sugar level); name the detail and roughly how much it changes. Assume a standard portion and recipe rather than asking about them. Never "
            "ask about sides, drinks or extras that weren't mentioned or shown, or anything visible in the photo. "
            "If they also asked something, answer it briefly here instead. "
            "For question/not_food/unclear: a helpful reply to the user."
        ),
    )

    def scaled(self, factor: float) -> "Estimate":
        """The same estimate with every item multiplied by factor (for portion changes)."""
        if factor == 1.0:
            return self
        items = [
            i.model_copy(update={
                "grams": round(i.grams * factor), "kcal": round(i.kcal * factor),
                "protein_g": round(i.protein_g * factor, 1), "carbs_g": round(i.carbs_g * factor, 1),
                "fat_g": round(i.fat_g * factor, 1),
                "portion": f"{i.portion} × {factor:g}",
            })
            for i in self.items
        ]
        return self.model_copy(update={"items": items})

    @property
    def kcal(self) -> float:
        return sum(i.kcal for i in self.items)

    @property
    def protein_g(self) -> float:
        return sum(i.protein_g for i in self.items)

    @property
    def carbs_g(self) -> float:
        return sum(i.carbs_g for i in self.items)

    @property
    def fat_g(self) -> float:
        return sum(i.fat_g for i in self.items)


SYSTEM_PROMPT = """\
You are a careful registered dietitian estimating what someone ate, for a personal calorie log.
The user is in Singapore: expect hawker/kopitiam food, home-cooked Chinese/Malay/Indian dishes,
bubble tea, kopi/teh, and also Western and Japanese/Korean food. Use local portion norms
(e.g. a hawker plate of chicken rice ~ 600 kcal; kopi with condensed milk ~ 100-140 kcal).

Method (do this internally, then fill the schema):
1. Identify every distinct component, including easy-to-miss calories: cooking oil, gravy, sauces,
   chili/sambal, fried shallots, coconut milk, sugar in drinks, dressings, toppings.
2. Estimate each component's weight in grams from visual cues: plate/bowl size (hawker plate ~ 23-26 cm,
   rice bowl ~ 11-12 cm), utensils, hands, packaging, and how full the container is.
3. Convert grams to kcal and macros using standard nutrition values for that preparation
   (fried vs steamed matters; rice cooked in chicken fat or coconut milk is denser than plain rice).
4. If the meal looks partly eaten or shared, estimate what is visible unless the user says otherwise.
5. Sum the items. Then decide which one missing detail, if any, would change the total the most.

Rules:
- The user's own words (caption, text, voice) override what you see: if they say "half", "no rice",
  "shared with 2 people", "large", or give a brand/restaurant, apply it.
- If the user names a packaged/chain item (e.g. "McSpicy", "Koi milk tea 50% sugar"), use known values.
- When the user logs food they ate, estimate it even when unsure rather than holding back to ask:
  several photos or dishes sent together are one meal, so include them all. Use kind=unclear only when
  the food can't be identified at all. (Asking about a food, e.g. "how many calories in X?", is still
  kind=question.)
- Keep item names short. Merge trivial garnishes into the main item.
- Round kcal to whole numbers and macros to 1 decimal.
- Never moralise about food choices.

Typical Singapore hawker reference values (standard plate/bowl; adjust to what you see):
chicken rice (roasted/steamed) 600-700 | char kway teow 740-850 | nasi lemak with fried chicken 700-850 |
laksa 600-700 | prawn mee soup 300-450 | fishball noodle soup (dry ~ +100) 350-450 | wanton mee dry 400-500 |
mee goreng 600-700 | bak chor mee dry 500-550 | hokkien mee 520-620 | fried carrot cake 400-550 |
roti prata plain 1 pc 150-200 (egg 230-270) | economic rice 2 veg 1 meat 550-700 | yong tau foo soup 300-400 |
fish soup with bee hoon 300-400 (milk +100) | chee cheong fun 300-350 | kaya toast set 1 slice-pair 250-300 |
kopi (condensed milk) 100-140, kopi-o 60-90, kopi-c 90-120, siew dai ~ -25%, kosong ~0-15 |
bubble tea 500 ml full sugar with pearls 300-450 (0% sugar ~ -150).
"Less oil", "less rice", "kosong", "siew dai", "dry/soup" materially change values; apply them.
"""

REFINE_PROMPT = """\
Here is the current estimate for this meal as JSON (already adjusted to the portion the user ate):
{previous}
{original}
The user says this needs correcting: "{correction}"

Return the full corrected estimate (all items, not just changed ones). Keep anything the user did not
dispute. If the correction only rescales the portion, rescale the affected items. Adding a forgotten item
("also had a kopi", "plus an egg") is a correction: include it. But if the message plainly describes a
different, separate meal instead of this one, return kind "new_meal" with no items. If the user says they
didn't eat any of it, return kind "not_food" with no items.
The correction often answers the question in the estimate's note: apply the answer and leave note empty,
unless the user asked something (answer it there) or a different detail would still swing the total by 150+ kcal.
"""


class GoalSuggestion(BaseModel):
    about_goal: bool = Field(
        True,
        description="False if the message is not about the user's body stats or goals at all "
        "(e.g. it describes food they ate or asks something unrelated).",
    )
    ok: bool = Field(description="False if there isn't enough info to suggest a goal")
    goal_kcal: int = Field(0, description="Suggested daily calorie target, rounded to nearest 50")
    explanation: str = Field(description="1-2 friendly sentences: how you got the number, or what info is missing")


GOAL_PROMPT = """\
The user wants help choosing a daily calorie target. They wrote: "{text}"
If they gave sex, age, height, weight and activity level (and optionally a goal like lose/maintain/gain),
compute TDEE with Mifflin-St Jeor and an activity multiplier, then apply the goal
(lose: -300 to -500 kcal, never below 1200 for women / 1500 for men; gain: +250 to +400).
If something essential is missing, set ok=false and ask for exactly what is missing.
"""

ANSWER_PROMPT = """\
You are a friendly, concise nutrition assistant inside a Telegram calorie-tracking bot for someone in Singapore.
Answer the user's message in at most 4 short sentences of plain text (no markdown).
Use their log below when relevant. If they want to log, edit or delete something, tell them how:
send a photo or describe the food to log; reply to a meal message to correct it; /undo removes the last entry;
/today, /week, /goal, /reminder, /help are the commands.

Their data:
{context}

Their message: "{question}"
"""


@dataclass
class MediaPart:
    data: bytes
    mime_type: str


class EstimatorError(RuntimeError):
    """Raised when Gemini cannot produce an estimate after retries.

    reason: 'quota' (rate limit / budget exhausted), 'rejected' (request refused, e.g. bad image or key),
    or 'network' (timeouts, 5xx, unusable output).
    """

    def __init__(self, message: str, reason: str = "network"):
        super().__init__(message)
        self.reason = reason


class Estimator:
    def __init__(self, api_key: str, model: str = MODEL):
        # Without a timeout a stalled request (e.g. a dropped connection mid-call) would hang forever.
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=30_000))
        self.model = model
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=Estimate,
            # Measured on the fixtures: 0.2 gave steady ~6s answers; the default 1.0 occasionally stalled
            # for 30s+ and then returned a "food" estimate with no items.
            temperature=0.2,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        )

    async def estimate(
        self, *, text: str = "", media: list[MediaPart] | None = None, notes: str = "", recent_meal: str = ""
    ) -> Estimate:
        prompt = _notes_block(notes)
        if recent_meal:
            prompt += (
                f"The user's previous logged meal (a few minutes ago) was: {recent_meal}\n"
                "If this message is a correction of that meal rather than new food, set kind=correction "
                "and leave items empty. A message that just names or describes that same food again "
                "(e.g. 'laksa' after a laksa photo) is a correction too, unless it says another / again / "
                "one more / second, which is new food.\n\n"
            )
        prompt += "Estimate this meal."
        if media and any(m.mime_type.startswith("audio/") for m in media):
            prompt += (
                "\nThe audio is the user speaking. Transcribe it into user_said. If they describe food they ate, "
                "estimate that meal; if they ask a question, set kind=question."
            )
        if text:
            prompt += f'\nThe user wrote: "{text}"'
        # Text before media, per Google's guidance for multimodal prompts.
        parts = [types.Part.from_text(text=prompt), *_media_parts(media)]
        return await self._call(parts)

    async def refine(
        self,
        previous: Estimate,
        correction: str,
        media: list[MediaPart] | None = None,
        notes: str = "",
        original_text: str = "",
    ) -> Estimate:
        original = f'When logging it, the user originally wrote: "{original_text}"\n' if original_text else ""
        prompt = _notes_block(notes) + REFINE_PROMPT.format(
            previous=previous.model_dump_json(include=_ESTIMATE_CORE), correction=correction, original=original
        )
        est = await self._call([types.Part.from_text(text=prompt), *_media_parts(media)])
        if est.kind == "correction":  # the model sometimes labels a corrected estimate this way; it is food
            est.kind = "food"
        return est

    async def suggest_goal(self, text: str) -> GoalSuggestion:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GoalSuggestion,
            temperature=0.1,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        )
        resp = await self._generate([types.Part.from_text(text=GOAL_PROMPT.format(text=text))], config)
        return resp.parsed if isinstance(resp.parsed, GoalSuggestion) else GoalSuggestion.model_validate_json(resp.text)

    async def answer(self, question: str, context: str) -> str:
        config = types.GenerateContentConfig(
            temperature=0.4,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        )
        prompt = ANSWER_PROMPT.format(context=context, question=question)
        resp = await self._generate([types.Part.from_text(text=prompt)], config)
        return (resp.text or "").strip() or "Sorry, I'm not sure about that one."

    async def _generate(self, parts: list[types.Part], config: types.GenerateContentConfig):
        last_exc: Exception | None = None
        reason = "network"
        for attempt in range(3):
            try:
                return await self.client.aio.models.generate_content(
                    model=self.model,
                    contents=[types.Content(role="user", parts=parts)],
                    config=config,
                )
            except genai_errors.ClientError as exc:
                last_exc = exc
                if getattr(exc, "code", None) != 429:
                    # Other 4xx (bad key, unreadable image, blocked) won't get better on retry.
                    raise EstimatorError(str(exc), "rejected") from exc
                reason = "quota"
                if attempt >= 1:  # one retry for a burst limit; a spent budget won't recover
                    break
            except Exception as exc:  # network errors, timeouts, 5xx
                last_exc = exc
                reason = "network"
            log.warning("Gemini attempt %d failed: %s", attempt + 1, last_exc)
            await asyncio.sleep(1.5 * (attempt + 1))
        raise EstimatorError(str(last_exc), reason) from last_exc

    async def _call(self, parts: list[types.Part]) -> Estimate:
        for attempt in range(2):
            resp = await self._generate(parts, self.config)
            try:
                est = resp.parsed if isinstance(resp.parsed, Estimate) else (
                    Estimate.model_validate_json(resp.text) if resp.text else None
                )
            except ValueError as exc:
                log.warning("Unparseable estimate (attempt %d): %s", attempt + 1, exc)
                continue
            if est is not None and est.kind == "food" and not est.items:  # 0 kcal is fine (water, coke zero)
                log.warning("Food estimate with no items (attempt %d)", attempt + 1)  # seen after a stalled call
                continue
            if est is not None:
                return est
            log.warning("Empty estimate (attempt %d, finish=%s)", attempt + 1, _finish_reason(resp))
        raise EstimatorError("Gemini returned no usable estimate")


_ESTIMATE_CORE = {"kind", "title", "items", "note"}


def _media_parts(media: list[MediaPart] | None) -> list[types.Part]:
    return [types.Part.from_bytes(data=m.data, mime_type=m.mime_type) for m in media or []]


def _notes_block(notes: str) -> str:
    if not notes.strip():
        return ""
    return f"Things to remember about this user's habits (apply when relevant):\n{notes.strip()}\n\n"


def _finish_reason(resp: types.GenerateContentResponse) -> str:
    try:
        return str(resp.candidates[0].finish_reason)
    except (IndexError, TypeError, AttributeError):
        return "unknown"
