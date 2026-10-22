"""Constants for Smarthome UI Core."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "smarthome_ui"

STORAGE_KEY: Final = "smarthome_ui_core"
STORAGE_VERSION: Final = 1

# Data the dashboard used to keep in its frontend storage and now keeps here, in the same shape.
DATA_KEYS: Final = ("done", "done_log", "household", "mow_log")
DONE_LOG_MAX: Final = 150

SIGNAL_DATA: Final = f"{DOMAIN}_data"
SIGNAL_TASKS: Final = f"{DOMAIN}_tasks"
SIGNAL_NOTIFY: Final = f"{DOMAIN}_notify"
SIGNAL_SENT: Final = f"{DOMAIN}_sent"
SIGNAL_DISPLAY_NEW: Final = f"{DOMAIN}_display_new"
SIGNAL_MOWER: Final = f"{DOMAIN}_mower"
SIGNAL_VACUUM: Final = f"{DOMAIN}_vacuum"
SIGNAL_PLANNER: Final = f"{DOMAIN}_planner"
SIGNAL_WASTE: Final = f"{DOMAIN}_waste"

# Notifications
TICK_INTERVAL: Final = timedelta(seconds=30)
PLANNER_INTERVAL: Final = timedelta(minutes=1)
WASTE_INTERVAL: Final = timedelta(hours=1)
CHORES_INTERVAL: Final = timedelta(minutes=15)
WATCH_INTERVAL: Final = timedelta(minutes=30)
CALENDAR_INTERVAL: Final = timedelta(minutes=5)
REPORT_TTL: Final = timedelta(minutes=2)  # a dashboard counts as gone if it stops reporting
FORGET_AFTER: Final = timedelta(minutes=15)  # a resolved notification may come again after this
CHANGE_COOLDOWN: Final = timedelta(seconds=30)
RECENT_MAX: Final = 20
DEFAULT_TASK_HOUR: Final = 9
# an area only counts as mowed after this many minutes there (unless a job says so)
MOW_MIN_MINUTES: Final = 10
# a room counts as cleaned by the robot after this many minutes of cleaning there
VAC_MIN_MINUTES: Final = 2

SERVICE_TASK_DONE: Final = "task_done"
SERVICE_TASK_REOPEN: Final = "task_reopen"
SERVICE_WASTE_ACK: Final = "waste_ack"
SERVICE_WASTE_SKIP: Final = "waste_skip"
SERVICE_AREA_MOWED: Final = "area_mowed"
SERVICE_TEST_NOTIFY: Final = "test_notify"
SERVICE_VACUUM_ROOMS: Final = "vacuum_rooms"

ATTR_TASK: Final = "task"
ATTR_DATE: Final = "date"
ATTR_AREA: Final = "area"
ATTR_MESSAGE: Final = "message"
