"""Frame builders and response parsing for Bliss Smart Blinds.

Wire format (verified against the vendor app's smali):

    [FF] [78] [EA] [41] [opcode] [sub] [payload ...]

No sequence number, no checksum, no length byte. Numeric payloads are
2-byte little-endian unless the motor range is 100 (HD0100 only).

Position convention: raw 0 == fully open. Scene fractions are 0.0..1.0
where 0.0 == fully retracted / rail fully open.

For a dual-servo TDBU blind (motor HD3800) both rails travel in a single
10-byte frame, top rail first.
"""

from __future__ import annotations

import datetime as _dt
import struct

from .const import (
    FRAME_HEADER,
    HEARTBEAT,
    OP_BOTH_DOWN,
    OP_BOTH_STOP,
    OP_BOTH_UP,
    OP_BOTTOM_BAR_DOWN,
    OP_BOTTOM_BAR_UP,
    OP_BOTTOM_TO_POSITION,
    OP_GOTO_TOP,
    OP_MOVE_BOTH_BARS,
    OP_READ_STATUS,
    OP_ROLLER_DOWN,
    OP_ROLLER_STOP,
    OP_ROLLER_UP,
    OP_SET_TIME,
    OP_SPEED_100,
    OP_SPEED_25,
    OP_SPEED_50,
    OP_SPEED_75,
    OP_TOP_TO_POSITION,
    POSITION_RANGE_DEFAULT,
    POSITION_RANGE_HD0100,
    BATTERY_BYTE_INDEX,
    BATTERY_EMPTY,
    BATTERY_FULL,
    BATTERY_LOW,
    SUBCODE_MOVE,
    SUBCODE_TIME,
)

__all__ = [
    "build_move_both_bars",
    "build_move_command",
    "build_move_top",
    "build_move_bottom",
    "build_stop",
    "build_up",
    "build_down",
    "build_speed",
    "build_read_status",
    "build_set_clock",
    "build_heartbeat",
    "encode_position",
    "battery_from_status",
    "raw_to_percent",
    "percent_to_raw",
    "write_needs_response",
    "validate_rail_positions",
    "clamp_rail_positions",
    "MIN_RAIL_GAP",
    "RailCollisionError",
    "rail_from_opcode",
    "motor_range_from_reply",
    "RAIL_TOP",
    "RAIL_BOTTOM",
    "OP_TOP_POSITION",
    "OP_BOTTOM_POSITION",
    "parse_status",
]


# --- helpers ----------------------------------------------------------------


def _frame(opcode: int, sub: int, payload: bytes = b"") -> bytes:
    """Assemble a complete command frame."""
    return FRAME_HEADER + bytes((opcode, sub)) + payload


def encode_position(percent: float, motor_range: int = POSITION_RANGE_DEFAULT) -> bytes:
    """Encode a 0-100 percentage as a little-endian payload.

    Mirrors ``getPositionBytes`` in the vendor app: the byte width is decided
    by the motor range, not by the value. Range 1000 -> 2 bytes LE,
    range 100 (MotorType.HD0100) -> 1 byte.
    """
    clamped = max(0.0, min(100.0, float(percent)))
    raw = int(round(clamped / 100.0 * motor_range))

    if motor_range == POSITION_RANGE_DEFAULT:
        return struct.pack("<H", raw)
    if motor_range == 100:
        return bytes((raw & 0xFF,))
    raise ValueError(f"unsupported motor range: {motor_range}")


def percent_to_raw(percent: float, motor_range: int = POSITION_RANGE_DEFAULT) -> int:
    """Convert a 0-100 percentage to the raw motor value."""
    clamped = max(0.0, min(100.0, float(percent)))
    return int(round(clamped / 100.0 * motor_range))


