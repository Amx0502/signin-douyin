/**
 * 抖音续火 · 消息模板解析
 *
 * 一条模板可以写成多行整段话，模板之间用「空行」分隔：
 *
 *   想你了~
 *   愿我们的友谊天长地久
 *
 *   在吗
 *   今天忙不忙
 *
 * → ["想你了~\n愿我们的友谊天长地久", "在吗\n今天忙不忙"]
 */

const LINE_TAIL_BLANK = /[ \t]+$/gm
const BLANK_LINE = /\n[ \t]*\n+/

/** 文本框内容 → 模板数组（整段保留内部换行，段尾空白去掉） */
export function parseTemplates(text) {
  return String(text ?? '')
    .replace(/\r\n?/g, '\n')
    .split(BLANK_LINE)
    .map(block => block.replace(LINE_TAIL_BLANK, '').trim())
    .filter(Boolean)
}

/** 模板数组 → 文本框内容（段间空一行，便于继续编辑） */
export function formatTemplates(list) {
  return (list || [])
    .map(item => String(item ?? '').replace(/\r\n?/g, '\n').trim())
    .filter(Boolean)
    .join('\n\n')
}
