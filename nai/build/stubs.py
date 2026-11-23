"""Deterministic Python contract stub generation and signature extraction (BLD-2, BLD-1)."""

from __future__ import annotations

import ast
import keyword
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StubFile:
    rel_path: str
    content: str


@dataclass(frozen=True)
class StubResult:
    files: list[StubFile]
    module_import: str
    package_name: str


def slugify_module(name: str) -> str:
    """Convert a hyphenated node/project name into a valid Python identifier."""
    s = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip()).lower()
    s = re.sub(r"_+", "_", s).strip("_") or "component"
    if s[0].isdigit() or keyword.iskeyword(s):
        s = f"mod_{s}"
    return s


def project_package_name(project: dict[str, Any]) -> str:
    settings = project.get("settings") or {}
    if settings.get("package_name"):
        return slugify_module(str(settings["package_name"]))
    return slugify_module(project.get("name") or "app")


def node_stub_rel_path(project: dict[str, Any], node: dict[str, Any]) -> str:
    pkg = project_package_name(project)
    mod = slugify_module(node["name"])
    return f"src/{pkg}/{mod}.py"


def entities_rel_path(project: dict[str, Any]) -> str:
    pkg = project_package_name(project)
    return f"src/{pkg}/entities.py"


def _render_entities_file(entities: list[dict[str, Any]]) -> str:
    lines = [
        '"""Shared contract entities generated from frozen parent contract (BLD-2)."""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "from typing import Any",
        "",
    ]
    for ent in entities:
        name = str(ent.get("name") or "Entity").strip()
        fields = list(ent.get("fields") or [])
        # Put required fields before optional fields for valid dataclass ordering
        req_fields = [f for f in fields if f.get("required", True)]
        opt_fields = [f for f in fields if not f.get("required", True)]

        lines.append("@dataclass")
        lines.append(f"class {name}:")
        doc_parts = [f"{f.get('name')}: {f.get('description', '')}".strip() for f in fields if f.get("description")]
        if doc_parts:
            lines.append(f'    """{"; ".join(doc_parts)}"""')
        if not fields:
            lines.append("    pass")
        else:
            for f in req_fields:
                fname = slugify_module(str(f["name"]))
                ftype = str(f.get("type") or "Any")
                lines.append(f"    {fname}: {ftype}")
            for f in opt_fields:
                fname = slugify_module(str(f["name"]))
                ftype = str(f.get("type") or "Any")
                lines.append(f"    {fname}: {ftype} | None = None")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _normalize_def_header(prov: dict[str, Any]) -> tuple[str, str]:
    """Return (def_line, func_name) from either a signature string or structured dict."""
    sig = str(prov.get("signature") or "").strip()
    if sig:
        clean = sig.rstrip(":").strip()
        if not clean.startswith(("def ", "async def ")):
            clean = f"def {clean}"
        m = re.match(r"^(?:async\s+)?def\s+([a-zA-Z_][a-zA-Z0-9_]*)", clean)
        fname = m.group(1) if m else slugify_module(str(prov.get("name") or "fn"))
        # Validate syntax by compiling a stub
        candidate = f"{clean}:\n    pass\n"
        try:
            ast.parse(candidate)
            return clean, fname
        except SyntaxError:
            pass

    fname = slugify_module(str(prov.get("name") or "operation"))
    params = prov.get("params") or []
    param_strs: list[str] = []
    for p in params:
        if isinstance(p, dict):
            pname = slugify_module(str(p.get("name") or "arg"))
            ptype = str(p.get("type") or "Any")
            param_strs.append(f"{pname}: {ptype}")
        else:
            param_strs.append(f"{slugify_module(str(p))}: Any")
    if not param_strs and prov.get("input_entities"):
        for idx, ent in enumerate(prov["input_entities"]):
            arg = slugify_module(str(ent))
            if idx > 0 and arg == slugify_module(str(prov["input_entities"][0])):
                arg = f"{arg}_{idx}"
            param_strs.append(f"{arg}: {ent}")

    ret_type = str(prov.get("returns") or "")
    if not ret_type and prov.get("output_entities"):
        outs = [str(x) for x in prov["output_entities"]]
        ret_type = outs[0] if len(outs) == 1 else f"tuple[{', '.join(outs)}]"
    ret_annot = f" -> {ret_type}" if ret_type else ""
    return f"def {fname}({', '.join(param_strs)}){ret_annot}", fname


