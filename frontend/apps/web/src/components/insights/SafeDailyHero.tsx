import { AlertTriangle, CalendarClock, Wallet } from 'lucide-react'

import type { InsightBudgetStatus } from '@beecount/api-client'
import { Button, Card, CardContent, useT } from '@beecount/ui'
import { Amount } from '@beecount/web-features'

import { useMoneyText } from '../../lib/insightMoney'

interface Props {
  /** `budget_type === 'total'` 的那条;没有总预算时为 null。 */
  totalBudget: InsightBudgetStatus | null
  daysRemaining: number
  currency: string
  onGoToBudgets: () => void
}

/**
 * "今天还能花" —— 全页最大的一个数字。
 *
 * 数字直接取服务端 `budget_status[type=total].safe_daily`,不在前端做
 * `remaining / days` 的第二套除法:周期最后一天服务端返的是整笔剩余而不是
 * 除法结果,前端再算一遍会算出 Infinity。
 *
 * 没有总预算时 **不渲染 0** —— 0 会被读成"一分钱都不能花"这个答案,而真实
 * 情况是"还没有可以回答这个问题的依据"。此时降级成引导去建预算的 CTA。
 */
export function SafeDailyHero({ totalBudget, daysRemaining, currency, onGoToBudgets }: Props) {
  const t = useT()
  const money = useMoneyText(currency)

  if (!totalBudget) {
    return (
      <Card className="bc-panel border-dashed">
        <CardContent className="flex flex-col items-start gap-3 py-6">
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <Wallet aria-hidden className="h-4 w-4 text-primary" />
            {t('insights.hero.noBudget.title')}
          </div>
          <p className="text-xs text-muted-foreground">{t('insights.hero.noBudget.desc')}</p>
          <Button type="button" size="sm" onClick={onGoToBudgets}>
            {t('insights.hero.noBudget.action')}
          </Button>
        </CardContent>
      </Card>
    )
  }

  const lastDay = daysRemaining <= 0

  return (
    <Card className="bc-panel">
      <CardContent className="space-y-2 py-6">
        <div className="flex items-center gap-2 text-[11px] uppercase tracking-wide text-muted-foreground">
          <CalendarClock aria-hidden className="h-4 w-4 text-primary" />
          {lastDay ? t('insights.hero.lastDayTitle') : t('insights.hero.title')}
        </div>
        {/* size="3xl" + sm:text-4xl —— 小屏 text-3xl,sm 以上 text-4xl。 */}
        <div>
          <Amount
            value={totalBudget.safe_daily}
            currency={currency}
            showCurrency
            animate
            bold
            size="3xl"
            className="sm:text-4xl"
            tone={totalBudget.exceeded ? 'negative' : 'default'}
          />
        </div>
        <p className="text-xs text-muted-foreground">
          {lastDay
            ? t('insights.hero.lastDaySub', { remaining: money(totalBudget.remaining) })
            : t('insights.hero.sub', {
                remaining: money(totalBudget.remaining),
                days: daysRemaining,
              })}
        </p>
        {/* exceeded 不能只靠数字变红表达 —— 图标 + 文字把"已超支多少"说出来。 */}
        {totalBudget.exceeded ? (
          <p className="flex items-center gap-1.5 text-xs font-medium text-destructive">
            <AlertTriangle aria-hidden className="h-3.5 w-3.5" />
            {t('insights.hero.exceeded', {
              over: money(totalBudget.used - totalBudget.amount),
            })}
          </p>
        ) : null}
      </CardContent>
    </Card>
  )
}
