"""Condition trees: three-state criteria evaluation and semantic diffs.

A protocol revision may express its inclusion/exclusion criteria either in the
legacy *flat* form (``inclusion``/``exclusion`` sections) or as a native
``criteria`` condition tree built from ``all_of`` / ``any_of`` / ``not``
operators and leaf conditions. Both forms compile to the same canonical tree,
which is what makes a *semantic* comparison between two revisions possible:

* every condition node carries a stable ``id``; its **semantic key** hashes the
  clinical content (operator, codes, thresholds, observation windows) and
  deliberately ignores ``label``/``description`` — renaming a condition never
  changes a cohort;
* the **skeleton key** hashes the tree structure plus condition ids, so two
  revisions edited concurrently can be aligned node-by-node instead of
  splicing fragments: when the skeletons differ, the whole criteria block is
  treated as one atomic change and never mixed leaf-by-leaf.

Evaluation is three-valued (``true`` / ``false`` / ``unknown``): a required
record that is absent yields ``unknown`` (missing data), never ``false``.
``and`` collapses to ``false`` on any ``false``, to ``unknown`` on any
``unknown``; ``or`` is the dual; ``not`` flips only the two definite values.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import date

TRUE = "true"
FALSE = "false"
UNKNOWN = "unknown"

VERDICT_LABELS = {TRUE: "满足", FALSE: "不满足", UNKNOWN: "未知（缺资料）"}

# Keys that never take part in the semantic identity of a condition.
NON_SEMANTIC_KEYS = {"id", "label", "description", "display_name", "note"}

LEAF_TYPES = {
    "age_range",
    "diagnosis",
    "lab_range",
    "medication",
    "diagnosis_present",
    "lab_threshold",
    "medication_present",
}


class ConditionTreeError(ValueError):
    """Raised when a native criteria tree is malformed."""


# ---------------------------------------------------------------------------
# small date helpers (kept local so this module has no service dependencies)
# ---------------------------------------------------------------------------
def parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def age_on(birth: date | None, moment: date) -> int | None:
    if birth is None:
        return None
    return moment.year - birth.year - ((moment.month, moment.day) < (birth.month, birth.day))


# ---------------------------------------------------------------------------
# compilation: flat rules -> canonical tree
# ---------------------------------------------------------------------------
def compile_flat_rules(rules: dict) -> dict | None:
    """Compile the legacy ``inclusion``/``exclusion`` sections into a tree.

    Condition ids are deterministic so a flat revision and a native revision
    that kept the same ids align node-by-node. Returns ``None`` when the rules
    carry no criteria at all.
    """
    inc = rules.get("inclusion") or {}
    exc = rules.get("exclusion") or {}
    children: list[dict] = []

    if inc.get("age_min") is not None or inc.get("age_max") is not None:
        node = {"id": "inclusion.age", "leaf": "age_range", "label": "年龄范围"}
        if inc.get("age_min") is not None:
            node["min"] = inc["age_min"]
        if inc.get("age_max") is not None:
            node["max"] = inc["age_max"]
        children.append(node)

    diag = inc.get("diagnosis")
    if diag and diag.get("codes"):
        children.append(
            {
                "id": "inclusion.diagnosis",
                "leaf": "diagnosis",
                "label": "必需诊断",
                "codes": list(diag["codes"]),
                "window_days": int(diag.get("window_days", 3650)),
            }
        )

    lab = inc.get("lab")
    if lab and lab.get("code"):
        children.append(
            {
                "id": f"inclusion.lab.{lab['code']}",
                "leaf": "lab_range",
                "label": f"必需检验 {lab['code']}",
                "code": lab["code"],
                "study_unit": lab.get("study_unit"),
                "min": lab.get("min"),
                "max": lab.get("max"),
                "window_days_before": int(lab.get("window_days_before", 30)),
                "window_days_after": int(lab.get("window_days_after", 0)),
            }
        )

    med = inc.get("medication")
    if med and med.get("codes"):
        children.append(
            {
                "id": "inclusion.medication",
                "leaf": "medication",
                "label": "必需用药",
                "codes": list(med["codes"]),
                "within_days_after_index": int(med.get("within_days_after_index", 30)),
            }
        )

    exc_diag = exc.get("diagnosis")
    if exc_diag and exc_diag.get("codes"):
        children.append(
            {
                "id": "exclusion.diagnosis",
                "label": "排除诊断",
                "not": {"leaf": "diagnosis_present", "codes": list(exc_diag["codes"])},
            }
        )

    exc_lab = exc.get("lab")
    if exc_lab and exc_lab.get("code"):
        node = {
            "id": f"exclusion.lab.{exc_lab['code']}",
            "label": f"排除检验 {exc_lab['code']}",
            "not": {
                "leaf": "lab_threshold",
                "code": exc_lab["code"],
                "window_days_before": int(exc_lab.get("window_days_before", 30)),
            },
        }
        if exc_lab.get("min") is not None:
            node["not"]["min"] = exc_lab["min"]
        if exc_lab.get("max") is not None:
            node["not"]["max"] = exc_lab["max"]
        children.append(node)

    exc_med = exc.get("medication")
    if exc_med and exc_med.get("codes"):
        children.append(
            {
                "id": "exclusion.medication",
                "label": "排除用药",
                "not": {"leaf": "medication_present", "codes": list(exc_med["codes"])},
            }
        )

    if not children:
        return None
    return {"id": "criteria", "all_of": children}


def validate_tree(node, path: str = "criteria", _inside_condition: bool = False) -> None:
    """Structural validation of a native criteria tree.

    Every condition needs a stable id for cross-revision alignment. The id
    may sit on the condition's outermost node (e.g. on a ``not`` wrapper);
    leaves inside an already-identified condition are internal detail. The
    tree root's own id labels the tree, not a condition.
    """
    if not isinstance(node, dict):
        raise ConditionTreeError(f"{path}: node must be an object")
    op_keys = [k for k in ("all_of", "any_of", "not") if k in node]
    if "leaf" in node:
        if node["leaf"] not in LEAF_TYPES:
            raise ConditionTreeError(f"{path}: unknown leaf type {node['leaf']!r}")
        if op_keys:
            raise ConditionTreeError(f"{path}: a leaf cannot also be an operator")
        if not _inside_condition and not node.get("id"):
            raise ConditionTreeError(f"{path}: every leaf condition needs a stable 'id'")
        return
    if len(op_keys) != 1:
        raise ConditionTreeError(f"{path}: expected exactly one of all_of/any_of/not")
    op = op_keys[0]
    inside = _inside_condition or (bool(node.get("id")) and path != "criteria")
    if op == "not":
        validate_tree(node["not"], f"{path}.not", inside)
        return
    children = node[op]
    if not isinstance(children, list) or not children:
        raise ConditionTreeError(f"{path}: {op} needs a non-empty list of children")
    for i, child in enumerate(children):
        validate_tree(child, f"{path}.{op}[{i}]", inside)


def criteria_tree(rules: dict) -> dict | None:
    """The canonical condition tree for a rules document, or ``None``."""
    native = rules.get("criteria")
    if native is not None:
        validate_tree(native)
        return native
    return compile_flat_rules(rules)


def required_diagnosis_codes(rules: dict) -> list[str]:
    """Codes of the required-diagnosis condition (drives the index date)."""
    flat = (rules.get("inclusion") or {}).get("diagnosis") or {}
    if flat.get("codes"):
        return list(flat["codes"])
    tree = rules.get("criteria")
    found: list[str] = []

    def walk(node):
        if found:
            return
        if not isinstance(node, dict):
            return
        if node.get("leaf") == "diagnosis" and node.get("codes"):
            found.extend(node["codes"])
            return
        for op in ("all_of", "any_of"):
            for child in node.get(op) or []:
                walk(child)
        if "not" in node:
            walk(node["not"])

    if tree:
        walk(tree)
    return found


def needed_kinds_codes(tree: dict | None) -> tuple[set[str], set[str]]:
    """Record kinds and lab codes the criteria read (normalization gate)."""
    kinds: set[str] = set()
    lab_codes: set[str] = set()

    def walk(node):
        if not isinstance(node, dict):
            return
        leaf = node.get("leaf")
        if leaf in ("diagnosis", "diagnosis_present"):
            kinds.add("diagnosis")
        elif leaf in ("lab_range", "lab_threshold"):
            kinds.add("lab")
            if node.get("code"):
                lab_codes.add(node["code"])
        elif leaf in ("medication", "medication_present"):
            kinds.add("medication")
        for op in ("all_of", "any_of"):
            for child in node.get(op) or []:
                walk(child)
        if "not" in node:
            walk(node["not"])

    if tree:
        walk(tree)
    return kinds, lab_codes


# ---------------------------------------------------------------------------
# semantic identity
# ---------------------------------------------------------------------------
def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _semantic_content(node: dict):
    if "leaf" in node:
        content = {k: v for k, v in node.items() if k not in NON_SEMANTIC_KEYS}
        return {"leaf": content}
    for op in ("all_of", "any_of"):
        if op in node:
            # AND/OR commute: child order is not semantic.
            children = sorted(_semantic_content_key(c) for c in node[op])
            return {op: children}
    if "not" in node:
        return {"not": _semantic_content_key(node["not"])}
    return {}


def _semantic_content_key(node: dict) -> str:
    return _canon(_semantic_content(node))


def semantic_key(node: dict) -> str:
    """Hash of the clinical content; labels and display names are ignored."""
    return hashlib.sha256(_semantic_content_key(node).encode()).hexdigest()[:16]


def _skeleton_content(node: dict):
    if "leaf" in node:
        return {"id": node.get("id")}
    for op in ("all_of", "any_of"):
        if op in node:
            return {op: sorted(_skeleton_content_key(c) for c in node[op])}
    if "not" in node:
        return {"not": _skeleton_content_key(node["not"])}
    return {}


def _skeleton_content_key(node: dict) -> str:
    return _canon(_skeleton_content(node))


def skeleton_key(node: dict) -> str:
    """Hash of tree structure + condition ids; parameters are ignored."""
    return hashlib.sha256(_skeleton_content_key(node).encode()).hexdigest()[:16]


def _label_of(node: dict) -> str | None:
    return node.get("label") or node.get("display_name") or node.get("description")


def _condition_nodes(tree: dict) -> dict[str, dict]:
    """Condition nodes keyed by their stable id (the outermost id-carrying
    node wins, so a ``not`` wrapper with an id represents the whole negated
    condition). The tree root's own id labels the tree, not a condition."""
    out: dict[str, dict] = {}

    def walk(node, is_root: bool = False):
        if not isinstance(node, dict):
            return
        node_id = node.get("id")
        if node_id and not is_root and node_id not in out:
            out[node_id] = node
            # Children of an identified condition are internal detail.
            return
        for op in ("all_of", "any_of"):
            for child in node.get(op) or []:
                walk(child)
        if "not" in node:
            walk(node["not"])

    walk(tree, is_root=True)
    return out


