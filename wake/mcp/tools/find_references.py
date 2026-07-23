from pathlib import Path

from pydantic import Field

import wake.ir as ir

from ..common import McpBuild
from .common import (
    Location,
    ToolInput,
    loc_str,
    mcp_tool,
    node_to_location,
    normalize_whitespace,
    overlapping_nodes_at_line,
    source_unit_to_file,
)


class FindReferencesInput(ToolInput):
    expression: str = Field(..., description="Expression to find references to")
    file_path: str | None = Field(
        None, description="Path to the Solidity file where the expression is used"
    )
    line: int | None = Field(
        None,
        description="Line number where the expression is used (1-based, as shown in editors)",
    )
    where: list[str] | None = Field(
        None, description="List of files and directories to search for references"
    )


# Map of string names to GlobalSymbol enum values
GLOBAL_SYMBOL_MAP: dict[str, ir.enums.GlobalSymbol] = {
    # Standalone symbols (-1 to -99) - referenced by Identifier nodes
    "abi": ir.enums.GlobalSymbol.ABI,
    "addmod": ir.enums.GlobalSymbol.ADDMOD,
    "assert": ir.enums.GlobalSymbol.ASSERT,
    "block": ir.enums.GlobalSymbol.BLOCK,
    "blockhash": ir.enums.GlobalSymbol.BLOCKHASH,
    "ecrecover": ir.enums.GlobalSymbol.ECRECOVER,
    "gasleft": ir.enums.GlobalSymbol.GASLEFT,
    "keccak256": ir.enums.GlobalSymbol.KECCAK256,
    "log0": ir.enums.GlobalSymbol.LOG0,
    "log1": ir.enums.GlobalSymbol.LOG1,
    "log2": ir.enums.GlobalSymbol.LOG2,
    "log3": ir.enums.GlobalSymbol.LOG3,
    "log4": ir.enums.GlobalSymbol.LOG4,
    "msg": ir.enums.GlobalSymbol.MSG,
    "mulmod": ir.enums.GlobalSymbol.MULMOD,
    "now": ir.enums.GlobalSymbol.NOW,
    "require": ir.enums.GlobalSymbol.REQUIRE,
    "revert": ir.enums.GlobalSymbol.REVERT,
    "ripemd160": ir.enums.GlobalSymbol.RIPEMD160,
    "selfdestruct": ir.enums.GlobalSymbol.SELFDESTRUCT,
    "sha256": ir.enums.GlobalSymbol.SHA256,
    "sha3": ir.enums.GlobalSymbol.SHA3,
    "suicide": ir.enums.GlobalSymbol.SUICIDE,
    "super": ir.enums.GlobalSymbol.SUPER,
    "tx": ir.enums.GlobalSymbol.TX,
    "type": ir.enums.GlobalSymbol.TYPE,
    "this": ir.enums.GlobalSymbol.THIS,
    "blobhash": ir.enums.GlobalSymbol.BLOBHASH,
    # Block member access - referenced by MemberAccess nodes
    "block.basefee": ir.enums.GlobalSymbol.BLOCK_BASEFEE,
    "block.chainid": ir.enums.GlobalSymbol.BLOCK_CHAINID,
    "block.coinbase": ir.enums.GlobalSymbol.BLOCK_COINBASE,
    "block.difficulty": ir.enums.GlobalSymbol.BLOCK_DIFFICULTY,
    "block.gaslimit": ir.enums.GlobalSymbol.BLOCK_GASLIMIT,
    "block.number": ir.enums.GlobalSymbol.BLOCK_NUMBER,
    "block.timestamp": ir.enums.GlobalSymbol.BLOCK_TIMESTAMP,
    "block.prevrandao": ir.enums.GlobalSymbol.BLOCK_PREVRANDAO,
    "block.blobbasefee": ir.enums.GlobalSymbol.BLOCK_BLOBBASEFEE,
    # Msg member access
    "msg.data": ir.enums.GlobalSymbol.MSG_DATA,
    "msg.sender": ir.enums.GlobalSymbol.MSG_SENDER,
    "msg.sig": ir.enums.GlobalSymbol.MSG_SIG,
    "msg.value": ir.enums.GlobalSymbol.MSG_VALUE,
    # Tx member access
    "tx.gasprice": ir.enums.GlobalSymbol.TX_GASPRICE,
    "tx.origin": ir.enums.GlobalSymbol.TX_ORIGIN,
    # Abi member access
    "abi.decode": ir.enums.GlobalSymbol.ABI_DECODE,
    "abi.encode": ir.enums.GlobalSymbol.ABI_ENCODE,
    "abi.encodePacked": ir.enums.GlobalSymbol.ABI_ENCODE_PACKED,
    "abi.encodeWithSelector": ir.enums.GlobalSymbol.ABI_ENCODE_WITH_SELECTOR,
    "abi.encodeWithSignature": ir.enums.GlobalSymbol.ABI_ENCODE_WITH_SIGNATURE,
    "abi.encodeCall": ir.enums.GlobalSymbol.ABI_ENCODE_CALL,
    # Bytes member access
    "bytes.concat": ir.enums.GlobalSymbol.BYTES_CONCAT,
    ".concat": ir.enums.GlobalSymbol.BYTES_CONCAT,
    ".length": ir.enums.GlobalSymbol.BYTES_LENGTH,
    ".push": ir.enums.GlobalSymbol.BYTES_PUSH,
    ".pop": ir.enums.GlobalSymbol.BYTES_POP,
    # String member access
    "string.concat": ir.enums.GlobalSymbol.STRING_CONCAT,
    # Address member access
    ".balance": ir.enums.GlobalSymbol.ADDRESS_BALANCE,
    ".code": ir.enums.GlobalSymbol.ADDRESS_CODE,
    ".codehash": ir.enums.GlobalSymbol.ADDRESS_CODEHASH,
    ".transfer": ir.enums.GlobalSymbol.ADDRESS_TRANSFER,
    ".send": ir.enums.GlobalSymbol.ADDRESS_SEND,
    ".call": ir.enums.GlobalSymbol.ADDRESS_CALL,
    ".delegatecall": ir.enums.GlobalSymbol.ADDRESS_DELEGATECALL,
    ".staticcall": ir.enums.GlobalSymbol.ADDRESS_STATICCALL,
    # Type member access
    ".name": ir.enums.GlobalSymbol.TYPE_NAME,
    ".creationCode": ir.enums.GlobalSymbol.TYPE_CREATION_CODE,
    ".runtimeCode": ir.enums.GlobalSymbol.TYPE_RUNTIME_CODE,
    ".interfaceId": ir.enums.GlobalSymbol.TYPE_INTERFACE_ID,
    ".min": ir.enums.GlobalSymbol.TYPE_MIN,
    ".max": ir.enums.GlobalSymbol.TYPE_MAX,
    # Function member access
    ".selector": ir.enums.GlobalSymbol.FUNCTION_SELECTOR,
    ".gas": ir.enums.GlobalSymbol.FUNCTION_GAS,
    ".address": ir.enums.GlobalSymbol.FUNCTION_ADDRESS,
    # User defined value type
    ".wrap": ir.enums.GlobalSymbol.USER_DEFINED_VALUE_TYPE_WRAP,
    ".unwrap": ir.enums.GlobalSymbol.USER_DEFINED_VALUE_TYPE_UNWRAP,
}


