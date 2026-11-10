![Wake: the fuzzing and testing framework for Solidity, written in Python](https://github.com/ack3-ai/wake/blob/main/images/wake_cover.png?raw=true)

# Wake

Wake is a Python framework for Solidity testing, fuzzing and static analysis. Tests, fuzz harnesses and detectors are ordinary Python; Wake 5 executes them against an EVM embedded in the Python process.

[![PyPI](https://img.shields.io/pypi/v/eth-wake?include_prereleases&label=pypi)](https://pypi.org/project/eth-wake/)
[![Python](https://img.shields.io/pypi/pyversions/eth-wake)](https://pypi.org/project/eth-wake/)
[![License: ISC](https://img.shields.io/badge/license-ISC-blue)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-ack3.ai%2Fwake-0E0F12)](https://ack3.ai/wake/docs/latest/)

Developed by [ack3](https://ack3.ai). Created at Ackee Blockchain in 2022. Open source under the [ISC license](LICENSE).

## Install

Wake 5 is a release candidate (`5.0.0rc4`). It needs Python 3.10 or newer and ships prebuilt wheels for Linux (x86_64, aarch64, i686; glibc and musl), macOS 14+ (Apple Silicon and Intel) and Windows (x64 and ARM64), for CPython 3.10 to 3.15.

```shell
pip3 install --pre eth-wake
```

With [uv](https://docs.astral.sh/uv/):

```shell
uv tool install --prerelease allow eth-wake
```

Wake downloads the Solidity compiler versions a project needs on first use. On Apple Silicon, `solc` runs through Rosetta 2 (`softwareupdate --install-rosetta`). No node software is required: the in-process EVM is the default engine. Anvil or Hardhat are needed only if you opt back into a JSON-RPC chain.

Wake 4 (`4.22.2`) remains available with `pip3 install eth-wake`; its documentation is at [latest](https://ack3.ai/wake/docs/latest/).

## Quick start

```shell
wake up --example counter   # example project in an empty directory, or `wake up` in your own
wake test                   # run the tests (pytest underneath)
wake detect all             # run every vulnerability and code-quality detector
wake mcp                    # expose the static analysis to an AI agent
```

`wake up` writes `wake.toml`, compiles the project and generates `pytypes`, Python types for every contract, so tests get autocompletion and type checking against your Solidity code.

A manually guided fuzz test from the [example project](examples/counter/tests/test_counter_fuzz.py): the auditor writes the flows and the invariant, the engine chooses the sequence.

```python
from wake.testing import *
from wake.testing.fuzzing import *
from pytypes.contracts.Counter import Counter


class CounterTest(FuzzTest):
    def pre_sequence(self) -> None:
        self.owner = random_account()
        self.counter = Counter.deploy(from_=self.owner)
        self.count = 0

    @flow()
    def flow_increment(self) -> None:
        self.counter.increment(from_=random_account())
        self.count += 1

    @flow()
    def flow_decrement(self) -> None:
        with may_revert(Panic(PanicCodeEnum.UNDERFLOW_OVERFLOW)) as e:
            self.counter.decrement(from_=random_account())
        if e.value is not None:
            assert self.count == 0
        else:
            self.count -= 1

    @invariant(period=10)
    def invariant_count(self) -> None:
        assert self.counter.count() == self.count


@chain.connect()
def test_counter_fuzz():
    CounterTest().run(sequences_count=10, flows_count=100)
```

The [getting started guide](https://ack3.ai/wake/docs/5.0.0rc3/testing-framework/getting-started/) walks through the same project step by step.

## What Wake does

- **Testing framework** on top of [pytest](https://docs.pytest.org), with generated `pytypes`, call traces, console logs, coverage and a debugger. [Docs](https://ack3.ai/wake/docs/5.0.0rc3/testing-framework/overview/)
- **Manually guided fuzzing**: flows and invariants written in Python, random sequencing by the engine, a two-phase shrinker that reduces a failing sequence to the transactions that matter. [Docs](https://ack3.ai/wake/docs/5.0.0rc3/testing-framework/fuzzing/)
- **In-process EVM** ([revm](https://github.com/bluealloy/revm)): no development node to install or launch, forking of live chains through `chain.connect(fork=...)`, cross-chain tests with several chains in one process. [Migration from Wake 4](https://ack3.ai/wake/docs/5.0.0rc3/testing-framework/migrating-from-wake-4/)
- **Vulnerability and code-quality detectors** (`wake detect`) with SARIF export, and **printers** (`wake print`) that extract control-flow graphs, inheritance, storage layouts, selectors and more. Both are plugin APIs for your own project-specific rules. [Detectors](https://ack3.ai/wake/docs/5.0.0rc3/static-analysis/using-detectors/) · [Printers](https://ack3.ai/wake/docs/5.0.0rc3/static-analysis/using-printers/) · [Writing your own](https://ack3.ai/wake/docs/5.0.0rc3/static-analysis/getting-started/)
- **MCP server** (`wake mcp`): the same static facts, served to language-model agents. [Docs](https://ack3.ai/wake/docs/5.0.0rc3/mcp/using-mcp/)
- **Language server** (`wake lsp`) and the [Solidity (Wake)](https://marketplace.visualstudio.com/items?itemName=AckeeBlockchain.tools-for-solidity) VS Code extension: diagnostics, references, graphs and detections in the editor. [Docs](https://ack3.ai/wake/docs/5.0.0rc3/language-server/)
- **Deployment scripts and mainnet interaction** with the same `pytypes` the tests use. [Docs](https://ack3.ai/wake/docs/5.0.0rc3/testing-framework/deployment/)
- **Solc version manager** (`wake svm`), Solidity support through 0.8.38, and GitHub Actions for [setting up Wake](https://github.com/marketplace/actions/wake-setup) and [running detectors](https://github.com/marketplace/actions/wake-detect) in CI.

## What changed in Wake 5

- The default execution engine is the in-process EVM. On the Daedaluzz maze benchmark Wake 5 executes 29,931 transactions per second, on par with Foundry (30,186) and 11.5× Echidna (2,600); the same measurement shows that on constant-guarded code the input generator, not the engine, bounds what any random fuzzer finds in eight hours. Both results are in the [Wake 5 preprint](#research).
- Chain history is bounded to the newest 256 blocks by default, which keeps multi-hour campaigns memory-stable; keep references to transaction objects you need later, or raise `testing.block_history`.
- `wake mcp` is new: the static analysis as MCP tools for agents.
- `pytypes` generated by Wake 4 must be regenerated. The [migration guide](https://ack3.ai/wake/docs/5.0.0rc3/testing-framework/migrating-from-wake-4/) lists every breaking change.

## Wake with AI agents

Wake is built so that an agent can write the harness and the engine can run it at native speed: harnesses are plain Python the agent can read, run and revise, and `wake mcp` answers structural questions about the code (contracts, functions, definitions and references, inheritance order, storage layout, state changes, selectors, external calls) from the compiled project. The server returns compact text, not JSON, because the consumer is a language model.

Register the server with your client. Claude Code:

```shell
claude mcp add wake -- wake mcp
```

Any client that reads an MCP configuration file:

```json
{
  "mcpServers": {
    "wake": { "command": "wake", "args": ["mcp"], "cwd": "/path/to/your/project" }
  }
}
```

Three prompts to paste into an agent. Each keeps the agent on the structural work and leaves the security judgement to a person; the reasons are in the [research](#research) below.

**Set up Wake in a repository**

```text
Set up Wake in this repository. Install it with `pip3 install --pre eth-wake`, run `wake up` to create wake.toml and generate pytypes, and fix any compilation configuration until `wake up` succeeds (remappings, solc version, include paths live in wake.toml). Then run `wake detect all` and `wake test`. Report: the solc versions used, the contracts compiled, every detector finding with its file and line, and the test results. Do not change any Solidity source.
```

**Write a manually guided fuzz test**

```text
Write a manually guided fuzz test in Wake for <contract> at <path>. Use `from wake.testing import *` and `from wake.testing.fuzzing import *`, a FuzzTest subclass with one @flow per state-changing entry point and @invariant checks, and run it with `wake test`. Keep a Python model of the expected state and compute expected values from the model BEFORE each transaction, never from the contract after it. For every revert path use may_revert/must_revert and assert the exact error and its parameters. Assert every event's parameters, not only that it was emitted. Add at least one invariant that does not restate the contract's own arithmetic (for example, credited balances never exceed assets held). After the run, open the coverage report and list the branches the flows did not reach. Report the flows, the invariants, the uncovered branches, and anything in the contract you were unsure how to model; do not silently copy contract logic into the model.
```

**Review a contract through the MCP server**

```text
Use the Wake MCP tools to map <contract>: list_contracts, list_contract_functions, get_c3_linearization, get_storage_layout, and get_state_changes for every external or public function, plus find_external_calls_by_selector for every external call. Produce a table of entry points with the state they write and the external calls they make, in call order, with path:line references from the tools. State facts the tools returned; mark anything you infer as an inference. Do not report vulnerabilities in this step.
```

What agents get right and wrong when they write fuzz tests is written up in the [Vibe Fuzzing Guide](https://ack3.ai/research/vibe-fuzzing-guide-for-wakes-manually-guided-fuzzing/): structure can be generated, three things must be verified by a person every time: what the test asserts, which values it asserts against, and which branches actually ran.

## Research

- **Wake 5: A Python Framework for Solidity Testing, Fuzzing, and Static Analysis** (Gattermayer, Převrátil, 2026). arXiv preprint: [arXiv:2611.09307](https://arxiv.org/abs/2611.09307). The framework design, a three-fuzzer benchmark over 75 eight-hour campaigns, the generator-prior analysis, five agent-written harness campaigns, and a static-analysis ablation in an agentic review. Benchmark fork and campaign data: [daedaluzz-benchmark](https://github.com/ack3-ai/daedaluzz-benchmark).
- [Benchmarking EVM fuzzers against an agent-written harness](https://ack3.ai/research/benchmarking-evm-fuzzers-against-an-agent-written-harness/) and [Static analysis MCP tools in an AI audit benchmark](https://ack3.ai/research/ablating-static-analysis-from-an-ai-security-benchmark/), the two research notes the preprint grew from.
- [Introducing manually guided fuzzing](https://ack3.ai/research/introducing-manually-guided-fuzzing-a-new-approach-in-smart-contract-testing/), the method behind Wake's harness model, and the [research journal's Wake articles](https://ack3.ai/research/tags/wake/).

Citing Wake:

```bibtex
@misc{gattermayer2026wake5,
  title         = {Wake 5: A Python Framework for Solidity Testing, Fuzzing, and Static Analysis},
  author        = {Gattermayer, Josef and P{\v{r}}evr{\'a}til, Michal},
  year          = {2026},
  eprint        = {2611.09307},
  archivePrefix = {arXiv},
  primaryClass  = {cs.SE},
  url           = {https://arxiv.org/abs/2611.09307}
}
```

## Documentation and community

- [Documentation](https://ack3.ai/wake/docs/5.0.0rc3/) (Wake 5) · [Changelog](https://ack3.ai/wake/docs/5.0.0rc3/changelog/) · [Contributing](https://ack3.ai/wake/docs/5.0.0rc3/contributing/)
- [Awesome Wake tests](https://github.com/Ackee-Blockchain/awesome-wake-tests): fuzz tests and unit tests from real audits
- Questions and bugs: [GitHub issues](https://github.com/ack3-ai/wake/issues). Security reports: see [SECURITY.md](SECURITY.md)
- Updates: [x.com/ack3ai](https://x.com/ack3ai)

## Support the project

Wake is listed on Giveth as a verified, GIVbacks-eligible project: [Wake: Solidity static analysis and fuzzing framework](https://giveth.io/project/wake:-solidity-static-analysis-and-fuzzing-framework). Every donation goes to the project.

## License

[ISC](LICENSE).