def diff_trees(old: dict | None, new: dict | None) -> dict:
    """Semantic diff of two condition trees, aligned by condition id.

    Leaf-level alignment is always reported for readability, but per-leaf
    mixing (``mixable``) is only allowed when the skeletons are identical:
    splicing fragments of two differently-shaped trees could manufacture a
    meaning neither revision ever had.
    """
    if old is None or new is None:
        return {
            "structure_changed": old is not new,
            "mixable": False,
            "conditions": [],
            "changed_count": 0,
        }
    old_nodes = _condition_nodes(old)
    new_nodes = _condition_nodes(new)
    conditions = []
    changed = 0
    for cid in sorted(set(old_nodes) | set(new_nodes)):
        o, n = old_nodes.get(cid), new_nodes.get(cid)
        if o is None:
            conditions.append(
                {
                    "condition_id": cid,
                    "change": "added",
                    "label_old": None,
                    "label_new": _label_of(n),
                    "old": None,
                    "new": _semantic_content(n),
                }
            )
            changed += 1
        elif n is None:
            conditions.append(
                {
                    "condition_id": cid,
                    "change": "removed",
                    "label_old": _label_of(o),
                    "label_new": None,
                    "old": _semantic_content(o),
                    "new": None,
                }
            )
            changed += 1
        else:
            same_semantics = semantic_key(o) == semantic_key(n)
            label_changed = _label_of(o) != _label_of(n)
            if same_semantics:
                conditions.append(
                    {
                        "condition_id": cid,
                        "change": "unchanged",
                        "label_only": label_changed,
                        "label_old": _label_of(o),
                        "label_new": _label_of(n),
                        "old": _semantic_content(o),
                        "new": _semantic_content(n),
                    }
                )
            else:
                conditions.append(
                    {
                        "condition_id": cid,
                        "change": "changed",
                        "label_old": _label_of(o),
                        "label_new": _label_of(n),
                        "old": _semantic_content(o),
                        "new": _semantic_content(n),
                    }
                )
                changed += 1
    structure_changed = skeleton_key(old) != skeleton_key(new)
    return {
        "structure_changed": structure_changed,
        # Per-leaf mixing is only meaningful over one shared skeleton.
        "mixable": not structure_changed,
        "conditions": conditions,
        "changed_count": changed,
    }


