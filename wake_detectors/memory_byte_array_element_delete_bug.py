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


class MemoryByteArrayElementDeleteBugDetector(Detector):
    _detections: List[DetectorResult]

    def __init__(self) -> None:
        self._detections = []

    def detect(self) -> List[DetectorResult]:
        return self._detections

    def visit_unary_operation(self, node: ir.UnaryOperation):
        if node.operator != ir.enums.UnaryOpOperator.DELETE:
            return

        expr = node.sub_expression
        while isinstance(expr, ir.TupleExpression):
            if len(expr.components) != 1 or expr.components[0] is None:
                return
            expr = expr.components[0]

        if not isinstance(expr, ir.IndexAccess):
            return

        t = expr.base_expression.type
        if (
            not isinstance(t, types.Bytes)
            or t.data_location != ir.enums.DataLocation.MEMORY
        ):
            return

        affected_versions = SolidityVersionRanges(
            [SolidityVersionRange(None, None, "0.8.37", False)]
        )
        if len(node.source_unit.version_ranges & affected_versions) == 0:
            return

        self._detections.append(
            DetectorResult(
                Detection(
                    node,
                    "Deleting a memory bytes element may corrupt adjacent bytes because of compiler bug in Solidity < 0.8.37",
                ),
                impact=DetectorImpact.MEDIUM,
                confidence=DetectorConfidence.MEDIUM,
                uri=generate_detector_uri(
                    name="memory-byte-array-element-delete-bug",
                    version=self.extra["package_versions"]["eth-wake"],
                ),
            )
        )

    @detector.command(name="memory-byte-array-element-delete-bug")
    def cli(self) -> None:
        """
        Memory byte array element deletion compiler bug in Solidity < 0.8.37
        """
