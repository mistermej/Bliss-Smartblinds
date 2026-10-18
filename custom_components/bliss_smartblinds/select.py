"""Select platform: pick a preset to send the blind to that position.

One entity per blind, whose options are the preset names. Choosing one moves
both rails to the stored positions - the moment the preset becomes usable from
a dashboard or an automation rather than something you can only configure.

A select rather than a button per preset because a handful of presets per blind
times seven blinds is a lot of buttons, and one entity carries the whole list.
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import BlissClient
from .const import DOMAIN, LOGGER_NAME
from .options import preset_at, presets_of

_LOGGER = logging.getLogger(LOGGER_NAME)

# One selector per blind, keyed on the address so it is stable across reloads
# and can be found again when the last preset is deleted.
PRESET_UNIQUE_ID = "{address}_preset"

# A blind sitting between presets has no label of its own. current_option
# returns None for it, which Home Assistant renders as its own lower-case
# "unknown", and that is what the user sees on screen - confirmed on hardware
# 2026-10-18.
#
# It cannot be capitalised by returning "Unknown" from current_option instead.
# A select displays one of its own options and nothing else: a value outside
# the dropdown is not a state the entity can show, and adding the word to the
# options to get it on screen would make it pickable - and picking it would
# fail, since no preset is called that. The position sensor has no such limit,
# which is why it reads "Custom" and "Unknown" freely.
#
# An earlier version of this comment claimed Home Assistant would display
# current_option whether or not it was one of the options. Nothing here could
# have shown that: the value was never returned, so the claim was never on
# screen to be checked. It is not asserted again without a build that does it.


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the preset selector for one blind."""
    client: BlissClient = hass.data[DOMAIN][entry.entry_id]
    presets = presets_of(dict(entry.options or {}))

    if not presets:
        # Deleting the last preset removes the entity that lists them.
        #
        # Simply not adding is not enough: the platform is set up again on every
        # options change, and an entity already in the registry would stay there
        # with an empty dropdown - a control that can never work, sitting on the
        # device forever.
        entity_registry = er.async_get(hass)
        entity_id = entity_registry.async_get_entity_id(
            "select", DOMAIN, PRESET_UNIQUE_ID
        )
        if entity_id is not None:
            entity_registry.async_remove(entity_id)
        return

    async_add_entities([BlissPresetSelect(client, entry, presets)])


class BlissPresetSelect(SelectEntity):
    """Choose a saved position to send this blind to.

    Its state is the preset the blind is sitting on, not the one last applied.
    """

    _attr_has_entity_name = True
    _attr_name = "Preset"
    _attr_translation_key = "preset"

    def __init__(
        self,
        client: BlissClient,
        entry: ConfigEntry,
        presets: dict,
    ) -> None:
        self.client = client
        self._presets = presets

        self._attr_unique_id = PRESET_UNIQUE_ID.format(address=client.address)
        self._attr_options = list(self._presets)

        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": client.name,
            "manufacturer": "Hunter Douglas",
            "model": client.model or "TDBU",
            "connections": {("mac", client.address)},
        }

    async def async_added_to_hass(self) -> None:
        """Follow the client, so a rail moved by hand updates the dropdown.

        Nothing else asks for a state write when a blind is moved by its own
        remote, so without this the selector would keep naming a preset the
        rails have already left.
        """
        await super().async_added_to_hass()
        self.client.add_update_listener(self._handle_client_update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister: a listener can outlive the entity that added it."""
        self.client.remove_update_listener(self._handle_client_update)
        await super().async_will_remove_from_hass()

    def _handle_client_update(self) -> None:
        """Push a new state, or stop trying when there is nowhere to push to."""
        if self.hass is None:
            self.client.remove_update_listener(self._handle_client_update)
            return
        self.async_write_ha_state()

    @property
    def current_option(self) -> str | None:
        """The preset the blind is at, or None when it is at none of them.

        This is what a restart comes back to, with nothing having to be
        stored: the rails are still where they were, so the selector is still
        on the preset they make up.

        None renders as Home Assistant's "unknown", which is the truth for a
        blind sitting between presets. It cannot read "Custom" the way the
        position sensor does - a select may only display its own options, so
        Custom would have to join the dropdown before it could be shown, and
        then it could be picked. Picking it would always fail, since no preset
        is called that.
        """
        return preset_at(self.client.position, self._presets)

    async def async_select_option(self, option: str) -> None:
        """Send the blind to the chosen preset's positions.

        A preset has no speed, so nothing is written here but the move. That
        is deliberate: a speed frame ahead of a move beeps the blind and shifts
        it about 2%, and the blind's own Speed setting is written by its own
        path when it changes.

        Not a setting: the selector already shows the preset the blind is at,
        and the rails move as soon as you pick one - that is the whole point.
        """
        preset = self._presets.get(option)
        if preset is None:
            raise ValueError(f"no preset called {option!r}")

        _LOGGER.debug(
            "%s: applying preset %s -> top %s%%, bottom %s%%",
            self.client.name,
            option,
            float(preset["top"]),
            float(preset["bottom"]),
        )

        # Maintenance Mode still applies. A read-only blind is not a licence to
        # move it because a preset was picked, and the client already refuses
        # and explains itself.
        try:
            await self.client.async_move_to(
                float(preset["top"]),
                float(preset["bottom"]),
            )
        except Exception as err:
            # Nothing is written here on purpose. The state is read from the
            # rails, and the rails have not gone anywhere, so the dropdown is
            # already showing the truth - and because it still differs from
            # what was just picked, picking it again fires, which is what makes
            # a retry from the frontend possible.
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError(
                f"{self.client.name}: could not apply preset {option} - {err}"
            ) from err

        # The move is verified against the motor before it returns, so the
        # cache holds where the blind ended up and the new state follows.
        self.async_write_ha_state()
