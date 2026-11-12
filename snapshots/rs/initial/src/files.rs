//! MCP server 1: `files` (SPEC.md section 2).

use std::fs;
use std::path::{Component, Path, PathBuf};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use regex::Regex;
use rmcp::model::{CallToolRequestParams, CallToolResponse, ListToolsResult, PaginatedRequestParams, Tool};
use rmcp::service::RequestContext;
use rmcp::{ErrorData, RoleServer, ServerHandler};
use serde::Serialize;
use serde_json::{Map, Value, json};

use crate::mcp_util::{self, ToolOut};

const LIST_MAX: usize = 500;
const SEARCH_MAX: usize = 200;
const REGEX_CACHE_MAX: usize = 64;
const FILE_CACHE_MAX_FILE: usize = 256 * 1024;
const FILE_CACHE_MAX_TOTAL: usize = 8 * 1024 * 1024;

/// A compiled search pattern: `(?m:pattern)`. A single line holds no `\n`, so on a line this
/// behaves exactly like `pattern`; on a whole file it can serve as a prefilter (see scan_lines).
struct Compiled {
    re: Regex,
    /// Whether `re` may be used on whole files to jump to candidate lines.
    whole_file: bool,
}

pub struct FilesServer {
    /// Canonical workspace root.
    root: PathBuf,
    tools: Arc<Vec<Tool>>,
    /// Recently compiled search patterns.
    regex_cache: Mutex<HashMap<String, Arc<Compiled>>>,
    /// File contents seen by `search`, revalidated by stat on every use.
    file_cache: Mutex<FileCache>,
}

#[derive(Default)]
struct FileCache {
    map: HashMap<PathBuf, (FileKey, Option<Arc<str>>)>,
    bytes: usize,
}

/// Identity of a file version: inode, size, mtime and ctime (ns). Any write changes it.
type FileKey = (u64, u64, u64, i64, i64, i64, i64);

fn file_key(md: &fs::Metadata) -> FileKey {
    use std::os::unix::fs::MetadataExt;
    (md.dev(), md.ino(), md.size(), md.mtime(), md.mtime_nsec(), md.ctime(), md.ctime_nsec())
}

impl FilesServer {
    /// The UTF-8 text of a regular file for `search`, or None when unreadable or not UTF-8.
    /// Small files are cached; a cache entry is used only while the file's stat key is unchanged.
    fn cached_text(&self, path: &Path) -> Option<Arc<str>> {
        let md = fs::metadata(path).ok()?;
        if !md.is_file() {
            return None;
        }
        let key = file_key(&md);
        if let Some((k, t)) = self.file_cache.lock().unwrap().map.get(path) {
            if *k == key {
                return t.clone();
            }
        }
        let bytes = fs::read(path).ok()?;
        let len = bytes.len();
        let text: Option<Arc<str>> = String::from_utf8(bytes).ok().map(Arc::from);
        if len <= FILE_CACHE_MAX_FILE {
            let mut c = self.file_cache.lock().unwrap();
            if c.bytes + len > FILE_CACHE_MAX_TOTAL {
                c.map.clear();
                c.bytes = 0;
            }
            if let Some((_, old)) = c.map.insert(path.to_path_buf(), (key, text.clone())) {
                c.bytes -= old.map(|t| t.len()).unwrap_or(0);
            }
            c.bytes += text.as_ref().map(|t| t.len()).unwrap_or(0);
        }
        text
    }

    fn compile(&self, pattern: &str) -> Result<Arc<Compiled>, String> {
        if let Some(c) = self.regex_cache.lock().unwrap().get(pattern) {
            return Ok(c.clone());
        }
        let c = Arc::new(match Regex::new(&format!("(?m:{pattern})")) {
            Ok(re) => Compiled { re, whole_file: prefilter_safe(pattern) },
            // e.g. an `(?x)` comment swallowing the closing parenthesis: use the plain pattern.
            Err(_) => Compiled {
                re: Regex::new(pattern).map_err(|e| format!("invalid regular expression: {e}"))?,
                whole_file: false,
            },
        });
        let mut cache = self.regex_cache.lock().unwrap();
        if cache.len() >= REGEX_CACHE_MAX {
            cache.clear();
        }
        cache.insert(pattern.to_string(), c.clone());
        Ok(c)
    }

