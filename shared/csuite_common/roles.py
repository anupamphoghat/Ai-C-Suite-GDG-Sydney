"""C-suite role registry and skill/context loading.

Role definitions live in ``agents/<role>/SKILL.md`` as front-mattered
markdown, and shared company context in ``context/*.md``. Adding a sixth
executive means adding a directory and one registry entry -- no code change
in either service.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoleSpec:
    key: str
    title: str
    short_title: str
    accent: str  # dashboard colour
    lens: str  # one-line description of what this executive is accountable for


ROLE_REGISTRY: Dict[str, RoleSpec] = {
    "cfo": RoleSpec(
        key="cfo",
        title="Chief Financial Officer",
        short_title="CFO",
        accent="#2E7D32",
        lens="capital allocation, unit economics, revenue and margin risk",
    ),
    "cso": RoleSpec(
        key="cso",
        title="Chief Strategy Officer",
        short_title="CSO",
        accent="#1565C0",
        lens="market positioning, competitive response, expansion sequencing",
    ),
    "cmo": RoleSpec(
        key="cmo",
        title="Chief Marketing Officer",
        short_title="CMO",
        accent="#AD1457",
        lens="demand generation, messaging, funnel efficiency and brand risk",
    ),
    "chro": RoleSpec(
        key="chro",
        title="Chief People Officer",
        short_title="CHRO",
        accent="#EF6C00",
        lens="talent capacity, attrition risk, org design and culture",
    ),
    "cto": RoleSpec(
        key="cto",
        title="Chief Technology Officer",
        short_title="CTO",
        accent="#6A1B9A",
        lens="architecture, reliability, security posture and technical debt",
    ),
}


def get_role(role_key: str) -> RoleSpec:
    key = (role_key or "").strip().lower()
    if key not in ROLE_REGISTRY:
        raise KeyError(
            f"Unknown role '{role_key}'. Known roles: {', '.join(sorted(ROLE_REGISTRY))}."
        )
    return ROLE_REGISTRY[key]


_FRONT_MATTER = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


def load_skill(role_key: str, agents_dir: str) -> str:
    """Read ``agents/<role>/SKILL.md``, stripping YAML front matter."""
    path = Path(agents_dir) / role_key / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"No SKILL.md for role '{role_key}' at {path}. Expected "
            f"{agents_dir}/{role_key}/SKILL.md."
        )
    return _FRONT_MATTER.sub("", path.read_text(encoding="utf-8")).strip()


def load_context(context_dir: str) -> str:
    """Concatenate every markdown file in the shared context directory."""
    directory = Path(context_dir)
    if not directory.is_dir():
        logger.warning("Context directory %s not found; proceeding without it", directory)
        return ""
    parts: List[str] = []
    for path in sorted(directory.glob("*.md")):
        parts.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts)


def number_lines(text: str) -> str:
    """Prefix every line with ``L<n>|`` so agents can cite precisely.

    This is the mechanism that makes citations verifiable: a reviewer on the
    dashboard can click a citation and see exactly which source lines the
    claim rests on.
    """
    lines = text.splitlines()
    width = max(len(str(len(lines))), 2)
    return "\n".join(f"L{str(i + 1).rjust(width)}| {line}" for i, line in enumerate(lines))


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
