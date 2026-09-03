import { CalendarDays, Flag } from 'lucide-react'

import type { GoalItem, GoalPlanItem } from '@beecount/api-client'
import { Button, useT } from '@beecount/ui'
import { Amount } from '@beecount/web-features'

import { useMoneyText } from '../../lib/insightMoney'

import { FeasibilityBadge } from './FeasibilityBadge'

interface Props {
  goal: GoalItem
  /** plan 端点挂了或该目标非 active 时为 null —— 卡片仍然完整可读。 */
  planItem: GoalPlanItem | null
  onEdit: () => void
  onDelete: () => void
}

/**
 * 进度条只有两个状态,不套预算页那套 `≥90% 红` 的阈值配色 —— 预算条量的是
 * "花掉多少"(越满越危险),目标条量的是"攒够多少"(越满越好),照搬会把
 * 攒满的目标画成深红。攒满后转绿(绿在预算配色里也是"没问题"),其余用主题色。
 */
function progressColor(ratio: number): string {
  return ratio >= 1 ? 'bg-green-500' : 'bg-primary'
}

export function GoalCard({ goal, planItem, onEdit, onDelete }: Props) {
  const t = useT()
  const money = useMoneyText(goal.currency)

  // progress_pct 可以 > 100(允许超额攒完),文字显示真实值,条封顶 100%。
  const pct = goal.progress_pct
  const barPct = Math.min(100, Math.max(0, pct))
  const ratio = goal.target_amount > 0 ? goal.saved_amount / goal.target_amount : 0
  const barId = `goal-progress-${goal.id}`

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border/60 bg-card p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-foreground">{goal.name}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
            <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
              <Flag aria-hidden className="h-3 w-3" />
              {t('goals.card.priority', { n: goal.priority })}
            </span>
            {goal.status !== 'active' ? (
              <span className="text-[11px] text-muted-foreground">
                {t(`goals.status.${goal.status}`)}
              </span>
            ) : null}
          </div>
        </div>
        {planItem ? <FeasibilityBadge feasibility={planItem.feasibility} /> : null}
      </div>

      <div className="space-y-1.5">
        <div
          role="progressbar"
          aria-valuenow={Math.round(pct)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-labelledby={barId}
          className="h-2 w-full overflow-hidden rounded-full bg-muted"
        >
          <div
            className={`h-full rounded-full transition-all ${progressColor(ratio)}`}
            style={{ width: `${barPct}%` }}
          />
        </div>
        <div id={barId} className="flex items-center justify-between text-xs">
          <span className="text-muted-foreground">
            {t('goals.card.savedOfTarget', {
              saved: money(goal.saved_amount),
              target: money(goal.target_amount),
            })}
          </span>
          <span className="font-mono tabular-nums text-foreground">
            {t('goals.card.percent', { pct: pct.toFixed(1) })}
          </span>
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-2 text-xs">
        <div>
          <dt className="text-muted-foreground">{t('goals.card.remaining')}</dt>
          <dd className="mt-0.5">
            <Amount value={goal.remaining_amount} currency={goal.currency} showCurrency size="sm" />
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t('goals.card.deadline')}</dt>
          <dd className="mt-0.5 inline-flex items-center gap-1 text-foreground">
            <CalendarDays aria-hidden className="h-3 w-3 text-muted-foreground" />
            {goal.deadline ? (
              <span>
                {goal.deadline}
                {planItem?.months_remaining != null
                  ? ` · ${t('goals.card.monthsLeft', { n: planItem.months_remaining })}`
                  : ''}
              </span>
            ) : (
              <span className="text-muted-foreground">{t('goals.card.noDeadline')}</span>
            )}
          </dd>
        </div>
        {planItem ? (
          <>
            <div>
              <dt className="text-muted-foreground">{t('goals.card.requiredMonthly')}</dt>
              <dd className="mt-0.5">
                {planItem.required_monthly == null ? (
                  <span className="text-muted-foreground">—</span>
                ) : (
                  <Amount
                    value={planItem.required_monthly}
                    currency={goal.currency}
                    showCurrency
                    size="sm"
                  />
                )}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">{t('goals.card.allocatedMonthly')}</dt>
              <dd className="mt-0.5">
                <Amount
                  value={planItem.allocated_monthly}
                  currency={goal.currency}
                  showCurrency
                  size="sm"
                  tone={planItem.shortfall > 0 ? 'negative' : 'default'}
                />
              </dd>
            </div>
          </>
        ) : null}
      </dl>

      <p className="text-[11px] text-muted-foreground">
        {planItem
          ? t(`goals.feasibility.${planItem.feasibility}.detail`, {
              allocated: money(planItem.allocated_monthly),
              required: money(planItem.required_monthly ?? 0),
              shortfall: money(planItem.shortfall),
              months: planItem.projected_months ?? 0,
            })
          : t('goals.card.noPlan')}
      </p>

      {planItem && planItem.feasibility === 'infeasible' ? (
        <GapOptions planItem={planItem} money={money} />
      ) : null}

      <div className="flex items-center justify-end gap-2">
        <Button type="button" variant="ghost" size="sm" onClick={onEdit}>
          {t('common.edit')}
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onDelete}>
          {t('common.delete')}
        </Button>
      </div>
    </div>
  )
}

/**
 * 差额补齐的三条路 —— 全部从服务端返的数字算出来,不给泛泛建议:
 * 把截止日推到 `projected_months` 之后 / 从某笔预算里腾出 `shortfall` /
 * 每月多 `shortfall` 的收入。
 */
function GapOptions({
  planItem,
  money,
}: {
  planItem: GoalPlanItem
  money: (value: number | null | undefined) => string
}) {
  const t = useT()
  return (
    <div className="rounded-lg border border-border/60 bg-muted/30 p-3">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {t('goals.gap.title')}
      </div>
      <ul className="mt-1.5 space-y-1 text-[11px] text-muted-foreground">
        {planItem.projected_months != null ? (
          <li>{t('goals.gap.shiftDeadline', { months: planItem.projected_months })}</li>
        ) : null}
        <li>{t('goals.gap.freeBudget', { shortfall: money(planItem.shortfall) })}</li>
        <li>{t('goals.gap.addIncome', { shortfall: money(planItem.shortfall) })}</li>
      </ul>
    </div>
  )
}
