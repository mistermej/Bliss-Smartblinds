"""Shared constants."""

from __future__ import annotations

import logging

DOMAIN = "bliss_smartblinds"
LOGGER_NAME = f"custom_components.{DOMAIN}"

CONF_ADDRESS = "address"
CONF_NAME = "name"
CONF_MOTOR_RANGE = "motor_range"

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
# How long the confirmation stays up before the sensor goes back to reporting
# the real state. Long enough to notice on a dashboard, short enough that it
# does not sit there looking like something is still happening.
SYNC_CONFIRM_SECONDS = 10.0

# Motor type reported by TDBU (dual-servo) blinds - confirmed on hardware.
MOTOR_TYPE_HD3800 = "HD3800"