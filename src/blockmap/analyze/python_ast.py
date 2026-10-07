"""Static analysis of one Python file: blocks, intra-file calls, imports, file I/O.

Everything here is deterministic and LLM-free. Line ranges are exact; I/O detection
is heuristic and every hit carries the line it came from so a reader can check it.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

PATH_EXTS = (
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".md",
    ".html",
    ".log",
    ".h5",
    ".hdf5",
    ".npz",
    ".npy",
    ".pt",
    ".pth",
    ".ckpt",
    ".pkl",
    ".pickle",
    ".parquet",
    ".feather",
    ".arrow",
    ".zarr",
    ".safetensors",
    ".bin",
    ".onnx",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".gif",
    ".tar",
    ".gz",
    ".zip",
    ".db",
    ".sqlite",
    ".xlsx",
    ".nc",
    ".tif",
    ".tiff",
)
WRITE_METHODS = {
    "to_csv",
    "to_parquet",
    "to_json",
    "to_pickle",
    "to_hdf",
    "to_excel",
    "to_feather",
    "write_text",
    "write_bytes",
    "savefig",
    "save_pretrained",
    "dump_json",
}
WRITE_FUNCS = {
    "save",
    "savez",
    "savez_compressed",
    "savetxt",
    "imsave",
    "imwrite",
    "dump",
    "safe_dump",
    "copy",
    "copy2",
    "copyfile",
    "move",
}
READ_METHODS = {"read_text", "read_bytes", "glob", "rglob", "iterdir"}
READ_FUNCS = {
    "read_csv",
    "read_parquet",
    "read_json",
    "read_pickle",
    "read_hdf",
    "read_excel",
    "read_feather",
    "read_table",
    "load",
    "loadtxt",
    "genfromtxt",
    "imread",
    "safe_load",
    "load_dataset",
    "snapshot_download",
    "hf_hub_download",
    "from_pretrained",
    "load_from_disk",
}
HF_FUNCS = {"load_dataset", "snapshot_download", "hf_hub_download", "from_pretrained"}
OUT_WORDS = ("out", "save", "dest", "write", "export", "report", "fig", "plot")
IN_WORDS = ("in", "input", "load", "src", "source", "read", "data", "feature", "probe", "ckpt", "checkpoint", "config", "cache")


@dataclass
class IOHit:
    path: str
    mode: str  # "read" | "write" | "mention"
    line: int
    how: str

    def as_dict(self) -> dict:
        return {"path": self.path, "mode": self.mode, "line": self.line, "how": self.how}


@dataclass
class FileFacts:
    path: str
    lines: int
    docstring: str = ""
    blocks: list[dict] = field(default_factory=list)
    calls: list[list[int]] = field(default_factory=list)
    imports: dict[str, str] = field(default_factory=dict)  # local name -> "module" or "module.attr"
    io: list[IOHit] = field(default_factory=list)
    cli_args: list[dict] = field(default_factory=list)
    has_main_guard: bool = False
    parse_error: str | None = None


def looks_like_path(s: str) -> bool:
    if not s or len(s) > 300 or "\n" in s or " " in s.strip() or s.startswith(("-", "http://", "https://")):
        return False
    low = s.lower().split("?")[0]
    if low.endswith(PATH_EXTS) or any(low.endswith(e + "}") for e in PATH_EXTS):
        return True
    return "/" in s and not s.startswith("/") and re.fullmatch(r"[\w.{}*\-/~]+", s) is not None


def _end(node: ast.AST) -> int:
    return max(getattr(n, "end_lineno", 0) or 0 for n in ast.walk(node))


def _first_para(doc: str | None) -> str:
    if not doc:
        return ""
    return doc.strip().split("\n\n", 1)[0].replace("\n", " ").strip()


def _is_main_guard(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )


def _blocks(tree: ast.Module, n_lines: int) -> list[dict]:
    """Tile the file into contiguous blocks: setup runs, each top-level def/class, the main guard."""
    blocks: list[dict] = []
    run_start, run_has_code = 1, False
    for node in tree.body:
        is_def = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        if is_def or _is_main_guard(node):
            decos = getattr(node, "decorator_list", None) or []
            start = decos[0].lineno if decos else node.lineno
            if run_has_code and start > run_start:
                blocks.append({"start": run_start, "end": start - 1, "name": None, "type": "setup"})
            elif blocks and start > run_start:
                pass  # blank gap: folded into the previous block below
            entry = {
                "start": start,
                "end": _end(node),
                "type": "guard" if not is_def else ("class" if isinstance(node, ast.ClassDef) else "def"),
                "name": "__main__" if not is_def else node.name,
                "_node": node,
            }
            if is_def:
                entry["doc"] = _first_para(ast.get_docstring(node))
            blocks.append(entry)
            run_start, run_has_code = entry["end"] + 1, False
        else:
            run_has_code = True
    if run_has_code or not blocks:
        blocks.append({"start": run_start, "end": n_lines, "name": None, "type": "setup"})
    for i in range(len(blocks) - 1):
        blocks[i]["end"] = blocks[i + 1]["start"] - 1
    blocks[0]["start"] = 1
    blocks[-1]["end"] = max(n_lines, blocks[-1]["start"])
    for i, b in enumerate(blocks):
        b["id"] = i
    return blocks


def _names_used(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


class _PathEval:
    """Turn an expression into a path template like 'results/{model}/x.csv', or None."""

    def __init__(self, consts: dict[str, str], argdefaults: dict[str, str]):
        self.consts = consts
        self.argdefaults = argdefaults
        self.locals: dict[str, str] = {}

    def __call__(self, node: ast.AST | None, depth: int = 0) -> str | None:
        if node is None or depth > 8:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                else:
                    inner = self(v.value, depth + 1) if isinstance(v, ast.FormattedValue) else None
                    parts.append(inner if inner and "/" in inner else "{" + _short(v) + "}")
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = self(node.left, depth + 1), self(node.right, depth + 1)
            if left is None and right is None:
                return None
            return f"{left or '{' + _short(node.left) + '}'}/{right or '{' + _short(node.right) + '}'}"
        if isinstance(node, ast.Name):
            if node.id in self.locals:
                return self.locals[node.id]
            if node.id in self.consts:
                return self.consts[node.id]
            # a parameter named like a CLI flag (features_dir <- --features-dir) usually carries its value
            return self.argdefaults.get(node.id)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in {"args", "opts", "cfg", "config", "params"}:
                return self.argdefaults.get(node.attr)
            if node.attr == "parent":
                inner = self(node.value, depth + 1)
                return inner.rsplit("/", 1)[0] if inner and "/" in inner else None
            return None
        if isinstance(node, ast.Call):
            fn = _callee(node)
            if fn in {
                "Path",
                "PurePath",
                "join",
                "expanduser",
                "resolve",
                "absolute",
                "str",
                "fspath",
                "with_suffix",
                "with_name",
            }:
                if fn == "join" and len(node.args) >= 2:
                    parts = [self(a, depth + 1) or "{" + _short(a) + "}" for a in node.args]
                    return "/".join(parts)
                target = node.args[0] if node.args else (node.func.value if isinstance(node.func, ast.Attribute) else None)
                if fn == "with_suffix" and isinstance(node.func, ast.Attribute):
                    base = self(node.func.value, depth + 1)
                    suf = self(node.args[0], depth + 1) if node.args else ""
                    return base.rsplit(".", 1)[0] + (suf or "") if base else None
                if fn in {"expanduser", "resolve", "absolute"} and isinstance(node.func, ast.Attribute):
                    return self(node.func.value, depth + 1)
                return self(target, depth + 1)
            if fn == "format" and isinstance(node.func, ast.Attribute):
                base = self(node.func.value, depth + 1)
                return re.sub(r"\{[^}]*\}", lambda m: m.group(0) if m.group(0) != "{}" else "{x}", base) if base else None
        return None


def _short(node: ast.AST) -> str:
    try:
        s = ast.unparse(node)
    except (ValueError, RecursionError):  # pragma: no cover - unparse is total on parsed trees
        return "x"
    s = re.sub(r"^(args|self|cfg|opts)\.", "", s)
    s = re.sub(r"[^\w.]+", "_", s).strip("_")
    return s[:24] or "x"


def _callee(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _callee_owner(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        v = f.value
        if isinstance(v, ast.Name):
            return v.id
        if isinstance(v, ast.Attribute):
            return v.attr
    return ""


def _mode_arg(call: ast.Call, pos: int) -> str | None:
    if len(call.args) > pos and isinstance(call.args[pos], ast.Constant) and isinstance(call.args[pos].value, str):
        return call.args[pos].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return None


def _collect_argparse(tree: ast.Module) -> tuple[list[dict], dict[str, str]]:
    args, defaults = [], {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and _callee(n) == "add_argument":
            flags = [a.value for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if not flags:
                continue
            dest = next((kw.value.value for kw in n.keywords if kw.arg == "dest" and isinstance(kw.value, ast.Constant)), None)
            long = next((f for f in flags if f.startswith("--")), flags[0])
            dest = dest or long.lstrip("-").replace("-", "_")
            default = None
            for kw in n.keywords:
                if kw.arg == "default":
                    if isinstance(kw.value, ast.Constant):
                        default = kw.value.value
                    elif isinstance(kw.value, ast.Call) and kw.value.args and isinstance(kw.value.args[0], ast.Constant):
                        default = kw.value.args[0].value  # Path("x")
            args.append(
                {
                    "flag": long,
                    "dest": dest,
                    "default": default if isinstance(default, (str, int, float, bool)) else None,
                    "line": n.lineno,
                }
            )
            if isinstance(default, str):
                defaults[dest] = default
    return args, defaults


def _module_consts(tree: ast.Module) -> dict[str, str]:
    consts: dict[str, str] = {}
    pe = _PathEval(consts, {})
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            val = pe(node.value)
            if val and looks_like_path(val):
                for t in targets:
                    if isinstance(t, ast.Name):
                        consts[t.id] = val
    return consts


_PARAM = "@param:"


def _mode_of(n: ast.Call, pos: int) -> str:
    """Mode string for open()/File(); a non-constant mode (append-or-write toggles) counts as write."""
    has_mode = len(n.args) > pos or any(kw.arg == "mode" for kw in n.keywords)
    mode = _mode_arg(n, pos)
    if mode is None:
        return "w?" if has_mode else "r"
    return mode


def _classify_calls(scope: ast.AST, pe: _PathEval, emit) -> None:
    """Find read/write calls in `scope` and pass (path, mode, line, how) to emit."""
    for n in ast.walk(scope):
        if not isinstance(n, ast.Call):
            continue
        fn, owner = _callee(n), _callee_owner(n)
        first = n.args[0] if n.args else None
        if fn == "open" and owner in {"", "io", "codecs", "gzip", "bz2"}:
            mode = _mode_of(n, 1)
            emit(pe(first), "write" if any(c in mode for c in "wax?") else "read", n.lineno, f"open({mode})")
        elif fn == "open" and isinstance(n.func, ast.Attribute):  # Path(x).open("w")
            mode = _mode_of(n, 0)
            emit(pe(n.func.value), "write" if any(c in mode for c in "wax?") else "read", n.lineno, f"Path.open({mode})")
        elif fn == "File" and owner in {"h5py", "tables", "zarr"}:
            mode = _mode_of(n, 1)
            emit(pe(first), "read" if mode == "r" else "write", n.lineno, f"{owner}.File({mode})")
        elif fn in WRITE_METHODS:
            explicit = {
                "savefig",
                "to_csv",
                "to_parquet",
                "to_json",
                "to_pickle",
                "to_hdf",
                "to_excel",
                "to_feather",
                "save_pretrained",
            }
            target = first if fn in explicit else None
            path = pe(target) if target is not None else (pe(n.func.value) if isinstance(n.func, ast.Attribute) else None)
            emit(path, "write", n.lineno, fn)
        elif fn in WRITE_FUNCS and owner in {
            "np",
            "numpy",
            "torch",
            "joblib",
            "pickle",
            "json",
            "yaml",
            "shutil",
            "imageio",
            "cv2",
            "plt",
            "sf",
            "tifffile",
        }:
            if fn in {"copy", "copy2", "copyfile", "move"} and len(n.args) >= 2:
                emit(pe(n.args[0]), "read", n.lineno, f"{owner}.{fn}")
                emit(pe(n.args[1]), "write", n.lineno, f"{owner}.{fn}")
            elif fn in {"dump", "safe_dump"} and len(n.args) >= 2:
                emit(pe(n.args[1]), "write", n.lineno, f"{owner}.{fn}")
            elif fn in {"save", "savez", "savez_compressed", "savetxt", "imsave", "imwrite"}:
                target = n.args[1] if owner == "torch" and len(n.args) > 1 else first
                emit(pe(target), "write", n.lineno, f"{owner}.{fn}")
        elif fn == "mkdir" and isinstance(n.func, ast.Attribute):
            emit(pe(n.func.value), "write", n.lineno, "mkdir")
        elif fn in READ_METHODS and isinstance(n.func, ast.Attribute):
            base = pe(n.func.value)
            if base and fn in {"glob", "rglob"} and n.args:
                base = f"{base}/{pe(n.args[0]) or '*'}"
            emit(base, "read", n.lineno, fn)
        elif fn in READ_FUNCS:
            if fn in HF_FUNCS:
                repo = pe(first)
                for kw in n.keywords:
                    if kw.arg in {"repo_id", "path", "pretrained_model_name_or_path"}:
                        repo = pe(kw.value) or repo
                if repo and re.fullmatch(r"[\w.\-]+/[\w.\-]+", repo) and not repo.lower().endswith(PATH_EXTS):
                    emit("hf:" + repo, "read", n.lineno, fn)  # "org/name" = a Hugging Face repo id
                elif repo:
                    emit(repo, "read", n.lineno, fn)
                continue
            if owner in {"json", "yaml", "pickle", "joblib"} and isinstance(first, ast.Call):
                continue  # json.load(open(p)): the open() is already counted
            emit(pe(first), "read", n.lineno, f"{owner + '.' if owner else ''}{fn}")
        elif fn == "glob" and owner in {"glob", ""}:
            emit(pe(first), "read", n.lineno, "glob")


def _bind_locals(scope: ast.AST, pe: _PathEval) -> None:
    """Record local variables that hold paths, in source order."""
    nodes = sorted(
        (n for n in ast.walk(scope) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.For))),
        key=lambda n: n.lineno,
    )
    for n in nodes:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            val = pe(n.value) if n.value is not None else None
            if val:
                for t in targets:
                    if isinstance(t, ast.Name):
                        pe.locals[t.id] = val
        elif isinstance(n, ast.For) and isinstance(n.target, ast.Name):
            it = n.iter
            while isinstance(it, ast.Call) and _callee(it) in {"sorted", "list", "tqdm", "enumerate"} and it.args:
                it = it.args[0]
            if not isinstance(it, ast.Call) or not isinstance(it.func, ast.Attribute):
                continue
            fn, base = _callee(it), pe(it.func.value)
            if not base:
                continue
            if fn in {"glob", "rglob"}:
                pe.locals[n.target.id] = f"{base}/{pe(it.args[0]) if it.args else '*'}"
            elif fn == "iterdir":
                pe.locals[n.target.id] = f"{base}/{{{n.target.id}}}"


def _scan_io(tree: ast.Module, consts: dict[str, str], argdefaults: dict[str, str], cli_args: list[dict]) -> list[IOHit]:
    hits: list[IOHit] = []
    used_paths: set[str] = set()

    def add(path: str | None, mode: str, line: int, how: str) -> None:
        if path and not path.startswith(_PARAM) and (looks_like_path(path) or path.startswith("hf:")):
            hits.append(IOHit(path, mode, line, how))
            used_paths.add(path)

    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    # 1) which parameters of each local function end up in a read or write call
    param_modes: dict[str, dict[int, set[tuple[str, str]]]] = {}
    for f in funcs:
        pe = _PathEval(consts, {})
        params = [a.arg for a in f.args.posonlyargs + f.args.args + f.args.kwonlyargs]
        for i, a in enumerate(params):
            pe.locals[a] = f"{_PARAM}{i}"
        _bind_locals(f, pe)

        def note(path, mode, line, how, _f=f.name):
            if path and path.startswith(_PARAM):
                idx = int(path[len(_PARAM) :].split("/", 1)[0])
                param_modes.setdefault(_f, {}).setdefault(idx, set()).add((mode, how))

        _classify_calls(f, pe, note)

    # 2) the real scan, per scope, plus calls into local helpers whose params are paths
    for scope in [*funcs, tree]:
        pe = _PathEval(consts, argdefaults)
        _bind_locals(scope, pe)
        _classify_calls(scope, pe, add)
        for n in ast.walk(scope):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in param_modes:
                for idx, uses in param_modes[n.func.id].items():
                    if idx < len(n.args):
                        for mode, how in sorted(uses):
                            add(pe(n.args[idx]), mode, n.lineno, f"{n.func.id}() -> {how}")

    # argparse defaults that look like paths but were never seen in a call: classify by flag name
    for a in cli_args:
        d = a.get("default")
        if isinstance(d, str) and looks_like_path(d) and d not in used_paths:
            name = a["dest"].lower()
            is_out = any(w in name for w in OUT_WORDS)
            # unnamed path flags are far more often inputs than outputs
            hits.append(IOHit(d, "write" if is_out else "read", a["line"], f"--{a['dest'].replace('_', '-')} default"))
    for name, val in consts.items():
        if val not in used_paths:
            hits.append(IOHit(val, "mention", 0, f"constant {name}"))
    seen, out = set(), []
    for h in sorted(hits, key=lambda h: h.line or 10**9):
        k = (h.path, h.mode)
        if k not in seen:
            seen.add(k)
            out.append(h)
    return out


def analyze_python(path: str, source: str) -> FileFacts:
    n_lines = source.count("\n") + (0 if source.endswith("\n") or not source else 1)
    facts = FileFacts(path=path, lines=max(n_lines, 1))
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        facts.parse_error = f"{e.msg} (line {e.lineno})"
        facts.blocks = [{"id": 0, "start": 1, "end": facts.lines, "name": None, "type": "setup"}]
        return facts
    facts.docstring = _first_para(ast.get_docstring(tree))
    facts.has_main_guard = any(_is_main_guard(n) for n in tree.body)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                facts.imports[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            for a in node.names:
                facts.imports[a.asname or a.name] = f"{mod}.{a.name}" if mod else a.name

    blocks = _blocks(tree, facts.lines)
    names = {b["name"]: b["id"] for b in blocks if b["name"] and b["type"] != "guard"}
    calls: set[tuple[int, int]] = set()
    for b in blocks:
        node = b.pop("_node", None)
        if node is None:
            continue
        used = _names_used(node)
        for nm in used:
            if nm in names and names[nm] != b["id"]:
                calls.add((b["id"], names[nm]))
        imported_used = sorted(nm for nm in used if nm in facts.imports)
        if imported_used:
            b["uses"] = imported_used
    facts.blocks = blocks
    facts.calls = [list(c) for c in sorted(calls)]

    facts.cli_args, argdefaults = _collect_argparse(tree)
    consts = _module_consts(tree)
    facts.io = _scan_io(tree, consts, argdefaults, facts.cli_args)
    return facts
