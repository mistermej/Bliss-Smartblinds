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
- **Position persistence** - the last known positions survive a restart.
- **Sync on demand** - a button per blind re-reads the real position from the
  motor, or call the `bliss_smartblinds.sync` action.
- **Auto-discovery** - a blind advertising over Bluetooth is offered for setup
  without typing its MAC address, and nothing is added until you confirm it.
- **Per-blind settings** — Maintenance Mode, connection timeout and motor
  speed, on each blind's Configure page.
- **Self-healing state** - the position is read automatically when a blind is
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

Or let Home Assistant find it: a blind that is advertising is offered for setup
automatically. The card shows the model and the MAC address, and nothing is
added until you confirm. It arrives named `Blind D6C5` (from the address, since
every blind of a given model broadcasts the same model name) — rename it in
Home Assistant's own Name and area dialog, or afterwards on the device page.

Any Hunter Douglas model is matched — the matcher is not tied to a model number.

You need a working Bluetooth integration, plus either a local Bluetooth adapter
or one or more
[ESPHome Bluetooth proxies](https://esphome.io/projects/?type=bluetooth) —
these motors are weak BLE devices and usually need a proxy in the same room.

The blind must already be set up and claimed in the Hunter Douglas app. It
accepts only one Bluetooth connection at a time, so close the vendor app before
controlling a blind from Home Assistant — and do not set the same blind up in a
second integration, because the two will take the link from each other.

---

## Usage

You get two covers per blind, for example:

| Entity | Controls |
|---|---|
| `cover.living_room_top_rail` | top rail, 0–100% |
| `cover.living_room_bottom_rail` | bottom rail, 0–100% |
| `button.living_room_sync` | re-read both rails from the motor |

Each rail is controlled independently, like the vendor app. The other rail stays
wherever it is when you move one of them.

### Settings

Each blind has three settings on its Configure page:

| Setting | What it does |
|---|---|
| **Maintenance Mode** | Makes the blind read-only. Position, battery and health keep updating, but it cannot be moved. Intended for while a blind is being physically worked on. |
| **Timeout** | How long to keep trying to reach the motor before giving up. These motors sleep between commands, so a move can take that long when a blind is asleep. |
| **Speed** | `Slow`, `Comfort` or `Standard`, the three speeds the vendor app offers. Sent once, when it changes. |

### Presets

Also under Configure: **position presets** — a saved top rail and bottom rail,
named after what they are for. The first screen is a menu, so choosing
what to do costs one tap rather than an **Action** dropdown plus a submit.

- **Add a preset** takes a name and both rail positions — one screen,
  one submit, with no preset dropdown anywhere on it.
- **Change or delete a preset** is one screen holding both jobs: the preset to
  act on, the fields, and a box to tick to remove that preset instead of
  saving. One submit either way, and anything left blank keeps its saved value,
  so changing only a position does not mean retyping the other one.

Every saved preset, with its positions, is listed at the top of that
screen before anything is chosen. A form cannot show a dropdown's choice in its
own boxes — it is drawn before the choice exists — so the values live in the
form's description instead of costing a submit to load them, and all of them
are readable at once rather than one at a time. Changing the dropdown after a
mistake brings up that preset's blanks rather than writing the previous one's
numbers over it.

Removing needs two deliberate actions: the box ticked, then submitted, with the
preset's name in the dropdown above it.

Top has to stay at least 1% above bottom — the same rail guard a manual move
gets, because it is a physical limit of the motor rather than a preference. It
is refused, never silently adjusted.

Deleting the last preset removes the two preset entities with it.

Each blind with presets also gets:

| Entity | What it does |
|---|---|
| **Preset** (select) | Pick a preset to send the blind to its positions. Usable from a dashboard or an automation. |
| **Position** (sensor) | Which preset the blind is at, or `Custom` if it has been moved off one. Derived from the live position, so it follows a move made with the blind's own remote. |

### Syncing

Press the blind's **Sync** button to ask the motor where it actually is. The
position is cached, so a blind that has been moved by its remote — or by
somebody else — shows the old value until you ask. Sync reads it and updates
every entity for that blind. The **Health** sensor reads `Syncing` and then
`Synced`, and a log line records what came back, battery included:

```
TV: synced - top rail 70.0%, bottom rail 30.1% and battery Low
```

The same thing is available as an action, which returns the parsed rails so you
can see the answer directly in **Developer Tools → Actions**:

```yaml
action: bliss_smartblinds.sync
data:
  address: "AA:BB:CC:DD:EE:FF"   # optional; omit to sync every blind
```

It only ever asks the motor where it is. It cannot move a blind.

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
**Connection** is for. It also shows `Syncing` and `Synced` while you are using
the sync button, falling back to the real state ten seconds later, and
`Maintenance Mode` while the blind has been taken out of use on its Configure
page.

**Signal strength** is the reading from the proxy that reached the blind, which
is the only figure that means anything: these motors are weak BLE devices, and a
local adapter usually never hears them. It reads `unknown` until the blind has
been connected at least once.

**Proxy** resolves the proxy's address to the name you gave it in Home Assistant,
so you see `BTP Zolder` rather than a MAC. The address stays available as the
`address` attribute. It reads `unknown` until the blind has been reached once.

**Battery** is reported by the motor itself, not measured. The mapping is
`Good` / `Low` / `Empty` from a single status bit — so treat it as a warning
light, not a fuel gauge. `Good` and `Low` have both been confirmed on real
hardware; `Empty` is decoded from the same mask and has never been seen, since
a blind that flat tends to get changed before anyone reports it.

The **sync** action reports it too: its log line and its reply both include the
battery, so a blind with a low battery does not sit there reporting perfect
positions and looking healthy.

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