def battery_from_status(data: bytes) -> int | None:
    """Decode the battery state from a status reply, or None if there is no room.

    The vendor app maps this in ``ScanContinuousViewModelBliss$JogConnection.mapMotorRetToBattery``:

        and-int/lit8 p1, p1, 0x18     ; mask bits 3 and 4
        masked == 0x00  -> 0  BATTERY_FULL
        masked == 0x08  -> 1  BATTERY_LOW
        masked == 0x10  -> 2  BATTERY_EMPTY
        anything else   -> -1 unrecognised

    The byte is index 5 - the only byte of the reply the position fields do not
    already account for:

        ff 01 02 03 d2 02 46 bc 02
                   ^^
                   battery

    Verified on an HD3800: both rails reported 0x02, which masks to 0, i.e.
    FULL, on a healthy blind. The LOW and EMPTY branches follow the vendor's
    own mapping but have not been seen on real hardware.

    ``-1`` is returned as None rather than passed on, because it means the byte
    held something we do not understand, and reporting a battery state we
    guessed at would be worse than reporting none.
    """
    if len(data) <= BATTERY_BYTE_INDEX:
        return None
    masked = data[BATTERY_BYTE_INDEX] & 0x18
    if masked == 0x00:
        return BATTERY_FULL
    if masked == 0x08:
        return BATTERY_LOW
    if masked == 0x10:
        return BATTERY_EMPTY
    return None


def raw_to_percent(raw: int, motor_range: int = POSITION_RANGE_DEFAULT) -> float:
    """Convert a raw motor value to 0-100. Raw 0 == fully open == 0%.

    Rounded to one decimal, which is the motor's own resolution at range 1000
    (one raw unit is 0.1%). Dividing and multiplying by 100 is not exact in
    binary floating point, so raw 280 came out as 28.000000000000004 and raw 301
    as 30.099999999999998. Those values were then persisted and logged, and the
    app and Home Assistant both only ever work to one decimal, so the noise is
    pure downside: it makes stored positions ugly to read and gives the
    verification comparison a value that looks wrong next to a clean input.
    """
    exact = max(0.0, min(100.0, (float(raw) / motor_range) * 100.0))
    return round(exact, 1)


# --- movement ---------------------------------------------------------------


def build_move_both_bars(
    top_percent: float,
    bottom_percent: float,
    motor_range: int = POSITION_RANGE_DEFAULT,
) -> bytes:
    """TDBU / dual-servo move. Both rails in one 10-byte frame, top first.

    Mirrors the ``isDoubleServo()`` branch of ``getMoveToCommand``:
        FF 78 EA 41 FE 03 <top_lo top_hi> <bottom_lo bottom_hi>
    """
    return _frame(
        OP_MOVE_BOTH_BARS,
        SUBCODE_MOVE,
        encode_position(top_percent, motor_range)
        + encode_position(bottom_percent, motor_range),
    )


def build_move_top(
    percent: float,
    motor_range: int = POSITION_RANGE_DEFAULT,
    dual_servo: bool = True,
) -> bytes:
    """Move the top rail only.

    Uses the dual-servo prefix (B1) when the motor is HD3800, otherwise the
    single-servo goto prefix (BF).
    """
    opcode = OP_TOP_TO_POSITION if dual_servo else OP_GOTO_TOP
    return _frame(opcode, SUBCODE_MOVE, encode_position(percent, motor_range))


def build_move_bottom(
    percent: float, motor_range: int = POSITION_RANGE_DEFAULT
) -> bytes:
    """Move the bottom rail only."""
    return _frame(
        OP_BOTTOM_TO_POSITION, SUBCODE_MOVE, encode_position(percent, motor_range)
    )


def build_move_command(
    top_percent: float,
    bottom_percent: float,
    adjust: str | None = None,
    motor_range: int = POSITION_RANGE_DEFAULT,
) -> bytes:
    """Build the move frame, using the narrowest one that does the job.

    ``FE 03`` carries a position for *both* rails. Using it to move a single
    rail therefore also commands the other one - to whatever the caller
    believed its position was. When that belief is stale the motor obeys it and
    drags the untouched rail somewhere nobody asked for, which is exactly what
    a caller holding a cached position would hit.

    So a single-rail request goes out as ``B1 03`` (top) or ``F4 03`` (bottom):
    neither carries a position for the other rail, so neither can move it. Only
    a genuinely coordinated move of both rails uses ``FE 03``.

    ``adjust`` is the rail the caller intends to move, or ``None`` for both.
    """
    if adjust == RAIL_TOP:
        return build_move_top(top_percent, motor_range, dual_servo=True)
    if adjust == RAIL_BOTTOM:
        return build_move_bottom(bottom_percent, motor_range)
    return build_move_both_bars(top_percent, bottom_percent, motor_range)


def build_stop() -> bytes:
    """Generic stop. The app uses the roller-stop frame for this."""
    return _frame(OP_ROLLER_STOP, SUBCODE_MOVE, b"\x01")


