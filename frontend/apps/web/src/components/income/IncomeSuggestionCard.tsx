import {
  AlertTriangle,
  ArrowRightLeft,
  Briefcase,
  Coins,
  GraduationCap,
  Lightbulb,
  Rocket,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import type { IncomeGrowthSuggestion, IncomeSuggestionKind } from '@beecount/api-client'
import { Badge, useT } from '@beecount/ui'

import {
  capacityColor,
  formatUnitNumber,
  potentialShape,
  showCapacityBar,
} from '../../lib/incomeSuggestion'
import { useMoneyText } from '../../lib/insightMoney'

interface Props {
  suggestion: IncomeGrowthSuggestion
  currency: string
  /** 用户职业档案里填的每周可投入小时数。null / 0 = 没填,此时不画容量条。 */
  availableHoursPerWeek: number | null
  /** 卡片索引 —— 只用来给容量条的 aria-labelledby 造唯一 id。 */
  index: number
}

/** 每个 kind 一个专属图标,始终图标 + 文字 —— Badge 一律 outline,颜色不承载语义。 */
const ICON_BY_KIND: Record<IncomeSuggestionKind, LucideIcon> = {
  freelance: Briefcase,
  side_project: Rocket,
  skill_upgrade: GraduationCap,
  job_switch: ArrowRightLeft,
  passive: Coins,
  other: Lightbulb,
}

/**
 * 单条收入建议卡片。
 *
 * 四个数字字段(potential low / high、effort、weeks)都可能是 null —— null 的
 * 含义是「模型没估出来」,**必须**渲染成 `common.dash`。渲染成 0 会变成一个
 * 编造出来的具体承诺(「每月 0 元」/「0 小时就能做」)。
 */
export function IncomeSuggestionCard({ suggestion, currency, availableHoursPerWeek, index }: Props) {
  const t = useT()
  const money = useMoneyText(currency)
  const Icon = ICON_BY_KIND[suggestion.kind]
  const dash = t('common.dash')

  const low = suggestion.monthly_potential_low
  const high = suggestion.monthly_potential_high
  // 四种形态分别对应四条文案 —— 一头缺失时说清「起码」还是「最多」,不要把
  // 缺失那头补成 0 然后画出一个假的区间。判定见 lib/incomeSuggestion.ts。
  const shape = potentialShape(low, high)
  const potential =
    shape === 'range'
      ? t('income.suggest.potentialRange', { low: money(low), high: money(high) })
      : shape === 'from'
        ? t('income.suggest.potentialFrom', { low: money(low) })
        : shape === 'upTo'
          ? t('income.suggest.potentialUpTo', { high: money(high) })
          : dash

  const effort = suggestion.effort_hours_per_week
  const weeks = suggestion.time_to_first_income_weeks
  const showCapacity = showCapacityBar(availableHoursPerWeek, effort)
  // 颜色看真实比例(可以 > 1,那正是「超出你的可投入时间」),宽度封顶 100%。
  const ratio =
    showCapacity && effort != null && availableHoursPerWeek
      ? effort / availableHoursPerWeek
      : 0
  const barId = `income-capacity-${index}`

  return (
    <div className="flex flex-col gap-2.5 rounded-xl border border-border/60 bg-card p-4">
      <Badge variant="outline" className="w-fit gap-1">
        <Icon aria-hidden className="h-3 w-3" />
        {t(`income.kind.${suggestion.kind}`)}
      </Badge>

      {/* 标题不 truncate:LLM 给的标题就是这条建议本身,截掉一半等于没给。 */}
      <div className="break-words text-sm font-semibold text-foreground">{suggestion.title}</div>

      {suggestion.rationale ? (
        <p className="break-words text-xs text-muted-foreground">{suggestion.rationale}</p>
      ) : null}

      <dl className="grid grid-cols-3 gap-2 text-xs">
        <div>
          <dt className="text-muted-foreground">{t('income.suggest.potential')}</dt>
          <dd className="mt-0.5 break-words font-mono tabular-nums text-foreground">{potential}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t('income.suggest.effort')}</dt>
          <dd className="mt-0.5 font-mono tabular-nums text-foreground">
            {effort == null
              ? dash
              : t('income.suggest.hoursPerWeek', { n: formatUnitNumber(effort) })}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t('income.suggest.firstIncome')}</dt>
          <dd className="mt-0.5 font-mono tabular-nums text-foreground">
            {weeks == null ? dash : t('income.suggest.weeks', { n: formatUnitNumber(weeks) })}
          </dd>
        </div>
      </dl>

      {showCapacity && effort != null && availableHoursPerWeek != null ? (
        <div className="space-y-1.5">
          <div
            role="progressbar"
            aria-valuenow={Math.round(ratio * 100)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-labelledby={barId}
            className="h-2 w-full overflow-hidden rounded-full bg-muted"
          >
            <div
              className={`h-full rounded-full transition-all ${capacityColor(ratio)}`}
              style={{ width: `${Math.min(100, ratio * 100)}%` }}
            />
          </div>
          <div id={barId} className="text-[11px] text-muted-foreground">
            {t('income.capacity.label', {
              effort: formatUnitNumber(effort),
              available: formatUnitNumber(availableHoursPerWeek),
            })}
          </div>
          {ratio > 1 ? (
            <p className="flex items-start gap-1.5 text-[11px] text-orange-600 dark:text-orange-500">
              <AlertTriangle aria-hidden className="mt-0.5 h-3 w-3 shrink-0" />
              <span>
                {t('income.capacity.over', {
                  effort: formatUnitNumber(effort),
                  available: formatUnitNumber(availableHoursPerWeek),
                })}
              </span>
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
