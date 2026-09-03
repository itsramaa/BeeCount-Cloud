/**
 * 账本财务洞察 API client —— `GET /ledgers/{ext}/insights`。
 *
 * 纯读端点,不参与同步:洞察数字由服务端从 projection 现算,既不写
 * `sync_changes` 也不需要 `base_change_id`,所以调用方不要套 `useLedgerWrite`。
 *
 * 服务端只回数字和 enum code(`diagnosis`),措辞一律由前端按自己的 locale 决定。
 */
import { authedGet } from './http'

/**
 * 诊断 code。判定规则在服务端 `services/insights.py::classify_diagnosis`:
 * - `insufficient_data` 完成周期不足 3 个,或基线收入 <= 0
 * - `reduce_spending`   当前周期支出高出自己的支出基线
 * - `increase_income`   支出没超基线,但结余率仍偏低
 * - `on_track`          以上都不成立
 */
export type InsightDiagnosis =
  | 'insufficient_data'
  | 'on_track'
  | 'reduce_spending'
  | 'increase_income'

/**
 * 统计窗口。`restricted_to_creator=true` 表示所有数字只统计了 caller 自己记的
 * 账(多成员共享账本),跟"账本全员合计"不是一个量 —— UI 必须透出。
 */
export interface InsightRange {
  start_at: string
  end_at: string
  basis_periods: number
  restricted_to_creator: boolean
}

/** 进行中的当前周期。**不参与 median 计算**,不要 concat 进 series。 */
export interface InsightCurrentPeriod {
  bucket: string
  income: number
  expense: number
  surplus: number
  days_total: number
  days_remaining: number
  days_elapsed: number
}

/**
 * 跨周期结余基线。
 *
 * `buffer` 是用户自己月支出的中位数绝对偏差(MAD)—— 波动缓冲垫,不是固定
 * 百分比。`allocatable` 已经是 `max(0, median_surplus - buffer)`,前端不用再算。
 */
export interface InsightBaseline {
  median_income: number
  median_expense: number
  median_surplus: number
  buffer: number
  allocatable: number
}

/** 已完成周期的收支。当前(进行中)周期不在其中。 */
export interface InsightSeriesItem {
  bucket: string
  income: number
  expense: number
  surplus: number
}

export interface InsightCategoryBaseline {
  category_name: string
  median: number
  mean: number
  periods_present: number
}

/** 当前周期的预算用量。`safe_daily` 在周期最后一天退化成"整笔剩余"而非做除法。 */
export interface InsightBudgetStatus {
  budget_id: string
  budget_type: string
  category_id: string | null
  category_name: string | null
  amount: number
  used: number
  remaining: number
  percent_used: number
  safe_daily: number
  exceeded: boolean
}

/** `recommended_amount` 是该分类的历史 median 原值,不乘任何系数。 */
export interface InsightRecommendationCategory {
  category_name: string
  recommended_amount: number
  basis_periods: number
}

export interface InsightRecommendation {
  categories: InsightRecommendationCategory[]
  saving_amount: number
  buffer_amount: number
}

export interface LedgerInsights {
  ledger_id: string
  currency: string
  month_start_day: number
  range: InsightRange
  current: InsightCurrentPeriod
  baseline: InsightBaseline
  series: InsightSeriesItem[]
  category_baselines: InsightCategoryBaseline[]
  budget_status: InsightBudgetStatus[]
  recommendation: InsightRecommendation
  diagnosis: InsightDiagnosis
}

/**
 * `lookbackPeriods` 服务端限 3..24,`tzOffsetMinutes` 限 -720..840;
 * 超范围直接 422,调用方负责在 UI 上就把可选值限死。
 */
export async function fetchLedgerInsights(
  token: string,
  ledgerId: string,
  options?: { lookbackPeriods?: number; tzOffsetMinutes?: number },
): Promise<LedgerInsights> {
  const query = new URLSearchParams()
  if (options?.lookbackPeriods) query.set('lookback_periods', `${options.lookbackPeriods}`)
  if (typeof options?.tzOffsetMinutes === 'number')
    query.set('tz_offset_minutes', `${options.tzOffsetMinutes}`)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return authedGet<LedgerInsights>(
    `/ledgers/${encodeURIComponent(ledgerId)}/insights${suffix}`,
    token,
  )
}
