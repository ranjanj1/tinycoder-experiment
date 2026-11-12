// run_command allowlist (SPEC section 3), shared by the shell server and the agent's pre-call hook.

const ALLOWED = new Set(["ls", "cat", "head", "tail", "wc", "diff", "python3"]);
const GIT_SUBCOMMANDS = new Set(["status", "diff", "log", "show"]);

/** Returns null when argv is allowed, otherwise a short reason. */
export function commandRejection(argv: unknown): string | null {
  if (!Array.isArray(argv) || argv.length < 1) return "argv must be a non-empty array of strings";
  if (!argv.every((a) => typeof a === "string")) return "argv must be a non-empty array of strings";
  const cmd = argv[0] as string;
  if (ALLOWED.has(cmd)) return null;
  if (cmd === "git") {
    if (argv.length >= 2 && GIT_SUBCOMMANDS.has(argv[1] as string)) return null;
    return `git subcommand not allowed: ${argv.length >= 2 ? argv[1] : "(none)"}`;
  }
  return `command not allowed: ${cmd}`;
}
