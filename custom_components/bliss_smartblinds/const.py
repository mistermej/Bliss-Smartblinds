"""Shared constants."""

from __future__ import annotations

import logging

DOMAIN = "bliss_smartblinds"
LOGGER_NAME = f"custom_components.{DOMAIN}"

CONF_ADDRESS = "address"
CONF_NAME = "name"
CONF_MOTOR_RANGE = "motor_range"

# --- Options (options.py) ---------------------------------------------------
# Stored inverted from the old ``allow_movement``: the switch in the UI is
# labelled "Maintenance Mode", so ON must mean the blind will not move. Keeping
# the old name would have meant a toggle labelled "Maintenance Mode" that turned
# movement *on*.
CONF_MAINTENANCE_MODE = "maintenance_mode"
# Seconds to keep trying to reach the motor before giving up. Deliberately a
# duration and not a retry count: these motors advertise periodically, so how
# long we have been waiting is what decides whether one was heard, and "3 tries"
# means anything from four seconds to a minute depending on the proxy.
CONF_CONNECT_TIMEOUT = "connect_timeout"
# Motor speed preset, written to the motor as a frame of its own when the
# setting changes - never as part of a move, because the frame that v1.4
# put in front of every move beeps the blind and shifts it about 2%.
#
# The three presets are the app's own, from the ``BlindSpeedOptions``
# enum in ``models/BlindSpeedOptions.smali``, which has exactly three
# members - SLOW, COMFORT and FAST - each wired to its gen2 frame.
# All three share opcode 0xF2 and differ only in a trailing byte, so
# they are one family, not four independent speed opcodes. The app's
# FAST is backed by the method the code happens to name
# ``gen2NormalSpeedManual``; on some app builds that same preset is
# the one labelled "Stock"/"Standard".
CONF_SPEED = "speed"
# Exactly the three presets the app offers. There is deliberately no "leave it
# alone" choice: the app has none either, and a fourth option that sends
# nothing reads on a dashboard as though the setting were ignored.
SPEED_CHOICES = ("slow", "comfort", "fast")
# "Standard" - the app's name for this preset - is the default, because it is
# the one a blind arrives from the factory running at.
DEFAULT_SPEED = "fast"

SPEED_LABELS = {
    "slow": "Slow",
    "comfort": "Comfort",
    # The app displays this preset as "Standard" even though the enum member
    # and the resource key are both called *fast* (``setSpeed.fast`` ->
    # "Standard" in res/values/strings.xml). The stored value stays "fast"
    # because that is the traceable identifier in the app's code; only the
    # label is the user-facing word. Do not "correct" one to match the other.
    "fast": "Standard",
}
DEFAULT_CONNECT_TIMEOUT = 45
CONNECT_TIMEOUT_MIN = 5
CONNECT_TIMEOUT_MAX = 300

# Position presets, stored in the entry's options so they live in Home Assistant
# rather than on the motor. A preset is a name plus the two rail positions -
# per blind, because a rail percentage is a physical position on one specific
# motor. There is no speed in a preset: the only thing that could have used one
# is a speed frame ahead of a move, which beeps the blind and shifts it about
# 2%. A blind's speed is a setting of its own.
#
# Stored here rather than in the motor's own favourite slots because that keeps
# them reversible, allows as many as you like, and needs no write protocol
# verified against hardware.
CONF_PRESETS = "presets"

# The three fields of one stored preset. The name is the key in the presets
# block, and the two positions live inside it:
#   {"Lights out": {"top": 100, "bottom": 0}}
CONF_PRESET_NAME = "name"
CONF_PRESET_TOP = "top"
CONF_PRESET_BOTTOM = "bottom"
# Which preset the dropdown is pointing at. Never stored - it is the question
# the form is asking, not part of the answer.
CONF_PRESET_PICK = "preset"
# Tick to remove that preset instead of saving changes to it. Also never
# stored: it is what the Submit should *do*, not something to keep.
CONF_PRESET_DELETE = "delete"

# --- GATT (presentation/util/BleCharacteristics.kt) --------------------------
SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1910"
COMMAND_CHARACTERISTIC_UUID = "00010405-0405-0607-0809-0a0b0c0d1910"
RESPONSE_CHARACTERISTIC_UUID = "00010304-0405-0607-0809-0a0b0c0d1910"

# --- Frame header -----------------------------------------------------------
# FF = frame start/sync, 78 EA 41 = MCU-manager header + command class.
# There is NO sequence number and NO checksum/CRC/length byte.
FRAME_HEADER = bytes((0xFF, 0x78, 0xEA, 0x41))

