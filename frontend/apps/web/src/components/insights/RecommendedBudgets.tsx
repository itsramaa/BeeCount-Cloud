import type { InsightBudgetStatus, InsightRecommendation } from '@beecount/api-client'
import { Button, Card, CardContent, CardHeader, CardTitle, useT } from '@beecount/ui'
import { Amount } from '@beecount/web-features'

interface Props {
  recommendation: InsightRecommendation
  /** 用来算 delta 的现有分类预算(按 category_name 对齐)。 */
  budgetStatus: InsightBudgetStatus[]
  currency: string
  onGoToBudgets: () => void
}

/**
 * 预算建议表 —— 分类 / 建议额 / 当前预算 / 差额。
 *
 * **没有"一键应用"按钮**:服务端 `recommendation.categories[]` 只带
 * `category_name`,而建预算需要 `category_id`,前端拿名字反查会在重名 /
 * 改名场景下把预算挂到错的分类上。这是契约限制,不是样式选择 —— 所以只提供
 * 跳转到预算页的入口。
 */
export function RecommendedBudgets({
  recommendation,
  budgetStatus,
  currency,
  onGoToBudgets,
}: Props) {
  const t = useT()

  if (recommendation.categories.length === 0) return null

  const currentByName = new Map<string, number>()
  for (const b of budgetStatus) {
    if (b.budget_type === 'category' && b.category_name) {
      currentByName.set(b.category_name, b.amount)
    }
  }

  const basisPeriods = recommendation.categories[0]?.basis_periods ?? 0

  return (
    <Card className="bc-panel">
      <CardHeader>
        <CardTitle className="text-base">{t('insights.recommend.title')}</CardTitle>
        <p className="text-xs text-muted-foreground">
          {t('insights.recommend.subtitle', { periods: basisPeriods })}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border/60 text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                <th scope="col" className="py-2 pr-3 font-medium">
                  {t('insights.recommend.colCategory')}
                </th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">
                  {t('insights.recommend.colSuggested')}
                </th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">
                  {t('insights.recommend.colCurrent')}
                </th>
                <th scope="col" className="py-2 text-right font-medium">
                  {t('insights.recommend.colDelta')}
                </th>
              </tr>
            </thead>
            <tbody>
              {recommendation.categories.map((item) => {
                const current = currentByName.get(item.category_name)
                const delta =
                  current === undefined ? null : item.recommended_amount - current
                return (
                  <tr key={item.category_name} className="border-b border-border/30">
                    <th scope="row" className="py-2 pr-3 text-left font-normal text-foreground">
                      {item.category_name}
                    </th>
                    <td className="py-2 pr-3 text-right">
                      <Amount
                        value={item.recommended_amount}
                        currency={currency}
                        showCurrency
                        size="sm"
                      />
                    </td>
                    <td className="py-2 pr-3 text-right">
                      {current === undefined ? (
                        <span className="text-xs text-muted-foreground">
                          {t('insights.recommend.noBudget')}
                        </span>
                      ) : (
                        <Amount value={current} currency={currency} showCurrency size="sm" />
                      )}
                    </td>
                    <td className="py-2 text-right">
                      {delta === null ? (
                        <span className="text-xs text-muted-foreground">—</span>
                      ) : (
                        <Amount
                          value={delta}
                          currency={currency}
                          showCurrency
                          size="sm"
                          sign="always"
                          tone={delta > 0 ? 'negative' : delta < 0 ? 'positive' : 'muted'}
                        />
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">{t('insights.recommend.applyHint')}</p>
          <Button type="button" variant="outline" size="sm" onClick={onGoToBudgets}>
            {t('insights.recommend.goToBudgets')}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
