#!/usr/bin/env python3
"""devswarm v1 entry point: kept so the v1 command line still works. The code lives in the `nai` package."""
from nai.cli import build_parser, main  # noqa: F401
from nai.coder import Coder  # noqa: F401
from nai.llm import LLM  # noqa: F401
from nai.orchestrator import Orchestrator  # noqa: F401
from nai.textutil import msg_to_dict, parse_args, preview, strip_think, truncate  # noqa: F401
from nai.workspace import Workspace  # noqa: F401

if __name__ == "__main__":
    main()
