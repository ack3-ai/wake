# Require custom error named arguments bug detector

Name: `require-custom-error-named-arguments-bug`

Reports custom errors passed as the second argument to the built-in `require` when their named arguments are out of declaration order and the source's Solidity version constraints allow a compiler from 0.8.26 through 0.8.36.
Parenthesized error constructors and qualified or inherited errors are included.

The IR compiler encodes these arguments in call-site order, potentially producing incorrect or malformed revert data.
Positional arguments, named arguments in declaration order, and standalone `revert` statements are unaffected.
String literals occupy no stack slots, so the detector compares the order of arguments excluding string literals.

The detector uses source version constraints and does not filter findings based on the selected compiler or `via_IR` setting.
The legacy pipeline is unaffected by the compiler bug, but switching compilation settings can expose the same source pattern.

The bug was fixed in Solidity 0.8.37. Upgrade the compiler, use positional arguments, or put named arguments in declaration order.
See the [bug announcement](https://www.soliditylang.org/blog/2026/09/10/misordered-named-parameters-in-require-with-custom-errors-bug/) for details.

## Parameters

The detector does not accept any additional parameters.
