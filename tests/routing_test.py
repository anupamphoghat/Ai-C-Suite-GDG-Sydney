"""Unit tests for routing plan validation.

The router is a live model deciding which executives get convened, so its
output is never trusted as-is. These tests cover what happens when it returns
something unusable -- the case that matters most on stage, because a router
that selects nobody must degrade to the full committee rather than produce an
empty run.

    python tests/routing_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "shared"))
sys.path.insert(0, str(REPO_ROOT / "services" / "orchestrator"))

from csuite_common.config import OrchestratorSettings  # noqa: E402

import engine as engine_module  # noqa: E402

PASS, FAIL = "\033[1;32m✓\033[0m", "\033[1;31m✗\033[0m"
failures: list[str] = []
ALL = ["cfo", "cso", "cmo", "chro", "cto"]


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}  {detail}")
        failures.append(label)


def make_engine(**overrides):
    settings = OrchestratorSettings(
        active_roles_csv=",".join(ALL),
        agent_urls_json="{}",
        **overrides,
    )

    class _Llm:
        model_name = "stub-model"

    return engine_module.RunEngine(settings=settings, decision_log=None, llm=_Llm())


def plan_output(choices):
    """choices: list of (role, selected, order)."""
    return engine_module._PlanOutput(
        interpretation="i",
        strategy="s",
        routing=[
            engine_module._RoleChoice(role=r, selected=sel, reason="because", order=o)
            for r, sel, o in choices
        ],
    )


def main() -> int:
    print("\n-- Normal routing --")
    eng = make_engine()
    plan = eng._validate_plan(
        plan_output([("cto", True, 1), ("cfo", True, 2), ("cso", False, 0),
                     ("cmo", False, 0), ("chro", False, 0)]),
        ALL,
    )
    check("engages exactly the selected roles", plan.engaged_roles == ["cto", "cfo"],
          str(plan.engaged_roles))
    check("keeps an entry for every available role", len(plan.routing) == 5)
    check("skipped roles retain their reason", all(r.reason for r in plan.skipped))
    check("sequences are 1..n", [r.sequence for r in plan.engaged] == [1, 2])

    print("\n-- Router returns nobody --")
    plan = eng._validate_plan(
        plan_output([(r, False, 0) for r in ALL]), ALL
    )
    check("falls back to the full committee", plan.engaged_roles == ALL, str(plan.engaged_roles))
    check("fallback is stated, not silent", "routing" in plan.strategy.lower(), plan.strategy)

    print("\n-- Router hallucinates a role --")
    plan = eng._validate_plan(
        plan_output([("cto", True, 1), ("cdo", True, 2), ("cfo", True, 3),
                     ("cso", False, 0), ("cmo", False, 0), ("chro", False, 0)]),
        ALL,
    )
    check("unknown role dropped", "cdo" not in plan.engaged_roles, str(plan.engaged_roles))
    check("known roles still engaged", plan.engaged_roles == ["cto", "cfo"],
          str(plan.engaged_roles))
    check("still one entry per available role", len(plan.routing) == 5)

    print("\n-- Router duplicates a role --")
    plan = eng._validate_plan(
        plan_output([("cto", True, 1), ("cto", True, 2), ("cfo", True, 3),
                     ("cso", False, 0), ("cmo", False, 0), ("chro", False, 0)]),
        ALL,
    )
    check("duplicates collapsed", plan.engaged_roles == ["cto", "cfo"], str(plan.engaged_roles))

    print("\n-- Router omits a role entirely --")
    plan = eng._validate_plan(
        plan_output([("cto", True, 1), ("cfo", True, 2)]), ALL
    )
    check("missing roles are added as not-engaged", len(plan.routing) == 5)
    missing = [r for r in plan.routing if r.role == "chro"][0]
    check("missing role is not silently engaged", missing.selected is False)
    check("missing role still carries an explanation", bool(missing.reason), missing.reason)

    print("\n-- Router exceeds the maximum --")
    eng_max = make_engine(routing_max_roles=2)
    plan = eng_max._validate_plan(
        plan_output([(r, True, i + 1) for i, r in enumerate(ALL)]), ALL
    )
    check("capped at routing_max_roles", len(plan.engaged) == 2, str(plan.engaged_roles))
    check("cap keeps the highest-priority roles", plan.engaged_roles == ["cfo", "cso"],
          str(plan.engaged_roles))

    print("\n-- Order is honoured, not input order --")
    plan = eng._validate_plan(
        plan_output([("cfo", True, 3), ("cso", True, 1), ("cmo", True, 2),
                     ("chro", False, 0), ("cto", False, 0)]),
        ALL,
    )
    check("engaged roles follow the router's order",
          plan.engaged_roles == ["cso", "cmo", "cfo"], str(plan.engaged_roles))

    print("\n-- routing_mode=all --")
    eng_all = make_engine(routing_mode="all")
    plan = eng_all._plan_from_roles(
        ALL, interpretation="i", strategy="s", reason_for_selected="r", model_used="none"
    )
    check("engages everyone", plan.engaged_roles == ALL)
    check("each role still carries a reason", all(r.reason for r in plan.routing))

    print()
    if failures:
        print(f"\033[1;31m{len(failures)} check(s) failed:\033[0m")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\033[1;32mAll checks passed.\033[0m\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
