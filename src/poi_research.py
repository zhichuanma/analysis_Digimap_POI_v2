"""Versioned Class decisions for step 11; no category or keyword fallback."""

import csv
from dataclasses import dataclass
import re
from types import MappingProxyType

import yaml

from src.classification import CHARGING_FACILITY_CODE, classify


FUNCTION_GROUPS = (
    "accommodation", "food_beverage", "retail", "leisure_tourism",
    "office_services", "education_health_public", "industry_logistics", "transport_parking",
)
RULE_FIELDS = (
    "class_code", "group_number", "category_number", "category_code", "group_name",
    "category_name", "class_name", "decision", "poi_function_group", "decision_reason",
    "rule_version", "rule_origin",
)


@dataclass(frozen=True)
class ResearchRules:
    version: str
    rows: object
    coverage: dict
    provenance: dict


def load_research_rules(config_path, *, code_root, dictionary, dictionary_inputs):
    """Validate complete dictionary coverage, exact names/parents and file binding.

    dictionary_inputs contains independently verified input fingerprints. A
    policy exception is kept separate from official dictionary Class counts.
    All codes, including Category Number, are read as text without padding.
    """
    from run import _UniqueKeyLoader
    from src.data import project_path, _artifact_fingerprint, _open_regular

    path = project_path(config_path, root=code_root, field="research_config")
    before = _artifact_fingerprint(path)
    with _open_regular(path) as stream:
        meta = yaml.load(stream, Loader=_UniqueKeyLoader)
    required = {"schema_version", "rule_version", "rules_file", "rules_fingerprint",
                "dictionary_inputs", "dictionary_class_count", "policy_exception_codes",
                "functional_groups", "subtype_policy", "time_policy"}
    if not isinstance(meta, dict) or type(meta.get("schema_version")) is not int or meta["schema_version"] not in (1, 2):
        raise ValueError("research rule configuration: invalid schema/fields")
    if meta["schema_version"] == 2:
        required |= {"composition_mode", "unmatched_class_policy"}
    if meta.keys() != required:
        raise ValueError("research rule configuration: invalid schema/fields")
    if meta["schema_version"] == 2 and (meta["composition_mode"] != "native_group" or meta["unmatched_class_policy"] != "review"):
        raise ValueError("research rules require native_group composition and explicit unmatched Class review")
    version = meta["rule_version"]
    if not isinstance(version, str) or not re.fullmatch(r"poi-research-v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("research rule version is invalid")
    if not isinstance(meta["functional_groups"], dict) or set(meta["functional_groups"]) != set(FUNCTION_GROUPS) or any(not isinstance(v, str) or not v.strip() for v in meta["functional_groups"].values()):
        raise ValueError("research configuration must define exactly eight function groups")
    actual_inputs = {k: {f: v[f] for f in ("sha256", "size_bytes")} for k, v in dictionary_inputs.items()}
    if meta["dictionary_inputs"] != actual_inputs or meta["dictionary_class_count"] != len(dictionary.classes):
        raise ValueError("research rules dictionary fingerprint/count mismatch")
    if meta["policy_exception_codes"] != [CHARGING_FACILITY_CODE]:
        raise ValueError("research policy exception must be the explicit EV charging code")
    table = project_path(meta["rules_file"], root=code_root, field="research_rules_file")
    table_fp = _artifact_fingerprint(table)
    if table_fp != meta["rules_fingerprint"]:
        raise ValueError("research rule table checksum mismatch")
    rows = {}
    with _open_regular(table) as stream:
        import io
        text = io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")
        try:
            reader = csv.DictReader(text, strict=True)
            if reader.fieldnames != list(RULE_FIELDS):
                raise ValueError("research rule table header mismatch")
            for row in reader:
                if row.keys() != set(RULE_FIELDS) or any(v is None for v in row.values()):
                    raise ValueError("research rule table malformed row")
                code = row["class_code"]
                if not re.fullmatch(r"[0-9]{8}", code) or code in rows:
                    raise ValueError(f"research rule invalid/duplicate Class: {code!r}")
                if row["rule_version"] != version:
                    raise ValueError(f"research rule version mismatch: {code}")
                if row["decision"] not in ("include", "exclude", "review") or not row["decision_reason"].strip():
                    raise ValueError(f"research Class requires decision and reason: {code}")
                group = row["poi_function_group"]
                if (group and group not in FUNCTION_GROUPS) or (row["decision"] != "include" and group != ""):
                    raise ValueError(f"research Class has invalid audit function group: {code}")
                if meta["schema_version"] == 1 and row["decision"] == "include" and not group:
                    raise ValueError(f"legacy research Class must have exactly one group iff included: {code}")
                if (row["group_number"], row["category_number"], row["category_code"]) != (code[:2], code[2:4], code[:4]):
                    raise ValueError(f"research Class parent/prefix mismatch: {code}")
                if code in dictionary.classes:
                    official = dictionary.classes[code]
                    expected = (dictionary.groups[code[:2]]["dictionary_name"], dictionary.categories[code[:4]]["dictionary_name"], official["dictionary_name"])
                    if row["rule_origin"] != "step07_dictionary" or (row["group_name"], row["category_name"], row["class_name"]) != expected:
                        raise ValueError(f"research Class dictionary name/origin mismatch: {code}")
                elif code != CHARGING_FACILITY_CODE or row["rule_origin"] != "user_policy_exception" or row["class_name"] != "":
                    raise ValueError(f"unrecognized research Class: {code}")
                if code == CHARGING_FACILITY_CODE and row["decision"] != "exclude":
                    raise ValueError("EV charging must always be excluded from research composition")
                rows[code] = MappingProxyType(row)
        finally:
            text.detach()
    missing = sorted(set(dictionary.classes) - set(rows))
    if missing:
        raise ValueError(f"research rule coverage incomplete: {missing}")
    if CHARGING_FACILITY_CODE not in rows:
        raise ValueError("missing explicit EV charging research exclusion")
    official_rows = [dict(rows[c]) for c in sorted(dictionary.classes)]
    coverage = {"dictionary_class_count": len(official_rows), "covered_class_count": len(official_rows),
                "uncovered_classes": [], "unrecognized_classes": [], "rule_version": version,
                "decisions": {d: sum(r["decision"] == d for r in official_rows) for d in ("include", "exclude", "review")},
                "all_rule_decisions": {d: sum(r["decision"] == d for r in rows.values()) for d in ("include", "exclude", "review")},
                "composition_mode": "native_group", "functional_group_role": "audit_only",
                "unmatched_class_policy": "review",
                "native_group_classes": {g: sum(r["decision"] == "include" and r["group_number"] == g for r in official_rows) for g in sorted(dictionary.groups)},
                "functional_group_classes": {g: sum(r["poi_function_group"] == g for r in official_rows) for g in FUNCTION_GROUPS},
                "included_without_function_group": [r["class_code"] for r in official_rows if r["decision"] == "include" and not r["poi_function_group"]],
                "review_classes": [r for r in official_rows if r["decision"] == "review"],
                "policy_exceptions": [dict(rows[c]) for c in sorted(set(rows) - set(dictionary.classes))],
                "group_definitions": meta["functional_groups"], "subtype_policy": meta["subtype_policy"], "time_policy": meta["time_policy"]}
    if before != _artifact_fingerprint(path) or table_fp != _artifact_fingerprint(table):
        raise ValueError("research rule configuration/table changed during reading")
    return ResearchRules(version, MappingProxyType(rows), coverage,
                         {"config_path": config_path, "config_fingerprint": before,
                          "rules_file": meta["rules_file"], "rules_fingerprint": table_fp,
                          "dictionary_inputs": actual_inputs, "config_schema_version": meta["schema_version"],
                          "composition_mode": "native_group", "unmatched_class_policy": "review"})


def research_classification(raw_code, *, dictionary, rules):
    """Quality and classification remain independent, including the EV exception."""
    result = classify(raw_code, dictionary.classes, include_ev_charging=True)
    rule = rules.rows.get(result.class_code)
    review = result.classification_status != "known" or (rule is not None and rule["decision"] == "review")
    if rule is None:
        decision, group, reason = "review", None, result.classification_status + "_classification"
    else:
        decision, group, reason = rule["decision"], rule["poi_function_group"] or None, rule["decision_reason"]
    # Config validation also enforces this; the guard makes the estimand explicit.
    if result.class_code == CHARGING_FACILITY_CODE:
        decision, group = "exclude", None
    return {"classification_status": result.classification_status,
            "group_code": result.group_code, "category_code": result.category_code,
            "category_number": result.category_code[2:] if result.category_code != "UNKNOWN" else "UNKNOWN",
            "class_code": result.class_code, "research_decision": decision,
            "research_classification_include": decision == "include", "poi_function_group": group,
            "functional_group_pending": decision == "include" and group is None,
            "group_dictionary_name": dictionary.groups.get(result.group_code, {}).get("dictionary_name"),
            "category_dictionary_name": dictionary.categories.get(result.category_code, {}).get("dictionary_name"),
            "class_dictionary_name": dictionary.classes.get(result.class_code, {}).get("dictionary_name"),
            "classification_review": review, "classification_reason": reason,
            "classification_rule_version": rules.version}