# ---------------------------------------------------------------------------
# three-state evaluation
# ---------------------------------------------------------------------------
def _verdict(value: str, reasons=None, evidence=None) -> dict:
    return {"verdict": value, "reasons": list(reasons or []), "evidence": list(evidence or [])}


def eval_tree(node: dict, data, index_date: date, verdicts: dict | None = None, _is_root: bool = True) -> dict:
    """Evaluate a condition node to true/false/unknown.

    ``data`` is a ``SubjectData`` (duck-typed: ``by_kind``). When ``verdicts``
    is given, every id-carrying condition node's verdict is recorded under its
    id (the tree root's own id is not a condition and is skipped).
    """
    if "leaf" in node:
        out = _eval_leaf(node, data, index_date)
    elif "all_of" in node:
        parts = [eval_tree(c, data, index_date, verdicts, False) for c in node["all_of"]]
        out = _combine_all(parts)
    elif "any_of" in node:
        parts = [eval_tree(c, data, index_date, verdicts, False) for c in node["any_of"]]
        out = _combine_any(parts)
    elif "not" in node:
        inner = eval_tree(node["not"], data, index_date, verdicts, False)
        flipped = {TRUE: FALSE, FALSE: TRUE, UNKNOWN: UNKNOWN}[inner["verdict"]]
        out = _verdict(flipped, inner["reasons"], inner["evidence"])
    else:  # pragma: no cover - validate_tree guards this
        raise ConditionTreeError("node is neither leaf nor operator")

    node_id = node.get("id")
    if node_id and not _is_root and verdicts is not None:
        verdicts[node_id] = out["verdict"]
    return out


