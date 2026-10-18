"""Options flow: a menu, then one screen per job.

The shape here is dictated by one Home Assistant fact worth stating rather than
rediscovering: **a flow only advances when the whole form is submitted.** There
is no "this field changed" message in the protocol at all -
``PUT /api/config/config_entries/flow/{flow_id}`` is the only way to move on,
and it carries every field at once.

So a form cannot grow a field when a dropdown is used, and it cannot repopulate
itself when a preset is picked - either would cost a Submit. The menu is what
removes that cost: a menu tap sends ``{next_step_id}`` with no form validation
at all, so choosing what to do is free.

* **Add** - tap, fill in, one submit. No preset dropdown anywhere on it.
* **Change or delete** - one screen holding the preset to act on, the fields
  (blank keeps what is saved) and a delete box. One submit either way.

What the Load step used to spend a Submit on is now in the form's
**description**. A form cannot show a dropdown's choice in its own fields -
they are rendered before the choice exists - so prefilling always cost one
Submit to ask and another to save. The description is built at the same moment
as the form, and Home Assistant renders it as markdown with its placeholders
substituted, so every preset's saved values are readable before anything is
picked and no Submit is spent on looking.

The menu itself is passed as a dict of step id to label, which is not a style
choice: the shipped frontend reads labels straight out of that dict, whereas a
plain list of ids is looked up in the translation files and renders an empty
button when the id has no entry.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_CONNECT_TIMEOUT,
    CONF_MAINTENANCE_MODE,
    CONF_PRESETS,
    CONF_PRESET_BOTTOM,
    CONF_PRESET_DELETE,
    CONF_PRESET_NAME,
    CONF_PRESET_PICK,
    CONF_PRESET_TOP,
    CONF_SPEED,
    CONNECT_TIMEOUT_MAX,
    CONNECT_TIMEOUT_MIN,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_SPEED,
    SPEED_CHOICES,
    SPEED_LABELS,
)
from .protocol import (
    MIN_RAIL_GAP,
    RAIL_BOTTOM,
    RAIL_TOP,
    RULE_TOP_ABOVE_BOTTOM,
    RailCollisionError,
    validate_rail_positions,
)

SPEED_OPTIONS = [
    SelectOptionDict(value=preset, label=SPEED_LABELS[preset])
    for preset in SPEED_CHOICES
]


def presets_of(options: dict) -> dict:
    """The presets in an options dict, or an empty dict if there are none.

    Read down to exactly the two fields a preset has, rather than copying what
    is stored. Earlier versions also stored a speed per preset, which nothing
    does anything with any more; dropping it here means one definition of what
    a preset is, and every reader - the listing, the sensor, the selector -
    sees the same two fields.
    """
    stored = options.get(CONF_PRESETS)
    if not isinstance(stored, dict):
        return {}
    return {
        str(name): {"top": preset["top"], "bottom": preset["bottom"]}
        for name, preset in stored.items()
        if isinstance(preset, dict) and "top" in preset and "bottom" in preset
    }


# How close a rail has to be to a preset to count as being on it. The position
# is read back from the motor and rounded somewhere along that path, so
# demanding an exact match would report a preset other than the one the blind
# is sitting exactly on.
POSITION_TOLERANCE = 1.0


def preset_at(position: dict, presets: dict) -> str | None:
    """The saved position the blind is sitting on, or None when it is at none.

    One rule, deliberately shared by the position sensor and the preset
    selector: two entities reading the same rails must never name different
    presets, and copying the comparison into both is how they would start to.

    Derived rather than remembered, for the same reason in both places. Nothing
    fires an event when a blind is moved by its own remote, so a stored state
    would disagree with where the rails actually are. It is also what survives
    a restart: the blind is still at the preset it was at, so the selector
    comes back on it without anything having to be stored.

    None means "not on a saved preset". Whether that reads as Custom or as
    Home Assistant's unknown is the calling entity's business - a sensor can
    say Custom because Custom is not a thing you can pick, and a select cannot,
    because a select may only display its own options.
    """
    top = position.get(RAIL_TOP)
    bottom = position.get(RAIL_BOTTOM)
    if top is None or bottom is None:
        return None

    for name, preset in sorted(presets.items()):
        near_top = abs(float(preset["top"]) - float(top)) <= POSITION_TOLERANCE
        near_bottom = (
            abs(float(preset["bottom"]) - float(bottom)) <= POSITION_TOLERANCE
        )
        if near_top and near_bottom:
            return name
    return None


def _preset_options(existing: dict) -> list[SelectOptionDict]:
    """Existing preset names, plus an explicit "nothing chosen" entry.

    A dropdown has to be offered its own default. Listing only saved presets
    left ``default=""`` outside the options, and Home Assistant then refused the
    submission outright - pressing Submit without picking was handed a failed
    request rather than "choose a preset first". The empty entry is what makes
    an unchosen pick reach the handler to be refused properly.
    """
    return [
        SelectOptionDict(value="", label="Choose a preset"),
        *(
            SelectOptionDict(value=name, label=name)
            for name in sorted(existing)
        ),
    ]


def _preset_dropdown(existing: dict) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=_preset_options(existing),
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _position_field() -> vol.Any:
    """A rail position that renders empty and tolerates being left blank.

    ``vol.Coerce(float)`` rather than a ``NumberSelector``. The selector is the
    nicer widget, but it rejects an empty submission with voluptuous's "expected
    float" - which is exactly what happened when a blank position was submitted,
    and made "blank means keep what is saved" unreachable. ``vol.Any`` accepts
    the empty string alongside the float, and ``_as_position`` reads it back as
    "not provided".
    """
    return vol.Any(vol.Coerce(float), "")


def _seed(values: dict | None, key: str, fallback: Any) -> Any:
    """A default for a field, but only when there genuinely is a value.

    ``default=None`` makes Home Assistant skip the field entirely, so a missing
    key and an explicit ``None`` both fall through to ``fallback``. An empty
    string is a real value and is kept - that is the user having cleared the
    box, which must survive a re-render.
    """
    value = (values or {}).get(key, fallback)
    return fallback if value is None else value


# --- the settings form -----------------------------------------------------
def build_schema(current: dict) -> vol.Schema:
    """The three blind-wide settings, seeded from what is already stored."""
    return vol.Schema(
        {
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


# --- the preset forms ------------------------------------------------------
def build_add_schema(values: dict | None = None) -> vol.Schema:
    """Name and both rails. No action, no preset - Add has one job.

    A preset carries no speed. It used to, and the field was filled in and
    stored, but the only thing that could have used it is a speed frame ahead
    of a move - which beeps the blind and shifts it about 2%. The blind's own
    Speed setting is the one that reaches the motor.
    """
    return vol.Schema(
        {
            vol.Optional(
                CONF_PRESET_NAME, default=str(_seed(values, CONF_PRESET_NAME, ""))
            ): str,
            vol.Optional(
                CONF_PRESET_TOP, default=_seed(values, CONF_PRESET_TOP, "")
            ): _position_field(),
            vol.Optional(
                CONF_PRESET_BOTTOM, default=_seed(values, CONF_PRESET_BOTTOM, "")
            ): _position_field(),
        }
    )


def format_preset_listing(existing: dict) -> str:
    """The saved presets as markdown, for the change form's description.

    This is what removes the Load Submit. Prefilling could only ever show one
    preset, and only after a Submit had said which one; the description is
    built while the form is built, so every preset is on screen before a choice
    exists, and none of them costs anything to look at.
    """
    if not existing:
        return "Nothing is saved for this blind yet."

    lines = ["**Saved presets**", ""]
    for name in sorted(existing):
        preset = existing[name]
        top = preset.get("top")
        bottom = preset.get("bottom")
        lines.append(
            # Names are user text dropped into markdown, so a stray newline
            # would end the entry rather than stay inside the label.
            f"- **{' '.join(str(name).splitlines())}** - "
            f"top {float(top):g}%, bottom {float(bottom):g}%"
        )
    return "\n".join(lines)


def build_change_schema(existing: dict, values: dict | None = None) -> vol.Schema:
    """One screen for both jobs: the preset, what to change, and delete.

    Nothing is prefilled from the preset, because the screen is built before
    which one will be chosen exists at all - that is the whole reason the saved
    values travel in the description instead. Every field left blank therefore
    means "keep what is saved".
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_PRESET_PICK, default=str(_seed(values, CONF_PRESET_PICK, ""))
            ): _preset_dropdown(existing),
            vol.Optional(
                CONF_PRESET_NAME, default=str(_seed(values, CONF_PRESET_NAME, ""))
            ): str,
            vol.Optional(
                CONF_PRESET_TOP, default=_seed(values, CONF_PRESET_TOP, "")
            ): _position_field(),
            vol.Optional(
                CONF_PRESET_BOTTOM, default=_seed(values, CONF_PRESET_BOTTOM, "")
            ): _position_field(),
            vol.Required(
                CONF_PRESET_DELETE,
                default=bool(_seed(values, CONF_PRESET_DELETE, False)),
            ): BooleanSelector(),
        }
    )


