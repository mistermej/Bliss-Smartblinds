"""Options flow: edit a blind's settings after it is set up.

Planned as v2.0. ``allow_movement`` was already being *read* from
``entry.options`` with no way to set it, which is the worst of both worlds: the
code implies a setting exists and the UI offers nothing.

Three settings, all of which change what is written to a motor or how long we
wait for one.

Every field uses an explicit ``homeassistant.helpers.selector`` rather than a
bare type. A bare ``bool`` or ``vol.In(...)`` is rendered as whatever widget the
Home Assistant version happens to decide - a checkbox, a radio group - and the
first version of this form came out as checkboxes for all three fields, which
made a dropdown for four speeds look like four checkboxes. Naming the widget
removes the guesswork and the version dependence.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.helpers.selector import (
    # A switch in a Home Assistant form is a ``BooleanSelector``. There is no
    # ``SwitchSelector`` in this module - importing one is a setup-time
    # ImportError that stops the integration loading. ``homeassistant.components
    # .switch`` is a different thing and has no business here.
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    # The mode enum for a *selector* is ``NumberSelectorMode``. Do not confuse
    # it with ``NumberMode`` from ``homeassistant.components.number``, which
    # sets a number *entity's* display mode. Both have BOX and SLIDER members,
    # so mixing them up reads as fine and fails only when Home Assistant tries
    # to import this module - at which point the integration does not load.
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_CONNECT_TIMEOUT,
    CONF_MAINTENANCE_MODE,
    CONF_SPEED,
    CONNECT_TIMEOUT_MAX,
    CONNECT_TIMEOUT_MIN,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_SPEED,
    SPEED_CHOICES,
    SPEED_LABELS,
)

# Exactly the three presets the vendor app offers. No "Motor default": that
# option existed because the integration did not know how to leave the motor's
# own setting alone, and the app has no such choice either. A stored value from
# an earlier version that no longer exists is treated as the default rather
# than silently producing no speed frame at all.
SPEED_OPTIONS = [
    SelectOptionDict(value=preset, label=SPEED_LABELS[preset])
    for preset in SPEED_CHOICES
]


def build_schema(current: dict) -> vol.Schema:
    """The settings form, seeded from what is already stored.

    Plain function rather than a method so the shape of the form can be tested
    without a Home Assistant config entry.
    """
    return vol.Schema(
        {
            # A switch, not a checkbox: this is a mode you flip while working on
            # a blind, and it reads as one on a dashboard. ON means the blind
            # will not move, which is the whole point of the name.
            vol.Required(
                CONF_MAINTENANCE_MODE,
                default=current.get(CONF_MAINTENANCE_MODE, False),
            ): BooleanSelector(),
            vol.Required(
                CONF_CONNECT_TIMEOUT,
                default=current.get(CONF_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=CONNECT_TIMEOUT_MIN,
                    max=CONNECT_TIMEOUT_MAX,
                    step=5,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            # Explicitly a dropdown. The four motor presets are separate
            # commands, not checkboxes you can tick together.
            vol.Required(
                CONF_SPEED,
                default=str(current.get(CONF_SPEED, DEFAULT_SPEED)),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=SPEED_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class BlissOptionsFlow(OptionsFlow):
    """Edit one blind's settings."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        # Deliberately our own attribute rather than Home Assistant's
        # ``config_entry``.
        #
        # Home Assistant injects ``config_entry`` onto the flow only from
        # 2024.11 onwards, and assigning it yourself is deprecated after that.
        # Relying on the injection therefore means the form breaks on anything
        # older, and ``getattr(self, "config_entry", None)`` returns None and
        # then explodes on ``None.options``. Keeping a private reference works
        # the same on every version and trips no deprecation.
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Show and store the settings form.

        ``async_create_entry`` with a ``data`` payload writes to
        ``entry.options`` - not ``entry.data``. Putting these in ``data`` would
        change the blind's identity and look like a new blind on every reload.
        """
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(user_input))

        return self.async_show_form(
            step_id="init", data_schema=build_schema(dict(self._entry.options or {}))
        )