def generate_contract_stubs(
    project: dict[str, Any],
    node: dict[str, Any],
    parent_contract: dict[str, Any] | None,
) -> StubResult:
    """Generate deterministic Python contract stub files for a component node (BLD-2)."""
    pkg = project_package_name(project)
    mod = slugify_module(node["name"])
    contract = parent_contract or node.get("contract") or {}
    entities = list(contract.get("entities") or [])

    files: list[StubFile] = [
        StubFile(rel_path="src/__init__.py", content=""),
        StubFile(rel_path=f"src/{pkg}/__init__.py", content=f'"""Package {pkg}."""\n'),
    ]

    entity_names: list[str] = []
    if entities:
        entity_names = [str(e["name"]).strip() for e in entities if e.get("name")]
        files.append(
            StubFile(
                rel_path=entities_rel_path(project),
                content=_render_entities_file(entities),
            )
        )

    comp_entry: dict[str, Any] | None = None
    for comp in contract.get("components") or []:
        if str(comp.get("name", "")).strip() == node["name"]:
            comp_entry = comp
            break

    provides = list((comp_entry or {}).get("provides") or [])
    lines = [
        f'"""Contract stub for component `{node["name"]}` (BLD-2)."""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
    ]
    if entity_names:
        lines.append(f"from .entities import {', '.join(entity_names)}")
        lines.append("")

    if not provides:
        lines.append("# No functions declared in contract `provides`; placeholder stub.")
        lines.append("def ping() -> bool:")
        lines.append('    """Placeholder contract entry."""')
        lines.append('    raise NotImplementedError("Contract stub not implemented")')
        lines.append("")
    else:
        for prov in provides:
            def_line, fname = _normalize_def_header(prov)
            desc = str(prov.get("description") or f"Contract operation `{fname}`.").strip()
            inputs = ", ".join(str(x) for x in (prov.get("input_entities") or ()))
            outputs = ", ".join(str(x) for x in (prov.get("output_entities") or ()))
            errors = ", ".join(str(x) for x in (prov.get("errors") or ()))
            doc_lines = [desc]
            if inputs:
                doc_lines.append(f"Inputs: {inputs}")
            if outputs:
                doc_lines.append(f"Outputs: {outputs}")
            if errors:
                doc_lines.append(f"Errors: {errors}")

            lines.append(f"{def_line}:")
            if len(doc_lines) == 1:
                lines.append(f'    """{doc_lines[0]}"""')
            else:
                lines.append('    """')
                for dl in doc_lines:
                    lines.append(f"    {dl}")
                lines.append('    """')
            lines.append(f'    raise NotImplementedError("Contract stub for {fname}")')
            lines.append("")

    files.append(
        StubFile(
            rel_path=node_stub_rel_path(project, node),
            content="\n".join(lines).rstrip() + "\n",
        )
    )
    return StubResult(files=files, module_import=f"{pkg}.{mod}", package_name=pkg)


def extract_stub_signatures(source: str) -> str:
    """Extract ONLY signatures, class definitions, and docstrings from Python source (BLD-1).

    Never includes implementation bodies of dependency functions/methods.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""

    out: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sig_src = _render_func_stub(node, indent="")
            out.append(sig_src)
        elif isinstance(node, ast.ClassDef):
            out.append(_render_class_stub(node))
    return "\n\n".join(out).strip()


def _render_func_stub(node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str) -> str:
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    args_src = ast.unparse(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    lines = [f"{indent}{prefix}{node.name}({args_src}){ret}:"]
    doc = ast.get_docstring(node)
    if doc:
        lines.append(f'{indent}    """{doc}"""')
    lines.append(f"{indent}    ...")
    return "\n".join(lines)


def _render_class_stub(cls_node: ast.ClassDef) -> str:
    bases = ", ".join(ast.unparse(b) for b in cls_node.bases)
    header = f"class {cls_node.name}({bases}):" if bases else f"class {cls_node.name}:"
    lines = [header]
    doc = ast.get_docstring(cls_node)
    if doc:
        lines.append(f'    """{doc}"""')
    body_added = False
    for item in cls_node.body:
        if isinstance(item, ast.AnnAssign):
            lines.append(f"    {ast.unparse(item)}")
            body_added = True
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(_render_func_stub(item, indent="    "))
            body_added = True
    if not body_added and not doc:
        lines.append("    ...")
    return "\n".join(lines)


def validate_stub_imports(
    workspace_root: Path,
    module_import: str,
    run_cmd: Any,
    python_exe: str | None = None,
) -> tuple[bool, str]:
    """Check generated stubs compile and import cleanly (BLD-2)."""
    py = python_exe or "python3"
    cmd = f'{py} -c "import sys; sys.path.insert(0, \'src\'); import {module_import}"'
    rc, out = run_cmd(cmd)
    return rc == 0, out

