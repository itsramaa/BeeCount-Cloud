import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import type { InsightSeriesItem } from '@beecount/api-client'
import { Card, CardContent, CardHeader, CardTitle, useLocale, useT } from '@beecount/ui'

import { formatCompactTick } from '../../i18n/format'

interface Props {
  /** **只**传 `insights.series`(已完成周期)。进行中的 `current` 不能 concat
   *  进来 —— 它是半个周期的部分数据,画进柱图会看起来像收支突然崩了。 */
  series: InsightSeriesItem[]
}

/**
 * 周期收支柱图 + 结余折线。样式跟首页 MonthlyTrendBars 对齐(同一套 token /
 * tick formatter / h-56 容器),用户在两个页面看到的是同一种图。
 */
export function PeriodSeriesChart({ series }: Props) {
  const t = useT()
  const { locale } = useLocale()
  const chinese = locale.startsWith('zh')

  const fmt = (v: number) =>
    v.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 })

  const seriesLabel = (name: string): string => {
    if (name === 'income') return t('home.trendBars.income')
    if (name === 'expense') return t('home.trendBars.expense')
    if (name === 'surplus') return t('home.trendBars.balance')
    return name
  }

  return (
    <Card className="bc-panel overflow-hidden">
      <CardHeader>
        <CardTitle className="text-base">{t('insights.series.title')}</CardTitle>
      </CardHeader>
      <CardContent>
        {series.length === 0 ? (
          <div className="flex h-48 items-center justify-center text-xs text-muted-foreground">
            {t('insights.series.empty')}
          </div>
        ) : (
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={series} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
                <XAxis
                  dataKey="bucket"
                  tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 11 }}
                  stroke="hsl(var(--border))"
                  interval={0}
                />
                <YAxis
                  tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 11 }}
                  stroke="hsl(var(--border))"
                  tickFormatter={(v) =>
                    formatCompactTick(v, { chinese, wanUnit: t('common.unit.10k') })
                  }
                />
                <Tooltip
                  contentStyle={{
                    background: 'hsl(var(--popover))',
                    border: '1px solid hsl(var(--border))',
                    borderRadius: 6,
                    fontSize: 12,
                  }}
                  cursor={{ fill: 'hsl(var(--muted) / 0.4)' }}
                  formatter={
                    ((v: number, name: string) => [
                      fmt(v),
                      seriesLabel(name),
                    ]) as unknown as never
                  }
                />
                <Legend
                  iconType="circle"
                  wrapperStyle={{ fontSize: 11 }}
                  formatter={(v: string) => seriesLabel(v)}
                />
                <Bar dataKey="income" fill="rgb(var(--income-rgb))" radius={[4, 4, 0, 0]} />
                <Bar dataKey="expense" fill="rgb(var(--expense-rgb))" radius={[4, 4, 0, 0]} />
                <Line
                  type="monotone"
                  dataKey="surplus"
                  stroke="hsl(var(--primary))"
                  strokeWidth={2}
                  dot={{ fill: 'hsl(var(--primary))', r: 3 }}
                  activeDot={{ r: 5 }}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
