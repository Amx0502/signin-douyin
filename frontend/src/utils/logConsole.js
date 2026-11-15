const LOG_PATTERN =
  /^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d{3})?)\s+\[([A-Z]+)\]\s+([\s\S]*)$/;

export function parseLogLine(line) {
  // 容错：也接受已结构化的 { time, level, message }，否则 String(obj) 会渲染成 [object Object]
  if (line && typeof line === 'object') {
    return {
      time: String(line.time ?? ''),
      level: String(line.level ?? 'INFO').toUpperCase(),
      message: String(line.message ?? ''),
    }
  }
  const text = String(line ?? '').replace(/^\s+/, "");
  const match = text.match(LOG_PATTERN);
  return match
    ? { time: match[1], level: match[2], message: match[3] }
    : { time: "", level: "INFO", message: text };
}

export function filterLogEntries(lines, level = "ALL") {
  const entries = (Array.isArray(lines) ? lines : []).map(parseLogLine);
  return level === "ALL"
    ? entries
    : entries.filter((entry) => entry.level === level);
}
