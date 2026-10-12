"""The Bliss Smart Blinds integration.

Unofficial community integration. Not affiliated with or endorsed by
Hunter Douglas. The BLE protocol was reverse-engineered by observing the
vendor Android app.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .client import BlissClient
from .const import CONF_ADDRESS, DOMAIN, LOGGER_NAME

_LOGGER = logging.getLogger(LOGGER_NAME)

PLATFORMS = ["cover", "sensor", "button"]

# Read-only: this action only ever sends the status-poll frame, which the
# motor answers with where its rails are. It cannot drive a motor, which is
# why it is back when the diagnostic service set (send_raw and friends) is
# not - those could move a blind to anywhere.
SYNC_SCHEMA = vol.Schema({vol.Optional(CONF_ADDRESS): cv.string})


async def _async_sync_one(client: BlissClient) -> dict:
    """Sync one blind, turning an unreachable one into a reportable answer.

    Two outcomes that look identical from outside - a motor that stayed silent,
    and a motor that could not be reached at all - are kept apart on purpose.
    The first means "wake it and try again"; the second means a proxy is
    missing, or the Hunter Douglas app is holding the single connection the
    blind allows. Collapsing them sends the user to the wrong place.

    Never raises: the caller is either a blind the user picked out by name, or
    a loop over every blind, and in the second case one failure must not
    swallow the results of all the others.
    """
    try:
        return await client.async_sync_positions()
    except ConnectionError as err:
        _LOGGER.warning("%s: sync failed: %s", client.name, err)
        return {"error": str(err)}


async def _async_sync(call: ServiceCall) -> dict:
    """Read a blind's real position from the motor.

    The action that has driven this integration since the first hardware
    experiments. Optional ``address``; without one, every configured blind
    is polled. Read-only by construction - see SYNC_SCHEMA.

    Returns the parsed rails so Developer Tools shows the answer directly,
    which is the whole point: the user sees where the blind is without
    reading the log.
    """
    hass = call.hass
    wanted = call.data.get(CONF_ADDRESS)
    clients: dict[str, BlissClient] = hass.data.get(DOMAIN, {})

    if wanted:
        client = next(
            (
                client
                for client in clients.values()
                if client.address.lower() == wanted.lower()
            ),
            None,
        )
        if client is None:
            raise HomeAssistantError(
                f"No configured Bliss blind with address {wanted}. Known "
                f"blinds: {', '.join(c.address for c in clients.values()) or 'none'}"
            )
        result = await _async_sync_one(client)
        if "error" in result:
            # A named blind that cannot be reached is a failed call, not a
            # quiet result: the user asked about that one blind by name.
            raise HomeAssistantError(
                f"{client.name}: sync failed - {result['error']}"
            )
        return {"status": result}

    if not clients:
        raise HomeAssistantError("No Bliss blinds are configured.")

    # Sequential on purpose: these motors allow one BLE connection at a time,
    # so polling them together would have them competing for the same link.
    # Every blind is attempted even if an earlier one fails - with several
    # proxies in a house, one sleeping motor must not silently skip the rest.
    statuses: dict[str, dict] = {}
    for client in clients.values():
        statuses[client.address] = await _async_sync_one(client)
    return {"status": statuses}


def _register_sync_service(hass: HomeAssistant) -> None:
    """Register the sync action once, however many blinds are configured."""
    if hass.services.has_service(DOMAIN, "sync"):
        return
    hass.services.async_register(
        DOMAIN,
        "sync",
        _async_sync,
        schema=SYNC_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


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
    _register_sync_service(hass)
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

    # The action belongs to the integration, not to one blind, so it stays
    # registered until the last config entry goes.
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, "sync")

    return unloaded