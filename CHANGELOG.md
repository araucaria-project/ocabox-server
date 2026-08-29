# Changelog
All notable changes to this project will be documented in this file.

## [Unreleased]
### Added
- **Deadline shedding** (rollout flag `TreeAlpacaObservatory.shed_expired_requests`,
  default off): a request whose absolute deadline already passed while queued is
  refused with `4004 TEMPORARY` *before* any device I/O. Previously an expired
  request still spawned the Alpaca HTTP coroutine only to cancel it.
### Changed
- Router drop paths (request expired on arrival; solve timeout) now log a
  throttled summary (first + every 100th, WARNING) with counters instead of a
  per-message ERROR — a client avalanche can no longer flood the journal.
...

## [2.4.0]
### Added
- **Mirror cell (M1 / active collimation) component** — kinds `mirrorcell`
  (interface contract; every method raises 3002 CRITICAL) and `mirrorcellACC`
  (ASA: vendor actions on the ACC focuser, routed with `kind=focuser` like
  `CoverCalibratorOCA`). GET-able (cacheable/subscribable) reads: `available`,
  `mirrorcellstatus` (raw dict), `positions` (metres), `motorstatuses`,
  `motorstatustexts`, `atsetpoint`, `moving`, plus per-motor scalars
  `motor<N>{position,status,statustext}` (`N` = 0…2) for one-value-per-subject
  telemetry. Commands: `moveallmotorsto`, `moveallmotorsoffset`,
  `moveonemotoroffset`, `stoponemotor`, `stopallmotors` — **unit-tested only,
  never commanded on hardware** (#31); reads verified live on jk15/zb08
  (wk06 has no mirror cell). Details in the `MirrorCell`/`MirrorCellACC`
  docstrings.
- **Error model** follows `Tertiary`, with a value/status split: motor fault →
  `4009 NORMAL` with `device_errno` on value reads (per-motor position faults
  only on its own motor), while status reads stay readable — the status enum is
  the fault channel; `HbridgeOpen` is normal idle, not a fault. Subsystem
  absent → `2002 CRITICAL` (permanent); malformed vendor replies → `2002
  NORMAL`; command parameters strictly validated (integer index, finite
  values) → `4007` before anything is sent; non-`ok` acknowledgement → `4009`.

  Config sketch (`ocabox-config-ocm`, as a child of the telescope):
  ```yaml
  mirrorcell:
    kind: mirrorcellACC
    device_number: 0        # the ACC focuser that carries the actions
  ```

## [2.3.18]
### Added
- **Tertiary (M3) read-back** — `TertiaryOCA` now exposes GET-able (hence
  cacheable/subscribable) attributes, all served by the single ASA AutoSlew
  vendor action `tertiarystatus` on the mount's ALPACA device:
  `tertiarystatus` (full dict) and its decomposed fields `nasmythport`,
  `angle`, `moving`, `motoron`, `portname`. Port numbers are the physical
  AutoSlew ports (jk15: 1=ADR6/beso, 2=ADR10/andor; verified live on jk15-tcu
  2026-07-29) — NOT 0-based.
- **Controller faults use the standard error model**: AutoSlew `ErrorRaised`
  is translated to `TreeOtherError(4009, NORMAL)` ("device reported an
  error") on every decomposed read, and a non-`true` acknowledgement of
  `selectnasmythport` raises `4009` as well — clients see a standard
  `OcaboxDeviceError` instead of polling a vendor-specific boolean.
  `tertiarystatus` always returns the raw dict (incl. `ErrorRaised`) as the
  diagnostic view of a faulted controller.
- Base `Tertiary` is now an explicit interface contract: every method raises
  `TreeStructureError(3002, CRITICAL)`, so a tree configured with the plain
  `tertiary` kind fails loudly instead of falling through to a nonexistent
  ALPACA endpoint.
### Changed
- `TertiaryOCA.selectnasmythport_put` validates its `Position` parameter
  (int or int-like string) and raises `TreeOtherError(4007, NORMAL)` instead
  of silently sending an empty-parameter movement action.
### Dependencies
- Requires ocabox-common ≥ `1.2.2` — fixes `TreeStructureError` dropping the
  `severity` argument (every 3002 was silently demoted to NORMAL, making
  SERVICE-policy clients retry forever against not-implemented endpoints).

## [2.3.17]
### Fixed
- `AlpacaConnector` now uses a single long-lived `aiohttp.ClientSession` per connector (created lazily on first request) instead of opening a fresh session + TCP connection per request. The old behaviour issued a fresh `getaddrinfo` on every poll (~150 DNS queries/s in production, all cache-missing), so a brief upstream-DNS hiccup saturated the resolver thread-pool with uncancellable lookups and the process never recovered without a restart (Phenomenon A — "loses all ALPACA hosts ~hourly, curl still works"). Keep-alive + a connector-level DNS cache (`ttl_dns_cache=30s`) collapse that storm.
- `IrisCcdConnector` no longer leaks a UDP socket per timed-out command. `_execute_command` dropped the cached endpoint with `del self._endpoints[address]` on timeout/reconnect without closing the transport, orphaning the socket FD; with the device unreachable this leaked ~4 FDs/min until `RLIMIT_NOFILE` (1024) was exhausted. The leak was previously masked by the hourly Phenomenon-A restarts and surfaced once the ALPACA fix above let the process run stably. New `_drop_endpoint()` helper closes the transport before dropping it.
### Changed
- `AlpacaConnector` session uses `ClientTimeout(total=10s, connect=5s, sock_connect=5s)` (aiohttp default is 5 min) and `TCPConnector(limit_per_host=8)`. The per-host cap also keeps the ASCOM driver queue shallow, mitigating the `code=1026` filterwheel queue-depth timeouts (Phenomenon B).
### Dependencies
- Added `aiodns` so aiohttp's `DefaultResolver` is the async (c-ares) resolver, which runs DNS on the event loop instead of the blocking `getaddrinfo` thread-pool.
> Note: this work ran on production `tic` since 2026-06-25 as version "2.3.16" from branch `fix/alpaca-dns-resolver-wedge`; it was renumbered to 2.3.17 on merge because master's 2.3.16 was taken by the 4009 release below.

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
- Requires ocabox-common ≥ `1.2.1`, which registers the 4009 code description
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
