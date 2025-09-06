# Changelog
All notable changes to this project will be documented in this file.

## [Unreleased]

## [2.0.0]
### Changed
- Refactor to be non-alpaca dependend
- directory structure refactored
### Added
- Support for non-alpaca components: Pillar, BESO, IRIS, etc. 
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