def _as_position(values: dict, key: str) -> float | None:
    """Read a rail position, or None if it was left blank.

    A number field left empty submits either None or an empty string depending
    on the Home Assistant version, and ``float("")`` is a ValueError rather than
    the graceful "not provided" the caller wants. Both mean the same thing here:
    the user did not set it.
    """
    value = values.get(key)
    if value is None or value == "":
        return None
    return float(value)


def _rail_guard(top: float, bottom: float) -> dict[str, str]:
    """Refuse positions that would drive the rails into each other.

    Never clamps. Silently moving the numbers is worse than refusing: the user
    gets a preset they did not ask for and no idea it happened.
    """
    try:
        validate_rail_positions(
            top, bottom, rule=RULE_TOP_ABOVE_BOTTOM, margin=MIN_RAIL_GAP
        )
    except RailCollisionError:
        return {
            CONF_PRESET_TOP: (
                f"Top has to stay at least {MIN_RAIL_GAP:g}% above bottom, "
                "or the rails will drive into each other"
            )
        }
    return {}


def apply_add(
    values: dict, existing: dict
) -> tuple[dict | None, dict[str, str]]:
    """Store a new preset. Returns ``(presets, {})`` or ``(None, errors)``.

    Errors map a field name to a message, which is how Home Assistant reports
    form problems. Both missing positions are named at once: reporting them one
    at a time means the user fixes the top rail, submits, and only then finds
    out the bottom was blank too.
    """
    name = (values.get(CONF_PRESET_NAME) or "").strip()
    if not name:
        return None, {CONF_PRESET_NAME: "Give the preset a name"}
    if name in existing:
        return None, {
            CONF_PRESET_NAME: f"A preset called {name} already exists"
        }

    top = _as_position(values, CONF_PRESET_TOP)
    bottom = _as_position(values, CONF_PRESET_BOTTOM)

    missing: dict[str, str] = {}
    if top is None:
        missing[CONF_PRESET_TOP] = "Add a top position"
    if bottom is None:
        missing[CONF_PRESET_BOTTOM] = "Add a bottom position"
    if missing:
        return None, missing

    errors = _rail_guard(top, bottom)
    if errors:
        return None, errors

    stored = dict(existing)
    stored[name] = {"top": top, "bottom": bottom}
    return stored, {}


