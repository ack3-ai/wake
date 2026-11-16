---
title: Development and testing framework
---

Wake development and testing framework is a Python-based framework for testing and deploying Solidity smart contracts based on [pytest](https://docs.pytest.org/en/latest/).
It utilizes `pytypes`, Python equivalents of Solidity types, to simplify writing scripts and easily discover typing errors.

## Features

- an in-process EVM based on [revm](https://github.com/bluealloy/revm), built into Wake, so there is no development chain to install or launch
- auto-completions when writing tests and deployment scripts thanks to `pytypes`
- type checking for all types generated in `pytypes`
- manually guided fuzzing with flows and invariants, leveraging multiprocessing to maximize the amount of inputs tested
- forking of live chains and cross-chain testing support
- integrated Python debugger ([ipdb](https://github.com/gotcha/ipdb)) attached on test failures
- call traces and `console.log` support for easier debugging
- deployment & mainnet interaction scripts
- [better performance](https://github.com/ack3-ai/python-testing-frameworks-benchmark) than other Python or JavaScript frameworks

## Testing engines

Tests run on the in-process `revm` engine by default.
[Anvil](https://github.com/foundry-rs/foundry/tree/master/crates/anvil) and [Hardhat](https://github.com/NomicFoundation/hardhat) development chains can be used instead over JSON-RPC, for example to connect to an already running node; Hardhat support is deprecated.
See [Anvil and Hardhat](./anvil-and-hardhat.md) for how to switch engines and what differs.
