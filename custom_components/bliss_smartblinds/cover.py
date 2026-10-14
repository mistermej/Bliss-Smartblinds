"""Cover platform: two cover entities per TDBU blind (top rail and bottom rail).

Design notes:
- One shared DataUpdateCoordinator per blind, so a 30 s poll produces one
  status request rather than one per cover entity.
- The coordinator is deliberately **not** first-refreshed during setup. These
  motors are sleepy, so a status poll at setup would either stall setup or raise
  ConfigEntryNotReady; the entities stay available and pick up a position as soon
  as the first scheduled poll succeeds.
- Both rails travel in one command, so moving one rail sends a position for the
  other too, held at its last known value.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, CoordinatorEntity

from .client import RAIL_BOTTOM, RAIL_TOP, BlissClient
from .const import DOMAIN, LOGGER_NAME

_LOGGER = logging.getLogger(LOGGER_NAME)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the two covers for one blind."""
    client: BlissClient = hass.data[DOMAIN][entry.entry_id]
    client.async_setup_storage(entry.entry_id)

    coordinator: DataUpdateCoordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=f"{client.name} position",
        # No periodic polling. The motor only moves when commanded, so the cached
        # position is refreshed on demand (after a command, or via the sync
        # service) rather than by asking a sleepy device every 30 seconds.
        update_interval=None,
        update_method=client.async_request_status,
    )

    async_add_entities(
        [
            BlissCover(coordinator, client, entry, RAIL_TOP),
            BlissCover(coordinator, client, entry, RAIL_BOTTOM),
        ]
    )


class BlissCover(CoordinatorEntity, CoverEntity):
    """One rail of a TDBU blind."""

    _attr_device_class = CoverDeviceClass.BLIND
    # Home Assistant composes the friendly name from the device's current
    # name plus this entity's own, and re-derives it when the device is
    # renamed. Without it the name was frozen on first creation.
    _attr_has_entity_name = True
    _attr_should_poll = False  # the coordinator handles polling
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        client: BlissClient,
        entry: ConfigEntry,
        rail: str,
    ) -> None:
        super().__init__(coordinator)
        self.client = client
        self.rail = rail

        # Just the rail. Home Assistant prepends the device's name because
        # _attr_has_entity_name is set below, and it does that from the
        # device's *current* name - so a device renamed to "Geen idee" yields
        # "Geen idee Top".
        #
        # The device name used to be baked in here, which froze every entity on
        # the name the integration first saw: a later rename in Home Assistant
        # reached the device and nothing underneath it.
        self._attr_name = "Top rail" if rail == RAIL_TOP else "Bottom rail"
        self._attr_unique_id = f"{entry.entry_id}_{rail}"

        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": client.name,
            "manufacturer": "Hunter Douglas",
            "model": client.model or "TDBU",
            "connections": {("mac", client.address)},
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to position changes.

        There is no periodic polling, so without this a sync - or any position
        read from the motor - would update the cache while the entity stayed on
        'unknown'.
        """
        await super().async_added_to_hass()
        self.client.add_update_listener(self._handle_client_update)

    async def async_will_remove_from_hass(self) -> None:
        self.client.remove_update_listener(self._handle_client_update)
        await super().async_will_remove_from_hass()

    def _handle_client_update(self) -> None:
        """Called by the client on the event loop when positions change."""
        self.async_write_ha_state()

    # --- state --------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Available based on how recently the motor answered, not on whether it
        happens to be be connected.

        These motors are sleepy and stop advertising between commands, so gating
        availability on a live connection leaves the entity looking permanently
        broken. A motor that has never answered is still available, with an
        unknown position, which is honest.
        """
        return self.client.is_present()

    @property
    def current_cover_position(self) -> int | None:
        value = self.client.position.get(self.rail)
        if value is None:
            return None
        return int(round(value))

    @property
    def is_closed(self) -> bool | None:
        pos = self.current_cover_position
        return None if pos is None else pos >= 95

    @property
    def is_open(self) -> bool | None:
        pos = self.current_cover_position
        return None if pos is None else pos <= 5

    # --- commands -----------------------------------------------------------

    async def _async_run(self, coro) -> None:
        """Run a client operation, surfacing failures to the user."""
        try:
            await coro
        finally:
            self.async_write_ha_state()
        # Refresh from the motor so the UI reflects what really happened.
        await self.coordinator.async_request_refresh()

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Send this rail to its 0% end.

        Each rail is controlled independently, like the vendor app, so this
        moves only this rail and holds the other where it is. Note that on a
        TDBU a rail value on its own does not mean open or closed: 0/0 and
        100/100 are both fully retracted, 0/100 and 100/0 both fully extended.
        """
        await self._async_run(self.client.async_set_rail(self.rail, 0.0))

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Send this rail to its 100% end."""
        await self._async_run(self.client.async_set_rail(self.rail, 100.0))

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._async_run(self.client.async_stop())

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        position = kwargs.get("position")
        if position is None:
            return
        await self._async_run(self.client.async_set_rail(self.rail, float(position)))

    # --- coordinator --------------------------------------------------------

    async def _handle_coordinator_update(self) -> None:
        """Keep the cached positions in step, without raising into the platform."""
        try:
            await super()._handle_coordinator_update()
        except Exception as err:  # noqa: BLE001
            # A sleeping motor is normal, not an error worth spamming about.
            _LOGGER.debug(
                "%s (%s): position refresh failed: %s",
                self.client.name,
                self.client.address,
                err,
            )
            self.async_write_ha_state()