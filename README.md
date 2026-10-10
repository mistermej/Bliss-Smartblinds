# Bliss Smartblinds

Unofficial Home Assistant integration for **Hunter Douglas Bliss Smart Blinds**,
reverse-engineered from the vendor Android app.

> **Not affiliated with, endorsed by, or supported by Hunter Douglas.**
> "Bliss" and "Hunter Douglas" are trademarks of their respective owner. This is
> a community project.

Control your blinds **locally over Bluetooth**. No cloud account, no internet
connection, no vendor server — once installed, it works offline.

> **Please note:** this is a hobby project provided as-is. I take no
> responsibility or liability for any damage. Blinds driven by this integration
> can crash, jam, get stuck, or end up in a defective or damaged state, and
> anything that happens to your blinds or your home is entirely at your own
> risk. Test carefully before trusting it with blinds you care about.

If you appreciate my work and want to support me in continuing this project, you
can do it through:

[![ko-fi](https://img.shields.io/badge/ko--fi-mistermej-FF5E5B?style=flat-square)](https://ko-fi.com/mistermej)

## Compatibility

**Only tested on the `HD3800` dual-servo (TDBU) motor** — the type used in the
Duette TDBU, which has a separately controllable top and bottom rail.

`HD0100` and single-servo roller blinds are implemented from the same reverse
engineering, but **no hardware testing has been done on them**. Everything in
this README describes the TDBU only.

## Features

- **Two cover entities per blind** — the top rail and the bottom rail, each
  independently controllable, matching how the Hunter Douglas app works.
- **Local only** — talks to the blinds over Bluetooth LE. Nothing leaves your
  house.
- **No authentication** — the vendor's account login is irrelevant to control.
- **Works with ESPHome Bluetooth proxies** — no local Bluetooth adapter needed.
- **Position persistence** — the last known positions survive a restart.
- **Self-healing state** — the position is read automatically when a blind is
  added, and after every command.
- **Collision guard** — clamps any move that would drive the two rails into each
  other, which could jam or damage the blind.

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS (category: *Integration*)
2. Install **Bliss Smartblinds**
3. Restart Home Assistant

### Manual

Copy `custom_components/bliss_smartblinds` into your Home Assistant `config`
directory, so you end up with `config/custom_components/bliss_smartblinds`, then
restart.

## Configuration

**Settings → Devices & Services → Add Integration → Bliss Smartblinds**

Enter the blind's Bluetooth MAC address, for example `AA:BB:CC:DD:EE:FF`. The
address is on the sticker inside the blind's head rail. The name is optional.

You need a working Bluetooth integration, plus either a local Bluetooth adapter
or one or more
[ESPHome Bluetooth proxies](https://esphome.io/projects/?type=bluetooth) —
these motors are weak BLE devices and usually need a proxy in the same room.

The blind must already be set up and claimed in the Hunter Douglas app. It
accepts only one Bluetooth connection at a time, so close the vendor app before
controlling a blind from Home Assistant.

## Usage

You get two covers per blind, for example:

| Entity | Controls |
|---|---|
| `cover.living_room_top_rail` | top rail, 0–100% |
| `cover.living_room_bottom_rail` | bottom rail, 0–100% |

Each rail is controlled independently, like the vendor app. The other rail stays
wherever it is when you move one of them.

### Movement safety

A TDBU blind has two rails that must never cross, and that must not sit so
close that one lifts the other — doing either can jam or damage the blind.

The integration therefore keeps a **minimum 1% gap** between them. If you ask
for a position that would close it, the move goes to the closest safe position
and a `Collision guard` warning is logged, naming the rail, where it stopped and
where you asked for.

This is **stricter than the Hunter Douglas app**, whose slider clamps to exactly
equal and applies no gap at all — so at equal positions it can even lift or push
down a rail a bit.

## Licence

MIT. See [LICENSE](LICENSE).