def _combine_all(parts: list[dict]) -> dict:
    """AND: false dominates unknown, unknown dominates true.

    Reasons/evidence accumulate up to and including the first non-true child,
    mirroring the flat evaluator's early return. When a *later* false child is
    what actually decides the outcome, its reasons are appended too — the
    decisive reason must never be silent.
    """
    reasons: list[str] = []
    evidence: list[dict] = []
    included_false = False
    for part in parts:
        reasons.extend(part["reasons"])
        evidence.extend(part["evidence"])
        if part["verdict"] != TRUE:
            included_false = part["verdict"] == FALSE
            break
    first_false = next((p for p in parts if p["verdict"] == FALSE), None)
    if first_false is not None:
        if not included_false:
            reasons.extend(first_false["reasons"])
            evidence.extend(first_false["evidence"])
        return _verdict(FALSE, reasons, evidence)
    if any(p["verdict"] == UNKNOWN for p in parts):
        return _verdict(UNKNOWN, reasons, evidence)
    return _verdict(TRUE, reasons, evidence)


def _combine_any(parts: list[dict]) -> dict:
    """OR: true > unknown > false."""
    if any(p["verdict"] == TRUE for p in parts):
        first = next(p for p in parts if p["verdict"] == TRUE)
        return _verdict(TRUE, first["reasons"], first["evidence"])
    if any(p["verdict"] == UNKNOWN for p in parts):
        first = next(p for p in parts if p["verdict"] == UNKNOWN)
        return _verdict(UNKNOWN, first["reasons"], first["evidence"])
    reasons: list[str] = []
    evidence: list[dict] = []
    for part in parts:
        reasons.extend(part["reasons"])
        evidence.extend(part["evidence"])
    return _verdict(FALSE, reasons, evidence)


