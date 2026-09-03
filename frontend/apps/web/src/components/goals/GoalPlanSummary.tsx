import { Users } from 'lucide-react'

import type { GoalPlanResponse } from '@beecount/api-client'
import { Badge, Card, CardContent, CardHeader, CardTitle, Tooltip, useT } from '@beecount/ui'
import { Amount } from '@beecount/web-features'

interface Props {
  plan: GoalPlanResponse
}

/**
 * 分配总览 —— 每月能拿出多少 / 目标总共需要多少 / 还剩多少没分配。
 *
 * `allocatable` 已经扣掉了 buffer(用户自己月支出的 MAD),不是"结余全额",
 * 所以这里不再展示 median_surplus 免得两个数打架 —— 完整基线在洞察页。
 */
export function GoalPlanSummary({ plan }: Props) {
  const t = useT()
  const shortOverall = plan.total_required_monthly > plan.allocatable

  return (
    <Card className="bc-panel">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-base">{t('goals.plan.title')}</CardTitle>
          {plan.restricted_to_creator ? (
            <Tooltip content={t('insights.restricted.hint')}>
              <Badge variant="outline" className="gap-1">
                <Users aria-hidden className="h-3 w-3" />
                {t('insights.restricted.label')}
              </Badge>
            </Tooltip>
          ) : null}
        </div>
        <p className="text-xs text-muted-foreground">
          {t('goals.plan.basis', { periods: plan.baseline.basis_periods })}
        </p>
      </CardHeader>
      <CardContent className="grid gap-3 md:grid-cols-3">
        <Tile
          label={t('goals.plan.allocatable')}
          value={plan.allocatable}
          currency={plan.currency}
        />
        <Tile
          label={t('goals.plan.required')}
          value={plan.total_required_monthly}
          currency={plan.currency}
          tone={shortOverall ? 'negative' : 'default'}
        />
        <Tile
          label={t('goals.plan.unallocated')}
          value={plan.unallocated}
          currency={plan.currency}
          tone="muted"
        />
      </CardContent>
    </Card>
  )
}

function Tile({
  label,
  value,
  currency,
  tone = 'default',
}: {
  label: string
  value: number
  currency: string
  tone?: 'default' | 'negative' | 'muted'
}) {
  const t = useT()
  return (
    <div className="rounded-xl border border-border/60 bg-card p-4">
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-1.5 flex items-baseline gap-1">
        <Amount value={value} currency={currency} showCurrency bold size="2xl" tone={tone} />
        <span className="text-xs text-muted-foreground">{t('goals.plan.perMonth')}</span>
      </div>
    </div>
  )
}
