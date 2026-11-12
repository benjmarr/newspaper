"""Environment configuration. Everything else lives in the database and is editable in Settings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_ALERT_FEED = "http://www.bom.gov.au/fwo/IDZ00059.warnings_vic.xml"


@dataclass
class Config:
    app_password: str
    tz_name: str = "Australia/Melbourne"
    paper_name: str = "The Morning Edition"
    data_dir: Path = Path("/data")
    backup_keep: int = 14
    alert_feed_url: str = DEFAULT_ALERT_FEED
    # Lets tests and offline development run without the scheduler.
    disable_scheduler: bool = False
    tz: ZoneInfo = field(init=False)

    def __post_init__(self) -> None:
        self.tz = ZoneInfo(self.tz_name)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "paper.db"

    @property
    def image_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def load_config() -> Config:
    password = os.environ.get("APP_PASSWORD", "").strip()
    if not password:
        raise SystemExit(
            "APP_PASSWORD is not set. Put APP_PASSWORD=<something long> in your .env file and restart."
        )
    try:
        keep = max(1, int(os.environ.get("BACKUP_KEEP", "14")))
    except ValueError:
        keep = 14
    return Config(
        app_password=password,
        tz_name=os.environ.get("TZ", "Australia/Melbourne") or "Australia/Melbourne",
        paper_name=os.environ.get("PAPER_NAME", "").strip() or "The Morning Edition",
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        backup_keep=keep,
        alert_feed_url=os.environ.get("ALERT_FEED_URL", "").strip() or DEFAULT_ALERT_FEED,
        disable_scheduler=os.environ.get("DISABLE_SCHEDULER", "") == "1",
    )
