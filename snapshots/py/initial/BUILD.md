# Build (lane PY)

Pure Python, so there is nothing to compile. The steps below only byte-compile the source
(faster first start) and make the launcher executable. Run them from the workspace root:

```sh
chmod +x bin/tinycoder
/Users/Shared/tc-kit/pyenv/bin/python3 -m compileall -q src
```

Runtime: `bin/tinycoder` runs `src/tinycoder/main.py` with the first of these interpreters
that exists: `$TINYCODER_PYTHON`, `./.venv/bin/python3`, `/Users/Shared/tc-kit/pyenv/bin/python3`
(the offline kit's venv with `mcp` 2.3.0 and `anthropic` 2.8.0), else `python3` on PATH.
The kit venv must be called by its own path: the `python3` symlink in `tc-kit/bin-py` does
not activate the venv, so `mcp` cannot be imported through it.

Pinned dependencies: `pyproject.toml` (direct) and `requirements.lock` (full set from the kit).
