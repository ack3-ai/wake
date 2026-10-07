from __future__ import annotations

from typing import List

import wake.ir as ir
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


class InheritanceOrderReversalOnStorageEndWarningBugDetector(Detector):
    _detections: List[DetectorResult]

    def __init__(self) -> None:
        self._detections = []

    def detect(self) -> List[DetectorResult]:
        return self._detections

    def visit_contract_definition(self, node: ir.ContractDefinition):
        if node.storage_layout is None or len(node.base_contracts) == 0:
            return

        affected_versions = SolidityVersionRanges(
            [SolidityVersionRange("0.8.29", True, "0.8.36", False)]
        )
        if len(node.source_unit.version_ranges & affected_versions) == 0:
            return

        cu_info = self.build_info.compilation_units.get(node.source_unit.cu_hash.hex())
        if cu_info is None:
            return

        if not any(
            error.error_code == "3495"
            and error.source_location is not None
            and error.source_location.file in node.source_unit.source_unit_names
            and (error.source_location.start, error.source_location.end)
            == node.storage_layout.byte_location
            for error in cu_info.errors
        ):
            return

        self._detections.append(
            DetectorResult(
                Detection(
                    node.storage_layout,
                    "Inheritance-dependent behavior may be miscompiled when this storage layout triggers Warning 3495 in Solidity >= 0.8.29 < 0.8.36",
                ),
                impact=DetectorImpact.MEDIUM,
                confidence=DetectorConfidence.MEDIUM,
                uri=generate_detector_uri(
                    name="inheritance-order-reversal-on-storage-end-warning-bug",
                    version=self.extra["package_versions"]["eth-wake"],
                ),
            )
        )

    @detector.command(name="inheritance-order-reversal-on-storage-end-warning-bug")
    def cli(self) -> None:
        """
        Inheritance order reversal compiler bug in Solidity >= 0.8.29 < 0.8.36
        """
