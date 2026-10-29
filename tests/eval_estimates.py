"""Measure estimate accuracy, run-to-run stability and when the card asks a question, against real Gemini.

Usage: PYTHONPATH=. .venv/bin/python tests/eval_estimates.py [runs] [out.json]
Each case runs `runs` times (default 3). Truth values are published figures (HPB / chain menus), so
they're only "about right" for any one plate; what matters is the trend across the set.
"""
import asyncio, json, pathlib, statistics, sys, time

from calbot import config
from calbot.estimator import Estimator, MediaPart

FIX = pathlib.Path(__file__).parent / "fixtures"

# (label, text, photo, truth kcal, should the card ask a question?). Truths are published figures:
# McDonald's SG / KFC SG menus and USDA; hawker dishes have none because HPB and lab studies of bought
# plates differ by up to 2x. None = no expectation.
CASES = [
    ("precise weights", "200g cooked white rice and 150g steamed skinless chicken breast", None, 508, False),
    ("McSpicy", "McSpicy burger", None, 541, False),
    ("Big Mac", "Big Mac", None, 558, False),
    ("McChicken", "McChicken", None, 391, False),
    ("medium fries", "McDonald's medium fries", None, 384, False),
    ("KFC thigh", "1 KFC original recipe thigh", None, 309, False),
    ("apple", "1 red apple", None, 95, False),
    ("kopi c siew dai", "kopi c siew dai", None, None, False),
    ("roti prata", "1 plain roti prata", None, None, False),
    ("chicken rice", "chicken rice", None, None, None),
    ("char kway teow", "char kway teow", None, None, None),
    ("laksa", "laksa", None, None, None),
    ("nasi lemak", "nasi lemak with fried chicken wing", None, None, None),
    ("economic rice", "economic rice 2 veg 1 meat", None, None, None),
    ("LeNu", "lenu pork wanton rice noodle", None, None, None),
    ("chicken alfredo", "chicken alfredo", None, None, None),
    ("noodles (vague)", "noodles", None, None, True),
    ("home pasta (vague)", "some pasta my mum made", None, None, True),
    ("photo chicken rice", "", "chicken_rice.jpg", None, None),
    ("photo laksa", "", "laksa.jpg", None, True),
    ("photo laksa, half", "shared with my bf, I had half", "laksa.jpg", None, None),
    ("photo nasi lemak", "", "nasi_lemak.jpg", None, None),
    ("photo burger", "", "burger.jpg", None, None),
    ("photo salad", "", "salad.jpg", None, None),
]


async def run_case(est: Estimator, sem: asyncio.Semaphore, case) -> dict:
    label, text, photo, truth, _ = case
    media = [MediaPart((FIX / photo).read_bytes(), "image/jpeg")] if photo else None
    async with sem:
        t = time.time()
        try:
            r = await est.estimate(text=text, media=media)
        except Exception as exc:  # keep going; one flaky call shouldn't sink the run
            return {"label": label, "error": str(exc)[:200]}
    return {
        "label": label, "truth": truth, "kind": r.kind, "kcal": round(r.kcal), "note": r.note, "secs": round(time.time() - t, 1),
        "items": [f"{i.name} {i.grams:.0f}g {i.kcal:.0f}" for i in r.items],
    }


async def main(runs: int, out: str | None) -> None:
    est = Estimator(config.load().gemini_api_key)
    sem = asyncio.Semaphore(4)
    results = await asyncio.gather(*(run_case(est, sem, c) for c in CASES for _ in range(runs)))
    if out:
        pathlib.Path(out).write_text(json.dumps(results, indent=1, ensure_ascii=False))
    ok = [r for r in results if "error" not in r]
    print(f"{'case':22} {'kcal (runs)':22} {'asks':5} truth")
    wrong_asks = 0
    for label, _, _, truth, expect_ask in CASES:
        rs = [r for r in ok if r["label"] == label]
        if not rs:
            print(f"{label:22} all runs failed")
            continue
        asks = sum(bool(r["note"]) for r in rs)
        if expect_ask is not None:
            wrong_asks += sum(bool(r["note"]) != expect_ask for r in rs)
        kcals = " ".join(str(r["kcal"]) for r in rs)
        print(f"{label:22} {kcals:22} {asks}/{len(rs):<3} {truth or ''}")
    print(f"\nerrors: {len(results) - len(ok)}   cards asking a question: {sum(bool(r['note']) for r in ok)}/{len(ok)}"
          f"   asked when it shouldn't / didn't when it should: {wrong_asks}")
    spread = []
    for label, *_ in CASES:
        ks = [r["kcal"] for r in ok if r["label"] == label]
        if len(ks) > 1 and statistics.mean(ks):
            spread.append(statistics.pstdev(ks) / statistics.mean(ks))
    if spread:
        print(f"run-to-run spread: median {statistics.median(spread):.0%}, max {max(spread):.0%}")
    truthy = [r for r in ok if r["truth"]]
    if truthy:
        mape = statistics.mean(abs(r["kcal"] - r["truth"]) / r["truth"] for r in truthy)
        within = sum(abs(r["kcal"] - r["truth"]) <= 0.2 * r["truth"] for r in truthy) / len(truthy)
        print(f"vs published values ({len(truthy)} runs): mean error {mape:.0%}, within 20%: {within:.0%}")

if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3, sys.argv[2] if len(sys.argv) > 2 else None))
