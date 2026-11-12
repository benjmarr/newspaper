"""APScheduler jobs: daily build, nightly backup, periodic weather refresh."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import backup, weather
from .edition import Builder, parse_build_time

log = logging.getLogger("paper.scheduler")


class Jobs:
    def __init__(self, builder: Builder):
        self.builder = builder
        self.config = builder.config
        self.db = builder.db
        self.scheduler = BackgroundScheduler(timezone=self.config.tz)

    def start(self) -> None:
        self.reschedule_build()
        self.scheduler.add_job(self._backup, CronTrigger(hour=2, minute=30, timezone=self.config.tz),
                               id="backup", replace_existing=True, misfire_grace_time=3600, coalesce=True)
        self.scheduler.add_job(self._weather, IntervalTrigger(minutes=30), id="weather", replace_existing=True,
                               coalesce=True, max_instances=1)
        self.scheduler.start()
        if self.builder.needs_catch_up():
            log.info("Today's edition is missing and build time has passed: building now")
            self.builder.start_background(reason="startup catch-up")
        elif not self.db.get_setting("weather_cache", None):
            self.scheduler.add_job(self._weather, id="weather-now", replace_existing=True)

    def reschedule_build(self) -> None:
        hh, mm = parse_build_time(self.db.get_setting("build_time", "05:00"))
        self.scheduler.add_job(self._build, CronTrigger(hour=hh, minute=mm, timezone=self.config.tz),
                               id="build", replace_existing=True, misfire_grace_time=3 * 3600, coalesce=True)
        log.info("Daily build scheduled for %02d:%02d %s", hh, mm, self.config.tz_name)

    def next_build(self):
        job = self.scheduler.get_job("build")
        return job.next_run_time if job else None

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _build(self) -> None:
        self.builder.build(reason="scheduled")

    def _backup(self) -> None:
        backup.run_recorded(self.db, self.config)

    def _weather(self) -> None:
        if self.builder.building:
            return
        weather.refresh(self.db, self.config.tz_name, self.config.alert_feed_url,
                        self.builder.weather_client or weather._client)