def _eval_leaf(node: dict, data, index_date: date) -> dict:
    leaf = node["leaf"]
    if leaf == "age_range":
        return _leaf_age(node, data, index_date)
    if leaf == "diagnosis":
        return _leaf_diagnosis_required(node, data, index_date)
    if leaf == "lab_range":
        return _leaf_lab_range(node, data, index_date)
    if leaf == "medication":
        return _leaf_medication_required(node, data, index_date)
    if leaf == "diagnosis_present":
        return _leaf_diagnosis_present(node, data)
    if leaf == "lab_threshold":
        return _leaf_lab_threshold(node, data, index_date)
    if leaf == "medication_present":
        return _leaf_medication_present(node, data)
    raise ConditionTreeError(f"unknown leaf {leaf!r}")  # pragma: no cover


def _leaf_age(node, data, index_date) -> dict:
    birth = None
    for _, n in data.by_kind("case"):
        birth = parse_date((n.normalized or {}).get("birth_date")) or birth
    age = age_on(birth, index_date)
    lo, hi = node.get("min"), node.get("max")
    if age is None:
        return _verdict(UNKNOWN, ["birth_date_missing"])
    if lo is not None and age < lo:
        return _verdict(FALSE, [f"age_below_min:{age}<{lo}"])
    if hi is not None and age > hi:
        return _verdict(FALSE, [f"age_above_max:{age}>{hi}"])
    return _verdict(TRUE, [f"age_ok:{age}"])


def _leaf_diagnosis_required(node, data, index_date) -> dict:
    codes = list(node.get("codes") or [])
    window = int(node.get("window_days", 3650))
    hits = []
    for s, n in data.by_kind("diagnosis"):
        code = (n.normalized or {}).get("code")
        d = parse_date((n.normalized or {}).get("diagnosis_date"))
        if code in codes and d and (index_date - d).days >= -window:
            hits.append((s, n, code, d))
    if not hits:
        if not data.by_kind("diagnosis"):
            return _verdict(UNKNOWN, ["diagnosis_missing"])
        return _verdict(FALSE, ["required_diagnosis_absent:" + ",".join(codes)])
    s, n, code, d = sorted(hits, key=lambda h: (h[3], h[0].id))[0]
    evidence = [{"source_record_id": s.id, "record_type": "diagnosis", "reason": f"diagnosis:{code}"}]
    return _verdict(TRUE, [f"diagnosis_ok:{code}"], evidence)


def _leaf_lab_range(node, data, index_date) -> dict:
    code = node["code"]
    before = int(node.get("window_days_before", 30))
    after = int(node.get("window_days_after", 0))
    vals = []
    for s, n in data.by_kind("lab"):
        if (n.normalized or {}).get("code") != code:
            continue
        d = parse_date((n.normalized or {}).get("collected_at"))
        val = n.converted_value
        if d is None or val is None:
            continue
        if -before <= (d - index_date).days <= after:
            vals.append((s, n, float(val), d))
    if not vals:
        return _verdict(UNKNOWN, [f"lab_missing:{code}"])
    lo, hi = node.get("min"), node.get("max")
    in_range = [
        v for v in vals if (lo is None or v[2] >= float(lo)) and (hi is None or v[2] <= float(hi))
    ]
    unit = node.get("study_unit") or ""
    if not in_range:
        s, n, val, d = sorted(vals, key=lambda v: (abs((v[3] - index_date).days), v[0].id))[0]
        evidence = [
            {
                "source_record_id": s.id,
                "record_type": "lab",
                "reason": f"lab_out_of_range:{code}={val}{n.study_unit}",
            }
        ]
        return _verdict(FALSE, [f"lab_out_of_range:{code}:{val}{unit}"], evidence)
    s, n, val, d = sorted(in_range, key=lambda v: (abs((v[3] - index_date).days), v[0].id))[0]
    evidence = [
        {
            "source_record_id": s.id,
            "record_type": "lab",
            "reason": f"lab:{code}={val}{n.study_unit}",
        }
    ]
    return _verdict(TRUE, [f"lab_ok:{code}"], evidence)


