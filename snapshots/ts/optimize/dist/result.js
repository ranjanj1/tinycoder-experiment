"use strict";
// Shared helpers for MCP tool results.
Object.defineProperty(exports, "__esModule", { value: true });
exports.ToolFail = void 0;
exports.okResult = okResult;
exports.errorResult = errorResult;
exports.lastChars = lastChars;
class ToolFail extends Error {
}
exports.ToolFail = ToolFail;
function okResult(value) {
    return { content: [{ type: "text", text: JSON.stringify(value) }] };
}
function errorResult(message) {
    return { content: [{ type: "text", text: `error: ${message}` }], isError: true };
}
/** Last `n` characters (Unicode code points) of `s`. */
function lastChars(s, n) {
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
