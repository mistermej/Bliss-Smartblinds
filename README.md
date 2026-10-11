# Bliss Smartblinds

Unofficial Home Assistant integration for **Hunter Douglas Bliss Smart Blinds**,
reverse-engineered from the vendor Android app.

Control your blinds **locally over Bluetooth** — no cloud account, no vendor
server, no internet connection. Once installed, it works offline.

> **Not affiliated with, endorsed by, or supported by Hunter Douglas.**
> "Bliss" and "Hunter Douglas" are trademarks of their respective owner. This is
> a community project.

> **Please note:** this is a hobby project provided as-is. I take no
> responsibility or liability for any damage. Blinds driven by this integration
> can crash, jam, get stuck, or end up in a defective or damaged state, and
> anything that happens to your blinds or your home is entirely at your own
> risk. Test carefully before trusting it with blinds you care about.

If you appreciate this project and want to support its continuation:

<a href="https://ko-fi.com/mistermej">
  <img src="https://storage.ko-fi.com/cdn/logomarkLogo.png" height="32" alt="Ko-fi" />
</a>

---

## Compatibility

**Tested on the `HD3800` motor.**

That is the motor this integration was verified against on real hardware.

Other motor types are implemented from the same reverse engineering, but they
have not been tested — if you have one of those, testing it needs someone with
that physical blind. Pull requests for other motor types are welcome.

---

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
- **Five diagnostic sensors per blind** — battery, health, connection, signal
  strength and which proxy is serving it.
- **Move verification** — every move is read back from the motor, and a move that
  did not land is reported rather than assumed to have worked.
- **Readable logs** — moves are logged in words at the default level; the
  protocol detail is one level down.

---

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS (category: *Integration*)
2. Install **Bliss Smartblinds**
3. Restart Home Assistant

### Manual

Copy `custom_components/bliss_smartblinds` into your Home Assistant `config`
directory, so you end up with `config/custom_components/bliss_smartblinds`, then
restart.

---

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

---

## Usage

You get two covers per blind, for example:

| Entity | Controls |
|---|---|
| `cover.living_room_top_rail` | top rail, 0–100% |
| `cover.living_room_bottom_rail` | bottom rail, 0–100% |

Each rail is controlled independently, like the vendor app. The other rail stays
wherever it is when you move one of them.

### Sensors

Each blind also gets five diagnostic sensors:

| Entity | States |
|---|---|
| Battery | `Good`, `Low`, `Empty` |
| Health | `OK`, or the last thing that went wrong |
| Connection | `Connected`, `Not Connected` |
| Signal strength | signal in dBm, as the proxy measured it |
| Proxy | which proxy last reached the blind, by name |

**Health** reads `OK` unless something has actually gone wrong. Its other states
(`Collision`, `Move failed`, `No motor feedback`, `Cannot find blind`,
`Position mismatch`) describe a real failure, and it returns to `OK` on the next
clean move. Reachability is deliberately *not* a health state — that is what
**Connection** is for.

**Signal strength** is the reading from the proxy that reached the blind, which
is the only figure that means anything: these motors are weak BLE devices, and a
local adapter usually never hears them. It reads `unknown` until the blind has
been connected at least once.

**Proxy** resolves the proxy's address to the name you gave it in Home Assistant,
so you see `BTP Zolder` rather than a MAC. The address stays available as the
`address` attribute. It reads `unknown` until the blind has been reached once.

**Battery** is reported by the motor itself, not measured. The mapping is
`Good` / `Low` / `Empty` from a single status bit — so treat it as a warning
light, not a fuel gauge. `Good` has been confirmed on real hardware; `Low` and
`Empty` are decoded from the same bit and are **not yet verified**, because a
flat battery is hard to come by. If you see one of them, please
[open an issue](https://github.com/mistermej/Bliss-Smartblinds/issues) — that
would confirm it, and an incorrect `Good` on a dying battery is worth catching.

None of these poll. They update when the blind is next heard from, which is after
every command, so a sleepy motor is never woken just to redraw a dashboard.

### Diagnostics

**Settings → Devices & Services → Bliss Smartblinds → ⋮ → Download diagnostics**
gives a snapshot of each blind: cached positions, battery, health, signal
strength and proxy. It is read-only and safe to attach to a bug report.

There are deliberately no services for sending raw commands. `send_raw` can drive
the motor with arbitrary frames, so it is not shipped — that is a decision, not
an oversight.

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

---

## Licence

MIT. See [LICENSE](LICENSE).
