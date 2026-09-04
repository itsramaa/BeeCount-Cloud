import { useEffect, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  ClipboardList,
  Loader2,
  PlugZap,
  Sparkles,
} from 'lucide-react'

import type { IncomeGrowthResponse } from '@beecount/api-client'
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  EmptyState,
  useT,
} from '@beecount/ui'

import { IncomeSuggestionCard } from './IncomeSuggestionCard'

interface Props {
  /** null = 这个桶还没跑过。 */
  data: IncomeGrowthResponse | null
  running: boolean
  availableHoursPerWeek: number | null
  onRun: () => void
  onOpenAiSettings: () => void
  onOpenCareerProfile: () => void
  onOpenTransactions: () => void
  onOpenGoals: () => void
  onStop: () => void
}

/**
 * 建议区。`assessment` 面板永远渲染在它上面,所以这里的每个空态都只是「这一块
 * 没有内容」,不会变成一整页空白。
 *
 * 一共八种互斥状态,按下面的顺序取第一个命中:
 *
 *   running                          → 进度面板(不是骨架屏,见下)
 *   data == null                     → 还没跑过
 *   generated=false + reason         → 五个专属空态,一个 reason 一个
 *   generated=true + suggestions=[]  → 「模型答了但没有可用内容」
 *   否则                              → 建议网格
 *
 * `generated=false` 的五个 reason 各有各的下一步动作,所以不能合并成一个
 * 「暂无建议」:`no_provider` 要去配 AI、`no_career_profile` 要去填档案、
 * `lever_not_income` 其实是**好消息**(不该画成失败)。
 */