def build_up() -> bytes:
    """Open / raise. For TDBU prefer ``build_move_both_bars`` with 0%."""
    return _frame(OP_ROLLER_UP, SUBCODE_MOVE, b"\x01")


def build_down() -> bytes:
    """Close / lower."""
    return _frame(OP_ROLLER_DOWN, SUBCODE_MOVE, b"\x01")


def build_both_up() -> bytes:
    """Open both rails. Equivalent to move-both at 0% on both."""
    return _frame(OP_BOTH_UP, SUBCODE_MOVE, b"\x01")


def build_both_down() -> bytes:
    """Close both rails."""
    return _frame(OP_BOTH_DOWN, SUBCODE_MOVE, b"\x01")


def build_both_stop() -> bytes:
    return _frame(OP_BOTH_STOP, SUBCODE_MOVE, b"\x01")


def build_bottom_bar_up() -> bytes:
    return _frame(OP_BOTTOM_BAR_UP, SUBCODE_MOVE, b"\x01")


def build_bottom_bar_down() -> bytes:
    return _frame(OP_BOTTOM_BAR_DOWN, SUBCODE_MOVE, b"\x01")


# The three speed presets the Hunter Douglas app offers. Taken from the
# ``BlindSpeedOptions`` enum in ``models/BlindSpeedOptions.smali``, whose
# three members point at ``BlissCommands``:
#
#   SLOW    -> gen2SlowSpeedManual     ff 78 ea 41 f2 00
#   COMFORT -> gen2ComfortSpeedManual  ff 78 ea 41 f2 01
#   FAST    -> gen2NormalSpeedManual   ff 78 ea 41 f2 02
#
# One family: the same 0xF2 opcode with a trailing byte picking the speed.
# The app's FAST is backed by a method the code happens to call
# "gen2Normal...", which is why "Normal", "Stock" and "Fast" all mean the
# same preset. This is a *different* command from the ``speed100`` /
# ``speed75`` / ``speed50`` / ``speed25`` frames below, which are 0xF0-0xF3
# with a 03 01 trailer and are not the ones the app's three named speeds
# use. Guessing F0-F3 for Slow/Comfort/Fast would have sent frames the app
# never sends.
GEN2_SPEED_FRAMES = {
    "slow": bytes.fromhex("ff78ea41f200"),
    "comfort": bytes.fromhex("ff78ea41f201"),
    "fast": bytes.fromhex("ff78ea41f202"),
}

# The motor answers a speed frame. Measured on hardware at 64 ms after the
# write, for Comfort: ``ff 01 02 03 de f2 01 00 00`` - the opcode and the
# preset byte echoed back. Nothing reads it yet, so it is recorded here rather
# than kept as a constant nothing would use.

# The matching timer-activation frames - the enum's ``timerCommand``, used
# when a scene or timer runs the move rather than the user tapping a button.
# Home Assistant only moves manually, so these are not sent.
GEN2_SPEED_TIMER_FRAMES = {
    "slow": bytes.fromhex("ff78ea41e500"),
    "comfort": bytes.fromhex("ff78ea41e501"),
    "fast": bytes.fromhex("ff78ea41e502"),
}

# Asks the motor which speed it is set to. Untested on hardware.
SPEED_STATUS_FRAME = bytes.fromhex("ff78ea41da")


def build_speed(preset: str) -> bytes:
    """Build the speed preset frame the vendor app sends for a named speed.

    ``preset`` is one of ``slow`` / ``comfort`` / ``fast``.

    The app writes this frame when the user changes the speed on a speed
    screen, and never as part of a move: ``BlindSpeedOptions.getManualCommand()``
    is reached only from ``SettingsBlindSpeedScreenKt`` and
    ``BlindSpeedScreenKt``. This integration does the same, from the options
    flow. It has to: on real hardware the frame alone beeps the blind, with or
    without a move behind it.
    """
    try:
        return GEN2_SPEED_FRAMES[preset]
    except KeyError:
        raise ValueError(
            f"speed must be one of {sorted(GEN2_SPEED_FRAMES)}, got {preset!r}"
        ) from None


# --- status / time ----------------------------------------------------------


def build_read_status(rail: int | None = None) -> bytes:
    """Poll motor status.

    ``rail`` sets the trailing byte of the request. Verified on real hardware:
    the default request ``ff 78 ea 41 d1 03 01`` returns the **top rail only**
    in a single 9-byte reply, so that byte is treated as a rail index and can
    be varied. Unverified: which value selects which rail.
    """
    index = 0x01 if rail is None else rail & 0xFF
    return _frame(OP_READ_STATUS, SUBCODE_MOVE, bytes((index,)))


