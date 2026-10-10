"""Classify individual native POI codes using a supplied small dictionary."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


CHARGING_FACILITY_CODE = "10540832"


@dataclass(frozen=True)
class ClassificationResult:
    """Classification and audit only; inclusion still needs later POI checks."""

    raw_code: object
    group_code: str
    category_code: str
    class_code: str
    classification_status: Literal["known", "unknown_dictionary", "missing", "malformed"]
    include_in_count: bool
    audit_reasons: tuple[str, ...]


def classify(
    raw_code: object,
    dictionary: Mapping[str, object],
    *,
    include_ev_charging: bool = False,
) -> ClassificationResult:
    """Preserve the raw value and check its string form without repairing it.

    Dictionary keys are complete eight-digit codes; values are not used here.
    For this scalar interface, None, float NaN and the empty string are missing.
    Every other value is converted with str(), without stripping or padding.
    Missing and malformed codes are retained as UNKNOWN. Only the exact
    charging-facility code is excluded when include_ev_charging is False.
    This flag does not establish spatial eligibility or pass POI cleaning.
    """
    if not isinstance(include_ev_charging, bool):
        raise TypeError("include_ev_charging must be a bool")

    if raw_code is None or (isinstance(raw_code, float) and math.isnan(raw_code)):
        code = ""
    else:
        code = str(raw_code)

    if code == "":
        return ClassificationResult(
            raw_code, "UNKNOWN", "UNKNOWN", "UNKNOWN",
            "missing", True, ("missing_code",),
        )
    if re.fullmatch(r"[0-9]{8}", code) is None:
        return ClassificationResult(
            raw_code, "UNKNOWN", "UNKNOWN", "UNKNOWN",
            "malformed", True, ("malformed_code",),
        )

    known = code in dictionary
    audit_reasons = () if known else ("unknown_dictionary_code",)
    include_in_count = include_ev_charging or code != CHARGING_FACILITY_CODE
    if not include_in_count:
        audit_reasons += ("charging_facility_excluded",)

    return ClassificationResult(
        raw_code=raw_code,
        group_code=code[:2],
        category_code=code[:4],
        class_code=code,
        classification_status="known" if known else "unknown_dictionary",
        include_in_count=include_in_count,
        audit_reasons=audit_reasons,
    )
