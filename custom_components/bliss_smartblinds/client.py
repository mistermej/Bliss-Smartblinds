"""Async BLE client for a single Bliss blind.

Transport note (important): this client deliberately does *not* use
``bleak.BleakClient`` directly. Inside Home Assistant, traffic to Bluetooth
devices must be routed through HA's Bluetooth stack so that ESPHome Bluetooth
proxies are used. Calling bleak directly would try the local OS adapter, which
does not exist in the HA Core container, and every connection would fail.

The supported route is:

    homeassistant.components.bluetooth.async_ble_device_from_address()
      -> bleak_retry_connector.establish_connection()

Positioning: a TDBU blind moves both rails in a *single* command
(``FF 78 EA 41 FE 03 <top> <bottom>``), so moving one rail requires knowing
where the other one is. This client keeps a local cache of the last
commanded/reported position per rail.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from bleak_retry_connector import (
    BleakClientWithServiceCache,
    close_stale_connections,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    async_rediscover_address,
    async_request_active_scan,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    COMMAND_CHARACTERISTIC_UUID,
    DOMAIN,
    LOGGER_NAME,
    POSITION_RANGE_DEFAULT,
    RESPONSE_CHARACTERISTIC_UUID,
)
from .protocol import (
    RULE_TOP_ABOVE_BOTTOM,
    build_move_both_bars,
    build_move_command,
    build_read_status,
    build_set_clock,
    build_speed,
    build_stop,
    RailCollisionError,
    MIN_RAIL_GAP,
    clamp_rail_positions,
    motor_range_from_reply,
    parse_status,
    validate_rail_positions,
    write_needs_response,
)

_LOGGER = logging.getLogger(LOGGER_NAME)

RAIL_TOP = "top"
RAIL_BOTTOM = "bottom"

# How long to keep collecting notifications after a write. The motor answers a
# status poll with TWO notifications (one per rail) a short interval apart, so
# the window must outlive the gap between them or the second rail is missed.
REPLY_WINDOW = 3.0
REPLY_GAP = 1.0

# After a move, wait for the motor to reach the target before reading back.
VERIFY_DELAY = 1.5
# How far the reported position may differ from the commanded one.
VERIFY_TOLERANCE = 5.0
# How long the motor may stay silent before the entity is called unavailable.
# These motors are sleepy and the link can be weak, so allow a generous window.
CONNECT_TIMEOUT = 45.0
# Release the BLE connection this long after the last operation. ESPHome proxies
# have a small, fixed number of connection slots, and a sleepy motor should be
# able to go back to sleep, so the link is closed when idle rather than held open.
IDLE_DISCONNECT = 8.0
STALE_SECONDS = 900.0
# Persisted-state storage.
STORAGE_VERSION = 1
SAVE_DEBOUNCE = 2.0
# Extra delay before a verification retry; a travelling motor may not answer.
VERIFY_RETRY_DELAY = 2.0
VERIFY_ATTEMPTS = 3


class BlissClient:
    """Manage the BLE connection to one Bliss blind."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str | None = None,
        motor_range: int = POSITION_RANGE_DEFAULT,
    ) -> None:
        self.hass = hass
        # Normalise to upper case. Home Assistant's Bluetooth manager keys
        # devices by the address bleak reported, which is upper case on BlueZ,
        # and its lookups are exact string matches. The config flow stores a
        # lower-case address (format_mac lower-cases), so a config-entry client
        # would never match anything - the blind would simply "not be seen by
        # any adapter or proxy". Diagnostics previously worked only because those
        # temporary clients kept the address exactly as the user typed it.
        self.address = str(address).upper()
        self.name = name or self.address
        self.motor_range = motor_range

        # Safety switches. Movement is OFF until explicitly enabled.
        self.movement_enabled = False
        # Positions are persisted so a restart does not lose them. Polling a
        # sleepy motor every 30s just to redraw the UI is not worth a BLE
        # connection each time; the cache is refreshed on demand instead.
        self._store: Store | None = None
        self._save_handle: asyncio.TimerHandle | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._load_task: asyncio.Task | None = None
        self._operations = 0
        # Incremented for every move. Verification that comes back for an older
        # sequence number describes a move that has since been replaced, and
        # reporting it would blame the motor for the newer move's position.
        self._move_seq = 0
        # A motor can only do one thing at a time. Commands are serialised
        # through this lock: a second request arriving mid-move waits for the
        # first to finish rather than being written while the motor travels.
        self._move_lock = asyncio.Lock()
        # Verification polls too. Two pollers at once interleave their
        # notifications, so replies get attributed to the wrong request and
        # every one of them comes back empty.
        self._verify_lock = asyncio.Lock()
        # Entities subscribe here so a position change is pushed to them. With
        # periodic polling off, nothing would otherwise re-read the cache - a
        # sync would update the motor's real position and leave the UI on
        # 'unknown'.
        self._listeners: list = []
        # The vendor app's own rule; see validate_rail_positions.
        self.rail_rule = RULE_TOP_ABOVE_BOTTOM

        self._client: Any | None = None
        self._lock = asyncio.Lock()
        self._responses: asyncio.Queue[bytes] = asyncio.Queue()

        # Last known / last commanded position per rail, 0-100.
        self.position: dict[str, float | None] = {
            RAIL_TOP: None,
            RAIL_BOTTOM: None,
        }
        self.available = False
        # When the motor last answered a status poll. Used for staleness.
        self.last_seen: float | None = None

    # --- connection ---------------------------------------------------------

    def _wake(self) -> None:
        """Nudge Home Assistant to go looking for a sleeping motor.

        Reading ``async_ble_device_from_address`` only consults HA's cache. If no
        scanner is currently listening, a blind that advertises for a moment and
        then sleeps never reaches the cache, so retrying the lookup on a timer
        achieves nothing. ``async_rediscover_address`` and
        ``async_request_active_scan`` make the configured proxies actually go
        and look for it.
        """
        try:
            async_rediscover_address(self.hass, self.address)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: rediscover failed: %s", self.address, err)
        try:
            self.hass.async_create_task(async_request_active_scan(self.hass))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: active scan request failed: %s", self.address, err)

    def _ble_device(self):
        """Look the address up in HA's Bluetooth manager.

        Returns None when the device has not been seen yet - typically because
        the blind is asleep or no proxy is in range. The device only appears
        after it has advertised at least once.
        """
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    def _disconnected_callback(self, _client) -> None:
        _LOGGER.debug("%s: disconnected callback", self.address)
        self.available = False

    async def async_connect(self, timeout: float = CONNECT_TIMEOUT) -> None:
        """Establish the connection, retrying until ``timeout``.

        Two things make a single attempt unreliable for these blinds:

        * They are **sleepy**. A motor advertises for a moment and then stops,
          so ``async_ble_device_from_address`` often returns None at the exact
          instant we look.
        * Signal is weak. A capture of one real blind showed rssi -98 dBm,
          which is close to the floor for a usable connection.

        So instead of failing on the first miss, poll for the device to appear
        and connect as soon as it does.
        """
        async with self._lock:
            if self._client is not None and self._client.is_connected:
                self.available = True
                return

            deadline = time.monotonic() + timeout
            last_error: str = "not seen by any Bluetooth adapter or proxy"

            while True:
                ble_device = self._ble_device()
                if ble_device is None:
                    # Nothing in the cache: ask the proxies to go and look.
                    self._wake()
                if ble_device is not None:
                    try:
                        await close_stale_connections(ble_device)
                        client = await establish_connection(
                            BleakClientWithServiceCache,
                            ble_device,
                            self.name,
                            self._disconnected_callback,
                            ble_device_callback=lambda: self._ble_device(),
                        )
                    except Exception as err:  # noqa: BLE001
                        last_error = f"{type(err).__name__}: {err}"
                        _LOGGER.debug(
                            "%s: connect attempt failed (%s), retrying",
                            self.address,
                            last_error,
                        )
                    else:
                        self._client = client
                        # Notifications carry motor replies. Absence is not fatal.
                        try:
                            await client.start_notify(
                                RESPONSE_CHARACTERISTIC_UUID,
                                self._handle_notification,
                            )
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.debug(
                                "%s: notifications unavailable: %s", self.address, err
                            )
                        self.available = True
                        _LOGGER.debug("%s: connected", self.address)
                        return

                if time.monotonic() >= deadline:
                    self.available = False
                    raise ConnectionError(
                        f"Could not reach {self.address} within {timeout:.0f}s. "
                        f"Last reason: {last_error}. Things to check: (1) is the "
                        "Hunter Douglas app open on a phone - a blind accepts only "
                        "one Bluetooth connection, so an active phone connection "
                        "stops it being visible; close the app first. (2) is an "
                        "ESPHome Bluetooth proxy near that blind - a weak proxy "
                        "may hear it once and then lose it. (3) wake the blind "
                        "with its remote and retry."
                    )

                await asyncio.sleep(1.5)

    def _handle_notification(self, _sender, data: bytearray) -> None:
        self._responses.put_nowait(bytes(data))

    def async_setup_storage(self, entry_id: str) -> None:
        """Load persisted positions. Positions do not change on their own, so
        this is sufficient after a restart - no polling needed."""
        self._store = Store(self.hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")

        async def _load() -> None:
            data = await self._store.async_load()
            if not data:
                return
            saved = data.get("positions") or {}
            for rail, value in saved.items():
                if rail in self.position and isinstance(value, (int, float)):
                    self.position[rail] = float(value)
            _LOGGER.info(
                "%s: restored positions from storage: %s", self.address, saved
            )
            # Tell the entities. Without this the restored values sit in the
            # cache while the covers keep rendering 'unknown', because nothing
            # else re-reads them now that polling is off.
            self.notify_listeners()

        self._load_task = self.hass.async_create_task(_load())

    async def async_flush(self) -> None:
        """Write pending positions immediately.

        Saving is debounced, so a restart shortly after a move could lose the
        last change. Called on unload and before disconnecting.
        """
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        if self._store is None:
            return
        await self._store.async_save(
            {
                "positions": {
                    k: v for k, v in self.position.items() if v is not None
                }
            }
        )

    def add_update_listener(self, callback) -> None:
        """Register a callback fired whenever the cached positions change."""
        self._listeners.append(callback)

    def remove_update_listener(self, callback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def notify_listeners(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s: update listener failed: %s", self.address, err)

    def _cancel_idle_disconnect(self) -> None:
        """Called when an operation starts; keep the connection while busy."""
        self._operations += 1
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _schedule_idle_disconnect(self) -> None:
        """Called when an operation ends; close the link once idle."""
        self._operations = max(0, self._operations - 1)
        if self._operations or self._idle_handle is not None:
            return

        def _close() -> None:
            self._idle_handle = None
            try:
                self.hass.async_create_task(self.async_disconnect())
            except Exception:  # noqa: BLE001
                pass

        self._idle_handle = self.hass.loop.call_later(IDLE_DISCONNECT, _close)

    def _schedule_save(self) -> None:
        """Debounced save, so a burst of moves costs one write."""
        if self._store is None or self._save_handle is not None:
            return

        async def _save() -> None:
            self._save_handle = None
            await self._store.async_save(
                {
                    "positions": {
                        k: v for k, v in self.position.items() if v is not None
                    }
                }
            )

        self._save_handle = self.hass.loop.call_later(SAVE_DEBOUNCE, self.hass.async_create_task, _save())

    def is_present(self, stale_after: float = STALE_SECONDS) -> bool:
        """Whether the entity should be treated as available.

        These motors are sleepy and stop advertising between commands, so
        "is it in the Bluetooth cache right now" is almost always False and
        makes the entity look permanently broken. Availability therefore tracks
        how long ago the motor last answered:

        * connected right now          -> available
        * never answered yet            -> available, so the UI is usable and the
          missing position is visible as unknown rather than as a dead entity
        * answered, but a while ago    -> available until STALE_SECONDS passes
        """
        if self.available:
            return True
        if self.last_seen is None:
            return True
        return (time.monotonic() - self.last_seen) < stale_after

    async def async_disconnect(self) -> None:
        await self.async_flush()
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        async with self._lock:
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("%s: disconnect error: %s", self.address, err)
                self._client = None
            self.available = False

    
    # --- writes -------------------------------------------------------------

    async def _async_write(self, frame: bytes, wait: float = 1.5) -> list[bytes]:
        """Write a frame and collect any notifications that follow."""
        self._cancel_idle_disconnect()
        try:
            return await self._async_write_inner(frame, wait)
        finally:
            self._schedule_idle_disconnect()

    async def _async_write_inner(self, frame: bytes, wait: float) -> list[bytes]:
        await self.async_connect()
        client = self._client
        assert client is not None

        # Drain stale replies first.
        while not self._responses.empty():
            self._responses.get_nowait()

        use_response = write_needs_response(client.services, COMMAND_CHARACTERISTIC_UUID)

        try:
            await client.write_gatt_char(
                COMMAND_CHARACTERISTIC_UUID, frame, response=use_response
            )
        except Exception as err:  # noqa: BLE001
            self.available = False
            mode = "with response" if use_response else "without response"
            raise ConnectionError(
                f"{self.address}: write ({mode}) of {frame.hex(' ')} failed: "
                f"{type(err).__name__}: {err}"
            ) from err

        replies: list[bytes] = []
        if wait > 0:
            try:
                while True:
                    replies.append(
                        await asyncio.wait_for(self._responses.get(), timeout=wait)
                    )
                    # Keep listening: a status poll answers with one notification
                    # per rail, separated by a short gap.
                    wait = REPLY_GAP
            except asyncio.TimeoutError:
                pass
        return replies

    # --- operations ---------------------------------------------------------

    async def async_move_to(
        self,
        top: float,
        bottom: float,
        *,
        force: bool = False,
        verify: bool = True,
        adjust: str | None = None,
    ) -> dict:
        """Move both rails, then verify - without blocking other moves.

        The lock is held only for the write. Verification polls for several
        seconds because a travelling motor does not answer, and holding the lock
        across it would queue a second command until the first had finished, so
        the rails travelled one after the other instead of together.

        The BLE link is held across the whole move *and* its verification:
        dropping it while the motor is still travelling makes the blind stop.
        """
        self._cancel_idle_disconnect()
        try:
            async with self._move_lock:
                result = await self._async_move_unlocked(
                    top, bottom, force=force, adjust=adjust
                )
            if verify:
                await self._async_verify_move(result)
            return result
        finally:
            self._schedule_idle_disconnect()

    async def _async_verify_move(self, result: dict) -> None:
        """Read the motor back and record whether it went where it was told.

        Runs outside the move lock so it never delays another command.
        """
        # Verification takes several seconds because a travelling motor does
        # not answer. If a newer move went out in the meantime, whatever we read
        # back now describes *that* move, not this one - reporting it would
        # blame the motor for a position it was correctly told to go to.
        if result["seq"] != self._move_seq:
            _LOGGER.debug(
                "%s: discarding verification of move %s, superseded by move %s",
                self.address,
                result["seq"],
                self._move_seq,
            )
            result["superseded"] = True
            return

        expected = (result["commanded"]["top"], result["commanded"]["bottom"])

        # One poller at a time. Concurrent polls interleave their notifications
        # and each comes back empty, which reads as "the motor said nothing"
        # when in fact the replies were simply crossed.
        async with self._verify_lock:
            # Re-checked *after* polling. Checking only before it is useless:
            # polling takes seconds, so by the time it finishes the move is
            # usually superseded, and reporting its result blames the motor for
            # a position it was correctly told to go to.
            if result["seq"] != self._move_seq:
                _LOGGER.debug(
                    "%s: discarding verification of move %s, superseded by "
                    "move %s while polling",
                    self.address,
                    result["seq"],
                    self._move_seq,
                )
                result["superseded"] = True
                return
            observed = await self._async_verify_positions(
                expected=expected, seq=result["seq"]
            )
        result["observed"] = observed

        if result["seq"] != self._move_seq:
            _LOGGER.debug(
                "%s: discarding verification of move %s, superseded by move %s",
                self.address,
                result["seq"],
                self._move_seq,
            )
            result["superseded"] = True
            return

        # A single-rail frame says nothing about the other rail, so only the
        # rail that was commanded can be judged.
        rails = (
            (result["adjust"],)
            if result.get("adjust")
            else (RAIL_TOP, RAIL_BOTTOM)
        )
        matches = self._compare(expected=expected, observed=observed, rails=rails)
        result["matches"] = matches
        result["verified"] = matches is not None

        # None means "could not tell" - never True. Reporting success when
        # nothing was observed is the one failure mode a guard must not have.
        if matches is None:
            _LOGGER.warning(
                "%s: move sent (%s) but the motor returned no position, so the "
                "result is UNVERIFIED.",
                self.address,
                ", ".join(
                    f"{rail}={expected[0] if rail == RAIL_TOP else expected[1]}"
                    for rail in rails
                ),
            )
        elif not matches:
            _LOGGER.warning(
                "%s: commanded %s but motor reports %s",
                self.address,
                ", ".join(
                    f"{rail}={expected[0] if rail == RAIL_TOP else expected[1]}"
                    for rail in rails
                ),
                {
                    rail: (info.get("position_percent") if info else None)
                    for rail, info in observed.items()
                },
            )

    async def _async_move_unlocked(
        self,
        top: float,
        bottom: float,
        *,
        force: bool = False,
        adjust: str | None = None,
    ) -> dict:
        """Clamp the request, build the frame and write it. No verification."""
        """Move both rails. This is the only way a TDBU blind moves.

        Safety, in order:

        1. ``movement_enabled`` - off by default; nothing can drive the motor
           until it is explicitly turned on.
        2. ``validate_rail_positions`` - the vendor app's own rule, top rail at
           or above the bottom rail (``rawTop >= rawBottom``). Equality is
           allowed: it is the normal position for a TDBU.
        3. Read-back verification - after the write, poll the motor and compare
           with what was commanded. The app does not validate outgoing moves and
           the motor's end stops are write-only, so confirming the result is the
           only way to notice the blind did not go where it was told.

        ``force`` skips only guard 2.
        """
        if not self.movement_enabled:
            raise ConnectionError(
                f"{self.address}: movement is disabled. Enable it explicitly "
                "with the set_movement_enabled service first."
            )

        # Clamp rather than refuse, which is what the vendor app does with its
        # slider: it clamps the handle to the other rail rather than refusing
        # the drag. The request is honoured as far as the rails safely allow and
        # the shortfall is reported, rather than the command being blocked.
        requested_top, requested_bottom = float(top), float(bottom)
        clamped = False
        if not force:
            top, bottom, clamped = clamp_rail_positions(
                requested_top,
                requested_bottom,
                rule=self.rail_rule,
                margin=0.0,
                adjust=adjust,
            )
            if clamped:
                moved = "Top" if adjust == RAIL_TOP else "Bottom"
                _LOGGER.warning(
                    "Collision guard %s - %s rail: stopped at %.1f%% instead of "
                    "%.1f%%. Top rail = %.1f%%, bottom rail = %.1f%%. They must "
                    "keep at least %s%% between them, or one rail lifts the other.",
                    self.address,
                    moved,
                    top if adjust == RAIL_TOP else bottom,
                    requested_top if adjust == RAIL_TOP else requested_bottom,
                    top,
                    bottom,
                    MIN_RAIL_GAP,
                )

        # Clamping produces values like 30.099999999999998. Round before they
        # reach a frame, the cache or the log.
        top, bottom = round(top, 1), round(bottom, 1)

        frame = build_move_command(top, bottom, adjust, self.motor_range)
        # INFO, not WARNING. Moving a blind is the integration working, and a
        # warning for it made Home Assistant raise a persistent "N occurrences"
        # card in the UI every time anyone touched a slider - noise that trains
        # you to ignore the log, which is exactly where the real warnings are.
        # INFO keeps the line in the log file for troubleshooting without
        # claiming something went wrong.
        _LOGGER.info(
            "%s: MOVE %s%s -> %s%s",
            self.address,
            f"top={top}" if adjust == RAIL_TOP else "",
            f"bottom={bottom}" if adjust == RAIL_BOTTOM else "",
            frame.hex(" "),
            " (FORCED, rail guard skipped)" if force else "",
        )
        self._move_seq += 1
        await self._async_write(frame)

        # Only the rail we actually commanded may be cached as "where it is
        # now". With a both-rails frame the other rail was commanded too, so it
        # is known; with a single-rail frame it was not, and its cached value
        # stays whatever was last actually observed.
        if adjust in (None, RAIL_TOP):
            self.position[RAIL_TOP] = float(top)
        if adjust in (None, RAIL_BOTTOM):
            self.position[RAIL_BOTTOM] = float(bottom)
        self._schedule_save()
        self.notify_listeners()

        result: dict = {
            "commanded": {"top": top, "bottom": bottom},
            "requested": {"top": requested_top, "bottom": requested_bottom},
            "clamped": clamped,
            "adjust": adjust,
            "seq": self._move_seq,
        }
        return result

    async def _async_verify_positions(
        self,
        expected: tuple[float, float],
        attempts: int = VERIFY_ATTEMPTS,
        seq: int | None = None,
    ) -> dict:
        """Poll for the motor's positions after a move, with retries.

        A motor that is still travelling may not answer a status poll, so a
        single empty poll proves nothing. Retry a few times before concluding
        the motor is not reporting.
        """
        for attempt in range(attempts):
            if seq is not None and seq != self._move_seq:
                # A newer move went out while this one was being verified. What
                # we would read back describes that move, so stop asking rather
                # than spend BLE traffic on an answer we must throw away.
                return {}
            await asyncio.sleep(VERIFY_DELAY if attempt == 0 else VERIFY_RETRY_DELAY)
            try:
                observed = await self.async_request_status()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "%s: verify poll %d/%d failed: %s",
                    self.address,
                    attempt + 1,
                    attempts,
                    err,
                )
                continue
            if observed:
                return observed
            _LOGGER.debug(
                "%s: verify poll %d/%d returned nothing",
                self.address,
                attempt + 1,
                attempts,
            )
        return {}

    @staticmethod
    def _compare(
        expected: tuple[float, float],
        observed: dict,
        rails: tuple[str, ...] = (RAIL_TOP, RAIL_BOTTOM),
    ) -> bool | None:
        """Compare the motor's reported positions with what was commanded.

        Returns:
            True  - every rail observed and within tolerance
            False - at least one rail observed and out of tolerance
            None  - nothing (or not enough) was observed, so it cannot be told

        ``None`` is the important case. An earlier version returned True when
        the motor said nothing at all, which reported a successful, verified
        move on the strength of an empty response.
        """
        verdicts: list[bool] = []
        for rail, want in zip(rails, expected):
            got = (observed.get(rail) or {}).get("position_percent")
            if got is None:
                return None  # incomplete - never claim success
            verdicts.append(abs(float(got) - float(want)) <= VERIFY_TOLERANCE)

        if not verdicts:
            return None
        return all(verdicts)

    async def async_set_rail(self, rail: str, percent: float) -> None:
        """Move one rail, leaving the other alone.

        The other rail is *not* commanded. Its position is only read so the
        collision guard has something to clamp against - it is never written to
        the motor, because a stale belief about it would drag that rail
        somewhere the user never asked for.
        """
        other = RAIL_BOTTOM if rail == RAIL_TOP else RAIL_TOP
        held = self.position[other]
        if held is None:
            # Unknown. Only the guard uses this, so the worst case is a clamp
            # computed against 0%; the frame sent carries one rail regardless.
            _LOGGER.warning(
                "%s: position of %s rail unknown, so the collision guard has "
                "nothing to clamp against. The move itself is unaffected.",
                self.address,
                other,
            )
            held = 0.0
        if rail == RAIL_TOP:
            await self.async_move_to(percent, held, adjust=RAIL_TOP)
        else:
            await self.async_move_to(held, percent, adjust=RAIL_BOTTOM)

    async def async_stop(self) -> None:
        await self._async_write(build_stop())
        # The blinds are at an unknown point now, so the cached positions are no
        # longer trustworthy and must not be restored as if they were.
        self.position = {RAIL_TOP: None, RAIL_BOTTOM: None}
        self._schedule_save()
        self.notify_listeners()

    async def async_set_speed(self, percent: int) -> None:
        await self._async_write(build_speed(percent), wait=0.0)

    async def async_set_clock(self, when: datetime | None = None) -> None:
        await self._async_write(build_set_clock(when), wait=0.0)

    async def async_request_status(self) -> dict:
        """Poll the motor and return every rail position it reports.

        Verified on real hardware: one poll produces **two** notifications, one
        per rail, distinguished by opcode - 0xD2 for the top rail and 0x70 for
        the bottom. Both must be collected; an earlier single-reply window only
        ever saw the top rail and silently reported no bottom position.
        """
        replies = await self._async_write(build_read_status(), wait=REPLY_WINDOW)
        parsed: dict[str, dict] = {}

        if replies:
            self.last_seen = time.monotonic()
            self._schedule_save()
            self.notify_listeners()

        for reply in replies:
            # The reply length reveals the motor range, so it never has to be
            # configured by hand.
            detected = motor_range_from_reply(reply)
            if detected != self.motor_range:
                _LOGGER.info(
                    "%s: motor range detected as %d (reply %d bytes), was %d",
                    self.address, detected, len(reply), self.motor_range,
                )
                self.motor_range = detected
            info = parse_status(reply, self.motor_range)
            rail = info.get("rail")
            percent = info.get("position_percent")
            if rail is not None and percent is not None:
                self.position[rail] = percent
                parsed[rail] = info
            else:
                _LOGGER.debug(
                    "%s: unassigned reply %s", self.address, reply.hex(" ")
                )

        if RAIL_TOP not in parsed:
            _LOGGER.debug("%s: no top-rail reply", self.address)
        if RAIL_BOTTOM not in parsed:
            _LOGGER.debug("%s: no bottom-rail reply", self.address)

        return parsed

    # --- state --------------------------------------------------------------

    def _absorb_unattributed(self, reply: bytes) -> None:
        """Log a position seen in a reply whose rail we cannot identify.

        A reply carries one rail's position but the frame does not say which.
        Rather than guessing - which would corrupt the cache and cause the
        untouched rail to jump - the value is only logged.
        """
        parsed = parse_status(reply, self.motor_range)
        percent = parsed.get("position_percent")
        if percent is not None:
            _LOGGER.debug(
                "%s: reply reports %.1f%% for an unidentified rail: %s",
                self.address,
                percent,
                reply.hex(" "),
            )