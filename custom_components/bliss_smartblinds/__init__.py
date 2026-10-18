"""The Bliss Smart Blinds integration.

Unofficial community integration. Not affiliated with or endorsed by
Hunter Douglas. The BLE protocol was reverse-engineered by observing the
vendor Android app.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import (
    EVENT_DEVICE_REGISTRY_UPDATED,
    EventDeviceRegistryUpdatedData,
)

from .client import BlissClient
from .const import (
    CONF_ADDRESS,
    CONF_CONNECT_TIMEOUT,
    CONF_MAINTENANCE_MODE,
    CONF_SPEED,
    DEFAULT_CONNECT_TIMEOUT,
    DOMAIN,
    LOGGER_NAME,
    DEFAULT_SPEED,
    SPEED_CHOICES,
SPEED_LABELS,
)

_LOGGER = logging.getLogger(LOGGER_NAME)

PLATFORMS = ["cover", "sensor", "button", "select"]

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
    _apply_options(client, dict(entry.options))

    # Worth having at DEBUG: a user reporting "my setting does nothing" cannot
    # be answered without knowing what the client was actually given, and this
    # line also proves the integration loaded the version being tested.
    _LOGGER.debug(
        "%s: settings applied - movement %s, connect timeout %.0fs, speed %s",
        name,
        "allowed" if client.movement_enabled else "BLOCKED",
        client.connect_timeout,
        SPEED_LABELS.get(client.speed, client.speed),
    )
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
            status = await client.async_request_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "%s: initial sync did not answer (%s); "
                "use the sync service when the blind is awake",
                address,
                err,
            )
        else:
            # async_request_status returns {} rather than raising when the
            # motor stays quiet, and at this point client.position holds the
            # values storage has just restored. Printing that cache claimed the
            # motor had reported them, so the line is built from the reply
            # alone: a rail the motor did not answer is not printed as if it
            # had. "reported no positions" is used rather than "no reply"
            # because a reply that does not decode to a rail is still a reply.
            if status:
                _LOGGER.info(
                    "%s: initial sync read positions %s",
                    address,
                    {
                        rail: info["position_percent"]
                        for rail, info in status.items()
                    },
                )
            else:
                _LOGGER.debug(
                    "%s: initial sync reported no positions; "
                    "use the sync service when the blind is awake",
                    address,
                )

    hass.data[DOMAIN][entry.entry_id] = client

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    _register_sync_service(hass)

    # The device does not exist until a platform has set up its first entity,
    # so this can only run once the forwards are done.
    _remember_options(hass, entry)
    _sync_device_name(hass, entry)
    entry.async_on_unload(
        _async_watch_device_renames(hass, entry)
    )
    return True


def _identifiers(entry: ConfigEntry) -> set[tuple[str, str]]:
    """The device-registry identifiers this integration owns for one blind."""
    return {(DOMAIN, entry.entry_id)}


@callback
def _sync_device_name(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Make the device's own name match the config entry's title.

    Only ever sets the name the *integration* provides. A user who has renamed
    the device has set ``name_by_user``, which Home Assistant prefers over
    anything an integration writes, so their choice wins and this does nothing.
    """
    dev_reg = dr.async_get(hass)
    # async_get_device is deprecated from Home Assistant 2026.8 - identifiers
    # and connections are no longer unique across config entries - and stops
    # working in 2027.8. async_get_devices reads the same index, so it finds
    # exactly what that found; ours can match at most one device anyway,
    # because the identifier carries this entry's id. That one exists only from
    # 2026.8, which is why hacs.json declares 2026.8.0 as the floor - see
    # tests/test_entry_title_sync.py for the table it is checked against.
    devices = dev_reg.async_get_devices(identifiers=_identifiers(entry))
    device = devices[0] if devices else None
    if device is None:
        # The device only exists once a platform has set up its first entity,
        # so a blind with no entities yet has nothing to sync into.
        return
    if device.name_by_user:
        return
    if device.name == entry.title:
        return
    dev_reg.async_update_device(device.id, name=entry.title)


@callback
def _async_watch_device_renames(
    hass: HomeAssistant, entry: ConfigEntry
) -> Callable[[], None]:
    """Follow a device rename back onto the config entry.

    Home Assistant's rename dialog for a config entry says the name "should be
    equal to the device name", and the two drift apart the moment either is
    renamed on its own. This closes that from the other side: renaming the
    device renames the entry, and moves the client's name with it so the log
    lines change in the same gesture.

    Returns an unsubscribe callback, for ``entry.async_on_unload``.
    """
    dev_reg = dr.async_get(hass)

    @callback
    def _device_updated(event: Event[EventDeviceRegistryUpdatedData]) -> None:
        if event.data.get("action") != "update":
            return
        device_id = event.data.get("device_id")
        if not device_id:
            return
        device = dev_reg.async_get(device_id)
        # Subset, not membership: a device can carry identifiers from more than
        # one integration if another one set it up, and ours is a tuple inside
        # the set, not a set inside it. ``our_ids not in device.identifiers``
        # compares a set against its elements and is always True for a
        # non-empty set of ours, which silently disabled this whole listener.
        if device is None or not _identifiers(entry).issubset(device.identifiers):
            return
        user_name = device.name_by_user
        # Only a rename the *user* made. An integration-side name write - from
        # _sync_device_name above, or a platform rebuilding its entities - is
        # not a decision to copy, and copying it would undo the user's title.
        if not user_name or user_name == entry.title:
            return
        client = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if client is not None:
            client.name = user_name
        hass.config_entries.async_update_entry(entry, title=user_name)

    return hass.bus.async_listen(EVENT_DEVICE_REGISTRY_UPDATED, _device_updated)