def build_set_clock(when: _dt.datetime | None = None) -> bytes:
    """Sync the motor's real-time clock (needed for schedules).

    Layout: FF 78 EA 41 02 00 <yy> <MM> <dd> <HH> <mm> <ss>

    ``yy`` is year-2000, month is 1-12. There is no weekday field and the
    motor stores naive local wall-clock time, so pass a naive local datetime
    (HA's local time, never UTC).
    """
    now = when or _dt.datetime.now()
    return _frame(
        OP_SET_TIME,
        SUBCODE_TIME,
        bytes(
            (
                (now.year - 2000) & 0xFF,
                now.month & 0xFF,
                now.day & 0xFF,
                now.hour & 0xFF,
                now.minute & 0xFF,
                now.second & 0xFF,
            )
        ),
    )


def build_heartbeat() -> bytes:
    """Keep-alive frame."""
    return HEARTBEAT


def write_needs_response(services, char_uuid: str) -> bool:
    """Decide between write-with-response and write-without-response.

    ``services`` is a bleak service collection (or anything iterable of objects
    with ``.characteristics``). Pass ``None`` when services are unknown.

    Verified on real hardware: the Bliss Command characteristic advertises only
    ``write`` (with response) - there is no "write without response". Writing
    without a response to it fails, so the mode must follow the characteristic's
    advertised properties rather than being guessed.
    """
    if not services:
        return True
    target = str(char_uuid).lower()
    for service in services:
        for char in service.characteristics:
            if str(char.uuid).lower() != target:
                continue
            props = {str(p).lower() for p in char.properties}
            if "write without response" in props and "write" not in props:
                return False
            return "write" in props
    return True


# --- response parsing -------------------------------------------------------


class RailCollisionError(ValueError):
    """Raised when a commanded position could drive the rails past each other.

    On a top-down-bottom-up blind the top rail descends as its percentage rises
    and the bottom rail rises as its percentage rises, so the two can be driven
    into each other. That jams or damages the blind, so such a move is refused
    rather than sent.
    """


# Candidate collision rules. NONE of these is confirmed - see the note in
# validate_rail_positions(). They exist so the real geometry can be encoded once
# measured, without rewriting the call sites.
RULE_NONE = "none"  # no guard (diagnostics only)
RULE_SUM = "sum"  # top + bottom <= max_sum
RULE_TOP_BELOW_BOTTOM = "top_below_bottom"  # bottom - top >= margin
RULE_TOP_ABOVE_BOTTOM = "top_above_bottom"  # top - bottom >= margin

RAIL_RULES = (
    RULE_NONE,
    RULE_SUM,
    RULE_TOP_BELOW_BOTTOM,
    RULE_TOP_ABOVE_BOTTOM,
)

# States the blind is known to rest in safely, from real hardware.
#   (70, 30) - rails far apart
#   (0, 0)   - rails in contact
# A TDBU also rests at equal values at any level (10/10, 39/39, 100/100) - the
# rails are parallel and travel together, so equal is the normal case.
KNOWN_GOOD_STATES = ((70, 30), (0, 0), (10, 10), (39, 39), (100, 100))

# Clearance between the rails, in raw motor units (1000 = full travel).
#
# It MUST stay at 0. The only real hazard is the bottom rail rising past the top
# one, and equality is a perfectly normal position for a TDBU - so any positive
# margin would forbid ordinary positions like 39/39 rather than making anything
# safer. Safety comes from the hard rule (rawTop >= rawBottom) plus reading the
# motor back after a move, not from refusing equal values.
DEFAULT_MARGIN = 0.0

# Minimum gap the rails must keep between them, in percent (100 = full travel).
#
# The vendor app's slider clamps to exactly equal and applies no gap, but on
# real hardware the user observed the bottom rail lifting the top rail when both
# sat at the same value. Equal is therefore treated as too close, and a small
# separation is enforced.
#
# 1.0% == 10 raw units out of 1000. Raise it if a blind still interferes.
MIN_RAIL_GAP = 1.0


