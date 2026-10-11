"""Sensor platform: battery state and signal strength, one set per blind.

- **Battery** comes from the motor. The vendor app reads it out of bits 3-4 of
  byte 5 of a status reply; see ``protocol.battery_from_status`` for the
  mapping and what has and has not been confirmed on hardware. It is a motor
  attribute, not a rail attribute, so there is one entity per blind rather than
  one per rail - both rails always report the same value.
- **RSSI** does not come from the blind at all. It is Home Assistant's own
  Bluetooth stack remembering the last advertisement, so it is only as fresh as
  the most recent connection and reads None until the blind has been seen.

Neither sensor polls. They read the client's cache, and are pushed an update
whenever a status reply lands, which is after every command. Asking a sleepy
motor a question every 30 seconds to draw a graph is not worth the wake-ups.
"""

from __future__ import annotations

import logging
import re

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import BlissClient
# A MAC anywhere inside a string. HA's Bluetooth source is a plain string and
# for a proxied blind it can carry the whole chain of proxies that relayed
# the connection, which no single registry entry would match. Pulling the
# addresses out of it means the shape of that string stops mattering.
_MAC_TOKEN = re.compile(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}")

# The address Home Assistant appends to an automatically named device,
# e.g. "BTP Zolder (58:E6:C5:19:07:6A)".
_APPENDED_ADDRESS = re.compile(r"\s*\([0-9A-F]{2}(?::[0-9A-F]{2}){5}\)\s*$")


def _device_friendly_name(device) -> str | None:
    """The name to show for a registry entry, or None if it has none.

    ``name_by_user`` wins over ``name``: what the user typed beats what Home
    Assistant generated.
    """
    if device is None:
        return None
    user = getattr(device, "name_by_user", None)
    if user:
        return str(user)
    name = getattr(device, "name", None)
    if not name:
        return None
    return _APPENDED_ADDRESS.sub("", str(name)) or str(name)


from .const import (
    BATTERY_EMPTY,
    BATTERY_LOW,
    DOMAIN,
    HEALTH_COLLISION,
    HEALTH_MISMATCH,
    HEALTH_MOVE_FAILED,
    HEALTH_OK,
    HEALTH_NO_FEEDBACK,
    HEALTH_NOT_FOUND,
    LOGGER_NAME,
)

_LOGGER = logging.getLogger(LOGGER_NAME)

# Worded as the vendor app words it, so the two can be compared directly:
# a user looking at both should not see different words for the same state.
BATTERY_STATES = {
    0: "Good",
    BATTERY_LOW: "Low",
    BATTERY_EMPTY: "Empty",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the battery and RSSI sensors for one blind."""
    client: BlissClient = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            BlissHealthSensor(client, entry),
            BlissBatterySensor(client, entry),
            BlissRssiSensor(client, entry),
            BlissConnectionSensor(client, entry),
            BlissProxySensor(client, entry),
        ]
    )


class _BlissSensorBase(SensorEntity):
    """Shared device registration and update wiring."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, client: BlissClient, entry: ConfigEntry) -> None:
        self.client = client
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": client.name,
            "manufacturer": "Hunter Douglas",
            "model": "Bliss Smart Blinds (TDBU)",
            "connections": {("mac", client.address)},
        }

    @property
    def available(self) -> bool:
        """Follow the same rule as the covers: present, not merely connected.

        These motors sleep between commands, so gating on a live connection
        leaves everything looking permanently broken.
        """
        return self.client.is_present()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.client.add_update_listener(self._handle_client_update)
        self._refresh()

    async def async_will_remove_from_hass(self) -> None:
        self.client.remove_update_listener(self._handle_client_update)
        await super().async_will_remove_from_hass()

    def _handle_client_update(self) -> None:
        """Push a new state.

        A listener can outlive the entity that registered it, and then every
        client update raises ``hass is None`` and is swallowed by the client's
        listener loop - an error every few seconds, for the life of the process,
        saying nothing useful. Unregister instead: there is no longer anywhere to
        push the state to.
        """
        if self.hass is None:
            self.client.remove_update_listener(self._handle_client_update)
            return
        self._refresh()
        self.async_write_ha_state()

    def _refresh(self) -> None:
        """Recompute anything cached ahead of a state write.

        The default does nothing. A sensor whose value is expensive, or which
        must not touch ``self.hass`` while Home Assistant is still wiring the
        entity up, overrides this instead of computing inside its property.
        """