SUBCODE_MOVE = 0x03
SUBCODE_MOVE_BOTTOM_FINE = 0x04
SUBCODE_TILT = 0x06
SUBCODE_TIME = 0x00

# --- Opcodes (models/BlissCommands.smali) -----------------------------------
OP_GOTO_TOP = 0xBF  # single-servo top
OP_TOP_TO_POSITION = 0xB1  # dual-servo top
OP_BOTTOM_TO_POSITION = 0xF4  # bottom rail
OP_MOVE_BOTH_BARS = 0xFE  # TDBU: both rails, two positions in one frame
OP_TILT_TO_ANGLE = 0xF9

OP_ROLLER_UP = 0xCF
OP_ROLLER_DOWN = 0x1F
OP_ROLLER_STOP = 0x5F  # also used as the generic stop command
OP_REVERSE = 0x81

OP_BOTTOM_BAR_UP = 0xE1
OP_BOTTOM_BAR_DOWN = 0x31

OP_BOTH_UP = 0xFA
OP_BOTH_STOP = 0xFB
OP_BOTH_DOWN = 0xFC

OP_SPEED_100 = 0xF0
OP_SPEED_75 = 0xF1
OP_SPEED_50 = 0xF2
OP_SPEED_25 = 0xF3

OP_READ_STATUS = 0xD1
OP_ID_STATUS = 0xDC
OP_READ_LOCATION = 0xB0
OP_SAVE_SETTINGS = 0x41
OP_SET_TIME = 0x02
OP_START_LIMIT_MODE = 0x21

SPEEDS = {
    100: OP_SPEED_100,
    75: OP_SPEED_75,
    50: OP_SPEED_50,
    25: OP_SPEED_25,
}

# --- Value scaling (models/BlissCommandsKt.smali, MotorTypeKt.getRange) ------
# Every motor uses a range of 1000 except HD0100, which uses 100.
# The range also decides the byte width: 1000 -> 2 bytes LE, 100 -> 1 byte.
POSITION_RANGE_DEFAULT = 1000
POSITION_RANGE_HD0100 = 100

# Raw position 0 == fully open. Reported position = 1 - raw / range.
# Scene fractions are 0.0..1.0 where 0.0 == fully retracted/open rail.

# --- Miscellaneous ----------------------------------------------------------
HEARTBEAT = bytes((0xFF, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01))
BATTERY_FULL = 0
BATTERY_LOW = 1
BATTERY_EMPTY = 2

# The battery state lives in bits 3-4 of this byte of a status reply.
# See protocol.battery_from_status for the vendor's mapping.
BATTERY_BYTE_INDEX = 5

# The words the motor's battery states are shown as. In const rather than
# sensor.py because the sync action and its log line need them too, and a
# second copy would be a second thing to keep in step.
BATTERY_LABELS = {
    BATTERY_FULL: "Good",
    BATTERY_LOW: "Low",
    BATTERY_EMPTY: "Empty",
}

# Health states for the status sensor. Short on purpose: this is meant to be
# glanceable in a dashboard, not read as a log line.
HEALTH_OK = "OK"
HEALTH_COLLISION = "Collision"
HEALTH_MOVE_FAILED = "Move failed"
HEALTH_NO_FEEDBACK = "No motor feedback"
# The blind could not be reached at all. A genuine failure - unlike simply
# not having heard from a sleeping motor, which is the normal resting state.
HEALTH_NOT_FOUND = "Cannot find blind"
HEALTH_MISMATCH = "Position mismatch"

# Transient, and unlike everything above these are not failures. A sync is a
# deliberate act by the user, so showing what it is doing is more useful than a
# blank moment followed by "OK" that could equally mean nothing happened. They
# fall back to whatever the real health was once the blind has answered.
HEALTH_SYNCING = "Syncing"
HEALTH_SYNCED = "Synced"

# Not a fault. It says the blind has been deliberately taken out of use, which
# is worth showing in the same place as the failures, because a blind sitting
# on "OK" whose moves are being ignored looks exactly like a blind that is
# broken in a way nothing can see.
HEALTH_MAINTENANCE_MODE = "Maintenance Mode"
# How long the confirmation stays up before the sensor goes back to reporting
# the real state. Long enough to notice on a dashboard, short enough that it
# does not sit there looking like something is still happening.
SYNC_CONFIRM_SECONDS = 10.0

# Motor type reported by TDBU (dual-servo) blinds - confirmed on hardware.
MOTOR_TYPE_HD3800 = "HD3800"