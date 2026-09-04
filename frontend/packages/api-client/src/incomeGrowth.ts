/**
 * 收入增长建议 API client —— `POST /ai/income-growth`。
 *
 * 端点有两半,前端必须分开对待:
 *   - `assessment` 是确定性的,**任何情况下都完整返回**(包括所有降级路径)。
 *   - `suggestions` 来自用户自己配的 chat provider,可能整段为空 —— 此时
 *     `generated=false` + `generated_reason` 给出具体原因。
 *
 * **降级是 HTTP 200,不是错误状态**。`generated=false` 不该走 catch 分支,
 * 也不该把 `assessment` 一起丢掉(见服务端 `routers/ai/income_growth.py`
 * 里「为什么降级是 200 而不是 400」)。真正会抛的只有鉴权失败、账本不可访问、
 * 以及 429 限流(10 次 / 300 秒 / 用户)。
 *
 * 纯读端点,不参与同步:不写 `sync_changes`、没有 `base_change_id`,所以调用方
 * 不要套 `useLedgerWrite`。
 */
import { authedPost } from './http'
import type { InsightDiagnosis } from './insights'

/** 该动哪一侧。服务端 `services/income_growth.py::classify_lever`。 */
export type IncomeLever = 'income' | 'spending' | 'both' | 'none'

/**
 * `target_monthly_gap` 这个数字的来源,优先级见服务端 `classify_gap`:
 * 目标缺口 > 缓冲垫不足 > 结余率地板 > 没有缺口。
 */
export type IncomeGapBasis = 'goal_shortfall' | 'buffer_deficit' | 'surplus_floor' | 'none'

export type IncomeSuggestionKind =
  | 'freelance'
  | 'side_project'
  | 'skill_upgrade'
  | 'job_switch'
  | 'passive'
  | 'other'

/** `generated=false` 时的原因 code。每个 code 在 UI 上对应一个专属空态。 */
export type IncomeGrowthGeneratedReason =
  | 'insufficient_data'
  | 'lever_not_income'
  | 'no_career_profile'
  | 'no_provider'
  | 'provider_failed'

/**
 * 确定性评估。`surplus_ratio` 是比例不是百分比(1.0 = 100%),服务端保留 4 位。
 * `target_monthly_gap` 可能是 0 —— UI 不要渲染 0 缺口。
 */
export interface IncomeGrowthAssessment {
  diagnosis: InsightDiagnosis
  lever: IncomeLever
  median_income: number
  median_expense: number
  median_surplus: number
  buffer: number
  surplus_ratio: number
  basis_periods: number
  target_monthly_gap: number
  gap_basis: IncomeGapBasis
}

/**
 * 一条建议。**后四个数字字段允许 null**,null = 模型没估出来,
 * UI 必须渲染成 dash,**绝对不能当成 0**(「每月 0 元」和「没估」不是一回事)。
 */
export interface IncomeGrowthSuggestion {
  kind: IncomeSuggestionKind
  title: string
  rationale: string
  monthly_potential_low: number | null
  monthly_potential_high: number | null
  effort_hours_per_week: number | null
  time_to_first_income_weeks: number | null
}

export interface IncomeGrowthResponse {
  ledger_id: string
  currency: string
  /** true = 数字只统计了 caller 自己记的账(多成员共享账本),UI 必须透出。 */
  restricted_to_creator: boolean
  assessment: IncomeGrowthAssessment
  generated: boolean
  generated_reason: IncomeGrowthGeneratedReason | null
  /** 只有模型 id,永远不含 api_key / base_url。 */
  model: string | null
  suggestions: IncomeGrowthSuggestion[]
}

export interface IncomeGrowthOptions {
  /** `Ledger.external_id`(客户端口径),不是内部主键。 */
  ledgerId: string
  /** 原始 `useLocale().locale` 值。服务端 pattern 限 zh / zh-CN / zh-TW / en。 */
  locale: string
  /** 服务端限 3..24,默认 6。 */
  lookbackPeriods?: number
  /** 服务端限 -720..840。JS 的 `getTimezoneOffset()` 符号相反,调用方取负号。 */
  tzOffsetMinutes?: number
}

/**
 * 这是一个「用户点了按钮在等」的请求,上游 LLM 最坏要 20 秒 —— 所以支持
 * `signal` 让用户主动放弃等待。abort 后 fetch 抛 `AbortError`,调用方自己
 * 区分「用户放弃」和「真失败」。
 */
export async function requestIncomeGrowth(
  token: string,
  options: IncomeGrowthOptions,
  init?: { signal?: AbortSignal },
): Promise<IncomeGrowthResponse> {
  return authedPost<IncomeGrowthResponse>(
    '/ai/income-growth',
    token,
    {
      ledger_id: options.ledgerId,
      locale: options.locale,
      ...(typeof options.lookbackPeriods === 'number'
        ? { lookback_periods: options.lookbackPeriods }
        : {}),
      ...(typeof options.tzOffsetMinutes === 'number'
        ? { tz_offset_minutes: options.tzOffsetMinutes }
        : {}),
    },
    undefined,
    init?.signal,
  )
}
