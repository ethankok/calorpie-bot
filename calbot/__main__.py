import logging
import os

# Opt in to PTB returning durations as timedelta (the coming default); on_voice uses .total_seconds().
os.environ.setdefault("PTB_TIMEDELTA", "1")

from . import config
from .bot import build_app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # hide a log line for every job run
    app = build_app(config.load())
    app.run_polling(allowed_updates=["message", "edited_message", "callback_query"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
