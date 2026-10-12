"""Config flow for Bliss Smart Blinds."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.device_registry import format_mac

from .const import CONF_ADDRESS, CONF_NAME, DOMAIN, LOGGER_NAME

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


class BlissConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for one Bliss blind."""

    VERSION = 1

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
        """Handle a Bluetooth discovery flow.

        HA creates this step when a BLE advertisement matches the
        ``bluetooth`` matcher in ``manifest.json`` (local name ``HD\\d{4}``).
        """
        address = discovery_info.address
        name = discovery_info.name or address
        await self.async_set_unique_id(address.lower())
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=name,
            data={CONF_ADDRESS: address, CONF_NAME: name},
        )