# calorie-bot

A private Telegram bot for counting calories. You send it a photo of your food, or type or say what you ate. It estimates the calories with Gemini, replies with a meal card, and keeps a daily total against your goal.

It is built for a few non-technical people in Singapore. It runs inside an app they already use, and its prompt includes reference values for hawker-centre food. It isn't publicly hosted: new users need an access code.

The parts worth reading are the [research behind the design](#research-how-the-estimates-work), the [accuracy eval](#measuring-it) and the [architecture](#architecture). To run your own copy, see [Running it](#running-it).

An example card:

```
🌙 Bowl of Laksa · supper
≈ 675 kcal
Protein 29g · Carbs 69g · Fat 32g

• Laksa noodles & gravy (1 standard bowl) — 490
• Laksa toppings (tau pok, prawns, fishcake) (standard serving) — 150
• Sambal chili (1 tbsp) — 35

💡 Estimated for a typical hawker bowl with coconut milk gravy. Finishing
all the rich broth adds around 150-200 kcal compared to leaving most behind.

Today: 675 / 1,800 kcal · 1,125 left
▓▓▓▓░░░░░░
[ ½ ] [ ✓ 1× ] [ 1½ ] [ 2× ]
[ ✏️ Fix ] [ 🌙 Supper ↻ ] [ 🗑 Delete ]
```

This is real output. In testing, replying "left most of the broth" lowered this card to 495 kcal.

## Using it

**Getting started.** Open the bot and send the access code. Then pick a daily goal: tap a preset, type a number, or tap "Help me pick" and describe yourself. You can also turn on an evening recap.

**Logging a meal.** Send any of these:

- a photo, or up to 6 photos at once (counted as one meal)
- text, such as "2 kaya toast, kopi c siew dai" (coffee with evaporated milk, less sugar)
- a voice note

A caption adds detail, such as "shared with 2". The bot guesses the meal type from the time of day. A meal of 400 kcal or more between 22:00 and 04:00 counts as supper. A day runs until 4am, so a 1am supper counts towards the day before.

**Fixing a meal.** To change an estimate:

- Tap a portion button: ½, 1×, 1½ or 2×.
- Reply to the card in your own words, such as "no rice" or "it was teh, not kopi". Gemini re-estimates using the original photo. Replying to your own photo works too.
- Within 15 minutes of logging, you don't need to reply. A message like "actually only half the rice" corrects the last meal.
- Tap the meal type to cycle through breakfast, lunch, dinner, supper and snack.
- Tap Delete. A Restore button undoes it.

**Asking questions.** "How many calories in a curry puff?" gets an answer and a "➕ Log it" button. Nothing is logged unless you tap it. The bot can also answer questions about your log, such as "what can I eat with 400 kcal left?"

**Habits.** Saying something like "I always ask for less rice" saves it as a note, and every later estimate uses it.

**Commands:**

| Command | What it does |
|---|---|
| `/today`, `/yesterday` | The day's meals and total |
| `/week` | The last 7 days, with a chart |
| `/undo` | Delete the last meal |
| `/goal` | Change the daily goal |
| `/reminder` | Set or turn off the evening recap |
| `/notes` | Show or clear saved habits |
| `/export` | Download all meals as a CSV file |
| `/cancel` | Stop whatever the bot is waiting for |
| `/help` | Show how to use the bot |

## Research: how the estimates work

Each photo, text or voice note goes to `gemini-3.8-flash` in one call. Gemini returns JSON that matches a fixed schema: the message type (meal, question or correction), a title, and a list of items with grams, kcal and macros. The code adds up the items itself.

The prompt asks Gemini to work the way a dietitian would:

1. List every component, including easy-to-miss calories: oil, gravy, sambal, coconut milk, and sugar in drinks.
2. Estimate grams from visual cues such as plate size, utensils and hands.
3. Convert grams to kcal.

The prompt includes reference values for Singapore food, such as 600–700 kcal for chicken rice and 100–140 kcal for kopi with condensed milk. Coffee-shop terms like *siew dai* (less sugar) and *kosong* (no sugar) change the values. A named chain item, such as a McSpicy, uses its published value.

### Design decisions

**Expect about 20% error per meal.** In an 802-meal feeding trial, Gemini 3.8 Flash was off by 21.9% per meal from a single photo [1]. Studies of older models found 36–64% error [2]. Models also overestimate fat [3], and much of the error comes from judging portion size, not from recognising the food [4]. So every estimate is easy to correct.

**One number, no confidence label.** An earlier version showed high, medium or low confidence and a kcal range. In testing, Gemini rated 8 of 9 real meals "high", so the label told the user nothing. Language models are known to report confidence that doesn't match how often they are right [5][6]. The five apps I checked (Cal AI, SnapCalorie, MyFitnessPal, Lose It! and MacroFactor) all show one editable number with no confidence label or range [7]. So the card does the same.

**Ask only when one detail matters.** Extra context is the biggest lever on accuracy. In one study, adding context cut error from 30.5% to 24.4%, and adding ingredient details cut it to 13.9% [8]. So the 💡 line, a question or a note on what the estimate assumed, appears only in two cases:

- One missing detail would change the total by about 150 kcal or more. Examples are how much gravy you drank, fried vs steamed, and sugar level.
- The input is too vague to estimate well, such as just "noodles".

The prompt tells it never to ask about portion size, sides you didn't mention, or anything visible in the photo. The question always comes with an estimate, never instead of one. A rough number you can fix in one reply is more useful than a question you must answer before anything is saved. The one case with no estimate is food the bot can't identify at all.

**Low temperature.** Estimates run at temperature 0.2. In testing, this gave more stable totals than the default of 1.0.

**No single right answer for hawker food.** Published values for the same dish disagree. For laksa, the Health Promotion Board (HPB) lists about 417 kcal a bowl. An A\*STAR study of bowls bought from hawker stalls measured about 881 kcal [9]. So the prompt gives ranges, and accuracy is only measured against foods with one published value, such as chain menu items.

### Measuring it

`tests/eval_estimates.py` runs 24 test foods against real Gemini, several times each. They include chain items, hawker dishes, vague inputs and photos. It reports:

- the error against published values
- how much the total changes between runs
- how often the card asks a question
- how often it asks when it shouldn't, or stays quiet when it should

On the last run, the foods with published values averaged about 4% error, and every run was within 20% of the published value. These are chain items and plain foods, which are the easiest case, so expect more error on hawker plates. It never asked about a precise input or stayed quiet about a vague one.

## Architecture

```
Telegram ──long polling──▶ PerChatUpdateProcessor ──▶ handlers (bot.py) ──▶ Estimator (estimator.py) ──▶ Gemini
                           (in order, per chat)               │
                                                              ▼
                                                SQLite (db.py) ──▶ meal card (render.py) ──▶ Telegram
```

The bot is one Python process built on [python-telegram-bot](https://python-telegram-bot.org/) (PTB). It uses long polling: it keeps asking Telegram for new messages, so it needs no public URL or open port.

Photos, text and voice notes are sent to Google's Gemini API to be estimated. Meals are stored only in the bot's SQLite database.

| File | What it does |
|---|---|
| `calbot/bot.py` | Telegram handlers: onboarding, logging, corrections, buttons, commands, reminders |
| `calbot/estimator.py` | Gemini prompts, the `Estimate` schema, and retries |
| `calbot/db.py` | SQLite storage, meal-type guessing, and the 4am day boundary |
| `calbot/render.py` | Meal cards, day summaries, and the weekly chart (matplotlib) |
| `calbot/config.py` | Settings from environment variables, falling back to the macOS Keychain |

**Messages are handled in order, one chat at a time.** After a restart or a network drop, Telegram can deliver many queued messages at once. `PerChatUpdateProcessor` runs different chats in parallel, but handles each chat's messages strictly in the order they arrived. Without it, `/undo` could run before the meal it should undo was saved.

**Each meal keeps its original estimate.** A meal row stores Gemini's estimate as JSON, plus a portion multiplier. The portion buttons rescale the stored estimate without calling Gemini again. A correction sends Gemini the stored estimate, the original photo and your words. Deleting a meal only marks it deleted, which is how Restore works.

**Old cards keep working.** Each button carries its meal's ID, and the `meal_links` table maps every bot message that shows a meal to that meal. So replies and taps on old cards reach the right meal, even after a restart. Pending "Try again" and "Log it" buttons are saved to disk too.

**Guards on cost and mistakes:**

- Each user gets 80 Gemini calls a day (`CALBOT_DAILY_AI_CAP`), and all users together get twice that. Corrections count.
- The same photo sent again within 30 minutes isn't counted twice. A similar meal within 10 minutes gets a "looks like a repeat" note.
- If Gemini fails 3 times in a row, the bot messages the owner (`CALBOT_OWNER_ID`). This catches a dead key or a spent budget.
- Every night at 04:30 it saves a dated copy of the database in `backups/`, next to the database, and keeps the newest 14. These copies are on the same disk.

## Running it

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

### Secrets

| Env var | Keychain item (Mac) | What it is |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `TELEGRAM_BOT_TOKEN_CALBOT` | Bot token from [@BotFather](https://t.me/BotFather) (`/newbot`) |
| `GEMINI_API_KEY` | `GEMINI_API_KEY_CALBOT` | Key from [Google AI Studio](https://aistudio.google.com/) |
| `ACCESS_CODE` | `ACCESS_CODE_CALBOT` | Any string you choose; new users send it to get in |

Set them as environment variables, or on a Mac store them in the Keychain. The bot and `eval_estimates.py` need all three. `e2e_scenario.py` needs only the Gemini key. `try_estimate.py` needs only the Gemini key.

These are optional:

| Env var | Default |
|---|---|
| `CALBOT_TZ` | `Asia/Singapore` |
| `CALBOT_DB` | `data/calbot.db` |
| `GEMINI_MODEL` | `gemini-3.8-flash` |
| `CALBOT_DAILY_AI_CAP` | `80` |
| `CALBOT_OWNER_ID` | none (set it to your Telegram user ID to get failure alerts) |

### Run and test

```bash
uv sync
uv run python -m calbot                            # run the bot
uv run pytest -q                                   # offline tests, no API calls
PYTHONPATH=. uv run python tests/e2e_scenario.py   # a full chat against real Gemini
PYTHONPATH=. uv run python tests/eval_estimates.py 3   # accuracy eval, 3 runs per food
PYTHONPATH=. uv run python scripts/try_estimate.py tests/fixtures/laksa.jpg "char kway teow"
```

`tests/fakegram.py` is a fake Telegram Bot API that runs inside the test process. It plugs into PTB as its HTTP layer, so whole conversations, including buttons, replies and albums, can be tested without a real bot token.

The offline tests stub Gemini, so they can't catch a change in how the real model reads a prompt. After changing a prompt or the schema, also run `e2e_scenario.py` and `eval_estimates.py`.

### Deploy

Production runs in Docker on a small always-on VM, an Oracle Cloud Always Free instance in Singapore. Put the secrets in an env file on the server, one `NAME=value` per line, readable only by you (`chmod 600`). The database lives on a volume mounted at `/data`.

```bash
rsync -a --delete --exclude .venv --exclude data --exclude .git ./ <host>:~/app/src/
ssh <host> 'cd ~/app/src && sudo docker build -t calbot . \
  && sudo docker stop -t 30 calbot; sudo docker rm calbot; \
  sudo docker run -d --name calbot --restart unless-stopped --env-file ~/app/calbot.env \
  -v /srv/calbot:/data --log-opt max-size=5m --log-opt max-file=3 calbot'
```

The logs should then say `Bot @<username> ready`.

Things to know:

- **One copy at a time.** Telegram allows one poller per bot token. A second copy causes `409 Conflict` errors.
- **Gemini regions.** Gemini refuses requests from some regions. Hong Kong was rejected; Singapore and Korea work.
- **Mac.** `scripts/install_mac.sh` runs the bot under launchd, macOS's background-service manager. It asks for the bot token, generates an access code, and fills in the paths in `deploy/calorie-bot.plist`. The Gemini key must already be in the Keychain.

## Inspiration

Photo-logging apps like Cal AI and SnapCalorie showed that a photo can be enough to log a meal. But they are separate apps with accounts and subscriptions, and they aren't tuned for hawker food. This bot puts the same idea inside Telegram, where there is nothing to install. It is tuned for Singapore food, and it stays small: it tracks calories and not much else.

A few open-source Telegram calorie bots were useful references: [phs_calorie_app](https://github.com/pyramidheadshark/phs_calorie_app) and [calorie_counter](https://github.com/ilyamil/calorie_counter). So was Google's [CalCam case study](https://developers.googleblog.com/calcam-transforming-food-tracking-with-the-gemini-api/) on food tracking with the Gemini API.

## Sources

1. Li et al., "Small Average Error, Wide Individual Error: General-Purpose LLMs Estimating Energy Intake from Meal Photos in a Feeding Trial", arXiv preprint, 2026. [arXiv:2610.26538](https://arxiv.org/abs/2610.26538)
2. LLM energy estimation on 52 food photos: GPT-4o 35.8%, Gemini 1.5 Pro 64.2% error. *Current Developments in Nutrition*, 2025. [PMC12513282](https://pmc.ncbi.nlm.nih.gov/articles/PMC12513282/)
3. LLMs vs dietitians on 15 standard hospital meals: fat overestimated by 34–52%. *Nutrients*, 2026. [PMC13029357](https://pmc.ncbi.nlm.nih.gov/articles/PMC13029357/)
4. Thames et al., "Nutrition5k", CVPR 2021: 26% error from images alone, 16.5% when depth is added. [PDF](https://openaccess.thecvf.com/content/CVPR2021/papers/Thames_Nutrition5k_Towards_Automatic_Nutritional_Understanding_of_Generic_Food_CVPR_2021_paper.pdf)
5. Xiong et al., "Can LLMs Express Their Uncertainty?", 2023. [arXiv:2306.13063](https://arxiv.org/abs/2306.13063)
6. Reward-model bias towards confident answers, 2024. [arXiv:2410.09724](https://arxiv.org/abs/2410.09724)
7. App help pages: [Cal AI](https://www.calai.app/faq), [SnapCalorie](https://www.snapcalorie.com/faq.html), [MyFitnessPal Meal Scan](https://support.myfitnesspal.com/hc/en-us/articles/360045761612-How-to-use-the-Meal-Scanner), [Lose It! Snap It](https://loseit.zendesk.com/hc/en-us/articles/47771695186580-How-to-Use-Snap-It), [MacroFactor](https://help.macrofactorapp.com/en/articles/258-ai-food-logging)
8. ChatGPT-5 energy estimation with and without added context. *Nutrients*, 2025. [PMC12655113](https://pmc.ncbi.nlm.nih.gov/articles/PMC12655113/)
9. Nutrient composition of Singapore hawker foods. *Foods*, 2021. [PMC8304763](https://pmc.ncbi.nlm.nih.gov/articles/PMC8304763/). HPB values are from [SG FoodID](https://pphtpc.hpb.gov.sg/web/sgfoodid/tools/food-search).

The published values in the eval come from the [McDonald's Singapore](https://www.mcdonalds.com.sg/food-menu/mcspicy) and [KFC Singapore](https://www.kfc.com.sg/nutritionandallergen) menus, and from USDA values for plain foods.

## License

MIT. See [LICENSE](LICENSE).