def apply_modify(
    values: dict, existing: dict
) -> tuple[dict | None, dict[str, str]]:
    """Change a saved preset. Blank fields keep what is already stored."""
    picked = str(values.get(CONF_PRESET_PICK) or "")
    if not picked or picked not in existing:
        return None, {CONF_PRESET_PICK: "Choose a preset to modify"}

    target = dict(existing[picked])
    name_field = (values.get(CONF_PRESET_NAME) or "").strip()

    # Blank keeps the name, so changing only the positions does not mean
    # retyping it.
    new_name = name_field or picked
    if new_name != picked and new_name in existing:
        return None, {
            CONF_PRESET_NAME: f"A preset called {new_name} already exists"
        }

    top = _as_position(values, CONF_PRESET_TOP)
    top = float(target["top"]) if top is None else top
    bottom = _as_position(values, CONF_PRESET_BOTTOM)
    bottom = float(target["bottom"]) if bottom is None else bottom

    errors = _rail_guard(top, bottom)
    if errors:
        return None, errors

    # The name is the key, so a rename is a delete plus an add.
    stored = dict(existing)
    stored.pop(picked, None)
    stored[new_name] = {"top": top, "bottom": bottom}
    return stored, {}


def apply_delete(name: str, existing: dict) -> dict:
    """Drop one preset by name."""
    stored = dict(existing)
    stored.pop(name, None)
    return stored


