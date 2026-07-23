from collections import defaultdict

from pydantic import Field

import wake.ir as ir
from wake.analysis import ModifiesStateFlag, modifies_state

from ..common import McpBuild
from .common import ToolInput, mcp_tool, node_loc, node_to_location, resolve_source_unit


class GetStateChangesInput(ToolInput):
    declaration_name: str = Field(
        ..., description="Name of the function or modifier to get state changes for"
    )
    file_path: str = Field(
        ...,
        description="Path to the Solidity file where the function or modifier is defined",
    )
    include_called_functions: bool = Field(
        ...,
        description="Whether to include state changes of called functions as well, or just the function and its modifiers",
    )


def _collect_function_modifies_state(
    node: ir.FunctionDefinition,
    m: set[tuple[ir.ExpressionAbc | ir.StatementAbc | ir.YulAbc, ModifiesStateFlag]],
) -> set[ir.FunctionDefinition | ir.ModifierDefinition]:
    if node.body is not None:
        m |= modifies_state(node.body)

    declarations = set()

    for mod_inv in node.modifiers:
        mod = mod_inv.modifier_name.referenced_declaration
        if isinstance(mod, ir.ContractDefinition):
            try:
                constructor = next(
                    f
                    for f in mod.functions
                    if f.kind == ir.enums.FunctionKind.CONSTRUCTOR
                )
                declarations.add(constructor)
                declarations.update(_collect_function_modifies_state(constructor, m))
            except StopIteration:
                pass
        elif isinstance(mod, ir.ModifierDefinition):
            declarations.add(mod)
            if mod.body is not None:
                m |= modifies_state(mod.body)
        else:
            raise AssertionError(f"Unexpected modifier type {type(mod)}")

    return declarations


@mcp_tool
def get_state_changes(input: GetStateChangesInput, *, build: McpBuild, **kwargs) -> str:
    """Get the state changes of a function or modifier at a given location in Solidity code."""
    declarations: list[ir.FunctionDefinition | ir.ModifierDefinition] = []

    source_unit = resolve_source_unit(build, input.file_path)

    for function in source_unit.functions:
        if not function.implemented:
            continue
        if function.name == input.declaration_name:
            declarations.append(function)

    for contract in source_unit.contracts:
        for function in contract.functions:
            if not function.implemented:
                continue
            if (
                function.name == input.declaration_name
                or function.canonical_name == input.declaration_name
                or f"{contract.name}.{function.name}" == input.declaration_name
            ):
                declarations.append(function)

        for modifier in contract.modifiers:
            if not modifier.implemented:
                continue
            if (
                modifier.name == input.declaration_name
                or modifier.canonical_name == input.declaration_name
                or f"{contract.name}.{modifier.name}" == input.declaration_name
            ):
                declarations.append(modifier)

    if not declarations:
        raise ValueError(
            f"Function or modifier {input.declaration_name} with implemented body not found in {input.file_path}"
        )
    if len(declarations) > 1:
        raise ValueError(
            f"Multiple functions or modifiers with name {input.declaration_name} found in {input.file_path}, choose one of: "
            + ", ".join(d.canonical_name for d in declarations)
        )

    declaration = declarations[0]
    assert declaration.body is not None
    target_declarations = {declaration}

    if isinstance(declaration, ir.FunctionDefinition):
        m = set()
        target_declarations.update(_collect_function_modifies_state(declaration, m))
    else:
        m = modifies_state(declaration.body)

    if len(m) == 0:
        return f"No state changes in {input.declaration_name}."

    grouped = defaultdict(list)
    for ir_node, modification in m:
        if isinstance(ir_node, ir.ExpressionAbc):
            assert ir_node.statement is not None
            grouped[ir_node.statement.declaration].append((ir_node, modification))
        elif isinstance(ir_node, ir.StatementAbc):
            grouped[ir_node.declaration].append((ir_node, modification))
        else:
            assert isinstance(ir_node, ir.YulAbc)
            grouped[ir_node.inline_assembly.declaration].append((ir_node, modification))

    blocks: list[str] = []

    for declaration, changes in grouped.items():
        if input.include_called_functions or declaration in target_declarations:
            decl_type = (
                "function"
                if isinstance(declaration, ir.FunctionDefinition)
                else "modifier"
            )
            rows = sorted(
                (
                    (
                        node_to_location(ir_node, build)["line"],
                        ir_node.source,
                        str(change),
                    )
                    for ir_node, change in changes
                ),
                key=lambda r: r[0],
            )
            block = [
                f"{declaration.canonical_name} ({decl_type}) @ {node_loc(declaration, build)}:"
            ]
            block += [
                f"  - line {line}: {code}  [{change_type}]"
                for line, code, change_type in rows
            ]
            blocks.append("\n".join(block))

    if not blocks:
        return f"No state changes in {input.declaration_name}."
    return "State changes:\n" + "\n".join(blocks)