def _leaf_medication_required(node, data, index_date) -> dict:
    codes = list(node.get("codes") or [])
    after = int(node.get("within_days_after_index", 30))
    meds = []
    for s, n in data.by_kind("medication"):
        code = (n.normalized or {}).get("code")
        d = parse_date((n.normalized or {}).get("start_date"))
        if code in codes and d and -1 <= (d - index_date).days <= after:
            meds.append((s, n, code, d))
    if not meds:
        if not data.by_kind("medication"):
            return _verdict(UNKNOWN, ["medication_missing"])
        return _verdict(FALSE, ["required_medication_absent:" + ",".join(codes)])
    s, n, code, d = sorted(meds, key=lambda m: (m[3], m[0].id))[0]
    evidence = [{"source_record_id": s.id, "record_type": "medication", "reason": f"medication:{code}"}]
    return _verdict(TRUE, [f"medication_ok:{code}"], evidence)


def _leaf_diagnosis_present(node, data) -> dict:
    codes = list(node.get("codes") or [])
    for s, n in data.by_kind("diagnosis"):
        code = (n.normalized or {}).get("code")
        if code in codes:
            evidence = [
                {
                    "source_record_id": s.id,
                    "record_type": "diagnosis",
                    "reason": f"exclusion_diagnosis:{code}",
                }
            ]
            return _verdict(TRUE, [f"exclusion_diagnosis:{code}"], evidence)
    return _verdict(FALSE)


def _leaf_lab_threshold(node, data, index_date) -> dict:
    code = node["code"]
    window = int(node.get("window_days_before", 30))
    for s, n in data.by_kind("lab"):
        if (n.normalized or {}).get("code") != code or n.converted_value is None:
            continue
        d = parse_date((n.normalized or {}).get("collected_at"))
        if d and -window <= (d - index_date).days <= 0:
            val = float(n.converted_value)
            if node.get("min") is not None and val >= float(node["min"]):
                evidence = [
                    {
                        "source_record_id": s.id,
                        "record_type": "lab",
                        "reason": f"exclusion_lab:{code}={val}",
                    }
                ]
                return _verdict(TRUE, [f"exclusion_lab:{code}"], evidence)
            if node.get("max") is not None and val <= float(node["max"]):
                evidence = [
                    {
                        "source_record_id": s.id,
                        "record_type": "lab",
                        "reason": f"exclusion_lab:{code}={val}",
                    }
                ]
                return _verdict(TRUE, [f"exclusion_lab:{code}"], evidence)
    return _verdict(FALSE)


def _leaf_medication_present(node, data) -> dict:
    codes = list(node.get("codes") or [])
    for s, n in data.by_kind("medication"):
        code = (n.normalized or {}).get("code")
        if code in codes:
            evidence = [
                {
                    "source_record_id": s.id,
                    "record_type": "medication",
                    "reason": f"exclusion_medication:{code}",
                }
            ]
            return _verdict(TRUE, [f"exclusion_medication:{code}"], evidence)
    return _verdict(FALSE)


# ---------------------------------------------------------------------------
# endpoint eligibility (re-judged along the index-date dependency)
# ---------------------------------------------------------------------------
def eval_endpoints(rules: dict, data, index_date: date | None) -> dict:
    """Endpoint eligibility relative to the index date.

    Three states per endpoint: ``eligible`` / ``not_eligible`` / ``unknown``
    (no follow-up records at all). Every verdict carries evidence pointing at
    the source rows it was read from.
    """
    specs = rules.get("endpoints") or {}
    out: dict[str, dict] = {}
    if not specs:
        return out
    if index_date is None:
        for key in specs:
            out[key] = {"status": "unknown", "reason": "index_date_unavailable", "evidence": []}
        return out
    for key, spec in specs.items():
        visit_types = set(spec.get("visit_types") or ["follow_up"])
        visits = [
            (s, n)
            for s, n in data.by_kind("followup")
            if (n.normalized or {}).get("visit_type", "scheduled") in visit_types
        ]
        if not visits:
            out[key] = {"status": "unknown", "reason": "followup_records_missing", "evidence": []}
            continue
        lo, hi = (spec.get("window_days") or [0, 100000])
        lo, hi = int(lo), int(hi)
        required = set(spec.get("required_response") or [])
        qualifying = []
        for s, n in visits:
            d = parse_date((n.normalized or {}).get("visit_date"))
            resp = (n.normalized or {}).get("response")
            if d is not None and lo <= (d - index_date).days <= hi and (not required or resp in required):
                qualifying.append((s, n, d, resp))
        if qualifying:
            qualifying.sort(key=lambda q: (q[2], q[0].id))
            s, n, d, resp = qualifying[0]
            out[key] = {
                "status": "eligible",
                "reason": f"endpoint_met:{key}:{resp}@{d.isoformat()}",
                "evidence": [
                    {
                        "source_record_id": s.id,
                        "record_type": "followup",
                        "reason": f"endpoint:{key}:{resp}@{d.isoformat()}",
                    }
                ],
            }
        else:
            out[key] = {
                "status": "not_eligible",
                "reason": f"endpoint_not_met:{key}",
                "evidence": [
                    {
                        "source_record_id": s.id,
                        "record_type": "followup",
                        "reason": f"endpoint_window_miss:{key}",
                    }
                    for s, n in visits
                ],
            }
    return out


