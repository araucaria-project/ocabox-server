# Changelog
All notable changes to this project will be documented in this file.

## [Unreleased]

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