def _remembered_options(hass: HomeAssistant, entry: ConfigEntry) -> dict | None:
    """The options recorded when this entry was last set up, or None.

    Home Assistant fires the update listeners for *any* change to an entry -
    a rename, a version bump - not only for options, so "what changed" has to
    be measured against a recorded copy rather than against the entry itself.
    """
    return hass.data.get(DOMAIN, {}).get(f"__options__{entry.entry_id}")


def _options_changed(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Whether the options actually differ from the ones last applied.

    Reloading on every listener call meant the cosmetic title write the
    device-rename listener makes tore the BLE connection down and reconnected
    for nothing: a rename is not a reason to drop the link to the motor.
    """
    return _remembered_options(hass, entry) != dict(entry.options)


def _remember_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Record the options currently applied, for _options_changed above.

    A deep copy on purpose. The presets block is a dict inside the options
    dict, and a shallow one would share it - so anything that edited a preset
    in place would leave the remembered copy already matching, and the change
    would never trigger the reload that re-derives the preset list.
    """
    hass.data.setdefault(DOMAIN, {})[f"__options__{entry.entry_id}"] = deepcopy(
        dict(entry.options)
    )


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change, and keep the device name in step."""
    # A renamed config entry lands here too, and the device should follow it
    # straight away rather than waiting for a reload that may not happen.
    _sync_device_name(hass, entry)

    if not _options_changed(hass, entry):
        return

    # Read before the reload, not after: _apply_options runs during setup, so
    # afterwards this would already hold the new value and the two would always
    # compare equal - the write would never fire, and a speed setting that
    # silently does nothing is the worst outcome a setting can have.
    was_speed = _speed_of(dict(_remembered_options(hass, entry) or {}))
    now_speed = _speed_of(dict(entry.options))

    await hass.config_entries.async_reload(entry.entry_id)

    # After the reload and not before: the reload creates the client, and the
    # setup it runs has already connected and read the motor's position. A
    # write scheduled earlier would race that first sync for the link.
    if was_speed != now_speed:
        _schedule_speed_write(hass, entry, now_speed)


def _schedule_speed_write(hass: HomeAssistant, entry: ConfigEntry, speed: str) -> None:
    """Write the new speed to the motor, in the background.

    Scheduled rather than awaited: a write connects on demand and the connect
    timeout is 45 s, so awaiting here would hold the options form open for up
    to that long to put one frame on the wire.
    """
    client = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if client is None:
        # Nothing to write to. The setting is still stored, and the next setup
        # applies it, so this is not worth a warning.
        return
    hass.async_create_task(_write_speed(client, speed))


async def _write_speed(client: BlissClient, speed: str) -> None:
    """Put the speed frame on the wire, and say plainly whether it went."""
    try:
        await client.async_set_speed(speed)
    except Exception as err:  # noqa: BLE001 - one frame, and the outcome matters
        _LOGGER.warning(
            "%s: speed setting changed to %s, frame not sent: %s",
            client.address,
            speed,
            err,
        )
    else:
        _LOGGER.debug(
            "%s: speed setting changed to %s, frame sent", client.address, speed
        )




def _speed_of(options: dict) -> str:
    """The speed an options dict selects, folded onto a real preset.

    A value stored by an earlier version - when "motor default" existed - is
    not a preset, and passing it to build_speed() would raise. Folding it onto
    the default here means one function decides what the speed is, used both to
    apply the options and to detect that they changed, so the two can never
    disagree and send a frame for something that is not a preset.
    """
    speed = str(options.get(CONF_SPEED, DEFAULT_SPEED))
    return speed if speed in SPEED_CHOICES else DEFAULT_SPEED


def _apply_options(client: BlissClient, options: dict) -> None:
    """Push stored options onto the client.

    Read on setup as well as after a reload, so a value stored by an earlier
    version - or edited by hand - takes effect rather than being ignored.
    """
    # Inverted: the switch is labelled "Maintenance Mode", so ON must mean the
    # blind will not move. Default off, because a configured blind that cannot
    # move is a broken blind and nobody switched it on.
    client.movement_enabled = not options.get(CONF_MAINTENANCE_MODE, False)
    client.connect_timeout = float(
        options.get(CONF_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT)
    )
    client.speed = _speed_of(options)


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