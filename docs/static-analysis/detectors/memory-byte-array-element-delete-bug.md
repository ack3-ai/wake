# Memory byte array element delete bug detector

Name: `memory-byte-array-element-delete-bug`

Reports `delete` applied to an element of a `bytes` value in memory when the source's Solidity version constraints allow a compiler older than 0.8.37.
This includes strings converted to `bytes`, indexed function results, and parenthesized expressions.

The legacy compiler clears 32 bytes instead of one, potentially corrupting subsequent bytes or neighboring memory.
Whole-array deletion, storage bytes, and arrays such as `bytes1[]` are unaffected.

The detector uses source version constraints and does not filter findings based on the selected compiler or `via_IR` setting.
The IR pipeline is unaffected by the compiler bug, but switching compilation settings can expose the same source pattern.

The bug was fixed in Solidity 0.8.37. Upgrade the compiler or replace the element deletion with an assignment of zero.
See the [bug announcement](https://www.soliditylang.org/blog/2026/09/10/memory-byte-array-element-delete-clears-whole-word-bug/) for details.

## Parameters

The detector does not accept any additional parameters.
