"""Layered author preferences with a fixed resolution order.

Hard constraints apply first, then occasion settings, project preferences and
author defaults. Two hard constraints that disagree, or two soft ones of equal
scope and priority that disagree, are a conflict.
"""
from .util import parse_date

UNION_KEYS = {"forbid_chars", "forbid_phrases"}
LEVEL = {"global": 0, "project": 1, "occasion": 2}


class PrefConflict(ValueError):
    pass


def _applies(p, occasion, project, ref):
    scope = p["scope"]
    if scope == "global":
        ok = True
    elif scope.startswith("occasion:"):
        ok = scope.split(":", 1)[1] == occasion
    elif scope.startswith("project:"):
        ok = scope.split(":", 1)[1] == project
    else:
        ok = False
    if ok and p.get("effective_from") and ref < parse_date(p["effective_from"]):
        ok = False
    if ok and p.get("effective_to") and ref > parse_date(p["effective_to"]):
        ok = False
    return ok


def resolve(prefs, occasion, project, ref):
    items = [p for p in prefs if _applies(p, occasion, project, ref)]
    keys = sorted({p["key"] for p in items})
    out = {}
    for key in keys:
        group = [p for p in items if p["key"] == key]
        if key in UNION_KEYS:
            vals = sorted({v for p in group for v in p["value"]})
            out[key] = {"value": vals, "source": sorted(p["id"] for p in group)}
            continue
        hard = [p for p in group if p.get("hard")]
        if hard:
            distinct = {repr(p["value"]) for p in hard}
            if len(distinct) > 1:
                raise PrefConflict(f"hard constraints disagree on {key!r}: {sorted(p['id'] for p in hard)}")
            out[key] = {"value": hard[0]["value"], "source": [hard[0]["id"]]}
            continue

        def rank(p):
            return (LEVEL[p["scope"].split(":", 1)[0]], p.get("priority", 0))

        top = max(rank(p) for p in group)
        winners = [p for p in group if rank(p) == top]
        if len({repr(p["value"]) for p in winners}) > 1:
            raise PrefConflict(f"equal authority preferences disagree on {key!r}: {sorted(p['id'] for p in winners)}")
        out[key] = {"value": winners[0]["value"], "source": [winners[0]["id"]]}
    return out


def values(resolved):
    return {k: v["value"] for k, v in resolved.items()}
