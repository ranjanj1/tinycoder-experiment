// Shared helpers for MCP tool results.
export class ToolFail extends Error {
}
export function okResult(value) {
    return { content: [{ type: "text", text: JSON.stringify(value) }] };
}
export function errorResult(message) {
    return { content: [{ type: "text", text: `error: ${message}` }], isError: true };
}
/** Last `n` characters (Unicode code points) of `s`. */
export function lastChars(s, n) {
    if (s.length <= n)
        return s;
    let i = s.length;
    let count = 0;
    while (i > 0 && count < n) {
        const c = s.charCodeAt(i - 1);
        i -= c >= 0xdc00 && c <= 0xdfff && i >= 2 && (s.charCodeAt(i - 2) & 0xfc00) === 0xd800 ? 2 : 1;
        count++;
    }
    return s.slice(i);
}
