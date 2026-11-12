"""MCP server 1: `files` (SPEC section 2)."""

import os
import re

from .mcpserve import ToolError, opt_int, opt_str, req_str, serve

MAX_LIST = 500
MAX_MATCHES = 200

TOOLS = [
    {
        "name": "list_files",
        "description": (
            "List files and directories under a path in the workspace, recursively, sorted by path. "
            "Skips .git. Returns at most 500 entries, then \"truncated\": true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory relative to the workspace root. Default \".\".",
                    "default": ".",
                }
            },
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a UTF-8 text file from the workspace. Optionally read only lines start_line..end_line "
            "(1-based, inclusive). Returns the content and the file's total line count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the workspace root."},
                "start_line": {"type": "integer", "minimum": 1, "description": "First line to read (1-based)."},
                "end_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Last line to read (inclusive, >= start_line).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write a UTF-8 text file in the workspace, replacing it if it exists and creating parent "
            "folders as needed. Returns the number of bytes written."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the workspace root."},
                "content": {"type": "string", "description": "The full new file content."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace one exact occurrence of old_text with new_text in a workspace file. Fails if "
            "old_text occurs zero times or more than once; include enough context to make it unique."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the workspace root."},
                "old_text": {"type": "string", "description": "Exact text to replace; must occur exactly once."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "search",
        "description": (
            "Search workspace files line by line with a regular expression (Python re syntax). "
            "Returns matches in path then line order, at most 200, then \"truncated\": true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for."},
                "path": {
                    "type": "string",
                    "description": "File or directory relative to the workspace root. Default \".\".",
                    "default": ".",
                },
            },
            "required": ["pattern"],
        },
    },
]


def _is_git(part):
    return part.lower() == ".git"