# ---------------------------------------------------------------------------
# change variables and mixed-rule construction (for minimal sufficient sets)
# ---------------------------------------------------------------------------
def change_variables(old_rules: dict, new_rules: dict, diff: dict) -> list[dict]:
    """The universe of atomic changes between two revisions.

    * one variable per changed condition — only when the trees share a
      skeleton (``diff['mixable']``);
    * a single ``criteria_block`` variable when the structure changed, so
      fragments of two trees are never spliced into a third meaning;
    * ``index_date`` / ``endpoints`` when those sections differ.
    """
    variables: list[dict] = []
    if diff.get("changed_count") or diff.get("structure_changed"):
        if diff.get("mixable"):
            for cond in diff["conditions"]:
                if cond["change"] == "changed":
                    variables.append(
                        {
                            "id": f"cond:{cond['condition_id']}",
                            "kind": "condition",
                            "condition_id": cond["condition_id"],
                            "label": cond.get("label_new") or cond.get("label_old") or cond["condition_id"],
                        }
                    )
        else:
            variables.append(
                {
                    "id": "criteria_block",
                    "kind": "criteria_block",
                    "label": "入排条件块（结构变化，整体替换）",
                }
            )
    if _canon(old_rules.get("index_date") or {}) != _canon(new_rules.get("index_date") or {}):
        variables.append({"id": "index_date", "kind": "index_date", "label": "索引日期定义"})
    if _canon(old_rules.get("endpoints") or {}) != _canon(new_rules.get("endpoints") or {}):
        variables.append({"id": "endpoints", "kind": "endpoints", "label": "终点定义"})
    return variables


def mix_rules(old_rules: dict, new_rules: dict, applied: list[str]) -> dict:
    """Build a hybrid rules document with the given change variables applied.

    The result always carries a native ``criteria`` tree (compiled from the
    old rules, with substitutions), so the hybrid is directly evaluable.
    """
    old_tree = criteria_tree(old_rules)
    new_tree = criteria_tree(new_rules)
    tree = copy.deepcopy(old_tree)
    mixed = {
        k: copy.deepcopy(v)
        for k, v in old_rules.items()
        if k not in ("inclusion", "exclusion", "criteria")
    }
    for var in applied:
        if var == "criteria_block":
            tree = copy.deepcopy(new_tree)
        elif var.startswith("cond:"):
            cid = var.split(":", 1)[1]
            replacement = _condition_nodes(new_tree).get(cid) if new_tree else None
            if replacement is None:
                raise ConditionTreeError(f"cannot mix unknown condition {cid!r}")
            tree = _replace_condition(tree, cid, replacement)
        elif var == "index_date":
            mixed["index_date"] = copy.deepcopy(new_rules.get("index_date") or {})
        elif var == "endpoints":
            mixed["endpoints"] = copy.deepcopy(new_rules.get("endpoints") or {})
        else:
            raise ConditionTreeError(f"unknown change variable {var!r}")
    if tree is not None:
        mixed["criteria"] = tree
    return mixed


def _replace_condition(node: dict, condition_id: str, replacement: dict) -> dict:
    """Return a copy of ``node`` with the id-matching condition substituted.

    Only called when both trees share a skeleton, so the position always
    exists and the replacement keeps the same id.
    """
    if node.get("id") == condition_id:
        return copy.deepcopy(replacement)
    node = copy.deepcopy(node)
    for op in ("all_of", "any_of"):
        if op in node:
            node[op] = [_replace_condition(c, condition_id, replacement) for c in node[op]]
    if "not" in node:
        node["not"] = _replace_condition(node["not"], condition_id, replacement)
    return node
