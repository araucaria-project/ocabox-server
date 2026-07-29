# Changelog
All notable changes to this project will be documented in this file.

## [Unreleased]

## [2.3.16]
### Added
- Device-reported faults now surface as `TreeOtherError(code=4009, NORMAL)`
  ("device reported an error") instead of `TreeValueError(2002)`. This
  separates *the device/driver faulted* (TIC worked, relaying the device's
  error) from *TIC failed to build the value*. The driver's numeric code is
  carried in `device_errno` (kwargs), so clients read e.g. ASCOM `1035`
  ("Telescope is not ready, please clear Error") without parsing the message
  string or reading server logs.
  - `AlpacaConnector.raise_tree_exeption`: numeric `AlpacaError` (other than
    device-busy `20072`) → `4009` with `device_errno=error_number`. Warning
    log now includes the errno and message.
  - `IrisCcdConnector`: device-replied `RuntimeError` on `get`/`put`/`call`
    → `4009` (was `2002`).
  - Pilar's device-reply path is **not** converted yet (its `get` re-raises
    raw `RuntimeError`, `put` returns a `{"status": "failed"}` dict) — tracked
    as a follow-up; see `doc/errors.md` "Device-reported errors".
### Dependencies
- Requires ocabox-common ≥ `1.1.2`, which registers the 4009 code description
  (dependency tracks git `master`; 4009 still functions without it — the
  description lookup just degrades to empty).

## [2.3.15]
### Fixed
- `IrisCcdConnector` no longer swallows transient TCP failures (`ConnectionError`, `BrokenPipeError`, `OSError`, `asyncio.TimeoutError`, `TimeoutError`) and returns `None` / `{"status": "failed"}`. The swallow caused cycle-query subscribers to escalate to `TreeValueError(2003, CRITICAL)` after retries, terminating PMS subscriptions permanently on transient device outages with no auto-recovery (issue #20). Transient IO now raises `TreeOtherError(4005, NORMAL)`; device-replied errors (`RuntimeError`) raise `TreeValueError(2002, NORMAL)`. Applied symmetrically to `get`, `put`, and `call`.
### Changed
- `PilarConnector` aligns with the new convention: `_TEMPORARY_IO_ERRORS` raise `TreeOtherError(4005, NORMAL)` instead of `TEMPORARY`. NORMAL surfaces sustained device-offline state to the operator (throttled logging via `ErrorPolicy.SERVICE`); TEMPORARY would silently retry inside the cycle-query layer for arbitrarily long outages. Single-poll blips are still absorbed by the pool's self-heal logic below the raise site.
- `doc/errors.md` — added "TEMPORARY vs NORMAL — blip vs sustained" subsection clarifying the convention. Updated the per-connector contract paragraph and the example table.
- `tree_conditional_freezer.py` — comment at the `2003` raise documenting that `severity=None` resolves to `NORMAL` via the `ResponseError` constructor (the freezer's fallback when no connector error supplied a severity).

## [2.3.13]
### Added
- `AlpacaConnector` translates Andor `DRV_ACQUIRING (20072)` to `TreeOtherError(code=4008, severity=TEMPORARY)` so clients can react to "device busy" without string-matching the wrapped error message. `AlpacaError` now retains `error_number` as an attribute.
### Dependencies
- ocabox-common bumped to `1.0.3` (registers code 4008 description).

## [2.3.12]
### Fixed
- Strip cyclic-query bookkeeping (`time_of_known_change`, `no_send_before`, `nr_of_unsuccessful_refreshes`) before forwarding requests to protocol connectors. The leak caused jk15-tcu's strict ASCOM driver to reject GETs with HTTP 400, freezing the cache at the last successful value (e.g. `camera.state` stuck at `EXPOSING` for hours).

## [2.3.10]
### Added
- Safety cutoff switch in `TreeBaseRequestBlocker` for dome entry protection
- Configurable list of dangerous commands blocked when cutoff is engaged (slew, dome movement, mirror covers, tracking, motor control)
- New commands in `TreeBlockerAccessGrantor`: `engage_safety_cutoff`, `disengage_safety_cutoff`, `safety_cutoff_state`
- Dedicated bypass parameter `request_safety_cutoff_bypass_param` for manual control devices operated inside the dome
- Distinct error code 1005 for safety cutoff blocks (separate from access denied 1004)
- Default `safety_cutoff_list` in configuration with 17 blocked commands

## [2.2.0]
### Fixed
- Ensure `_on_subcontractor_return` is always called in `TreeBaseProvider`, preventing stale CyclicQuery cache
- Guard `_on_subcontractor_return` against its own exceptions so they do not replace the computed response
### Changed
- Directory structure: `comunication` → `communication`, `data_colection` → `data_collection`, `specialistic` → `specialized`
- Reorganized tree components, telescope devices, protocols, and utils into clean purpose-driven directories

## [2.1.1]
### Changed
- Dependencies version bump
- Restored proper error handling

## [2.1.0]
### Added
- Pilar protocol connector and configuration
- IRIS CCD protocol connector and configuration
- BESO spectrograph protocol connector
- Dummy protocol connector for testing
- Universal connector factory for multi-protocol support

## [2.0.0]
### Changed
- Refactor to be non-alpaca dependend
- directory structure refactored
### Added
- Support for non-alpaca components: Pilar, BESO, IRIS, etc. 
### Removed
- Resource manager
- Program runner
 

## [1.0.4]
### Changed
- Python 3.10 required
- Dependencies cleanup
- Default config and tree_build updated for development config

## [1.0.3]
### Added
- add `Tertiary` component to alpaca driver as new kind

## [1.0.2]
### Changed
- The application has been adapted to the new requirements in version 1.0.1 of `ocabox-common`.

## [1.0.1]
### Added
- Add new service request `reload_config`. This request reload configuration files and send it to NATS.

## [1.0.0]
### Added
- Project core files added and initialized.
- The first version of the project after separating the server part from the [ocabox](https://github.com/araucaria-project/ocabox) project. 
The change history before the split can be found in the ocabox project change history to version 1.0.17 .



[Unreleased]: https://github.com/araucaria-project/ocabox-server

[1.0.3]: https://github.com/araucaria-project/ocabox-server
