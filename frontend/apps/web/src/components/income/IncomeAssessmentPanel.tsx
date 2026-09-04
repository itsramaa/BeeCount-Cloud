import { TrendingDown, TrendingUp, Wallet } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import type { IncomeGrowthAssessment } from '@beecount/api-client'
import { Card, CardContent, CardHeader, CardTitle, useT } from '@beecount/ui'
import { Amount } from '@beecount/web-features'

// 图标 / 色调表从洞察页的诊断条借用,不在这里复制一份 —— 同一个 diagnosis code
// 在两个页面必须是同一个图标和同一个色调,两份表迟早分叉。
import { ICON_BY_DIAGNOSIS, TONE_BY_DIAGNOSIS } from '../insights/DiagnosisCard'
import { useMoneyText } from '../../lib/insightMoney'

interface Props {
  assessment: IncomeGrowthAssessment
  currency: string
}

/**
 * 确定性评估面板 —— 不经过 LLM,任何降级路径下都完整渲染。
 *
 * ## 每一行只读一个字段
 *
 * 三行文案分别只看 `diagnosis` / `lever` / `gap_basis` **一个**字段,各自一张
 * 「一个枚举值一个 key」的映射:
 *
 *   line 1  `insights.diagnosis.{diagnosis}.label`   (复用洞察页的 key)
 *   line 2  `income.lever.{lever}`
 *   line 3  `income.gapBasis.{gap_basis}`
 *
 * **不要**改成按 lever × gap_basis × diagnosis 组合分支。一是组合数会爆,二是
 * `t()` 对没匹配上的占位符渲染成**空字符串**(见 `LocaleProvider.applyTemplate`)
 * —— 一个组合分支漏传参数不会报错,只会让句子里的金额静默消失。所以同一组的
 * 四个 key 一律收同一套参数:lever 组 5 个(income / expense / surplus / pct /
 * periods),gapBasis 组 2 个(buffer / pct)。
 *
 * 唯一允许的单字段分支:`insufficient_data` 时整块隐掉中位数瓦片和缺口块 ——
 * 那些数字此时不可信,用 0 填满比不显示更有害。
 */
export function IncomeAssessmentPanel({ assessment, currency }: Props) {
  const t = useT()
  const money = useMoneyText(currency)
  const Icon = ICON_BY_DIAGNOSIS[assessment.diagnosis]

  // 服务端算好的比例(1.0 = 100%),保留 4 位。渲染成一位小数的百分比 ——
  // **这是整页唯一一个百分比**,其余数字全部是服务端给的金额原值,前端不自己
  // 派生任何服务端没算的比率。
  const pct = (assessment.surplus_ratio * 100).toFixed(1)
  const insufficient = assessment.diagnosis === 'insufficient_data'
  // 0 缺口不渲染成一个大大的「¥0」—— 那读起来像「你差 0 元」,而实际含义是
  // 「现在没有缺口」,后者由 gapBasis 那句话表达。
  const hasGap = assessment.target_monthly_gap > 0 && assessment.gap_basis !== 'none'

  const gapSentence = (
    <p className="text-xs text-muted-foreground">
      {t(`income.gapBasis.${assessment.gap_basis}`, {
        buffer: money(assessment.buffer),
        pct,
      })}
    </p>
  )

  return (
    <Card className="bc-panel">
      <CardHeader>
        <CardTitle className="text-base">{t('income.assessment.title')}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-start gap-3">
          <Icon
            aria-hidden
            className={`mt-0.5 h-5 w-5 shrink-0 ${TONE_BY_DIAGNOSIS[assessment.diagnosis]}`}
          />
          <div className="min-w-0 space-y-1">
            <div className="text-sm font-semibold text-foreground">
              {t(`insights.diagnosis.${assessment.diagnosis}.label`)}
            </div>
            <p className="text-xs text-muted-foreground">
              {t(`income.lever.${assessment.lever}`, {
                income: money(assessment.median_income),
                expense: money(assessment.median_expense),
                surplus: money(assessment.median_surplus),
                pct,
                periods: assessment.basis_periods,
              })}
            </p>
          </div>
        </div>

        {insufficient ? null : (
          <>
            {hasGap ? (
              // 缺口是这一页的主数字,用洞察页 allocatable 那套 primary 边框处理,
              // 视觉层级跟「每月可分配」对齐 —— 两者是同一类「据此行动」的数。
              <div className="rounded-xl border border-primary/40 bg-primary/5 p-4">
                <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
                  {t('income.gap.label')}
                </div>
                <div className="mt-1.5">
                  <Amount
                    value={assessment.target_monthly_gap}
                    currency={currency}
                    showCurrency
                    bold
                    size="3xl"
                    className="sm:text-4xl"
                  />
                </div>
                <div className="mt-1">{gapSentence}</div>
              </div>
            ) : (
              gapSentence
            )}

            <div className="grid gap-3 sm:grid-cols-3">
              <MedianTile
                label={t('insights.baseline.medianIncome')}
                value={assessment.median_income}
                currency={currency}
                icon={TrendingUp}
                tone="positive"
              />
              <MedianTile
                label={t('insights.baseline.medianExpense')}
                value={assessment.median_expense}
                currency={currency}
                icon={TrendingDown}
                tone="negative"
              />
              <MedianTile
                label={t('insights.baseline.medianSurplus')}
                value={assessment.median_surplus}
                currency={currency}
                icon={Wallet}
                tone={assessment.median_surplus >= 0 ? 'positive' : 'negative'}
              />
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

/** `buffer` 故意没有瓦片 —— 它只在 buffer_deficit 那句话里出现,单独立一个数字
 *  会让用户把「波动缓冲垫」当成第四个统计量扫过去。 */
function MedianTile({
  label,
  value,
  currency,
  icon: Icon,
  tone,
}: {
  label: string
  value: number
  currency: string
  icon: LucideIcon
  tone: 'positive' | 'negative'
}) {
  return (
    <div className="rounded-xl border border-border/60 bg-card p-4">
      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-muted-foreground">
        <Icon aria-hidden className="h-4 w-4" />
        <span>{label}</span>
      </div>
      <div className="mt-1.5">
        <Amount value={value} currency={currency} showCurrency bold size="2xl" tone={tone} />
      </div>
    </div>
  )
}