def validate_rail_positions(
    top: float,
    bottom: float,
    *,
    rule: str = RULE_SUM,
    margin: float = DEFAULT_MARGIN,
    max_sum: float = 100.0,
) -> None:
    """Refuse a move that could drive the TDBU rails into each other.

    The rule comes from the vendor app, not from guessing. Evidence chain:

    * ``VerticalBlindWindowStrategy.calculateTopBarProgress`` clamps the top bar
      to the bottom bar: ``if (touch > bottomBarProgress) touch =
      bottomBarProgress`` (smali:563). The mirror clamp is in
      ``calculateBottomBarProgress`` (smali:454).
    * The widget works in raw motor units (``BlindWindowView.max = 1000.0f``) and
      converts both ways with ``1 - raw/range``, so widgetProgress = 1000 - raw.

    Hence ``topBarProgress <= bottomBarProgress`` reduces to ``rawTop >=
    rawBottom``. Anything else asks the bottom rail to rise past the top rail -
    that is the collision.

    This matches the hardware exactly: (0, 0) puts both rails at their lowest,
    i.e. in contact, and (70, 30) leaves them far apart.

    Critically, the app's **command path** performs no such check - each rail is
    clamped only to 0..1000 independently. The relationship exists purely in the
    slider, so an integration driving the motor directly must enforce it itself.
    The motor's end stops are write-only (no numeric limit read-back exists), so
    nothing else would stop a bad move.

    ``margin`` is extra clearance in raw units on top of the hard rule. Use
    ``TEST_MARGIN`` (100) for first-time testing; it then refuses an exact
    contact move, which is expected.
    """
    t = float(top)
    b = float(bottom)

    if not (0.0 <= t <= 100.0) or not (0.0 <= b <= 100.0):
        raise ValueError(f"positions must be 0-100, got top={t} bottom={b}")

    if rule not in RAIL_RULES:
        raise ValueError(f"unknown rule {rule!r}, expected one of {RAIL_RULES}")

    if rule == RULE_NONE:
        return

    if rule == RULE_SUM:
        if t + b > max_sum - margin:
            raise RailCollisionError(
                f"refusing: top={t}% + bottom={b}% = {t + b}% exceeds "
                f"{max_sum - margin}% (rule={rule}, margin={margin})"
            )
    elif rule == RULE_TOP_BELOW_BOTTOM:
        if b - t < margin:
            raise RailCollisionError(
                f"refusing: bottom={b}% is not at least {margin}% above "
                f"top={t}% (rule={rule}, margin={margin})"
            )
    elif rule == RULE_TOP_ABOVE_BOTTOM:
        if t - b < margin:
            raise RailCollisionError(
                f"refusing: top={t}% is not at least {margin}% above "
                f"bottom={b}% (rule={rule}, margin={margin})"
            )


def rule_allows_known_good_states(
    rule: str, margin: float = DEFAULT_MARGIN
) -> bool:
    """Does this rule accept every state the blind is known to rest in?"""
    try:
        for top, bottom in KNOWN_GOOD_STATES:
            validate_rail_positions(top, bottom, rule=rule, margin=margin)
    except (RailCollisionError, ValueError):
        return False
    return True

# Response opcodes that carry a rail position. Verified on real hardware: a
# status poll returns TWO notifications, one per rail, each with its own opcode.
#
#   top    : ff 01 02 03 d2 02 47 bd 02  -> opcode 0xD2, 71%, raw 701
#   bottom : ff 01 02 03 70 02 1f 2d 01  -> opcode 0x70, 31%, raw 301
#
# Identified by matching against known real positions (top 70%, bottom 30%).
RAIL_TOP = "top"
RAIL_BOTTOM = "bottom"

OP_TOP_POSITION = 0xD2
OP_BOTTOM_POSITION = 0x70

RAIL_BY_OPCODE = {
    OP_TOP_POSITION: RAIL_TOP,
    OP_BOTTOM_POSITION: RAIL_BOTTOM,
}