    pub fn new(workspace: &Path) -> anyhow::Result<Self> {
        let root = fs::canonicalize(workspace)
            .map_err(|e| anyhow::anyhow!("workspace {}: {}", workspace.display(), e))?;
        Ok(Self { root, tools: Arc::new(tool_defs()), regex_cache: Mutex::new(HashMap::new()), file_cache: Mutex::new(FileCache::default()) })
    }

    /// Resolve a workspace-relative path, following symlinks, and refuse anything outside the
    /// workspace, absolute, or inside `.git`. Returns (absolute resolved path, workspace-relative path).
    fn resolve(&self, p: &str) -> Result<(PathBuf, String), String> {
        if p.is_empty() {
            return Err("path is empty".into());
        }
        let rel = Path::new(p);
        if rel.is_absolute() || p.starts_with('/') || p.starts_with('\\') {
            return Err(format!("absolute paths are not allowed: {p}"));
        }
        // Walk component by component. While every prefix exists, `cur` is canonical (all
        // symlinks followed), so `..` is a plain pop. Once a component is missing, nothing after
        // it can be a symlink, so the rest is applied lexically.
        let mut norm = self.root.clone();
        let mut missing = false;
        for c in rel.components() {
            match c {
                Component::CurDir => {}
                Component::ParentDir => {
                    norm.pop();
                }
                Component::Normal(name) => {
                    norm.push(name);
                    if !missing {
                        match fs::symlink_metadata(&norm) {
                            Ok(_) => match fs::canonicalize(&norm) {
                                Ok(c) => norm = c,
                                Err(_) => return Err(format!("cannot resolve path (dangling symlink?): {p}")),
                            },
                            Err(_) => missing = true,
                        }
                    }
                }
                _ => return Err(format!("absolute paths are not allowed: {p}")),
            }
        }
        let relp = match norm.strip_prefix(&self.root) {
            Ok(r) => r.to_path_buf(),
            Err(_) => return Err(format!("path is outside the workspace: {p}")),
        };
        if relp.components().any(|c| c.as_os_str() == ".git") {
            return Err(format!("path is inside .git: {p}"));
        }
        let rels = relp.to_string_lossy().replace('\\', "/");
        Ok((norm, if rels.is_empty() { ".".into() } else { rels }))
    }

    fn call(&self, name: &str, args: &Map<String, Value>) -> ToolOut {
        match name {
            "list_files" => self.list_files(args),
            "read_file" => self.read_file(args),
            "write_file" => self.write_file(args),
            "edit_file" => self.edit_file(args),
            "search" => self.search(args),
            _ => Err(format!("unknown tool: {name}")),
        }
    }

    fn list_files(&self, args: &Map<String, Value>) -> ToolOut {
        let p = mcp_util::opt_str(args, "path")?.unwrap_or(".");
        let (abs, rel) = self.resolve(p)?;
        let md = fs::metadata(&abs).map_err(|e| format!("{p}: {e}"))?;
        let mut entries: Vec<(String, bool, u64)> = Vec::new();
        if md.is_dir() {
            let prefix = if rel == "." { String::new() } else { format!("{rel}/") };
            self.walk(&abs, &prefix, true, &mut |path, is_dir, size| entries.push((path, is_dir, size)));
        } else {
            entries.push((rel, false, md.len()));
        }
        entries.sort_unstable_by(|a, b| a.0.cmp(&b.0));
        let truncated = entries.len() > LIST_MAX;
        entries.truncate(LIST_MAX);
        let entries = entries
            .iter()
            .map(|(path, is_dir, size)| Entry { path, kind: if *is_dir { "dir" } else { "file" }, size: *size })
            .collect();
        mcp_util::ser(&ListOut { entries, truncated })
    }