# --- the flow --------------------------------------------------------------
class BlissOptionsFlow(OptionsFlow):
    """Edit one blind's settings and presets."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        # Our own attribute rather than Home Assistant's ``config_entry``,
        # which is only injected from 2024.11 onwards.
        self._entry = config_entry
        # Which preset the form currently on screen was seeded from, or None
        # when nothing has been typed yet. Blank fields keep what is saved, so
        # the only way to land one preset's numbers on another is to switch the
        # dropdown *after* an error has put typed values back on the form - and
        # a form cannot report a field change, so the switch is only visible
        # when the whole thing is submitted.
        self._seeded_from: str | None = None

    def _presets(self) -> dict:
        return presets_of(dict(self._entry.options or {}))

    def _save(self, presets: dict) -> dict[str, Any]:
        merged = dict(self._entry.options or {})
        merged[CONF_PRESETS] = presets
        self._seeded_from = None
        return self.async_create_entry(title="", data=merged)

    def _change_form(
        self,
        existing: dict,
        *,
        values: dict | None = None,
        errors: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Render the change screen, always carrying its list of saved values.

        Every render of this step needs the placeholder: Home Assistant
        substitutes it while localizing the description, and an ICU format call
        with a missing argument does not fall back to showing the key, it
        reports a translation error. So the listing goes out whether the form
        is fresh, re-rendered after a mistake, or empty.
        """
        if values is not None:
            self._seeded_from = str(values.get(CONF_PRESET_PICK) or "")
        return self.async_show_form(
            step_id="preset_change",
            data_schema=build_change_schema(existing, values),
            errors=errors,
            description_placeholders={"presets": format_preset_listing(existing)},
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Pick Settings, or pick what to do with the presets.

        A menu rather than an action dropdown: a dropdown choice would need a
        Submit before it did anything, and this costs one tap instead.
        """
        existing = self._presets()
        menu = {"settings": "Settings", "preset_add": "Add a preset"}
        if existing:
            menu["preset_change"] = "Change or delete a preset"
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Show and store the three blind-wide settings.

        ``async_create_entry`` with ``data`` writes to ``entry.options`` - not
        ``entry.data``, which would change the blind's identity.
        """
        options = dict(self._entry.options or {})

        if user_input is not None:
            merged = dict(options)
            merged.update(dict(user_input))
            return self.async_create_entry(title="", data=merged)

        return self.async_show_form(
            step_id="settings", data_schema=build_schema(options)
        )

    async def async_step_preset_add(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create a preset. One screen, one submit."""
        existing = self._presets()

        if user_input is None:
            return self.async_show_form(
                step_id="preset_add", data_schema=build_add_schema()
            )

        presets, errors = apply_add(user_input, existing)
        if errors:
            # Re-render with what they typed, so a missing position does not
            # cost them the name and the other rail as well.
            return self.async_show_form(
                step_id="preset_add",
                data_schema=build_add_schema(user_input),
                errors=errors,
            )
        return self._save(presets)

    async def async_step_preset_change(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Change a preset, or delete it - one screen, one submit either way.

        They share a screen because they share their only question: which
        preset. As steps, they would cost a menu entry and a Submit apiece just
        to find out what was about to happen, and a form cannot grow a delete
        box once you have decided to delete - nothing about a form changes
        until the whole of it is submitted.

        The checkbox is the whole of the deletion safeguard: ticked, then
        submitted, with the preset's name in the dropdown above it and listed
        above that. Two deliberate actions, and the screen never hides what is
        about to go.
        """
        existing = self._presets()

        if user_input is None:
            # Stay on the screen that was asked for and say why it is empty.
            # Redirecting to Add would show a form nobody asked for, and a menu
            # cannot carry an error at all.
            errors = (
                None if existing else {"base": "There are no presets to change yet"}
            )
            return self._change_form(existing, errors=errors)

        picked = str(user_input.get(CONF_PRESET_PICK) or "")
        if picked not in existing:
            # Everything typed is kept: only the choice was wrong.
            return self._change_form(
                existing,
                values=user_input,
                errors={CONF_PRESET_PICK: "Choose a preset to change or delete"},
            )

        # The form on screen may still carry a *different* preset's values.
        # Only a Submit reveals a change of mind, so a dropdown switched after
        # an error re-render would otherwise write one preset's numbers onto
        # another. Show this one instead, blank, and keep nothing of the old.
        if self._seeded_from and self._seeded_from != picked:
            return self._change_form(existing, values={CONF_PRESET_PICK: picked})

        if bool(user_input.get(CONF_PRESET_DELETE)):
            return self._save(apply_delete(picked, existing))

        presets, errors = apply_modify(user_input, existing)
        if errors:
            return self._change_form(existing, values=user_input, errors=errors)
        return self._save(presets)
