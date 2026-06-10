"""Run smoke_test.py against a `trading_bot_smoke` database on the configured
server (BOT_DATABASE_URL from .env), instead of hardcoded localhost."""
import os
import re
import runpy

from app.config import load_settings

url = load_settings().database_url
os.environ["BOT_SMOKE_DATABASE_URL"] = re.sub(r"/[^/?]+$", "/trading_bot_smoke", url)
runpy.run_path("smoke_test.py", run_name="__main__")
