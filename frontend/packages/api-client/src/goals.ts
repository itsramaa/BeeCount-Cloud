/**
 * 攒钱目标(financial goals)API client —— CRUD + 分配计划。
 *
 * **server-only 实体,不参与同步**:目标只存服务端(同 personal_access_tokens
 * 的先例),不写 `sync_changes`、没有 `read_*_projection`、没有 `/write/*` 端点。
 * 所以 goal mutation **不要**走 `useLedgerWrite` / `retryOnConflict` /
 * `base_change_id` —— 那套是给同步实体的写冲突检测用的。
 *
 * 共享账本里成员之间互相隔离:别人的目标一律 404。跨成员读 plan 回
 * **200 + `items: []`**,不是 404。
 */
import { authedDelete, authedGet, authedPatch, authedPost } from './http'

export type GoalStatus = 'active' | 'achieved' | 'archived'
export type GoalStatusFilter = GoalStatus | 'all'

/**
 * 可行性 code。判定规则在服务端 `services/goal_plan.py::classify_feasibility`:
 * - `feasible`    分配到的月供覆盖了所需月供
 * - `tight`       刚好覆盖,一次意外支出就破
 * - `infeasible`  分配不足,有 shortfall
 * - `no_deadline` 没设截止日期,因此没有"每月必须存多少"
 */
export type GoalFeasibility = 'feasible' | 'tight' | 'infeasible' | 'no_deadline'

/** `remaining_amount` / `progress_pct` 是服务端算好的派生量,前端不再自己算。
 *  `saved_amount` 允许超过 `target_amount`,此时 `progress_pct` > 100。 */
export interface GoalItem {
  id: string
  ledger_id: string
  name: string
  target_amount: number
  saved_amount: number
  remaining_amount: number
  progress_pct: number
  currency: string
  deadline: string | null
  priority: number
  status: GoalStatus
  created_at: string
  updated_at: string
}

export interface GoalListResponse {
  ledger_id: string
  ledger_currency: string
  items: GoalItem[]
}

export interface GoalCreatePayload {
  name: string
  target_amount: number
  saved_amount?: number
  /** `YYYY-MM-DD`,必须严格晚于今天(服务端 POST / PATCH 两条路径都校验)。 */
  deadline?: string | null
  priority?: number
  status?: GoalStatus
}

/**
 * 部分更新:只有显式给出的字段会改。
 *
 * 代价:`deadline` 一旦设上就没法清空(`null` 被服务端当成"不动这个字段"),
 * 需要清空时删掉目标重建。UI 应该在提示里写明,不要放一个不生效的清除按钮。
 */
export type GoalPatchPayload = Partial<GoalCreatePayload>

/** `months_remaining` / `required_monthly` / `projected_months` 可为 null
 *  —— 没有 deadline 就没有"每月必须存多少",没有分配就估不出完成月数。 */
export interface GoalPlanItem {
  goal_id: string
  name: string
  target_amount: number
  saved_amount: number
  remaining_amount: number
  priority: number
  deadline: string | null
  months_remaining: number | null
  required_monthly: number | null
  allocated_monthly: number
  shortfall: number
  projected_months: number | null
  feasibility: GoalFeasibility
}

/** 跟 insights 同一份 `compute_surplus_baseline` 口径,`buffer` 是月支出 MAD。 */
export interface GoalPlanBaseline {
  median_income: number
  median_expense: number
  median_surplus: number
  buffer: number
  allocatable: number
  basis_periods: number
}

/** `items` 只含 `status === 'active'` 的目标 —— achieved / archived 不参与分配。 */
export interface GoalPlanResponse {
  ledger_id: string
  currency: string
  restricted_to_creator: boolean
  baseline: GoalPlanBaseline
  allocatable: number
  total_required_monthly: number
  unallocated: number
  items: GoalPlanItem[]
}

export async function fetchGoals(
  token: string,
  ledgerId: string,
  status?: GoalStatusFilter,
): Promise<GoalListResponse> {
  const suffix = status ? `?status=${encodeURIComponent(status)}` : ''
  return authedGet<GoalListResponse>(
    `/ledgers/${encodeURIComponent(ledgerId)}/goals${suffix}`,
    token,
  )
}

export async function createGoal(
  token: string,
  ledgerId: string,
  payload: GoalCreatePayload,
): Promise<GoalItem> {
  return authedPost<GoalItem>(`/ledgers/${encodeURIComponent(ledgerId)}/goals`, token, payload)
}

export async function updateGoal(
  token: string,
  ledgerId: string,
  goalId: string,
  patch: GoalPatchPayload,
): Promise<GoalItem> {
  return authedPatch<GoalItem>(
    `/ledgers/${encodeURIComponent(ledgerId)}/goals/${encodeURIComponent(goalId)}`,
    token,
    patch,
  )
}

/** 服务端回 204 无 body;`parseResponse` 已处理,这里签名就是 `void`。 */
export async function deleteGoal(
  token: string,
  ledgerId: string,
  goalId: string,
): Promise<void> {
  await authedDelete<void>(
    `/ledgers/${encodeURIComponent(ledgerId)}/goals/${encodeURIComponent(goalId)}`,
    token,
  )
}

export async function fetchGoalPlan(
  token: string,
  ledgerId: string,
  options?: { lookbackPeriods?: number; tzOffsetMinutes?: number },
): Promise<GoalPlanResponse> {
  const query = new URLSearchParams()
  if (options?.lookbackPeriods) query.set('lookback_periods', `${options.lookbackPeriods}`)
  if (typeof options?.tzOffsetMinutes === 'number')
    query.set('tz_offset_minutes', `${options.tzOffsetMinutes}`)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return authedGet<GoalPlanResponse>(
    `/ledgers/${encodeURIComponent(ledgerId)}/goals/plan${suffix}`,
    token,
  )
}
