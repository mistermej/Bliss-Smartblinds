"""Diagnostics support for Home Assistant.

This is Home Assistant's own read-only "Download diagnostics" button, found on
the integration page. Users attach the download to a bug report and it tells us
what the blind was doing without anyone having to describe it.

**This is not the diagnostic service set that v1.0 removed.** Those were
``send_raw``, ``move_rail``, ``poll_status`` and friends - arbitrary frames
driven at the motor, with ``send_raw`` able to drive it. This platform only
reads state that Home Assistant already holds and hands it back as JSON. It
cannot write to the blind.

Nothing here contains credentials: the MAC address is the blind's own hardware
identifier, which is already visible in the user's own config.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .client import BlissClient
from .const import DOMAIN, LOGGER_NAME

_LOGGER = logging.getLogger(LOGGER_NAME)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the state needed to diagnose a problem, read-only."""
    client: BlissClient = hass.data[DOMAIN][entry.entry_id]

    return {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "home_assistant": {"version": HA_VERSION},
        "client": {
            "address": client.address,
            "motor_range": client.motor_range,
            "movement_enabled": client.movement_enabled,
            "available": client.available,
            "seconds_since_seen": client.seconds_since_seen(),
            "rssi": _safe(client.async_get_rssi),
        },
        "positions": dict(client.position),
        # The raw motor code rather than only our label, so a report shows what
        # the blind actually said.
        "battery_code": client.battery,
        # Awaited, not called: async_flush persists the cache to storage, so
        # this shows what is actually saved rather than only what is in memory.
        "stored": await _safe_async(client.async_flush),
    }


async def _safe_async(coro: Any) -> Any:
    """Await something best-effort, returning the reason instead of raising."""
    try:
        return await coro
    except Exception as err:  # noqa: BLE001
        return f"unavailable: {type(err).__name__}: {err}"


def _safe(func: Any) -> Any:
    """Run something best-effort, returning the reason instead of raising.

    A diagnostics download that throws is worse than one with a gap in it: the
    user gets an error page instead of a file, and the reason for the problem is
    lost along with everything else.
    """
    try:
        return func()
    except Exception as err:  # noqa: BLE001
        return f"unavailable: {type(err).__name__}: {err}"