def clamp_rail_positions(
    top: float,
    bottom: float,
    *,
    rule: str = RULE_TOP_ABOVE_BOTTOM,
    margin: float = 0.0,
    adjust: str | None = None,
    gap: float = MIN_RAIL_GAP,
) -> tuple[float, float, bool]:
    """Clamp a requested position into the safe envelope instead of refusing.

    Returns ``(top, bottom, clamped)``.

    The vendor app behaves this way: its slider clamps the handle to the other
    rail's position rather than refusing the drag
    (``VerticalBlindWindowStrategy.calculateTopBarProgress``). Matching that is
    both friendlier and truer to the product - you ask for 51%, it goes to the
    most it safely can, and tells you.

    ``adjust`` names the rail that was being moved, so it is that one that gets
    clamped. Without it, the bottom rail is adjusted.
    """
    t = float(top)
    b = float(bottom)

    if not (0.0 <= t <= 100.0) or not (0.0 <= b <= 100.0):
        raise ValueError(f"positions must be 0-100, got top={t} bottom={b}")

    if rule == RULE_NONE:
        return t, b, False

    if rule == RULE_TOP_ABOVE_BOTTOM:
        required = max(margin, gap)
        if t - b >= required:
            return t, b, False
        if adjust == RAIL_TOP:
            return min(100.0, b + required), b, True
        return t, max(0.0, t - required), True

    if rule == RULE_TOP_BELOW_BOTTOM:
        if b - t >= margin:
            return t, b, False
        if adjust == RAIL_BOTTOM:
            return t, min(100.0, t + margin), True
        return max(0.0, b - margin), b, True

    if rule == RULE_SUM:
        total = t + b
        if total <= max_sum_of(t, b) - margin:
            return t, b, False
        return t, max(0.0, max_sum_of(t, b) - margin - t), True

    return t, b, False


def max_sum_of(top: float, bottom: float) -> float:
    """Nominal ceiling for the sum rule (kept as a function for clarity)."""
    return 100.0


def motor_range_from_reply(data: bytes) -> int:
    """Infer the motor range from the length of a status reply.

    ``getPositionBytes`` writes 2 bytes when the motor range is 1000 and 1 byte
    when it is 100 (only ``MotorType.HD0100``), and a status reply mirrors the
    position field. So a 9-byte reply means range 1000 and a 7-byte reply means
    range 100 - which removes the need to ask the user for a value they cannot
    reasonably know.

    Defaults to 1000 when the reply is too short to tell, since that is every
    motor except HD0100.
    """
    if len(data) >= 9:
        return POSITION_RANGE_DEFAULT
    if len(data) == 7:
        return POSITION_RANGE_HD0100
    return POSITION_RANGE_DEFAULT


def rail_from_opcode(opcode: int | None) -> str | None:
    """Which rail a status reply describes, or None if unrecognised."""
    if opcode is None:
        return None
    return RAIL_BY_OPCODE.get(int(opcode))


def parse_status(data: bytes, motor_range: int = POSITION_RANGE_DEFAULT) -> dict:
    """Decode a motor status reply.

    Layout confirmed against a real HD3800 reply to ``readStatus``::

        ff 01 02 03 d2 02 46 bc 02

        [0]    0xFF    frame start
        [1..3]         header/counter, meaning unconfirmed
        [4]    0xD2    opcode (request D1 is answered by D2)
        [5]    0x02    meaning unconfirmed
        [6]    0x46    position as a percentage (70)
        [7..8] LE      position as a raw motor value (0x02BC = 700)

    A reply describes **one rail only** - the other rail is not in the frame.
    Whether varying the request's trailing byte selects the rail is unverified.
    """
    result: dict = {
        "raw": data.hex(" "),
        "length": len(data),
        "header": data[0:4].hex(" ") if len(data) >= 4 else None,
        "opcode": None,
        "rail": None,
        "flag": None,
        "percent_direct": None,
        "position_percent": None,
        "position_raw": None,
        "battery": battery_from_status(data),
        "notes": [],
    }

    if not data:
        result["notes"].append("empty response")
        return result

    if len(data) > 4:
        result["opcode"] = data[4]
        result["rail"] = rail_from_opcode(data[4])
        if result["rail"] is None:
            result["notes"].append(
                f"opcode 0x{data[4]:02x} is not a known position opcode"
            )
    if len(data) > 5:
        result["flag"] = data[5]
    if len(data) > 6:
        result["percent_direct"] = data[6]

    if len(data) >= 9:
        raw = int.from_bytes(data[7:9], "little")
        result["position_raw"] = raw
        result["position_percent"] = raw_to_percent(raw, motor_range)
    elif len(data) >= 7:
        result["position_percent"] = float(data[6])
        result["notes"].append(
            "reply shorter than 9 bytes; used byte[6] as the percentage"
        )
    else:
        result["notes"].append("response too short for a position field")

    result["notes"].append(
        "a poll returns one reply per rail; collect them all for both positions"
    )
    return result
