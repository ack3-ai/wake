# Built-in tools

The `wake mcp` server exposes the following tools. Every tool operates on the statically compiled project and returns human-readable text. Optional parameters are marked with `?`.

!!! tip
    An MCP client's tool listing always reflects the live set of tools and their input schemas, including any [custom tools](writing-tools.md) contributed by plugins.

## Project overview

| Tool                        | Description                                                                 | Parameters                                                                             |
|-----------------------------|-----------------------------------------------------------------------------|---------------------------------------------------------------------------------------|
| `list_contracts`            | List all contracts in the project.                                          | `paths?`, `kind_filter?`                                                               |
| `list_contract_functions`   | List the functions of a contract, resolved across inheritance.             | `contract_name`, `file_path?`, `mutability_filter?`, `visibility_filter?`             |
| `list_modifiers`            | List modifiers and the functions that use them.                            | `contract_name?`                                                                       |
| `get_c3_linearization`      | C3 linearization (method resolution order) of a contract.                  | `contract_name`, `file_path?`                                                          |

## Source code

| Tool                     | Description                                                                              | Parameters                                                                                   |
|--------------------------|-----------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| `get_contract_source`    | Source code of a contract.                                                               | `contract_name`, `file_path?`, `number_lines?`                                                |
| `get_function_source`    | Source code of a function.                                                               | `contract_name`, `function_name`, `file_path?`, `search_in_base_contracts?`, `number_lines?` |
| `get_definition_source`  | Source code of any definition (contract, struct, enum, error, event, …).                | `definition_name`, `file_path?`, `number_lines?`                                              |

## Navigation & references

| Tool                | Description                                                                 | Parameters                                    |
|---------------------|-----------------------------------------------------------------------------|-----------------------------------------------|
| `go_to_definition`  | Definition(s) of an identifier at a given location.                        | `file_path`, `line`, `expression`             |
| `find_references`   | References to an identifier (by name, or at a given location).             | `expression`, `file_path?`, `line?`, `where?` |
| `get_expression_type` | Resolved type of an expression at a given location, with referenced user-defined types. | `file_path`, `line`, `expression`             |

## Storage & state

| Tool                       | Description                                                                          | Parameters                                                     |
|----------------------------|-------------------------------------------------------------------------------------|---------------------------------------------------------------|
| `analyze_state_variables`  | State variables of a contract, with storage slots and immutables.                   | `contract_name`, `file_path?`                                 |
| `get_storage_layout`       | Storage slot/offset layout of a contract.                                           | `contract_name`, `file_path?`                                 |
| `get_state_changes`        | State changes performed by a function or modifier (optionally including callees).   | `declaration_name`, `file_path`, `include_called_functions`   |

## Selectors & calls

| Tool                               | Description                                                        | Parameters   |
|------------------------------------|-------------------------------------------------------------------|--------------|
| `find_functions_by_selector`       | Functions matching a 4-byte function selector.                    | `selector`   |
| `find_functions_by_regex`          | Functions whose name matches a regular expression.               | `pattern`, `file_path?`, `contract_name?` |
| `find_external_calls_by_selector`  | External call sites for a given function selector.               | `selector`   |

## Utility

| Tool                | Description                                                                     | Parameters                    |
|---------------------|--------------------------------------------------------------------------------|-------------------------------|
| `is_known_contract` | Whether a contract matches a known, published contract (by code checksum).     | `contract_name`, `file_path?` |
| `echo`              | Echo back a message. Useful as a connectivity check before compilation finishes. | `message`                     |

!!! note "Locating declarations"
    Tools that take a `file_path` accept it as optional whenever the name is unambiguous across the project. When a name is ambiguous, the tool reports the candidates so the call can be narrowed with `file_path`.
