import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { CircleDashed, TrendingUp, Users } from 'lucide-react'

import { fetchLedgerInsights, type LedgerInsights } from '@beecount/api-client'
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  EmptyState,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  Tooltip,
  useT,
  useToast,
} from '@beecount/ui'
import { Amount } from '@beecount/web-features'

import { BaselineTiles } from '../../components/insights/BaselineTiles'
import { DiagnosisCard } from '../../components/insights/DiagnosisCard'
import { PeriodSeriesChart } from '../../components/insights/PeriodSeriesChart'
import { RecommendedBudgets } from '../../components/insights/RecommendedBudgets'
import { SafeDailyHero } from '../../components/insights/SafeDailyHero'
import { useAuth } from '../../context/AuthContext'
import { useLedgers } from '../../context/LedgersContext'
import { usePageCache } from '../../context/PageDataCacheContext'
import { useSyncRefresh } from '../../context/SyncSocketContext'
import { localizeError } from '../../i18n/errors'
import { useMoneyText } from '../../lib/insightMoney'

/** 服务端限 3..24;这里只放四档,避免给出一个服务端会 422 的值。 */
const LOOKBACK_OPTIONS = [3, 6, 12, 24] as const
const DEFAULT_LOOKBACK = 6

/**
 * 财务洞察页 —— 把 `/ledgers/{id}/insights` 的数字翻成能照着做决定的一页。
 *
 * 页面自己负责取数(fetch + 缓存 + 错误提示),展示组件全部无状态。数字口径
 * 一律用服务端算好的字段,不在前端重算 median / safe_daily —— 两套算法迟早
 * 对不上。
 */
export function InsightsPage() {
  const t = useT()
  const toast = useToast()
  const navigate = useNavigate()
  const { token } = useAuth()
  const { activeLedgerId, currency } = useLedgers()
  const money = useMoneyText(currency)

  const bucket = activeLedgerId || '__none__'
  const [data, setData] = usePageCache<LedgerInsights | null>(`insights:${bucket}:data`, null)
  const [lookback, setLookback] = useState<number>(DEFAULT_LOOKBACK)
  // 首帧没有缓存时是 loading;有缓存则直接显示旧值,后台静默刷新。
  const [loading, setLoading] = useState<boolean>(() => data === null)

  const notifyError = useCallback(
    (err: unknown) => toast.error(localizeError(err, t), t('notice.error')),
    [toast, t],
  )

  const refresh = useCallback(async () => {
    if (!activeLedgerId) {
      setData(null)
      setLoading(false)
      return
    }
    try {
      const resp = await fetchLedgerInsights(token, activeLedgerId, {
        lookbackPeriods: lookback,
        // 服务端按这个偏移切周期边界。JS 的 getTimezoneOffset 符号跟服务端
        // 参数相反(东八区是 -480),取负号对齐。
        tzOffsetMinutes: -new Date().getTimezoneOffset(),
      })
      setData(resp)
    } catch (err) {
      notifyError(err)
    } finally {
      setLoading(false)
    }
    // setData 来自 usePageCache,引用稳定
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, activeLedgerId, lookback, notifyError])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // 洞察的数字全部派生自交易 / 预算,同步流一动就得重算。
  useSyncRefresh(() => {
    void refresh()
  })

  const totalBudget = useMemo(
    () => data?.budget_status.find((b) => b.budget_type === 'total') ?? null,
    [data],
  )

  const goToBudgets = useCallback(() => navigate('/app/budgets'), [navigate])

  if (!activeLedgerId) {
    return (
      <Card className="bc-panel">
        <CardHeader>
          <CardTitle>{t('nav.insights')}</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">{t('shell.selectLedgerFirst')}</p>
        </CardContent>
      </Card>
    )
  }

  if (loading && !data) {
    return <InsightsSkeleton />
  }

  if (!data) {
    return (
      <Card className="bc-panel">
        <CardHeader>
          <CardTitle>{t('nav.insights')}</CardTitle>
        </CardHeader>
        <CardContent>
          <EmptyState
            icon={<CircleDashed className="h-6 w-6" />}
            title={t('insights.loadFailed.title')}
            description={t('insights.loadFailed.desc')}
          />
        </CardContent>
      </Card>
    )
  }

  const insufficient = data.diagnosis === 'insufficient_data'
  // 没有总预算时英雄区降级成 CTA,诊断条上移到第一位 —— 页面第一屏必须是
  // 一个有内容的结论,不能是一张引导卡。
  // safe_daily 只来自当前周期的预算用量,跟 median 无关,所以样本不足时仍然有效。
  const heroFirst = totalBudget !== null
  const diagnosisStrip = (
    <>
      <DiagnosisCard
        diagnosis={data.diagnosis}
        baseline={data.baseline}
        current={data.current}
        basisPeriods={data.range.basis_periods}
        currency={data.currency}
      />
      {/* increase_income 是唯一「支出没超基线、但结余率仍偏低」的诊断 —— 也就是
          唯一该动收入侧的情形。只在这一个 code 下给收入增长页入口,其余诊断下
          给这个入口等于建议用户去做一个更难的动作。DiagnosisCard 本身不动。 */}
      {data.diagnosis === 'increase_income' ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border/60 bg-card px-4 py-3">
          <p className="min-w-0 text-xs text-muted-foreground">{t('income.cta.title')}</p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => navigate('/app/income-growth')}
          >
            <TrendingUp aria-hidden className="mr-1 h-4 w-4" />
            {t('income.cta.action')}
          </Button>
        </div>
      ) : null}
    </>
  )
  const hero = (
    <SafeDailyHero
      totalBudget={totalBudget}
      daysRemaining={data.current.days_remaining}
      currency={data.currency}
      onGoToBudgets={goToBudgets}
    />
  )

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-foreground">{t('nav.insights')}</h1>
          {data.range.restricted_to_creator ? (
            <Tooltip content={t('insights.restricted.hint')}>
              <Badge variant="outline" className="gap-1">
                <Users aria-hidden className="h-3 w-3" />
                {t('insights.restricted.label')}
              </Badge>
            </Tooltip>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <Label className="text-xs text-muted-foreground" htmlFor="insights-lookback">
            {t('insights.window.label')}
          </Label>
          <Select value={`${lookback}`} onValueChange={(v) => setLookback(Number(v))}>
            <SelectTrigger id="insights-lookback" className="h-9 w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {LOOKBACK_OPTIONS.map((n) => (
                <SelectItem key={n} value={`${n}`}>
                  {t('insights.window.option', { n })}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {heroFirst ? hero : diagnosisStrip}
      {heroFirst ? diagnosisStrip : hero}

      {/* 样本不足时不用 0 填满基线 / 建议 / 图表 —— 三块整体换成一个说明为什么
          的空态,当前周期卡保留(那是真实数据)。 */}
      {insufficient ? (
        <Card className="bc-panel">
          <CardContent className="py-2">
            <EmptyState
              icon={<CircleDashed className="h-6 w-6" />}
              title={t('insights.insufficient.title')}
              description={
                data.baseline.median_income <= 0
                  ? t('insights.insufficient.noIncome')
                  : t('insights.insufficient.needPeriods', { n: data.range.basis_periods })
              }
            />
          </CardContent>
        </Card>
      ) : (
        <BaselineTiles baseline={data.baseline} currency={data.currency} />
      )}

      <CurrentPeriodCard data={data} money={money} />

      {insufficient ? null : (
        <>
          <RecommendedBudgets
            recommendation={data.recommendation}
            budgetStatus={data.budget_status}
            currency={data.currency}
            onGoToBudgets={goToBudgets}
          />
          <PeriodSeriesChart series={data.series} />
        </>
      )}
    </div>
  )
}

