"""Try the estimator from the command line on image paths and/or food descriptions.

Usage: PYTHONPATH=. uv run python scripts/try_estimate.py tests/fixtures/laksa.jpg "char kway teow"
Needs only the Gemini key (env var or Keychain, see calbot/config.py).
"""
import asyncio, sys, time, pathlib
from calbot.config import _secret
from calbot.estimator import Estimator, MediaPart

est = Estimator(_secret("GEMINI_API_KEY"))

async def main():
    for arg in sys.argv[1:]:
        t = time.time()
        p = pathlib.Path(arg)
        if p.exists():
            r = await est.estimate(media=[MediaPart(p.read_bytes(), "image/jpeg")])
        else:
            r = await est.estimate(text=arg)
        print(f"== {arg} ({time.time()-t:.1f}s) kind={r.kind} title={r.title!r} kcal={r.kcal:.0f}")
        for i in r.items: print(f"   {i.name}: {i.portion} {i.grams:.0f}g {i.kcal:.0f}kcal P{i.protein_g} C{i.carbs_g} F{i.fat_g}")
        print("   note:", r.note)
asyncio.run(main())
