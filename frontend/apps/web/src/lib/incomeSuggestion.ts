/**
 * 收入建议卡片里两个「算错了肉眼看不出来」的纯判定,单独放出来是为了能不起
 * DOM 就测(本仓 web 测试没有 jsdom / testing-library,组件渲染无法断言)。
 *
 * 两条规则都关乎正确性而不是样式:
 *   1. `monthly_potential_*` 的 null 表示「模型没估出来」,**必须**渲染成 dash。
 *      渲染成 0 会把「不知道」变成一个编出来的具体承诺(「每月 0 元」)。
 *   2. 容量条量的是「消耗掉多少可用时间」,越满越糟,所以配色跟预算用量同一套
 *      阈值,而不是目标进度那套「越满越好」的配色。
 */

/** 潜在收入该用哪条文案。null = 两头都没估出来,渲染 `common.dash`。 */
export type PotentialShape = 'range' | 'from' | 'upTo' | null

export function potentialShape(
  low: number | null,
  high: number | null
): PotentialShape {
  if (low != null && high != null) return 'range'
  if (low != null) return 'from'
  if (high != null) return 'upTo'
  return null
}

/**
 * 阈值跟 `components/dashboard/BudgetUsagePanel.tsx::thresholdColor` 逐字相同 ——
 * 同一类语义(用掉多少可用额度)必须同一套配色。
 */
export function capacityColor(ratio: number): string {
  if (ratio >= 1.0) return 'bg-red-700'
  if (ratio >= 0.9) return 'bg-red-500'
  if (ratio >= 0.7) return 'bg-orange-500'
  return 'bg-green-500'
}

/** 只有「填了可投入时间」且「这条建议估了投入」时才画条。 */
export function showCapacityBar(
  availableHoursPerWeek: number | null,
  effortHoursPerWeek: number | null
): boolean {
  return (
    availableHoursPerWeek != null && availableHoursPerWeek > 0 && effortHoursPerWeek != null
  )
}

/** 小时 / 周数去掉无意义的小数尾巴:5 → "5",7.5 → "7.5"。 */
export function formatUnitNumber(value: number): string {
  return Number.isInteger(value) ? `${value}` : value.toFixed(1)
}
