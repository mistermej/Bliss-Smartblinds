"""Button platform: sync button to re-read the blind's position from the motor."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import BlissClient
from .const import DOMAIN, LOGGER_NAME

_LOGGER = logging.getLogger(LOGGER_NAME)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sync button for one blind."""
    client: BlissClient = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            BlissSyncButton(client, entry),
        ]
    )


class BlissSyncButton(ButtonEntity):
    """Sync button to re-read the blind's position from the motor."""

    _attr_has_entity_name = True
    _attr_name = "Sync"

    def __init__(
        self,
        client: BlissClient,
        entry: ConfigEntry,
    ) -> None:
        self.client = client
        self._attr_unique_id = f"{client.address}_sync"

        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": client.name,
            "manufacturer": "Hunter Douglas",
            "model": client.model or "TDBU",
            "connections": {("mac", client.address)},
        }

    async def async_press(self) -> None:
        """Re-read motor status and update the cached positions.

        The answer is the Health sensor and the INFO line, both of which carry
        the positions. No popup: the sync takes a second or two, a notification
        that has to be dismissed is worse than the log line it duplicates, and
        the Health sensor already says "Syncing" then "Synced" on a dashboard
        without anyone having to clear it.
        """
        _LOGGER.debug("%s: sync button pressed", self.client.name)
        try:
            await self.client.async_sync_positions()
        except ConnectionError as err:
            # A blind that cannot be reached is the honest answer, and
            # HomeAssistantError is what makes the message reach the UI instead
            # of "Unknown error".
            raise HomeAssistantError(
                f"{self.client.name}: sync failed - {err}"
            ) from err