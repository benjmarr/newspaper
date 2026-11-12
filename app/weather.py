"""Weather from Open-Meteo, official warnings from the Bureau of Meteorology, derived heads-up chips."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import feedparser
import httpx

from . import text
from .db import Database, iso, parse_iso, utcnow
from .feeds import USER_AGENT

log = logging.getLogger("paper.weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
BOM_WARNINGS_PAGE = "http://www.bom.gov.au/vic/warnings/"
STALE_AFTER = timedelta(hours=3)

WMO = {
    0: ("Clear", "☀️"), 1: ("Mainly clear", "🌤️"), 2: ("Partly cloudy", "⛅"), 3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"), 48: ("Freezing fog", "🌫️"),
    51: ("Light drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Heavy drizzle", "🌧️"),
    56: ("Freezing drizzle", "🌧️"), 57: ("Freezing drizzle", "🌧️"),
    61: ("Light rain", "🌦️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"), 67: ("Freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"), 73: ("Snow", "🌨️"), 75: ("Heavy snow", "❄️"), 77: ("Snow grains", "🌨️"),
    80: ("Showers", "🌦️"), 81: ("Showers", "🌧️"), 82: ("Heavy showers", "🌧️"),
    85: ("Snow showers", "🌨️"), 86: ("Snow showers", "🌨️"),
    95: ("Thunderstorms", "⛈️"), 96: ("Thunderstorms with hail", "⛈️"), 99: ("Thunderstorms with hail", "⛈️"),
}
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def describe(code: int | None) -> tuple[str, str]:
    return WMO.get(int(code) if code is not None else -1, ("—", "🌡️"))


def compass(deg: float | None) -> str:
    if deg is None:
        return ""
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(15.0, connect=8.0), follow_redirects=True,
                        headers={"User-Agent": USER_AGENT})


def geocode(query: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    query = query.strip()
    if len(query) < 2:
        return []
    with (client or _client()) as c:
        resp = c.get(GEOCODE_URL, params={"name": query, "count": 10, "language": "en", "format": "json"})
        resp.raise_for_status()
        results = resp.json().get("results") or []
    return [{"name": r.get("name"), "admin1": r.get("admin1") or "", "country": r.get("country") or "",
             "latitude": round(float(r["latitude"]), 4), "longitude": round(float(r["longitude"]), 4)}
            for r in results if "latitude" in r and "longitude" in r]


def fetch_forecast(lat: float, lon: float, tz_name: str, client: httpx.Client | None = None) -> dict[str, Any]:
    params = {
        "latitude": lat, "longitude": lon, "timezone": tz_name, "forecast_days": 6,
        "wind_speed_unit": "kmh", "temperature_unit": "celsius", "precipitation_unit": "mm",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
                   "wind_speed_10m,wind_gusts_10m,wind_direction_10m,precipitation,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "precipitation_sum,wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,"
                 "uv_index_max,sunrise,sunset",
    }
    with (client or _client()) as c:
        resp = c.get(FORECAST_URL, params=params)
        if resp.status_code >= 400:
            try:
                reason = resp.json().get("reason") or ""
            except ValueError:
                reason = ""
            raise ValueError(f"Open-Meteo HTTP {resp.status_code} {reason}".strip())
        data = resp.json()
    cur = data.get("current") or {}
    d = data.get("daily") or {}
    days = []
    for i, day in enumerate(d.get("time") or []):
        def at(key):
            values = d.get(key) or []
            return values[i] if i < len(values) else None
        days.append({
            "date": day, "code": at("weather_code"), "max": at("temperature_2m_max"),
            "min": at("temperature_2m_min"), "rain_chance": at("precipitation_probability_max"),
            "rain_mm": at("precipitation_sum"), "wind": at("wind_speed_10m_max"),
            "gusts": at("wind_gusts_10m_max"), "wind_dir": compass(at("wind_direction_10m_dominant")),
            "uv": at("uv_index_max"), "sunrise": (at("sunrise") or "")[-5:], "sunset": (at("sunset") or "")[-5:],
        })
    if not days:
        raise ValueError("Forecast response had no daily data")
    return {
        "current": {
            "temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"), "code": cur.get("weather_code"),
            "wind": cur.get("wind_speed_10m"), "gusts": cur.get("wind_gusts_10m"),
            "wind_dir": compass(cur.get("wind_direction_10m")), "precip": cur.get("precipitation"),
            "time": cur.get("time"),
        },
        "days": days,
    }


def fetch_warnings(url: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    with (client or _client()) as c:
        resp = c.get(url)
        resp.raise_for_status()
        content = resp.content
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries and not parsed.feed.get("title"):
        raise ValueError("Warnings feed was not readable")
    items = []
    for e in parsed.entries[:20]:
        title = text.strip_html(e.get("title") or "")
        link = e.get("link") or BOM_WARNINGS_PAGE
        if not link.startswith(("http://", "https://")):
            link = BOM_WARNINGS_PAGE
        if title:
            items.append({"title": title, "link": link})
    return items


def derive_chips(forecast: dict[str, Any] | None) -> list[dict[str, str]]:
    """Heads-up chips worked out from the forecast numbers (not official warnings)."""
    if not forecast or not forecast.get("days"):
        return []
    chips = []
    for label, day in zip(("Today", "Tomorrow"), forecast["days"][:2]):
        def num(key):
            v = day.get(key)
            return float(v) if v is not None else None
        tmax, tmin, gusts = num("max"), num("min"), num("gusts")
        rain, chance, uv = num("rain_mm"), num("rain_chance"), num("uv")
        code = int(day["code"]) if day.get("code") is not None else -1
        if tmax is not None and tmax >= 38:
            chips.append(("heat", f"{label}: extreme heat, {tmax:.0f}°"))
        elif tmax is not None and tmax >= 32:
            chips.append(("heat", f"{label}: hot, {tmax:.0f}°"))
        if tmin is not None and tmin <= 2:
            chips.append(("frost", f"{label}: frost risk, low {tmin:.0f}°"))
        if gusts is not None and gusts >= 60:
            chips.append(("wind", f"{label}: strong wind, gusts {gusts:.0f} km/h"))
        if (rain is not None and rain >= 10) or (chance is not None and chance >= 80 and (rain or 0) >= 4):
            chips.append(("rain", f"{label}: wet day, {rain or 0:.0f} mm"))
        if uv is not None and uv >= 8 and label == "Today":
            chips.append(("uv", f"{label}: UV {uv:.0f}, {'extreme' if uv >= 11 else 'very high'}"))
        if code in (95, 96, 99):
            chips.append(("storm", f"{label}: thunderstorms possible"))
    return [{"kind": k, "text": t} for k, t in chips]


def refresh(db: Database, tz_name: str, alert_url: str, client_factory=_client) -> None:
    """Update the saved forecast and warnings. Failures are recorded, never raised."""
    loc = db.get_setting("location")
    now = iso(utcnow())
    try:
        forecast = fetch_forecast(loc["latitude"], loc["longitude"], tz_name, client_factory())
        db.set_setting("weather_cache", {"forecast": forecast, "fetched_at": now, "location": loc})
        db.set_setting("weather_error", None)
    except Exception as exc:
        log.warning("Forecast fetch failed: %s", exc)
        db.set_setting("weather_error", {"error": str(exc)[:200] or type(exc).__name__, "at": now})
    try:
        items = fetch_warnings(alert_url, client_factory())
        db.set_setting("warnings_cache", {"items": items, "fetched_at": now, "ok": True})
    except Exception as exc:
        log.warning("Warnings fetch failed: %s", exc)
        prev = db.get_setting("warnings_cache", {}) or {}
        db.set_setting("warnings_cache", {"items": prev.get("items", []), "fetched_at": prev.get("fetched_at"),
                                          "ok": False, "error": f"{type(exc).__name__}"[:120], "failed_at": now})


def snapshot(db: Database) -> dict[str, Any]:
    """What gets stored with an edition (and shown for the latest edition, refreshed live)."""
    return {"weather": db.get_setting("weather_cache", {}) or {},
            "weather_error": db.get_setting("weather_error", None),
            "warnings": db.get_setting("warnings_cache", {}) or {}}


def view(snap: dict[str, Any], tz, today: date | None = None) -> dict[str, Any]:
    """Shape a snapshot for the template."""
    cache = snap.get("weather") or {}
    forecast = cache.get("forecast")
    fetched = parse_iso(cache.get("fetched_at"))
    err = snap.get("weather_error")
    stale = bool(forecast) and (err is not None or (fetched is not None and utcnow() - fetched > STALE_AFTER))
    days = []
    if forecast:
        for d in forecast["days"]:
            try:
                dd = date.fromisoformat(d["date"])
            except (ValueError, TypeError):
                continue
            desc, icon = describe(d.get("code"))
            days.append({**d, "desc": desc, "icon": icon, "dow": dd.strftime("%a"), "day": dd})
        if today is not None:
            # When showing an old saved forecast, start from today if it is in the data.
            later = [d for d in days if d["day"] >= today]
            days = later or days
    current = None
    if forecast and forecast.get("current"):
        c = forecast["current"]
        desc, icon = describe(c.get("code"))
        current = {**c, "desc": desc, "icon": icon}
    warnings = snap.get("warnings") or {}
    return {
        "location": cache.get("location"),
        "current": current,
        "today": days[0] if days else None,
        "outlook": days[1:6],
        "as_of": fetched.astimezone(tz) if fetched else None,
        "stale": stale,
        "error": err,
        "chips": derive_chips({"days": days}) if days else [],
        "warnings": warnings.get("items", []),
        "warnings_ok": warnings.get("ok", False),
        "warnings_known": bool(warnings),
        "warnings_as_of": (parse_iso(warnings.get("fetched_at")).astimezone(tz)
                           if warnings.get("fetched_at") else None),
        "warnings_page": BOM_WARNINGS_PAGE,
    }
