import { AlertTriangle, CheckCircle2, CircleDashed, TrendingUp } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import type { InsightBaseline, InsightCurrentPeriod, InsightDiagnosis } from '@beecount/api-client'
import { Card, CardContent, useT } from '@beecount/ui'

import { useMoneyText } from '../../lib/insightMoney'

interface Props {
  diagnosis: InsightDiagnosis
  baseline: InsightBaseline
  current: InsightCurrentPeriod
  basisPeriods: number
  currency: string
}

/** 每个 code 一个专属图标 —— 颜色不是唯一的语义载体。 */
const ICON_BY_DIAGNOSIS: Record<InsightDiagnosis, LucideIcon> = {
  insufficient_data: CircleDashed,
  on_track: CheckCircle2,
  reduce_spending: AlertTriangle,
  increase_income: TrendingUp,
}

const TONE_BY_DIAGNOSIS: Record<InsightDiagnosis, string> = {
  insufficient_data: 'text-muted-foreground',
  on_track: 'text-green-600 dark:text-green-500',
  reduce_spending: 'text-red-600 dark:text-red-500',
  increase_income: 'text-orange-600 dark:text-orange-500',
}

/**
 * 诊断条 —— 图标 + 标签 + 一句引用真实数字的解释。
 *
 * 解释句必须带上具体金额:只说"支出偏高"用户无法据此做任何动作,说"本期支出
 * ¥3200 高于你的中位数 ¥2000,少花 ¥1200 就回到线上"才可执行。
 */
export function DiagnosisCard({ diagnosis, baseline, current, basisPeriods, currency }: Props) {
  const t = useT()
  const money = useMoneyText(currency)
  const Icon = ICON_BY_DIAGNOSIS[diagnosis]

  return (
    <Card className="bc-panel">
      <CardContent className="flex items-start gap-3 py-4">
        <Icon aria-hidden className={`mt-0.5 h-5 w-5 shrink-0 ${TONE_BY_DIAGNOSIS[diagnosis]}`} />
        <div className="min-w-0 space-y-1">
          <div className="text-sm font-semibold text-foreground">
            {t(`insights.diagnosis.${diagnosis}.label`)}
          </div>
          <p className="text-xs text-muted-foreground">
            {t(`insights.diagnosis.${diagnosis}.detail`, {
              periods: basisPeriods,
              expense: money(current.expense),
              median: money(baseline.median_expense),
              gap: money(Math.max(0, current.expense - baseline.median_expense)),
              surplus: money(baseline.median_surplus),
              income: money(baseline.median_income),
            })}
          </p>
        </div>
      </CardContent>
    </Card>
  )
}
