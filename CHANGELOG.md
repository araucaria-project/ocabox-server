# Changelog
All notable changes to this project will be documented in this file.

## [Unreleased]

## [2.6.1]
### Fixed
- Freezer: reply margin (`alarm_timeout`) adapts to the client's request
  window — `min(configured, max(0.1, 0.2*window))`; windows shorter than the
  fixed margin no longer 4004-livelock (tight-T2 clients get served) (#44)
...

## [2.6.0]
### Added
- **Staleness Contract, phase 2**: freezer honors `value_policy` +
  `time_of_data_max_age` (T2, default `2*tolerance`) — healthy cadence stays
  on T1, failures masked and retried inside the (T1, T2] repair window, rich
  `Value(None, tags={reason, last_good, last_good_ts})` past T2 once per
  episode, counter retired for declared requests; TreeCache treats recovery
  after a failure episode as a value change. Undeclared requests unchanged.
  (ocabox-common#8, #40, resolves #28)

## [2.5.2]
### Added
- `value_policy` reserved in the cyclic-query strip-list (Staleness Contract
  phase 0, #39) — future clients cannot leak it to ASCOM drivers.

## [2.5.1]
### Fixed
- **astropy 8 compatibility**: `tree_ephemeris` used `get_moon` (removed in astropy 7);
  replaced with `get_body("moon", ...)`, compatible with astropy 5–8.

## [2.5.0]
### Added
- **Deadline shedding** (flag `TreeAlpacaObservatory.shed_expired_requests`, default
  off): requests already past their deadline are refused with `4004 TEMPORARY`
  before any device I/O.
- **Negative caching in `TreeCache`** (flag `TreeCache.negative_cache.*`, default
  off): failures (codes 2002/2003/4002/4005/4009) are served fail-fast per address
  with escalating TTL (1 s → 10 s, monotonic), one real probe per TTL; cache-served
  errors carry `from_negative_cache` and are not counted by the freezer toward 2003.
### Changed
- `TreeCache._known_values`: list → dict keyed by `str(address)`.
- Router drop paths (expired on arrival, solve timeout): throttled WARNING with
  counters instead of per-message ERROR.

## [2.4.0]
### Added
- **Mirror cell (M1) component**: kinds `mirrorcell` (interface contract, 3002) and
  `mirrorcellACC` (ASA, via the ACC focuser) — status/position/per-motor reads,
  movement and stop commands (never commanded on hardware yet, #31).
- Mirror-cell error model: motor fault → `4009` + `device_errno`; subsystem absent →
  `2002 CRITICAL`; invalid parameters → `4007`; non-`ok` acknowledgement → `4009`.

## [2.3.18]
### Added
- **Tertiary (M3) read-back**: GET-able `tertiarystatus` plus decomposed
  `nasmythport`, `angle`, `moving`, `motoron`, `portname` (physical AutoSlew ports).
- AutoSlew `ErrorRaised` and failed `selectnasmythport` acknowledgement → `4009`.
- Base `Tertiary` is an interface contract (every method → `3002 CRITICAL`).
### Changed
- `selectnasmythport_put` validates `Position` (→ `4007` on bad input).
### Dependencies
- ocabox-common ≥ 1.2.2 (`TreeStructureError` no longer drops `severity`).

## [2.3.17]
### Fixed
- `AlpacaConnector`: single long-lived aiohttp session per connector + DNS cache
  (was: new session and `getaddrinfo` per request — the hourly "loses all ALPACA
  hosts" wedge).
- `IrisCcdConnector`: UDP socket leak on timed-out commands (FD exhaustion).
### Changed
- Alpaca session: `ClientTimeout(total=10 s, connect=5 s)`, `limit_per_host=8`.
### Dependencies
- `aiodns` (async c-ares resolver on the event loop).

(Ran on production tic as "2.3.16" since 2026-06-25; renumbered on merge.)

## [2.3.16]
### Added
- Device-reported faults → `TreeOtherError(4009, NORMAL)` with `device_errno`
  (Alpaca numeric errors except busy `20072`; IRIS-CCD `RuntimeError`). Pilar not
  converted yet.
### Dependencies
- ocabox-common ≥ 1.2.1 (registers 4009).

## [2.3.15]
### Fixed
- `IrisCcdConnector` no longer swallows transient TCP failures (#20): transient IO →
  `4005 NORMAL`, device-replied errors → `2002 NORMAL`.
### Changed
- `PilarConnector`: `_TEMPORARY_IO_ERRORS` → `4005 NORMAL` (was TEMPORARY).
- `doc/errors.md`: "TEMPORARY vs NORMAL" subsection.

## [2.3.13]
### Added
- Andor `DRV_ACQUIRING (20072)` → `4008 TEMPORARY` ("device busy");
  `AlpacaError.error_number` retained.
### Dependencies
- ocabox-common 1.0.3 (registers 4008).

## [2.3.12]
### Fixed
- Cyclic-query bookkeeping fields stripped before forwarding to connectors
  (the leak froze caches via HTTP 400 on strict ASCOM drivers).

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
