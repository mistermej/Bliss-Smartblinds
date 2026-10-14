## v{{VERSION}} - {{SUMMARY}}

Keep-a-Changelog shaped. One line saying what a user would notice, then the
facts. Lead with what changed for them, not with what changed in the code.

### Compatible with

- **HD3800** - tested on hardware

### Added

What is new. If there is a new entity, action or sensor, name it exactly as it
appears in the UI, so the note can be searched.

### Fixed

What was wrong before, and what it looked like from the outside. "Sync silently
skipped every blind behind a sleeping one" is useful; "fixed a loop bug in
`_async_sync_one`" is not.

Mention the cause where it explains the symptom. A bug that sends someone
looking in the wrong place is worth naming.

### Known limitations

Honest edges, carried forward until they are resolved or verified. Two kinds
worth keeping here:

- Decoded but **never seen on hardware** - say so explicitly, and say what is
  confirmed. A state nobody has observed is not a working state.
- Behaviour that is **correct but surprising** - a blind accepts only one
  Bluetooth connection; discovery names every blind after its model.

Anything here should already be in the README too. If it is only here, add it
there.

### Testing

One line on what was verified and how, if it is not obvious. Say what could
**not** be verified - "discovery ships untested" is information a user needs.