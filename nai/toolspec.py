"""OpenAI-style schemas for the v1 workspace and orchestrator tools (v1 'Tool catalog')."""
from __future__ import annotations


def fn(name: str, desc: str, props: dict, required=()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(required)},
        },
    }


def S(desc: str) -> dict:
    return {"type": "string", "description": desc}


READ_FILE = fn("read_file", "Read a text file from the workspace. Optionally a 1-based inclusive line range.",
               {"path": S("Relative path"), "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"])
WRITE_FILE = fn("write_file", "Create or overwrite a file with the full given content. Parent directories are created.",
                {"path": S("Relative path"), "content": S("Complete file content")}, ["path", "content"])
EDIT_FILE = fn("edit_file", "Replace one exact occurrence of old_str with new_str in a file. old_str must be unique.",
               {"path": S("Relative path"), "old_str": S("Exact text to replace (must occur once)"), "new_str": S("Replacement text")},
               ["path", "old_str", "new_str"])
LIST_FILES = fn("list_files", "List the directory tree (ignores .git, node_modules, venvs, caches).",
                {"path": S("Relative directory, default '.'"), "max_depth": {"type": "integer"}})
RUN_COMMAND = fn("run_command", "Run a bash command in the workspace root: install deps, build, run tests, etc. Non-interactive; must terminate.",
                 {"command": S("Bash command"), "timeout_seconds": {"type": "integer", "description": "Optional timeout override"}},
                 ["command"])

CODER_TOOLS_BASE = [READ_FILE, WRITE_FILE, EDIT_FILE, LIST_FILES]

DELEGATE = fn("delegate_to_coder",
              "Give a focused implementation task to the coder model. It writes/edits files in the workspace itself and returns a summary. "
              "Be explicit: file paths, signatures, behaviours, edge cases, which tests should pass.",
              {"task": S("Detailed task description"),
               "context_files": {"type": "array", "items": {"type": "string"},
                                 "description": "Workspace files whose contents are shown to the coder up front"}},
              ["task"])
CONSULT = fn("consult_reasoner",
             "Send a message to a named, persistent conversation with another instance of the reasoning model (no tools). "
             "Use distinct ids for distinct roles, e.g. 'architect', 'reviewer', 'debugger'. The conversation remembers earlier turns.",
             {"conversation_id": S("Name of the conversation"), "message": S("Your question or request"),
              "context_files": {"type": "array", "items": {"type": "string"}, "description": "Workspace files to include verbatim"},
              "reset": {"type": "boolean", "description": "Start this conversation over"}},
             ["conversation_id", "message"])
UPDATE_PLAN = fn("update_plan", "Overwrite the project plan/checklist (markdown) stored in .agent/plan.md. Survives context resets.",
                 {"content": S("Full markdown plan with [ ] / [x] checkboxes")}, ["content"])
FINISH = fn("finish", "End the run. Only call after the spec is implemented and tests/build were run successfully.",
            {"status": {"type": "string", "enum": ["success", "partial", "blocked"]}, "summary": S("What was built, how to run it, caveats")},
            ["status", "summary"])

ORCH_TOOLS = [READ_FILE, WRITE_FILE, EDIT_FILE, LIST_FILES, RUN_COMMAND, DELEGATE, CONSULT, UPDATE_PLAN, FINISH]