    /// Recursive walk that skips `.git`, does not descend into symlinked directories and
    /// skips symlinks that resolve outside the workspace.
    fn walk(&self, dir: &Path, prefix: &str, sizes: bool, f: &mut dyn FnMut(String, bool, u64)) {
        let rd = match fs::read_dir(dir) {
            Ok(r) => r,
            Err(_) => return,
        };
        for ent in rd.flatten() {
            let name = ent.file_name();
            if name == ".git" {
                continue;
            }
            let name = name.to_string_lossy();
            let path = format!("{prefix}{name}");
            let ft = match ent.file_type() {
                Ok(t) => t,
                Err(_) => continue,
            };
            if ft.is_symlink() {
                let target = match fs::canonicalize(ent.path()) {
                    Ok(t) => t,
                    Err(_) => continue,
                };
                match target.strip_prefix(&self.root) {
                    Ok(r) if !r.components().any(|c| c.as_os_str() == ".git") => {}
                    _ => continue,
                }
                if let Ok(md) = fs::metadata(&target) {
                    f(path, md.is_dir(), if md.is_dir() { 0 } else { md.len() });
                }
            } else if ft.is_dir() {
                f(path.clone(), true, 0);
                self.walk(&ent.path(), &format!("{path}/"), sizes, f);
            } else {
                let size = if sizes { ent.metadata().map(|m| m.len()).unwrap_or(0) } else { 0 };
                f(path, false, size);
            }
        }
    }

    fn read_file(&self, args: &Map<String, Value>) -> ToolOut {
        let p = mcp_util::req_str(args, "path")?;
        let start = mcp_util::opt_int(args, "start_line")?;
        let end = mcp_util::opt_int(args, "end_line")?;
        if let Some(s) = start {
            if s < 1 {
                return Err("start_line must be >= 1".into());
            }
        }
        if let Some(e) = end {
            if e < start.unwrap_or(1) {
                return Err("end_line must be >= start_line".into());
            }
        }
        let (abs, _) = self.resolve(p)?;
        let bytes = fs::read(&abs).map_err(|e| format!("{p}: {e}"))?;
        let text = String::from_utf8(bytes).map_err(|_| format!("{p}: file is not valid UTF-8"))?;
        let total = text.split_inclusive('\n').count();
        let content = if start.is_none() && end.is_none() {
            text
        } else {
            let s = start.unwrap_or(1) as usize;
            let e = end.map(|e| e as usize).unwrap_or(usize::MAX);
            let mut out = String::new();
            for (i, line) in text.split_inclusive('\n').enumerate() {
                let n = i + 1;
                if n > e {
                    break;
                }
                if n >= s {
                    out.push_str(line);
                }
            }
            out
        };
        mcp_util::ser(&ReadOut { content: &content, total_lines: total })
    }

    fn write_file(&self, args: &Map<String, Value>) -> ToolOut {
        let p = mcp_util::req_str(args, "path")?;
        let content = mcp_util::req_str(args, "content")?;
        let (abs, _) = self.resolve(p)?;
        if abs.is_dir() {
            return Err(format!("{p} is a directory"));
        }
        if let Some(parent) = abs.parent() {
            fs::create_dir_all(parent).map_err(|e| format!("{p}: {e}"))?;
        }
        fs::write(&abs, content.as_bytes()).map_err(|e| format!("{p}: {e}"))?;
        Ok(format!("{{\"bytes_written\":{}}}", content.len()))
    }

    fn edit_file(&self, args: &Map<String, Value>) -> ToolOut {
        let p = mcp_util::req_str(args, "path")?;
        let old = mcp_util::req_str(args, "old_text")?;
        let new = mcp_util::req_str(args, "new_text")?;
        if old.is_empty() {
            return Err("old_text must not be empty".into());
        }
        let (abs, _) = self.resolve(p)?;
        let bytes = fs::read(&abs).map_err(|e| format!("{p}: {e}"))?;
        let text = String::from_utf8(bytes).map_err(|_| format!("{p}: file is not valid UTF-8"))?;
        let first = match text.find(old) {
            Some(i) => i,
            None => return Err(format!("old_text not found in {p}")),
        };
        // Count overlapping occurrences too: any second start position means "more than once".
        let next = first + old.chars().next().map(|c| c.len_utf8()).unwrap_or(1);
        if text[next..].contains(old) {
            return Err(format!("old_text occurs more than once in {p}; include more context"));
        }
        let mut out = String::with_capacity(text.len() + new.len());
        out.push_str(&text[..first]);
        out.push_str(new);
        out.push_str(&text[first + old.len()..]);
        fs::write(&abs, out.as_bytes()).map_err(|e| format!("{p}: {e}"))?;
        Ok("{\"replacements\":1}".into())
    }