export function IncomeSuggestionsPanel({
  data,
  running,
  availableHoursPerWeek,
  onRun,
  onOpenAiSettings,
  onOpenCareerProfile,
  onOpenTransactions,
  onOpenGoals,
  onStop,
}: Props) {
  const t = useT()
  const generated = data?.generated === true
  const hasCards = generated && (data?.suggestions.length ?? 0) > 0

  return (
    <Card className="bc-panel">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle className="text-base">{t('income.suggestions.title')}</CardTitle>
            {/* 「模型写的」标在区块头上,不标在每张卡上 —— 逐卡重复一遍会让它变成
                装饰,读者反而不再读它。 */}
            {hasCards ? (
              <Badge variant="outline" className="gap-1">
                <Sparkles aria-hidden className="h-3 w-3" />
                {t('income.suggestions.modelBadge')}
              </Badge>
            ) : null}
          </div>
        </div>
        {hasCards && data?.model ? (
          // 真实模型 id,不是「AI 生成」这种含糊说法,也不编造置信度分数。
          <p className="text-[11px] text-muted-foreground">
            {t('income.suggestions.modelLine', { model: data.model })}
          </p>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-3">
        {running ? (
          <RunningPanel onStop={onStop} />
        ) : data === null ? (
          <EmptyState
            icon={<Sparkles className="h-6 w-6" />}
            title={t('income.state.notRun.title')}
            description={t('income.state.notRun.desc')}
            action={
              <Button type="button" size="sm" onClick={onRun}>
                {t('income.run')}
              </Button>
            }
          />
        ) : !data.generated ? (
          <DegradedState
            data={data}
            onRun={onRun}
            onOpenAiSettings={onOpenAiSettings}
            onOpenCareerProfile={onOpenCareerProfile}
            onOpenTransactions={onOpenTransactions}
            onOpenGoals={onOpenGoals}
          />
        ) : data.suggestions.length === 0 ? (
          <EmptyState
            icon={<Sparkles className="h-6 w-6" />}
            title={t('income.state.empty.title')}
            description={t('income.state.empty.desc')}
            action={
              <Button type="button" size="sm" onClick={onRun}>
                {t('income.action.tryAgain')}
              </Button>
            }
          />
        ) : (
          <>
            {/* 没填可投入小时数时,整块只提示一次 —— 每张卡都提示一遍是同一句话
                重复 5 次。 */}
            {availableHoursPerWeek == null || availableHoursPerWeek <= 0 ? (
              <p className="text-[11px] text-muted-foreground">
                {t('income.suggestions.capacityUnknown')}
              </p>
            ) : null}
            {/* 两列而不是三列:每张卡带一段 rationale 段落,三列会把它压成一条
                窄柱子。 */}
            <div className="grid gap-3 md:grid-cols-2">
              {data.suggestions.map((suggestion, index) => (
                <IncomeSuggestionCard
                  key={`${suggestion.kind}-${index}`}
                  suggestion={suggestion}
                  currency={data.currency}
                  availableHoursPerWeek={availableHoursPerWeek}
                  index={index}
                />
              ))}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

/**
 * 运行中面板 —— 故意**不用** Skeleton。
 *
 * 骨架屏承诺的是「内容马上到,形状就是这样」。这里等的是用户自己配的上游 LLM,
 * 5-20 秒,而且可能最终什么都没有(`generated=false`)。所以给的是一个诚实的
 * 进度面板:说清预计多久、走了多少秒、以及一个真的能停下来的按钮。
 *
 * `role="status"` + `aria-live="polite"` 让屏幕阅读器知道这里在等;秒数计数器
 * `aria-hidden` —— 每秒播报一次数字是噪音。
 */
function RunningPanel({ onStop }: { onStop: () => void }) {
  const t = useT()
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    const id = setInterval(() => setElapsed((n) => n + 1), 1000)
    return () => clearInterval(id)
  }, [])

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex flex-col items-center gap-3 px-6 py-10 text-center"
    >
      <Loader2 aria-hidden className="h-6 w-6 animate-spin text-muted-foreground" />
      <div className="space-y-1">
        <div className="text-sm font-semibold text-foreground">{t('income.running.title')}</div>
        <div className="text-xs text-muted-foreground">{t('income.running.desc')}</div>
      </div>
      <div aria-hidden className="font-mono text-xs tabular-nums text-muted-foreground">
        {t('income.running.elapsed', { seconds: elapsed })}
      </div>
      <Button type="button" variant="outline" size="sm" onClick={onStop}>
        {t('income.running.stop')}
      </Button>
    </div>
  )
}

/** `generated=false` 的五个 reason,一个 reason 一个空态 + 一个专属下一步动作。 */
function DegradedState({
  data,
  onRun,
  onOpenAiSettings,
  onOpenCareerProfile,
  onOpenTransactions,
  onOpenGoals,
}: {
  data: IncomeGrowthResponse
  onRun: () => void
  onOpenAiSettings: () => void
  onOpenCareerProfile: () => void
  onOpenTransactions: () => void
  onOpenGoals: () => void
}) {
  const t = useT()
  const action = (label: string, onClick: () => void) => (
    <Button type="button" size="sm" variant="outline" onClick={onClick}>
      {label}
    </Button>
  )

  switch (data.generated_reason) {
    case 'no_provider':
      return (
        <EmptyState
          icon={<PlugZap className="h-6 w-6" />}
          title={t('income.state.noProvider.title')}
          description={t('income.state.noProvider.desc')}
          action={action(t('income.action.openAiSettings'), onOpenAiSettings)}
        />
      )
    case 'no_career_profile':
      return (
        <EmptyState
          icon={<ClipboardList className="h-6 w-6" />}
          title={t('income.state.noProfile.title')}
          description={t('income.state.noProfile.desc')}
          action={action(t('income.action.fillProfile'), onOpenCareerProfile)}
        />
      )
    case 'insufficient_data':
      return (
        <EmptyState
          icon={<CircleDashed className="h-6 w-6" />}
          title={t('income.state.insufficientData.title')}
          description={t('income.state.insufficientData.desc', {
            periods: data.assessment.basis_periods,
          })}
          action={action(t('income.action.openTransactions'), onOpenTransactions)}
        />
      )
    case 'lever_not_income':
      // 好消息态:绿色对勾而不是警告三角。「你的结余率没问题」不是一次失败。
      return (
        <EmptyState
          icon={<CheckCircle2 className="h-6 w-6 text-green-600 dark:text-green-500" />}
          title={t('income.state.leverNotIncome.title')}
          description={t('income.state.leverNotIncome.desc', {
            pct: (data.assessment.surplus_ratio * 100).toFixed(1),
          })}
          action={action(t('income.action.openGoals'), onOpenGoals)}
        />
      )
    case 'provider_failed':
    default:
      return (
        <EmptyState
          icon={<AlertTriangle className="h-6 w-6" />}
          title={t('income.state.providerFailed.title')}
          description={t('income.state.providerFailed.desc')}
          action={action(t('income.action.tryAgain'), onRun)}
        />
      )
  }
}
