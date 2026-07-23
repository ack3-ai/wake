from collections import defaultdict
from typing import Literal, TypedDict

from pydantic import Field

import wake.ir as ir
from wake.analysis import ModifiesStateFlag, modifies_state

from ..common import McpBuild
from .common import Location, ToolInput, mcp_tool, node_to_location, resolve_source_unit


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


class StateChange(TypedDict):
    code: str
    change_type: str
    line: int


class StateChangesByDeclaration(TypedDict):
    declaration_type: Literal["function", "modifier"]
    location: Location
    state_changes: list[StateChange]


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
def get_state_changes(
    input: GetStateChangesInput, *, build: McpBuild, **kwargs
) -> dict[str, StateChangesByDeclaration]:
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
        return {}

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

    result: dict[str, StateChangesByDeclaration] = {}

    for declaration, changes in grouped.items():
        if input.include_called_functions or declaration in target_declarations:
            result[declaration.canonical_name] = StateChangesByDeclaration(
                declaration_type="function"
                if isinstance(declaration, ir.FunctionDefinition)
                else "modifier",
                location=node_to_location(declaration, build),
                state_changes=sorted(
                    [
                        StateChange(
                            code=ir_node.source,
                            change_type=str(change),
                            line=node_to_location(ir_node, build)["line"],
                        )
                        for ir_node, change in changes
                    ],
                    key=lambda x: x["line"],
                ),
            )

    return result
