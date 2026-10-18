"""Config flow for Bliss Smart Blinds."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.device_registry import format_mac

from .const import CONF_ADDRESS, CONF_NAME, DOMAIN, LOGGER_NAME
from .options import BlissOptionsFlow

_LOGGER = logging.getLogger(LOGGER_NAME)


def _validate_mac(value: str) -> str:
    """Normalise and sanity-check a MAC address."""
    cleaned = value.strip().upper().replace("-", ":")
    parts = cleaned.split(":")
    if len(parts) != 6 or any(
        len(p) != 2 or any(c not in "0123456789ABCDEF" for c in p) for p in parts
    ):
        raise vol.Invalid("invalid_mac", "Expected a MAC address like AA:BB:CC:DD:EE:FF")
    return format_mac(cleaned)


def _user_schema(default_mac: str | None = None) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_ADDRESS, default=default_mac or ""): str,
            vol.Optional(CONF_NAME, default=""): str,
        }
    )



def _default_name(model: str | None, address: str) -> str:
    """A usable name for a blind we know nothing about but its advertisement.

    The model number is what the motor broadcasts, and it is the same on every
    blind in the house, so it is useless as a name - seven of them all read
    "HD3800". The last two octets of the MAC are unique per motor, and the model
    is already visible on the device page as the device model.

    Kept short so it reads well as a device name and as the prefix Home
    Assistant puts in front of every entity underneath it.
    """
    tail = address.replace(":", "")[-4:].upper()
    return f"Blind {tail}"


class BlissConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for one Bliss blind."""

    VERSION = 1

    def __init__(self) -> None:
        """The advertisement a discovery flow is asking about."""
        # Set by async_step_bluetooth and read by the confirm step. It has to
        # live on the flow rather than be passed along, because Home Assistant
        # re-enters the flow from the confirm form with no discovery_info.
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        super().__init__()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow.

        This has to be a ``@staticmethod`` **on the config flow class in this
        file**, and not a function in ``__init__.py``. Home Assistant looks for
        the options handler here, and nowhere else - an integration that only
        offers it from ``__init__.py`` loads perfectly, moves blinds, and shows
        no Configure button anywhere, with nothing in the log to explain it.
        """
        return BlissOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                address = _validate_mac(user_input[CONF_ADDRESS])
            except vol.Invalid:
                errors["base"] = "invalid_mac"
            else:
                name = user_input.get(CONF_NAME) or address
                await self.async_set_unique_id(address.lower())
                self._abort_if_unique_id_configured()

                # Not probing the blind here on purpose: BLE probes in a config
                # flow are slow and often time out on sleepy devices. A wrong MAC
                # shows up quickly as an unavailable entity instead.
                return self.async_create_entry(
                    title=name,
                    # motor_range is intentionally NOT asked for. It is detected
                    # from the status reply length, so the user is never asked for
                    # a value they cannot know.
                    data={
                        CONF_ADDRESS: address,
                        CONF_NAME: name,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(
                user_input.get(CONF_ADDRESS) if user_input else None
            ),
            errors=errors,
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Bluetooth advertisement matching the manifest matcher.

        This *finds* a blind and asks you about it. It does not add it.

        An earlier build called ``async_create_entry`` from here, so every
        Bloom-shaped advertisement in range silently became a configured blind
        with a move it never asked for. Discovery is meant to surface something
        and let you decide, which for a house of blinds means you can see which
        ones are in reach and add them one at a time as you set up each room.
        """
        address = discovery_info.address
        await self.async_set_unique_id(address.lower())
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        # Shown on the discovery card. Every Bliss motor advertises the same
        # model number, so without the address the card cannot tell you which
        # blind you are looking at - and they would all read "HD3800".
        self.context["title_placeholders"] = {
            "name": f"{discovery_info.name} ({address})"
            if discovery_info.name
            else address
        }

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before adding a discovered blind."""
        if self._discovery_info is None:
            # Abort rather than show a form that can never be completed: a
            # blank confirmation the user submits against twice is worse than
            # an honest "that was lost, try again".
            return self.async_abort(reason="no_discovery_info")

        address = self._discovery_info.address

        if user_input is not None:
            # The typed name wins, so "Livingroom" becomes the device name and
            # the covers underneath read "Livingroom Top" / "Livingroom Bottom".
            name = (user_input.get(CONF_NAME) or "").strip() or _default_name(
                self._discovery_info.name, address
            )
            return self.async_create_entry(
                title=name,
                # motor_range is intentionally NOT asked for. It is detected
                # from the status reply length, so the user is never asked for
                # a value they cannot know.
                data={
                    CONF_ADDRESS: address,
                    CONF_NAME: name,
                },
            )

        # Deliberately an *empty* schema rather than none at all.

        # No name field here: Home Assistant's own Name-and-area dialog asks
        # the name and the area together, and a field of ours split that into
        # two popups. No data_schema either - HA's async_show_form takes
        # data_schema=None as a supported option, so an empty Schema bought
        # nothing. (An earlier build carried one on the theory that it made
        # the Name-and-area dialog appear; HA's source does not support that,
        # so it is gone and only a run on a real install can say whether the
        # dialog really needed it.)
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovery_info.name or address,
                "address": address,
            },
        )