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


@dataclass(frozen=True)
class NativeDictionary:
    """Maps use complete 2/4/8-digit codes, never local fragments."""

    groups: dict
    categories: dict
    classes: dict

    def rows(self):
        return [dict(row) for table in (self.groups, self.categories, self.classes)
                for _, row in sorted(table.items())]


def native_dictionary(group_path, category_path, class_path, *, data_root):
    """Read the supplied pipe-delimited UTF-8 dictionaries without repairing keys.

    Category Number is the globally unique foreign-key target used by the
    class file. Class Number may be reused under distinct categories. ID is
    separately unique within each input table and is retained for tracing.
    """
    import csv
    import io
    from src.data import open_source, sha256

    paths = (group_path, category_path, class_path)
    before = {path: sha256(path, data_root=data_root) for path in paths}

    def read(path, header, width):
        with open_source(path, data_root=data_root) as stream:
            text = io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")
            try:
                reader = csv.reader(text, delimiter="|", strict=True)
                if next(reader, None) != header:
                    raise ValueError(f"{path}: unexpected dictionary header")
                ids = set()
                result = []
                for record_no, values in enumerate(reader, 1):
                    location = f"{path} record {record_no}"
                    if len(values) != len(header):
                        raise ValueError(f"{location}: wrong field count")
                    identity, local, name = values[:3]
                    if re.fullmatch(r"[1-9][0-9]*", identity) is None or identity in ids:
                        raise ValueError(f"{location}: invalid or duplicate ID {identity!r}")
                    ids.add(identity)
                    if re.fullmatch(rf"[0-9]{{{width}}}", local) is None:
                        raise ValueError(f"{location}: invalid {width}-digit local code {local!r}")
                    if len(values) == 4 and re.fullmatch(r"[0-9]{2}", values[3]) is None:
                        raise ValueError(f"{location}: invalid parent foreign key")
                    result.append({"source_path": path, "source_record_no": record_no,
                                   "source_id": identity, "local_code": local,
                                   "dictionary_name": name,
                                   "foreign_key": values[3] if len(values) == 4 else ""})
                if not result:
                    raise ValueError(f"{path}: dictionary table is empty")
                return result
            finally:
                text.detach()

    groups, categories, classes = {}, {}, {}
    category_by_local = {}

    def insert(table, row, level, code, parent):
        if code in table:
            raise ValueError(f"{row['source_path']}: duplicate complete {level} code {code}")
        table[code] = dict(row, level=level, code=code, parent_code=parent)

    for row in read(group_path, ["ID", "Group Number", "Group Description"], 2):
        insert(groups, row, "group", row["local_code"], "")
    for row in read(category_path, ["ID", "Category Number", "Category Description", "Group Number Foreign Key"], 2):
        parent, local = row["foreign_key"], row["local_code"]
        if parent not in groups:
            raise ValueError(f"{category_path}: missing group parent {parent}")
        if local in category_by_local:
            raise ValueError(f"{category_path}: ambiguous Category Number foreign-key target {local}")
        code = parent + local
        insert(categories, row, "category", code, parent)
        category_by_local[local] = code
    for row in read(class_path, ["ID", "Class Number", "Classification Description", "Category Number Foreign Key"], 4):
        local_parent = row["foreign_key"]
        if local_parent not in category_by_local:
            raise ValueError(f"{class_path}: missing category parent {local_parent}")
        parent = category_by_local[local_parent]
        insert(classes, row, "class", parent + row["local_code"], parent)
    if before != {path: sha256(path, data_root=data_root) for path in paths}:
        raise ValueError("dictionary input changed during reading")
    return NativeDictionary(groups, categories, classes)


def merge_classification_frequencies(dictionary, frequencies):
    """Outer join raw full-source frequencies to official complete codes.

    Frequencies are the typed, format-audited rows produced by step 06.
    Missing/malformed values remain separate in raw_audit and aggregate into
    UNKNOWN in the class/level tables. Charging rows are never removed here.
    """
    from collections import Counter

    totals = {level: Counter() for level in ("group", "category", "class")}
    raw_audit, seen = [], set()
    for item in frequencies:
        row = dict(item)
        count = row["count"]
        if type(count) is not int or count <= 0:
            raise ValueError("raw frequency counts must be positive integers")
        key = row["raw_type"], row["raw_value"]
        if key in seen:
            raise ValueError("duplicate raw classification frequency key")
        seen.add(key)
        status = row["format_status"]
        if status not in ("valid", "missing", "malformed"):
            raise ValueError("invalid classification format status")
        code = row["class_code"]
        if status == "valid":
            if re.fullmatch(r"[0-9]{8}", code) is None or (row["group_code"], row["category_code"]) != (code[:2], code[:4]):
                raise ValueError("classification prefixes disagree")
        elif any(row[f"{level}_code"] != "UNKNOWN" for level in totals):
            raise ValueError("missing/malformed classifications require UNKNOWN")
        row["dictionary_status"] = ("matched" if code in dictionary.classes else "source_only") if status == "valid" else "not_applicable"
        raw_audit.append(row)
        for level in totals:
            totals[level][row[f"{level}_code"]] += count
    merged = []
    for code in sorted(set(dictionary.classes) | set(totals["class"])):
        present = code in totals["class"]
        official = code in dictionary.classes
        group, category = (code[:2], code[:4]) if code != "UNKNOWN" else ("UNKNOWN", "UNKNOWN")
        merged.append({"class_code": code, "group_code": group, "category_code": category,
                       "group_dictionary_name": dictionary.groups.get(group, {}).get("dictionary_name", ""),
                       "category_dictionary_name": dictionary.categories.get(category, {}).get("dictionary_name", ""),
                       "class_dictionary_name": dictionary.classes.get(code, {}).get("dictionary_name", ""),
                       "source_count": totals["class"][code],
                       "dictionary_status": "not_applicable" if code == "UNKNOWN" else "matched" if official and present else "dictionary_only" if official else "source_only"})
    expected = sum(row["count"] for row in raw_audit)
    if any(sum(counts.values()) != expected for counts in totals.values()) or sum(row["source_count"] for row in merged) != expected:
        raise ValueError("classification frequency totals do not reconcile")
    return {"classes": merged, "raw_audit": raw_audit, "level_counts": totals, "source_count": expected}
