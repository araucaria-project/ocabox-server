# ocabox-server

The aim of the project was to create a single API to support many telemetry tools and systems used in astronomical
research. Initially, the project was to mediate in the exchange of data between users and the local server managing
the astronomical telescope. At the same time, by using caching in the program, it was supposed to relieve the server
managing the telescope.

## Installation

### Install with poetry

First, make sure you have poetry installed. For this, use the command:
```bash
poetry --version
```
If the poetry version is displayed, you can proceed to the next step. Otherwise, install poetry first.
In the next step, download and go to the main directory of the project:
```bash
  git clone https://github.com/araucaria-project/ocabox.git
  cd ocabox/
```
Next install requirements
```bash
  poetry install
```

## Usage/Examples

If you installed the project correctly, you can run one of the available scripts. To do this, execute the command:
```bash
  poetry run <script_name> <params>
```
available scripts

| Script name       | Description                                                                                      | Params                              |
|-------------------|--------------------------------------------------------------------------------------------------|-------------------------------------|
| tests             | Run all unit test. Read [Running Tests](#running-tests) for mor <br/>information                 |                                     |
| server            | Run server from build file. Read [configuration](#configuration) <br/>section to mor information |                                     |


## Configuration

The default configuration is stored in the `obsrv/config.yaml` file. It can be overwritten by other configuration files in 
the `obsrv/configuration/` directory. There are also scripts that build the application in this directory. By default, 
the `obsrv/configuration/config.yaml` and `/etc/ocabox/config.yaml` files (if exist) is imported and overwrites the default configuration. Files with different 
names must be explicitly added in the build script or client application. It is recommended to keep the configuration 
in the `obsrv/configuration/config.yaml` file, the file must be created during the first installation.

The configuration file is divided into sections. They can be overwritten in full or in parts.

### Server

#### router
In the `router` section, network socket parameters and general requests parameters are configured.

For example, to configure a server that has a router class called `MySampleRouterName` on the front, 
the configuration should look like this:
```yaml
router:
  MySampleRouterName:
    port: 5559
    url: *
    protocol: tcp
    timeout: 30
```
* `port` - port where requests will be received
* `url` - url address
* `protocol` - used protocol
* `timeout` - timeout after which the query expires and is canceled

#### data_collection
In the `data_collector` section, the default parameters of individual module types in the tree are set.
Please note that in this section there are only default values for modules, and they can be overwritten individually 
for each block in the [tree](#tree) section.

For example, module `TreeAlpacaObservatory` configuration will look like this:

```yaml
data_collection:
  TreeAlpacaObservatory:
    address: localhost:80
    api_version: 1
    url_protocol: http
```
* `address` - address alpaca server
* `api_version` - alpaca api version
* `url_protocol` - url protocol

#### tree
In this section, you configure each block of the tree individually. Block names in the tree cannot be duplicated, 
so the configurations in this section are unique for each block.

For example, given a block named `test_observatory` of type `TreeAlpacaObservatory`, its configuration would look like this:

```yaml
tree:
  test_observatory:
    address: 192.168.2.1:80
    observatory:
      comment: Simulated observatory for tests
      components:
        dome:
          kind: dome
          device_number: 0
```
* `address` - address alpaca server. this address will override the default address given in section `data_collection` 
for `TreeAlpacaObservatory`
* `observatory` - This parameter is a dictionary containing information about the construction of the alpaca server. 
More about this in the `TreeAlpacaObservatory` class documentation.


#### OCABOX_BUILD_FILE_NAME
This parameter indicates the name of the tree building script. The script should be in directory `obsrv/configuration/` or `/etc/ocabox/`.
There should be a method `tree_build()` in the script that returns a router object with a complete tree. An example 
script is in the file `obsrv/configuration/tree_build_example.py`. It is possible to provide an absolute path to the file, 
in which case the file can be located anywhere.

## Running Tests

If you installed the project correctly, you can run all tests by execute the command:
```bash
  poetry run tests
```
Warning ! Some tests require alpaca simulator running or/and NATS server.

## Run Locally

To run the sample server, you must successfully install the project. Then make sure that the example builder script is set in the configuration:
```yaml
OCABOX_BUILD_FILE_NAME: "tree_build_example.py"
```
then just run the command:
```bash
  poetry run server
```
In case of problems, it is recommended to remove all overwritten configurations and run the server with default one.

### Alpaca simulator

A working alpaca server is required for the full operation of the example programs and some tests. For this, it 
is recommended to use the alpaca simulator available [**here**](https://github.com/ASCOMInitiative/ASCOM.Alpaca.Simulators).

### NATS
A working NATS server is required for the full operation of the example programs and some tests.

## Construction and how it works

In general, the server consists of 2 parts, **router** and **tree**. The router is the front block of the application 
and is responsible for network communication, receiving and sending messages. The server works on the request-response 
principle, which means that it can generate only one response per request. The router block consists of 
the `Router` and `RequestSolver` classes. This second class has been separated from the main `Router` class 
to be able to change the communication protocol in the future. In this class, you create tasks (in asynchronous 
code, think of a task as a thread) and encode/decode requests/response messages into their `ValueResponse` and 
`ValueRequest` object representation.

The second part of the server is **tree**. It is a tree of interconnected modules responsible for processing requests 
and generating responses. The creation of the tree is done in the build file. More described in 
section [OCABOX_BUILD_FILE_NAME](#OCABOX_BUILD_FILE_NAME) and examples directory.

There are many modules, please refer to the documentation of each module before using it, as some of them 
have special requirements for adjacency with other modules. Each module has a unique id name. With its help, it is 
possible to configure it individually. If it is missing, it will be taken from the values set for the given module type. 
Some modules have an address that is used in requests to redirect them to the right place. For this reason, the very 
construction of the tree depends on how the queries sent by clients will look like.

## License
The [MIT](LICENSE) License
