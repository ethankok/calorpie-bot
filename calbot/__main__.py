import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# Opt in to PTB returning durations as timedelta (the coming default); on_voice uses .total_seconds().
os.environ.setdefault("PTB_TIMEDELTA", "1")

from . import config
from .bot import build_app


def log_in_timezone(tz: ZoneInfo) -> None:
    """Log in the users' timezone, not the container's UTC, so log lines line up with meal times."""
    # staticmethod: a plain function set on the class would be bound, get the formatter as `ts`, and break every log line.
    logging.Formatter.converter = staticmethod(lambda ts: datetime.fromtimestamp(ts, tz).timetuple())


def main() -> None:
    settings = config.load()
    log_in_timezone(ZoneInfo(settings.default_tz))
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # hide a log line for every job run
    app = build_app(settings)
    app.run_polling(allowed_updates=["message", "edited_message", "callback_query"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