def _read_bytes(path):
    """Read a whole file with raw fd calls (about 3x cheaper than open()/read()).

    O_NONBLOCK: a FIFO in the workspace reads as empty instead of hanging the server.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        want = max(os.fstat(fd).st_size + 1, 1 << 16)
        chunks = []
        while True:
            try:
                chunk = os.read(fd, want)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


# Constructs whose result can differ between "search one line" and "search the whole text"
# even though the line is a substring of the text: string anchors (\A, \Z), lookarounds,
# atomic groups and possessive quantifiers (they can swallow the newline and never give it
# back). Patterns without them can be pre-checked against the whole file at C speed: a match
# on any single line implies a match on the whole text (compiled with MULTILINE so ^ and $
# mean line start/end; files containing \r are excluded because a line's trailing \r is
# stripped before matching). Escaped look-alikes merely disable the shortcut.
_PREFILTER_UNSAFE = re.compile(r"\\[AZ]|\(\?<?[=!]|\(\?>|[*+?}]\+")


_MAX_FALSE_CANDIDATES = 64


def _lines_plain(text, rx, has_cr, start=0, lineno=1):
    """Yield (line number, line) for each line of text[start:] that rx matches.

    `start` is a line start. Lines are split on \n; a trailing newline does not start
    another line (as in read_file); with has_cr a trailing \r is stripped from each line.
    """
    lines = (text[start:] if start else text).split("\n")
    if lines[-1] == "":
        lines.pop()
    search = rx.search
    for i, line in enumerate(lines, lineno):
        if has_cr and line.endswith("\r"):
            line = line[:-1]
        if search(line):
            yield i, line


def _lines_jump(text, whole, rx):
    r"""Same results as _lines_plain for a text without \r and a prefilter-safe pattern,
    but jumps between candidate lines at C speed instead of testing every line.

    From a line start `pos`, `whole` (MULTILINE) finds the leftmost whole-text match q. No
    line between pos and q's line can match: its line match would also be a whole-text
    match left of q. So only q's line needs the exact per-line check, then scanning resumes
    at the next line. Whole-text matches can straddle lines without any line matching (e.g.
    `a\s+b` on "a\nb"); after _MAX_FALSE_CANDIDATES of those the rest of the file is
    scanned line by line, which bounds the worst case.
    """
    n = len(text)
    search, line_search = whole.search, rx.search
    pos, lineno, false_hits = 0, 1, 0
    while pos < n:
        m = search(text, pos)
        if m is None:
            return
        q = m.start()
        nl = text.rfind("\n", pos, q)
        ls = pos if nl < 0 else nl + 1  # start of the line holding q
        if ls >= n:
            return  # an empty match after the final newline: not a line
        lineno += text.count("\n", pos, ls)
        le = text.find("\n", ls)
        if le < 0:
            le = n
        line = text[ls:le]
        if line_search(line):
            yield lineno, line
        else:
            false_hits += 1
            if false_hits > _MAX_FALSE_CANDIDATES:
                if le < n:
                    yield from _lines_plain(text, rx, False, le + 1, lineno + 1)
                return
        pos, lineno = le + 1, lineno + 1


def _prefilter(pattern):
    """A whole-text pre-check pattern for `pattern`, or None when it would not be exact."""
    if _PREFILTER_UNSAFE.search(pattern):
        return None
    try:
        return re.compile(pattern, re.MULTILINE)
    except re.error:
        return None


class Files:
    def __init__(self, workspace):
        self.root = os.path.realpath(workspace)
        if not os.path.isdir(self.root):
            raise SystemExit(f"tinycoder: workspace is not a directory: {workspace}")

    # -- path handling ------------------------------------------------------------------

    def resolve(self, path):
        """Return (real absolute path, display path relative to the workspace)."""
        if path == "":
            path = "."
        if "\x00" in path:
            raise ToolError("path contains a NUL byte")
        if os.path.isabs(path) or path.startswith("\\"):
            raise ToolError(f"absolute path not allowed: {path}")
        parts = re.split(r"[/\\]", path)
        if any(_is_git(p) for p in parts):
            raise ToolError(f"path inside .git not allowed: {path}")
        real = os.path.realpath(os.path.join(self.root, path))
        if not self._inside(real):
            raise ToolError(f"path is outside the workspace: {path}")
        rel = os.path.relpath(real, self.root)
        if rel != "." and any(_is_git(p) for p in rel.split(os.sep)):
            raise ToolError(f"path inside .git not allowed: {path}")
        display = os.path.normpath(path).replace(os.sep, "/")
        if display.startswith("../") or display == "..":
            display = rel.replace(os.sep, "/")  # lexical form escapes; show the resolved one
        return real, display

    def _inside(self, real):
        return real == self.root or real.startswith(self.root + os.sep)

    def _walk(self, top, display_top, sizes=True):
        """Yield (display_path, real_path, is_dir, size) below `top`, unsorted, skipping .git.

        Symlinks are listed by their target type when the target is inside the workspace
        (and skipped otherwise); symlinked directories are not descended into. With
        sizes=False, regular files report size 0 and cost no stat call.
        """
        stack = [(top, display_top)]
        while stack:
            real_dir, disp_dir = stack.pop()
            try:
                it = os.scandir(real_dir)
            except OSError:
                continue
            with it:
                for entry in it:
                    if _is_git(entry.name):
                        continue
                    disp = entry.name if disp_dir == "." else f"{disp_dir}/{entry.name}"
                    try:
                        if entry.is_symlink():
                            target = os.path.realpath(entry.path)
                            if not self._inside(target):
                                continue
                            rel = os.path.relpath(target, self.root)
                            if rel != "." and any(_is_git(p) for p in rel.split(os.sep)):
                                continue
                            st = os.stat(target)
                            is_dir = os.path.isdir(target)
                            yield disp, target, is_dir, 0 if is_dir else st.st_size
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            yield disp, entry.path, True, 0
                            stack.append((entry.path, disp))
                        elif sizes:
                            yield disp, entry.path, False, entry.stat(follow_symlinks=False).st_size
                        else:
                            yield disp, entry.path, False, 0
                    except OSError:
                        continue

    # -- tools --------------------------------------------------------------------------

    def list_files(self, args):
        real, display = self.resolve(opt_str(args, "path", "."))
        if not os.path.exists(real):
            raise ToolError(f"no such directory: {display}")
        if not os.path.isdir(real):
            raise ToolError(f"not a directory: {display}")
        entries = sorted(self._walk(real, display), key=lambda e: e[0])
        out = {
            "entries": [
                {"path": d, "type": "dir" if is_dir else "file", "size": size}
                for d, _, is_dir, size in entries[:MAX_LIST]
            ]
        }
        if len(entries) > MAX_LIST:
            out["truncated"] = True
        return out

    def _read_text(self, real, display):
        if not os.path.exists(real):
            raise ToolError(f"no such file: {display}")
        if os.path.isdir(real):
            raise ToolError(f"is a directory: {display}")
        try:
            data = _read_bytes(real)
        except OSError as exc:
            raise ToolError(f"cannot read {display}: {exc.strerror or exc}")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"file is not valid UTF-8: {display}")

    def read_file(self, args):
        real, display = self.resolve(req_str(args, "path"))
        start = opt_int(args, "start_line", 1)
        end = opt_int(args, "end_line", 1)
        if start is not None and end is not None and end < start:
            raise ToolError("end_line must be >= start_line")
        text = self._read_text(real, display)
        lines = text.split("\n")
        if lines[-1] == "":
            lines.pop()  # a trailing newline does not start another line
        total = len(lines)
        if start is None and end is None:
            return {"content": text, "total_lines": total}
        lo = (start or 1) - 1
        hi = total if end is None else min(end, total)
        chunk = lines[lo:hi]
        content = "\n".join(chunk)
        if chunk and (hi < total or text.endswith("\n")):
            content += "\n"
        return {"content": content, "total_lines": total}

    def write_file(self, args):
        real, display = self.resolve(req_str(args, "path"))
        content = req_str(args, "content")
        if os.path.isdir(real):
            raise ToolError(f"is a directory: {display}")
        data = content.encode("utf-8", "surrogatepass")
        try:
            os.makedirs(os.path.dirname(real), exist_ok=True)
            with open(real, "wb") as f:
                f.write(data)
        except OSError as exc:
            raise ToolError(f"cannot write {display}: {exc.strerror or exc}")
        return {"bytes_written": len(data)}

    def edit_file(self, args):
        real, display = self.resolve(req_str(args, "path"))
        old = req_str(args, "old_text")
        new = req_str(args, "new_text")
        if old == "":
            raise ToolError("old_text must not be empty")
        text = self._read_text(real, display)
        count = text.count(old)
        if count == 0:
            raise ToolError(f"old_text not found in {display}")
        if count > 1:
            raise ToolError(f"old_text occurs {count} times in {display}; make it unique")
        try:
            with open(real, "wb") as f:
                f.write(text.replace(old, new, 1).encode("utf-8", "surrogatepass"))
        except OSError as exc:
            raise ToolError(f"cannot write {display}: {exc.strerror or exc}")
        return {"replacements": 1}

    def search(self, args):
        pattern = req_str(args, "pattern")
        real, display = self.resolve(opt_str(args, "path", "."))
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}")
        if not os.path.exists(real):
            raise ToolError(f"no such file or directory: {display}")
        if os.path.isdir(real):
            files = sorted(
                (d, r) for d, r, is_dir, _ in self._walk(real, display, sizes=False) if not is_dir
            )
        else:
            files = [(display, real)]
        whole = _prefilter(pattern)
        matches = []
        truncated = False
        for disp, path in files:
            try:
                text = _read_bytes(path).decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # unreadable or not UTF-8: skipped
            has_cr = "\r" in text
            if whole is not None and not has_cr:
                hits = _lines_jump(text, whole, rx)
            else:
                hits = _lines_plain(text, rx, has_cr)
            for i, line in hits:
                if len(matches) == MAX_MATCHES:
                    truncated = True
                    break
                matches.append({"path": disp, "line": i, "text": line})
            if truncated:
                break
        out = {"matches": matches}
        if truncated:
            out["truncated"] = True
        return out


def main(workspace):
    files = Files(workspace)
    handlers = {t["name"]: getattr(files, t["name"]) for t in TOOLS}
    serve("files", TOOLS, handlers)
