"""Serve a service with the stub model patched in.

    python tests/serve_stubbed.py executive   --port 8101   # needs EXEC_ROLE
    python tests/serve_stubbed.py orchestrator --port 8080

Patching happens before the service module is imported, so the service picks
up the stub without any test-only code living in the service itself.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "shared"))
sys.path.insert(0, str(REPO_ROOT / "tests"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=["executive", "orchestrator"])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    import csuite_common.llm as llm_module
    from stub_model import StubGeminiClient

    llm_module.GeminiClient = StubGeminiClient  # type: ignore[assignment]

    service_dir = REPO_ROOT / "services" / args.service
    sys.path.insert(0, str(service_dir))

    import main as service_main  # noqa: F401  (imports the patched GeminiClient)

    import uvicorn

    uvicorn.run(service_main.app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
