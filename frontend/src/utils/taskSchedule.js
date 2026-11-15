/**
 * 任务日期计划（每日 / 指定日期模式）的纯函数工具。
 *
 * 班级魔方、抖音续火、学习通三处任务面板共用同一套排期语义：
 * start_date/end_date 闭区间、skip_dates 排除、skip_weekends 周末跳过、
 * 指定日期模式按 run_dates 实际生效日计算。此前三处各持一份
 * 逐字相同的实现，收敛到这里单点维护。
 */

/** 日期是否落在计划区间内（闭区间，任一端为空表示不限）。 */
export function dateInRange(value, plan) {
  return (!plan.start_date || value >= plan.start_date) && (!plan.end_date || value <= plan.end_date)
}

/** 指定日期模式下实际会执行的日期（去掉区间外、被排除与周末）。 */
export function effectiveSpecificDates(plan) {
  const skipped = new Set(plan.skip_dates || [])
  return [...new Set(plan.run_dates || [])].filter(value => {
    if (!dateInRange(value, plan) || skipped.has(value)) return false
    if (!plan.skip_weekends) return true
    const weekday = new Date(`${value}T00:00:00`).getDay()
    return weekday !== 0 && weekday !== 6
  }).sort()
}

/** 排期摘要文案（任务列表一行的「每天执行 · 周末跳过」等）。 */
// 数字与单位之间用不换行空格：「23 天」不会被拆成「23」+「天」两行
const bindUnit = text => String(text).replace(/(\d+)\s+(天|次|个)/g, '$1\u00A0$2')

export function schedulePlanSummary(plan) {
  const excluded = (plan.skip_dates || []).length
  if ((plan.date_mode || 'daily') === 'daily') {
    return bindUnit(
      ['每天执行', plan.skip_weekends ? '周末跳过' : '', excluded ? `排除 ${excluded} 天` : '']
        .filter(Boolean).join(' · '),
    )
  }
  const selected = (plan.run_dates || []).length
  const effective = effectiveSpecificDates(plan).length
  return bindUnit(
    [`指定 ${selected} 天`, `实际 ${effective} 天`, plan.auto_disable_after_finish ? '结束后关闭' : '']
      .filter(Boolean).join(' · '),
  )
}

/** 起止范围摘要文案。 */
export function scheduleRangeSummary(plan) {
  if (!plan.start_date && !plan.end_date) return '不限制开始和结束日期'
  return `${plan.start_date || '不限'} 至 ${plan.end_date || '不限'}`
}

/** 校验日期计划：不合法时抛 Error（调用方捕获后自行提示）。 */
export function validateDatePlan(plan) {
  if (plan.start_date && plan.end_date && plan.start_date > plan.end_date) throw new Error('开始日期不能晚于结束日期')
  if (plan.date_mode === 'specific' && !(plan.run_dates || []).length) throw new Error('指定日期模式下请至少选择一个执行日期')
  if (plan.date_mode === 'specific' && !effectiveSpecificDates(plan).length) throw new Error('指定日期计划至少需要保留一个实际执行日期')
}