class BlissHealthSensor(_BlissSensorBase):
    """What went wrong last, in one glanceable word.

    Everything here is already logged, but a log is the wrong place to look when
    you want to know whether a blind is behaving. This answers it on the device
    page instead.

    Reset by the next clean move, so it reads as "is anything wrong right now"
    rather than "has anything ever gone wrong".

    Failures only. Whether the blind can be reached is not a failure - these
    motors sleep between commands - and it is already reported by the separate
    Connection sensor, so repeating it here would only invite the wrong reading.
    How long ago it was last heard is kept as an attribute rather than promoted
    to a state.
    """

    _key = "health"
    _attr_name = "Health"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        HEALTH_OK,
        HEALTH_COLLISION,
        HEALTH_MOVE_FAILED,
        HEALTH_NO_FEEDBACK,
    HEALTH_NOT_FOUND,
        HEALTH_MISMATCH,
    ]
    _attr_icon = "mdi:heart-pulse"

    @property
    def native_value(self) -> str:
        return self.client.health

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "detail": self.client.health_detail,
            "seconds_since_seen": self.client.seconds_since_seen(),
        }


class BlissBatterySensor(_BlissSensorBase):
    """The motor's battery, as one of the vendor's three states.

    ENUM, not the BATTERY device class. The motor reports three named states,
    not a percentage, and ``SensorDeviceClass.BATTERY`` expects a number -
    pairing it with a string state renders wrongly in the UI. The vendor's own
    names are used verbatim so they match what the Hunter Douglas app shows,
    which is what someone comparing the two will expect.

    Automations trigger on the state, e.g. ``Full`` to ``Low``.
    """

    _key = "battery"
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["Good", "Low", "Empty"]
    _attr_icon = "mdi:battery"

    @property
    def native_value(self) -> str | None:
        state = self.client.battery
        if state is None:
            return None
        return BATTERY_STATES.get(state)

    @property
    def extra_state_attributes(self) -> dict:
        # The raw vendor code, so someone reading a bug report can see exactly
        # what the motor said rather than only our label for it.
        return {"battery_code": self.client.battery}


class BlissConnectionSensor(_BlissSensorBase):
    """Whether a live Bluetooth connection exists right now.

    Two states, Connected and Not Connected, and nothing else. A third state
    was tried here - "Unavailable", for not having been heard from recently -
    and it was the wrong idea: it reads as a fault, when for these motors it is
    simply what a healthy blind looks like all day.

    Both states are in play. A connection exists during and shortly after a
    command; between commands these motors sleep and stop advertising, so Not
    Connected is the normal resting state rather than a problem. The
    seconds_since_seen attribute carries the detail that a third state would
    otherwise have carried.
    """

    _key = "connection"
    _attr_name = "Connection"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["Connected", "Not Connected"]
    _attr_icon = "mdi:bluetooth-connect"

    @property
    def native_value(self) -> str:
        return "Connected" if self.client.available else "Not Connected"

    @property
    def extra_state_attributes(self) -> dict:
        return {"seconds_since_seen": self.client.seconds_since_seen()}


