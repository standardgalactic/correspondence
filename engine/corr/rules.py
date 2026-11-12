"""Small declarative evaluator with three valued logic.

Results are True, False or None (unknown). A positive requirement passes only
on True. Absence of evidence is None, never False, unless a record is explicitly
negated. Operations: has, missing, eq, in, all, any, not, date_lt, date_lte,
date_gt, date_gte.
"""
from .util import parse_date

OPS = {"has", "missing", "eq", "in", "all", "any", "not", "date_lt", "date_lte", "date_gt", "date_gte"}


class Ctx:
    def __init__(self, evidence, project, goal, ref_date):
        self.evidence = evidence
        self.project = project
        self.goal = goal
        self.ref = ref_date
        self.binds = {}
        self.trace = []


def resolve(ref, ctx):
    if not (isinstance(ref, str) and ref.startswith("$")):
        return ref
    parts = ref[1:].split(".")
    head = parts[0]
    if head == "ref_date":
        return ctx.ref.isoformat()
    if head == "project":
        obj = ctx.project
        if len(parts) == 1:
            return obj["id"]
    elif head == "goal":
        obj = ctx.goal
    elif head in ctx.binds:
        obj = ctx.binds[head]
    else:
        return None
    for p in parts[1:]:
        if isinstance(obj, dict) and p in obj:
            obj = obj[p]
        else:
            return None
    return obj


def _matches(e, spec, ctx):
    if e["predicate"] != spec["predicate"]:
        return False
    pr = spec.get("project", "any")
    if pr != "any" and e.get("project") != resolve(pr, ctx):
        return False
    if "value_in" in spec:
        if str(e.get("value", "")).lower() not in [str(v).lower() for v in spec["value_in"]]:
            return False
    if "value_eq" in spec:
        if str(e.get("value")) != str(resolve(spec["value_eq"], ctx)):
            return False
    if spec.get("use", "plan") not in e.get("uses", []):
        return False
    if "max_age_days" in spec:
        age = (ctx.ref - parse_date(e["observed"])).days
        if age < 0 or age > spec["max_age_days"]:
            return False
    return True


def _note(ctx, op, result, detail):
    ctx.trace.append({"op": op, "result": result, "detail": detail})
    return result


def _kleene_all(vals):
    if any(v is False for v in vals):
        return False
    if any(v is None for v in vals):
        return None
    return True


def _kleene_any(vals):
    if any(v is True for v in vals):
        return True
    if any(v is None for v in vals):
        return None
    return False


def evaluate(expr, ctx):
    if not isinstance(expr, dict) or len(expr) != 1:
        raise ValueError(f"malformed expression: {expr!r}")
    op, arg = next(iter(expr.items()))
    if op == "all":
        return _kleene_all([evaluate(x, ctx) for x in arg])
    if op == "any":
        return _kleene_any([evaluate(x, ctx) for x in arg])
    if op == "not":
        v = evaluate(arg, ctx)
        return None if v is None else (not v)
    if op in ("has", "missing"):
        found = [e for e in ctx.evidence if _matches(e, arg, ctx) and not e.get("negated")]
        negated = [e for e in ctx.evidence if _matches(e, arg, ctx) and e.get("negated")]
        desc = f"{arg['predicate']}" + (f"@{resolve(arg['project'], ctx)}" if arg.get("project") else "")
        if op == "missing":
            return _note(ctx, op, not found, f"{desc}: {len(found)} matching record(s)")
        if found:
            best = sorted(found, key=lambda e: (e["observed"], e["id"]))[-1]
            if "bind" in arg:
                ctx.binds[arg["bind"]] = best
            return _note(ctx, op, True, f"{desc}: {best['id']}")
        if negated:
            return _note(ctx, op, False, f"{desc}: explicitly negated by {negated[0]['id']}")
        return _note(ctx, op, None, f"{desc}: no record, unknown")
    if op in ("eq", "in"):
        a, b = resolve(arg[0], ctx), resolve(arg[1], ctx)
        if a is None or b is None:
            return _note(ctx, op, None, f"unresolved operand in {arg!r}")
        res = (a == b) if op == "eq" else (a in b)
        return _note(ctx, op, res, f"{a!r} {op} {b!r}")
    if op.startswith("date_"):
        a, b = resolve(arg[0], ctx), resolve(arg[1], ctx)
        if a is None or b is None:
            return _note(ctx, op, None, f"unresolved date operand in {arg!r}")
        da, db = parse_date(a), parse_date(b)
        res = {"date_lt": da < db, "date_lte": da <= db, "date_gt": da > db, "date_gte": da >= db}[op]
        return _note(ctx, op, res, f"{da} {op[5:]} {db}")
    raise ValueError(f"unknown operation {op!r}")


def walk(expr):
    """Yield (op, arg) for every node of an expression tree."""
    if not isinstance(expr, dict) or len(expr) != 1:
        yield ("?", expr)
        return
    op, arg = next(iter(expr.items()))
    yield (op, arg)
    if op in ("all", "any"):
        for x in arg:
            yield from walk(x)
    elif op == "not":
        yield from walk(arg)
