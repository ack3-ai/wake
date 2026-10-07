from __future__ import annotations

from typing import List

import wake.ir as ir
import wake.ir.types as types
from wake.core.solidity_version import SolidityVersionRange, SolidityVersionRanges
from wake.detectors import (
    Detection,
    Detector,
    DetectorConfidence,
    DetectorImpact,
    DetectorResult,
    detector,
)
from wake_detectors.utils import generate_detector_uri


class RequireCustomErrorNamedArgumentsBugDetector(Detector):
    _detections: List[DetectorResult]

    def __init__(self) -> None:
        self._detections = []

    def detect(self) -> List[DetectorResult]:
        return self._detections

    def visit_identifier(self, node: ir.Identifier):
        if node.referenced_declaration != ir.enums.GlobalSymbol.REQUIRE:
            return

        parent = node.parent
        if (
            not isinstance(parent, ir.FunctionCall)
            or parent.expression != node
            or len(parent.arguments) != 2
        ):
            return

        error_call = parent.arguments[1]
        while isinstance(error_call, ir.TupleExpression):
            if len(error_call.components) != 1 or error_call.components[0] is None:
                return
            error_call = error_call.components[0]

        if (
            not isinstance(error_call, ir.FunctionCall)
            or len(error_call.names) == 0
            or not isinstance(error_call.type, types.Error)
        ):
            return

        expr = error_call.expression
        while isinstance(expr, ir.TupleExpression):
            assert len(expr.components) == 1 and expr.components[0] is not None
            expr = expr.components[0]

        assert isinstance(expr, (ir.Identifier, ir.MemberAccess))
        error = expr.referenced_declaration
        assert isinstance(error, ir.ErrorDefinition)

        names = tuple(
            name
            for name, argument in zip(error_call.names, error_call.arguments)
            if not isinstance(argument.type, types.StringLiteral)
        )
        if names == tuple(
            p.name for p in error.parameters.parameters if p.name in names
        ):
            return

        affected_versions = SolidityVersionRanges(
            [SolidityVersionRange("0.8.26", True, "0.8.37", False)]
        )
        if len(node.source_unit.version_ranges & affected_versions) == 0:
            return

        self._detections.append(
            DetectorResult(
                Detection(
                    error_call,
                    "Named custom error arguments in require may be encoded in the wrong order because of compiler bug in Solidity >= 0.8.26 < 0.8.37",
                ),
                impact=DetectorImpact.LOW,
                confidence=DetectorConfidence.HIGH,
                uri=generate_detector_uri(
                    name="require-custom-error-named-arguments-bug",
                    version=self.extra["package_versions"]["eth-wake"],
                ),
            )
        )

    @detector.command(name="require-custom-error-named-arguments-bug")
    def cli(self) -> None:
        """
        Named custom error arguments in require compiler bug in Solidity >= 0.8.26 < 0.8.37
        """