    fn search(&self, args: &Map<String, Value>) -> ToolOut {
        let pattern = mcp_util::req_str(args, "pattern")?;
        let p = mcp_util::opt_str(args, "path")?.unwrap_or(".");
        let compiled = self.compile(pattern)?;
        let (abs, rel) = self.resolve(p)?;
        let md = fs::metadata(&abs).map_err(|e| format!("{p}: {e}"))?;
        let mut files: Vec<(String, PathBuf)> = Vec::new();
        if md.is_dir() {
            let prefix = if rel == "." { String::new() } else { format!("{rel}/") };
            self.walk(&abs, &prefix, false, &mut |path, is_dir, _| {
                if !is_dir {
                    let full = abs.join(&path[prefix.len()..]);
                    files.push((path, full));
                }
            });
            files.sort_unstable_by(|a, b| a.0.cmp(&b.0));
        } else {
            files.push((rel, abs));
        }
        let mut matches: Vec<Match> = Vec::new();
        let mut truncated = false;
        'outer: for (path, fpath) in &files {
            let text = match self.cached_text(fpath) {
                Some(t) => t,
                None => continue,
            };
            let ok = scan_lines(&text, &compiled.re, compiled.whole_file, &mut |n, line| {
                if matches.len() == SEARCH_MAX {
                    truncated = true;
                    return false;
                }
                matches.push(Match { path: path.clone(), line: n, text: line.to_string() });
                true
            });
            if !ok {
                break 'outer;
            }
        }
        mcp_util::ser(&SearchOut { matches, truncated })
    }
}

#[derive(Serialize)]
struct Entry<'a> {
    path: &'a str,
    #[serde(rename = "type")]
    kind: &'static str,
    size: u64,
}

#[derive(Serialize)]
struct ListOut<'a> {
    entries: Vec<Entry<'a>>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    truncated: bool,
}

#[derive(Serialize)]
struct ReadOut<'a> {
    content: &'a str,
    total_lines: usize,
}

#[derive(Serialize)]
struct Match {
    path: String,
    line: usize,
    text: String,
}

#[derive(Serialize)]
struct SearchOut {
    matches: Vec<Match>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    truncated: bool,
}

/// Whether `(?m:pattern)` may be run over a whole file to find candidate lines. For a file with
/// only `\n` line endings, any match of `pattern` inside one line is also a match of
/// `(?m:pattern)` in the whole text at the same span (`^`/`$` hold at line boundaries, `\b`
/// sees `\n` as a non-word char). Patterns with `\A`, `\z` or inline flags (which could turn
/// `m` off) are excluded; files containing `\r` use the plain per-line scan.
fn prefilter_safe(pattern: &str) -> bool {
    !(pattern.contains("\\A") || pattern.contains("\\z") || pattern.contains("(?"))
}

/// Calls `hit(line_number, line)` for every line of `text` matching `re`, in order, like
/// `text.lines()`. Stops and returns false as soon as `hit` returns false.
fn scan_lines(text: &str, re: &Regex, whole_file: bool, hit: &mut dyn FnMut(usize, &str) -> bool) -> bool {
    match whole_file && !text.as_bytes().contains(&b'\r') {
        true => {
            // Jump between candidate positions found by the multiline regex; every candidate
            // line is confirmed with the per-line regex. See multiline_prefilter.
            let mut pos = 0usize;
            let mut line_no = 1usize;
            let mut counted_to = 0usize;
            while pos < text.len() {
                let m = match re.find_at(text, pos) {
                    Some(m) => m,
                    None => break,
                };
                let ls = text[..m.start()].rfind('\n').map(|i| i + 1).unwrap_or(0).max(pos);
                if ls >= text.len() {
                    break;
                }
                let le = text[m.start()..].find('\n').map(|i| i + m.start()).unwrap_or(text.len());
                line_no += bytecount_nl(&text.as_bytes()[counted_to..ls]);
                counted_to = ls;
                let line = &text[ls..le];
                if re.is_match(line) && !hit(line_no, line) {
                    return false;
                }
                pos = le + 1;
            }
        }
        false => {
            for (i, line) in text.lines().enumerate() {
                if re.is_match(line) && !hit(i + 1, line) {
                    return false;
                }
            }
        }
    }
    true
}

