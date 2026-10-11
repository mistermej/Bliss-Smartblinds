"""The Bliss Smart Blinds integration.

Unofficial community integration. Not affiliated with or endorsed by
Hunter Douglas. The BLE protocol was reverse-engineered by observing the
vendor Android app.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .client import BlissClient
from .const import DOMAIN, LOGGER_NAME

_LOGGER = logging.getLogger(LOGGER_NAME)

PLATFORMS = ["cover", "sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one blind from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    address = entry.data["address"]
    name = entry.data.get("name") or address
    motor_range = entry.data.get("motor_range", 1000)

    client = BlissClient(hass, address, name, motor_range)

    # Movement is allowed by default. The rail guard (rawTop >= rawBottom, taken
    # from the vendor app) always applies, and post-move verification always
    # reports whether the motor complied. The previous per-session
    # `set_movement_enabled` gate was a testing scaffold: it defaulted to off,
    # lived in memory only, and left configured entities unable to do anything.
    # It is still available as a service to stop movement without editing config.
    client.movement_enabled = entry.options.get("allow_movement", True)
    # Restore persisted positions before the first operation, so a save can
    # never be skipped and the entities have their state from the start.
    client.async_setup_storage(entry.entry_id)

    # Deliberately NOT fatal. These motors are sleepy and may be out of range at
    # startup; failing setup would put the entry in a retry loop forever. The
    # entities reconnect on their own once the blind advertises again.
    try:
        await client.async_connect()
    except ConnectionError as err:
        _LOGGER.debug(
            "%s: not reachable yet, will retry in the background: %s", address, err
        )
    else:
        # Read the real positions straight away, so a newly added blind shows
        # where it actually is instead of sitting on 'unknown' until someone
        # thinks to sync it. Read-only, and best effort: a sleepy motor that
        # does not answer here can be synced later.
        try:
            await client.async_request_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "%s: initial sync did not answer (%s); "
                "use the sync service when the blind is awake",
                address,
                err,
            )
        else:
            _LOGGER.info(
                "%s: initial sync read positions %s", address, client.position
            )

    hass.data[DOMAIN][entry.entry_id] = client

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and disconnect."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    client: BlissClient | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if client is not None:
        # Persist before dropping the client, so the last known positions
        # survive the restart.
        await client.async_flush()
        await client.async_disconnect()

    return unloaded