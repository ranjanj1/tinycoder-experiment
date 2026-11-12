"""Import SDK submodules without running their packages' re-export `__init__` files.

`import mcp.server.lowlevel` normally executes `mcp/__init__.py` and `mcp/server/__init__.py`
first, which import the whole SDK surface (client, in-memory transport, MCPServer, HTTP and
auth code, ...): about a third of a server's start-up time and memory, none of it used by a
stdio server. Likewise `anthropic.types/__init__.py` re-exports ~1,800 type modules, all
loaded because the client's tracing helper imports one of them.

`shim()` registers such packages as lazy module objects instead. The SDK code itself is
unchanged and every module still loads from its own file:

- a submodule (`pkg.sub`) loads normally, just that module;
- a re-exported name (`from pkg import Name`) is resolved from the package's own `__init__`
  source *when that file is a pure re-export list* (only a docstring, `from __future__`, and
  `from .module import Name [as Alias]` lines, no alias shadowing a submodule): then only the
  defining submodule is imported, exactly as the real `__init__` would bind it;
- anything else executes the real `__init__` into the shim (once), so code that relies on
  other package contents keeps working, just without the saving.
"""

import ast
import importlib
import importlib.machinery
import sys
import types


def _pure_reexports(module):
    """Map alias -> (relative submodule, name) if the package __init__ only re-exports;
    None otherwise (then the real __init__ is executed on demand)."""
    try:
        source = module.__spec__.loader.get_source(module.__name__)
        tree = ast.parse(source or "")
    except Exception:
        return None
    mapping = {}
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # docstring
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            for alias in node.names:
                if alias.name == "*":
                    return None
                mapping[alias.asname or alias.name] = (node.module, alias.name)
            continue
        return None  # assignments, calls, conditionals, absolute or parent imports ...
    for alias in mapping:
        if importlib.machinery.PathFinder.find_spec(f"{module.__name__}.{alias}", module.__path__):
            return None  # an alias that shadows a submodule: binding order would matter
    return mapping


class _LazyPackage(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        state = self.__dict__
        if "_tinycoder_reexports" not in state:
            state["_tinycoder_reexports"] = _pure_reexports(self)
        reexports = state["_tinycoder_reexports"]
        if reexports is not None and name in reexports:
            submodule, original = reexports[name]
            value = getattr(importlib.import_module(f"{self.__name__}.{submodule}"), original)
            state[name] = value
            return value
        full = f"{self.__name__}.{name}"
        if importlib.machinery.PathFinder.find_spec(full, self.__path__) is not None:
            return importlib.import_module(full)  # a submodule: load just that
        if not state.get("_tinycoder_loaded"):
            state["_tinycoder_loaded"] = True
            self.__spec__.loader.exec_module(self)  # fall back to the real package init
            if name in state:
                return state[name]
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")


def gc_resume():
    """End the start-up GC pause begun in main.py: freeze the start-up objects (modules,
    classes, schemas live for the whole process) so later collections never rescan them,
    then turn the collector back on."""
    import gc

    gc.freeze()
    gc.enable()


def subpackages(name, below):
    """Dotted names of every package under `name`.`below` (inclusive), parents first, found
    by scanning the directory (nothing is imported). [] if the package is not installed."""
    import os

    spec = importlib.machinery.PathFinder.find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        return []
    found = []
    stack = [(os.path.join(spec.submodule_search_locations[0], *below.split(".")), f"{name}.{below}")]
    while stack:
        path, dotted = stack.pop()
        if not os.path.isfile(os.path.join(path, "__init__.py")):
            continue
        found.append(dotted)
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_dir() and entry.name != "__pycache__" and entry.name.isidentifier():
                    stack.append((entry.path, f"{dotted}.{entry.name}"))
    found.sort(key=lambda n: n.count("."))
    return found


def shim(*names):
    """Register each dotted package name (parents first) as a lazy package, if not imported."""
    for name in names:
        if name in sys.modules:
            continue
        parent, _, child = name.rpartition(".")
        path = sys.modules[parent].__path__ if parent else None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.submodule_search_locations is None:
            continue  # not installed or not a package: leave normal import alone
        mod = _LazyPackage(name)
        mod.__spec__ = spec
        mod.__loader__ = spec.loader
        mod.__file__ = spec.origin
        mod.__path__ = list(spec.submodule_search_locations)
        mod.__package__ = name
        sys.modules[name] = mod
        if parent:
            setattr(sys.modules[parent], child, mod)