fn bytecount_nl(b: &[u8]) -> usize {
    b.iter().filter(|&&c| c == b'\n').count()
}

fn tool_defs() -> Vec<Tool> {
    let path_prop = |d: &str| json!({"type": "string", "description": d});
    vec![
        mcp_util::tool(
            "list_files",
            "List files and directories under a workspace path, recursively, sorted by path. Skips .git. At most 500 entries.",
            json!({"type": "object", "properties": {
                "path": path_prop("Directory relative to the workspace root. Default \".\".")
            }, "required": []}),
        ),
        mcp_util::tool(
            "read_file",
            "Read a UTF-8 text file. Optionally only lines start_line..end_line (1-based, inclusive). Returns the content and the file's total line count.",
            json!({"type": "object", "properties": {
                "path": path_prop("File path relative to the workspace root."),
                "start_line": {"type": "integer", "minimum": 1, "description": "First line to return (1-based)."},
                "end_line": {"type": "integer", "minimum": 1, "description": "Last line to return (inclusive, >= start_line)."}
            }, "required": ["path"]}),
        ),
        mcp_util::tool(
            "write_file",
            "Write a file with the given content, replacing it if it exists. Creates parent folders.",
            json!({"type": "object", "properties": {
                "path": path_prop("File path relative to the workspace root."),
                "content": {"type": "string", "description": "The full new file content."}
            }, "required": ["path", "content"]}),
        ),
        mcp_util::tool(
            "edit_file",
            "Replace one exact occurrence of old_text with new_text in a file. Fails if old_text occurs zero times or more than once.",
            json!({"type": "object", "properties": {
                "path": path_prop("File path relative to the workspace root."),
                "old_text": {"type": "string", "description": "Exact text to replace; must occur exactly once."},
                "new_text": {"type": "string", "description": "Replacement text."}
            }, "required": ["path", "old_text", "new_text"]}),
        ),
        mcp_util::tool(
            "search",
            "Search UTF-8 files for lines matching a regular expression (Rust regex syntax). Returns matches in path then line order, at most 200.",
            json!({"type": "object", "properties": {
                "pattern": {"type": "string", "description": "Regular expression matched against each line."},
                "path": path_prop("File or directory relative to the workspace root. Default \".\".")
            }, "required": ["pattern"]}),
        ),
    ]
}

impl ServerHandler for FilesServer {
    fn get_info(&self) -> rmcp::model::ServerConfig {
        mcp_util::server_info("tinycoder-files")
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult::with_all_items((*self.tools).clone()))
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let empty = Map::new();
        let args = request.arguments.as_ref().unwrap_or(&empty);
        let out: ToolOut = self.call(&request.name, args);
        Ok(mcp_util::to_response(out))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn collect(text: &str, re: &Regex, whole_file: bool) -> Vec<(usize, String)> {
        let mut v = Vec::new();
        scan_lines(text, re, whole_file, &mut |n, l| {
            v.push((n, l.to_string()));
            true
        });
        v
    }

    #[test]
    fn multiline_scan_matches_plain_per_line_scan() {
        let patterns = [
            "a", "^a", "a$", "^$", "^", "$", "b+c", "\\bab\\b", "a.b", "\\s+", "[^a]", "a\\nb", "x*",
            "^a.*c$", "(a|bc)$", "\\Bb", "c\\s*$", "^\\s*$", "ab|^c", "[a-c]{3}", "é", "\\Aa", "c\\z",
            "(?-m)^b", "(?s)a.b",
        ];
        let alphabet = ['a', 'b', 'c', ' ', '\n', '\n', 'é', '\t', '\r'];
        let mut seed: u64 = 12345;
        let mut rnd = || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (seed >> 33) as usize
        };
        for _ in 0..3000 {
            let len = rnd() % 30;
            let text: String = (0..len).map(|_| alphabet[rnd() % alphabet.len()]).collect();
            for p in patterns {
                let plain = Regex::new(p).unwrap();
                let wrapped = Regex::new(&format!("(?m:{p})")).unwrap();
                let want = collect(&text, &plain, false);
                assert_eq!(collect(&text, &wrapped, false), want, "per-line {p:?} {text:?}");
                assert_eq!(collect(&text, &wrapped, prefilter_safe(p)), want, "whole-file {p:?} {text:?}");
            }
        }
    }
}