class BlissProxySensor(_BlissSensorBase):
    """Which proxy last reached this blind, by the name the user gave it.

    A MAC address is what Home Assistant reports, but "ESPHome proxy bedroom"
    is what the user recognises, so the MAC is resolved through the device
    registry to a friendly name. The MAC is kept as an attribute in case it is
    the useful one.

    Unknown until the blind has been reached once, and unknown if this version
    of Home Assistant does not expose it at all - both are normal, so neither is
    an error.
    """

    _key = "proxy"
    _attr_name = "Proxy"
    _attr_icon = "mdi:bluetooth-connect"

    def __init__(self, client, entry) -> None:
        super().__init__(client, entry)
        self._source: str | None = None
        self._name: str | None = None

    def _refresh(self) -> None:
        """Cache the name now, while Home Assistant is definitely listening.

        ``native_value`` must not do this work itself. Home Assistant calls it at
        moments when the entity is not yet wired up - ``self.hass`` is None then
        - and reading it there raised, so the name was never resolved and the
        sensor sat on the raw MAC it was meant to replace. It is also called far
        more often than the source changes.
        """
        try:
            source = self.client.async_get_service_source()
        except Exception as err:  # noqa: BLE001 - see below
            _LOGGER.debug("%s: could not read the bluetooth source: %s",
                          self.client.address, err)
            source = None
        self._source = source
        # A friendlier name is a nicety. Letting a failure here propagate would
        # abort async_added_to_hass and Home Assistant would refuse to add the
        # entity at all - which is what happened when the registry call raised.
        try:
            self._name = self._resolve_name(source) if source else None
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: could not name the proxy: %s",
                          self.client.address, err)
            self._name = None

    @property
    def native_value(self) -> str | None:
        return self._name or self._source

    @property
    def extra_state_attributes(self) -> dict:
        return {"address": self._source}

    def _resolve_name(self, address: str) -> str | None:
        """Turn whatever Home Assistant reports as the source into a name.

        HA's ``BluetoothServiceInfo.source`` is a plain string, not an address
        type, and for a proxied blind it is not always a bare MAC: it can carry
        the chain of proxies that relayed the connection, which no single
        registry entry matches. So rather than assume its shape, pull every
        MAC-shaped token out of it and try those. Matching is case-insensitive
        because HA reports upper case while the registry stores whatever the
        integrating plugin supplied.

        The lookup goes through ``async_get_devices(connections=...)``, which is
        the same registry index the old ``async_get_device(connections=...)`` used
        - so it finds exactly what that found, without the deprecated call.

        That distinction is the whole bug. The proxy is registered under the
        ``bluetooth`` domain, not ``mac``; an earlier version of this lookup
        offered both keys to ``async_get_device`` and worked. Dropping the
        ``("bluetooth", ...)`` key while moving off the deprecated API is what
        cost the name, and three rewrites since have not brought it back.
        ``async_get_device_by_connection`` is not the replacement: it requires a
        ``config_entry_id`` argument that is mandatory and positional.

        Two registry entries exist for each proxy, and not as one would expect:

            BTP Zolder                       connections: [mac, 58:e6:c5:19:07:68]
            BTP Zolder (58:E6:C5:19:07:6A)  connections: [bluetooth, 58:E6:C5:19:07:6A]

        The ESPHost node and the bluetooth proxy advertise on different MACs, and
        the proxy's own entry has an empty identifiers list, so an identifier
        lookup could never find it even with the right arguments. Indexing by
        connection is what works.

        HA appends the address to that entry's name, which repeats information
        the user already has in the MAC attribute, so it is trimmed off. A name
        the user typed is never trimmed.
        """
        if self.hass is None:
            return None

        tokens = _MAC_TOKEN.findall(address.upper())
        if not tokens:
            _LOGGER.debug(
                "%s: HA reported the source as %r, which holds no MAC to look up",
                self.client.address, address,
            )
            return None

        try:
            from homeassistant.helpers import device_registry as dr

            registry = dr.async_get(self.hass)
        except Exception as err:  # noqa: BLE001 - a name is never worth an error
            _LOGGER.debug(
                "%s: could not read the device registry to name the proxy: %s",
                self.client.address, err,
            )
            return None

        for token in tokens:
            try:
                devices = registry.async_get_devices(
                    connections={("bluetooth", token), ("mac", token)}
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "%s: registry lookup for %s failed: %s",
                    self.client.address, token, err,
                )
                continue
            for device in devices:
                name = _device_friendly_name(device)
                if name:
                    return name

        _LOGGER.debug(
            "%s: no registry entry for the source %r (looked for %s)",
            self.client.address, address, ", ".join(tokens),
        )
        return None

class BlissRssiSensor(_BlissSensorBase):
    """Signal strength of the last advertisement Home Assistant heard."""

    _key = "rssi"
    _attr_name = "Signal strength"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        return self.client.async_get_rssi()