def _ir_to_locations(
    node: ir.DeclarationAbc, where: list[Path] | None, build: McpBuild
) -> list[Location]:
    return [
        node_to_location(ref, build)
        for ref in node.references
        if where is None or any(ref.source_unit.file.is_relative_to(w) for w in where)
    ]


def _global_symbol_to_locations(
    build: McpBuild, symbol: ir.enums.GlobalSymbol, where: list[Path] | None
) -> list[Location]:
    return [
        node_to_location(ref, build)
        for ref in build.reference_resolver.get_global_symbol_references(symbol)
        if where is None or any(ref.source_unit.file.is_relative_to(w) for w in where)
    ]


def _collect_references(input: FindReferencesInput, build: McpBuild) -> list[Location]:
    where = input.where or []
    if not where:
        where_paths = None
    else:
        where_paths = [Path(w).resolve() for w in where]

    if input.file_path is None or input.line is None:
        if input.expression in GLOBAL_SYMBOL_MAP:
            return _global_symbol_to_locations(
                build, GLOBAL_SYMBOL_MAP[input.expression], where_paths
            )

        possible_declarations: set[ir.DeclarationAbc] = set()
        for source_unit in build.source_units.values():
            for declaration in source_unit.declarations_iter():
                if declaration.name == normalize_whitespace(input.expression):
                    possible_declarations.add(declaration)
                elif declaration.canonical_name == normalize_whitespace(
                    input.expression
                ):
                    possible_declarations.add(declaration)
                elif (
                    isinstance(
                        declaration, (ir.FunctionDefinition, ir.ModifierDefinition)
                    )
                    and isinstance(declaration.parent, ir.ContractDefinition)
                    and f"{declaration.parent.name}.{declaration.name}"
                    == normalize_whitespace(input.expression)
                ):
                    possible_declarations.add(declaration)
        if len(possible_declarations) == 0:
            raise ValueError(f"No declaration named {input.expression} found")
        if len(possible_declarations) > 1:
            locations = []
            for decl in possible_declarations:
                line, _ = decl.source_unit.get_line_col_from_byte_offset(
                    decl.byte_location[0]
                )
                locations.append(
                    f"{source_unit_to_file(build, decl.source_unit)}:{line}"
                )
            raise ValueError(
                f"Multiple declarations named {input.expression} found at: {', '.join(locations)}"
            )

        return _ir_to_locations(possible_declarations.pop(), where_paths, build)

    nodes = overlapping_nodes_at_line(build, input.file_path, input.line)
    normalized_expression = normalize_whitespace(input.expression)

    node = None
    for n in nodes:
        if (
            isinstance(n, (ir.ExpressionAbc, ir.IdentifierPath, ir.UserDefinedTypeName))
            and normalize_whitespace(n.source) == normalized_expression
        ):
            node = n
            break

        if (
            isinstance(n, ir.MemberAccess)
            and normalize_whitespace(n.member_name) == normalized_expression
        ):
            node = n
            break

        if isinstance(n, (ir.IdentifierPath, ir.UserDefinedTypeName)):
            for part in n.identifier_path_parts:
                source = n.source_unit.file_source[
                    part.byte_location[0] : part.byte_location[1]
                ].decode("utf-8")
                if (
                    normalize_whitespace(source) == normalized_expression
                    or normalize_whitespace(part.name) == normalized_expression
                ):
                    node = n
                    break
            if node is not None:
                break

    if node is None:
        raise ValueError(
            f"No expression named {input.expression} found at {input.file_path}:{input.line}"
        )

    if isinstance(node, ir.YulIdentifier):
        if node.external_reference is None:
            raise ValueError(
                f"Identifier at {input.file_path}:{input.line} is not a Solidity reference"
            )
        return _ir_to_locations(
            node.external_reference.referenced_declaration, where_paths, build
        )
    elif isinstance(
        node,
        (
            ir.Identifier,
            ir.MemberAccess,
            ir.IdentifierPath,
            ir.UserDefinedTypeName,
            ir.IdentifierPathPart,
        ),
    ):
        decl = node.referenced_declaration
        if isinstance(decl, ir.enums.GlobalSymbol):
            return _global_symbol_to_locations(build, decl, where_paths)
        elif isinstance(decl, ir.SourceUnit):
            raise ValueError(
                f"Identifier at {input.file_path}:{input.line} references a source unit"
            )

        if isinstance(decl, frozenset):
            result = set()
            for d in decl:
                result.update(_ir_to_locations(d, where_paths, build))
            return list(result)
        else:
            return _ir_to_locations(decl, where_paths, build)
    else:
        raise ValueError(
            f"No valid expression named `{input.expression}` found at {input.file_path}:{input.line}"
        )


@mcp_tool
def find_references(input: FindReferencesInput, *, build: McpBuild, **kwargs) -> str:
    """Find references to a specific identifier at a given location in Solidity code."""
    locations = _collect_references(input, build)
    if not locations:
        return f"No references to `{input.expression}` found."
    body = "\n".join(f"- {loc_str(l)}" for l in locations)
    return f"References to `{input.expression}` ({len(locations)}):\n{body}"
