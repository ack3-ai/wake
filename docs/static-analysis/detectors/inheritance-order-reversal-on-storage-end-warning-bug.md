# Inheritance order reversal on storage end warning bug detector

Name: `inheritance-order-reversal-on-storage-end-warning-bug`

Reports inherited contracts with a custom storage layout that triggers compiler Warning 3495 when the source's Solidity version constraints allow a compiler from 0.8.29 through 0.8.35.

The warning indicates that the contract's storage layout is very close to the end of storage. In affected compilers, emitting the warning reverses the contract's inheritance order, potentially changing initialization, constructor execution, and virtual function or modifier resolution.
The detector reports potential exposure without checking whether reversing the inheritance order changes observable behavior.

The detector uses source version constraints and does not filter findings based on the selected compiler, optimizer, or `via_IR` setting.
Both compilation pipelines are affected. Findings require retained compiler diagnostics and a successfully constructed IR.

The bug was fixed in Solidity 0.8.36. Upgrade the compiler or move the custom storage layout away from the end of storage.
See the [bug announcement](https://www.soliditylang.org/blog/2026/07/09/inheritance-order-reversal-on-storage-end-warning-bug/) for details.

## Parameters

The detector does not accept any additional parameters.