/**
 * 当前(进行中)周期。单独一张卡 + 常驻说明 —— 这个周期故意没算进中位数,
 * 不说清楚的话用户会拿它跟基线对比后得出错误结论。
 */
function CurrentPeriodCard({
  data,
  money,
}: {
  data: LedgerInsights
  money: (value: number | null | undefined) => string
}) {
  const t = useT()
  return (
    <Card className="bc-panel">
      <CardHeader>
        <CardTitle className="text-base">
          {t('insights.current.title', { bucket: data.current.bucket })}
        </CardTitle>
        <p className="text-xs text-muted-foreground">{t('insights.current.excludedNote')}</p>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
          <Metric label={t('home.trendBars.income')} value={data.current.income} currency={data.currency} tone="positive" />
          <Metric label={t('home.trendBars.expense')} value={data.current.expense} currency={data.currency} tone="negative" />
          <Metric
            label={t('home.trendBars.balance')}
            value={data.current.surplus}
            currency={data.currency}
            tone={data.current.surplus >= 0 ? 'positive' : 'negative'}
          />
          <div className="rounded-xl border border-border/60 bg-card p-4">
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
              {t('insights.current.progress')}
            </div>
            <div className="mt-1.5 font-mono text-2xl font-bold tabular-nums text-foreground">
              {t('insights.current.progressValue', {
                elapsed: data.current.days_elapsed,
                total: data.current.days_total,
              })}
            </div>
          </div>
        </div>
        <p className="text-xs text-muted-foreground">
          {t('insights.current.remainingDays', {
            days: data.current.days_remaining,
            surplus: money(data.current.surplus),
          })}
        </p>
      </CardContent>
    </Card>
  )
}

function Metric({
  label,
  value,
  currency,
  tone,
}: {
  label: string
  value: number
  currency: string
  tone: 'positive' | 'negative'
}) {
  return (
    <div className="rounded-xl border border-border/60 bg-card p-4">
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-1.5">
        <Amount value={value} currency={currency} showCurrency bold size="2xl" tone={tone} />
      </div>
    </div>
  )
}

/** 骨架屏形状对齐最终卡片顺序。英雄区只放灰块,绝不先渲染 0。 */
function InsightsSkeleton() {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <Skeleton className="h-6 w-28" />
        <Skeleton className="h-9 w-32" />
      </div>
      <Card className="bc-panel">
        <CardContent className="space-y-3 py-6">
          <Skeleton className="h-3 w-32" />
          <Skeleton className="h-10 w-48" />
          <Skeleton className="h-3 w-56" />
        </CardContent>
      </Card>
      <Card className="bc-panel">
        <CardContent className="flex items-start gap-3 py-4">
          <Skeleton className="h-5 w-5 rounded-full" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-3 w-full max-w-md" />
          </div>
        </CardContent>
      </Card>
      <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="space-y-2 rounded-xl border border-border/60 bg-card p-4">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="h-7 w-24" />
          </div>
        ))}
      </div>
      <Card className="bc-panel">
        <CardContent className="py-6">
          <Skeleton className="h-56 w-full" />
        </CardContent>
      </Card>
    </div>
  )
}
