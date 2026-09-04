import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ClipboardList, Users } from 'lucide-react'

import { requestIncomeGrowth, type IncomeGrowthResponse } from '@beecount/api-client'
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Tooltip,
  useLocale,
  useT,
  useToast,
} from '@beecount/ui'

import { CareerProfileDialog } from '../../components/income/CareerProfileDialog'
import { IncomeAssessmentPanel } from '../../components/income/IncomeAssessmentPanel'
import { IncomeSuggestionsPanel } from '../../components/income/IncomeSuggestionsPanel'
import { useAuth } from '../../context/AuthContext'
import { useLedgers } from '../../context/LedgersContext'
import { usePageCache } from '../../context/PageDataCacheContext'
import { useSyncRefresh } from '../../context/SyncSocketContext'
import { localizeError } from '../../i18n/errors'

/** 服务端限 3..24;跟洞察页同四档,不给出一个服务端会 422 的值。 */
const LOOKBACK_OPTIONS = [3, 6, 12, 24] as const
const DEFAULT_LOOKBACK = 6

/**
 * 收入增长建议页 —— 确定性评估(始终有)+ 模型生成的建议(可能没有)。
 *
 * ## 为什么只有一个按钮能触发
 *
 * 每次请求都可能是一次**用户自己付费**的 LLM 调用,而且服务端限流 10 次 / 300 秒。
 * 所以:mount 不取数、同步事件不取数、改窗口 / 改语言不取数。只有页头那颗
 * 「开始分析 / 重新分析」按钮会发请求。
 *
 * ## 缓存分桶 + 为什么用 key 强制重挂
 *
 * `usePageCache` 只在 **mount 的首帧**读 cache(见 PageDataCacheContext 注释)。
 * 所以光把 lookback / locale 拼进 key 是不够的 —— 改窗口时组件没重挂,旧的
 * `data` 会留在 state 里,于是用户会看到一份按 6 个周期算的结果被标成 12 个周期。
 * 外层只管选择器,内层用 `key={bucket}` 重挂,读到的是那个桶自己的状态(通常
 * 是「还没跑过」),而不是把旧结果重新贴标签。
 *
 * 不写 localStorage:结果是一次性的分析快照,不是设置。
 */
export function IncomeGrowthPage() {
  const t = useT()
  const { locale } = useLocale()
  const { activeLedgerId } = useLedgers()
  const [lookback, setLookback] = useState<number>(DEFAULT_LOOKBACK)

  if (!activeLedgerId) {
    return (
      <Card className="bc-panel">
        <CardHeader>
          <CardTitle>{t('nav.incomeGrowth')}</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">{t('shell.selectLedgerFirst')}</p>
        </CardContent>
      </Card>
    )
  }

  const bucket = `${activeLedgerId}:${lookback}:${locale}`
  return (
    <IncomeGrowthBody
      key={bucket}
      bucket={bucket}
      ledgerId={activeLedgerId}
      locale={locale}
      lookback={lookback}
      onLookbackChange={setLookback}
    />
  )
}

function IncomeGrowthBody({
  bucket,
  ledgerId,
  locale,
  lookback,
  onLookbackChange,
}: {
  bucket: string
  ledgerId: string
  locale: string
  lookback: number
  onLookbackChange: (next: number) => void
}) {
  const t = useT()
  const toast = useToast()
  const navigate = useNavigate()
  const { token, profileMe } = useAuth()
  const { currency } = useLedgers()

  const [data, setData] = usePageCache<IncomeGrowthResponse | null>(`income:${bucket}:data`, null)
  const [running, setRunning] = useState(false)
  const [stale, setStale] = useState(false)
  const [profileOpen, setProfileOpen] = useState(false)
  // 从 no_career_profile 空态进弹窗时置上,存盘成功后自动跑一次。其他入口
  // (页头按钮 / 设置页)打开时永远是 false,不会偷偷替用户花一次调用。
  const runAfterProfileSave = useRef(false)
  const abortRef = useRef<AbortController | null>(null)

  const availableHours = profileMe?.career_profile?.available_hours_per_week ?? null

  const run = useCallback(async () => {
    if (running) return
    const controller = new AbortController()
    abortRef.current = controller
    setRunning(true)
    try {
      const resp = await requestIncomeGrowth(
        token,
        {
          ledgerId,
          // 原样发 UI 语言。服务端 pattern 认 zh / zh-CN / zh-TW / en 四种,
          // 正好是本仓 locale 的全集。
          locale,
          lookbackPeriods: lookback,
          // 服务端按这个偏移切周期边界。JS 的 getTimezoneOffset 符号跟服务端
          // 参数相反(东八区是 -480),取负号对齐(同 InsightsPage.tsx:77)。
          tzOffsetMinutes: -new Date().getTimezoneOffset(),
        },
        { signal: controller.signal },
      )
      setData(resp)
      setStale(false)
    } catch (err) {
      // 用户自己点了「停止等待」—— 不是错误,上一份状态原样留着。
      if (err instanceof DOMException && err.name === 'AbortError') {
        toast.info(t('income.running.stopped'))
        return
      }
      // 失败**不清空**已有结果:上一次跑出来的建议仍然有效,把它换成一个错误
      // 空态等于因为一次网络抖动没收用户的东西。429 也走这条路(localizeError)。
      toast.error(localizeError(err, t), t('notice.error'))
    } finally {
      abortRef.current = null
      setRunning(false)
    }
    // setData 来自 usePageCache,引用稳定
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, ledgerId, lookback, locale, running, toast, t])

  const stop = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  // 离开页面 / 换桶时放掉在飞的请求。结果无处可存(setData 属于已卸载的那个桶),
  // 留着它只是让用户的 provider 白算一次。
  useEffect(() => () => abortRef.current?.abort(), [])

  // 只置脏,**永远不重新取数**(见上:付费 + 限流)。要不要再跑由用户决定。
  useSyncRefresh(() => {
    setStale(true)
  })

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-foreground">{t('nav.incomeGrowth')}</h1>
          {/* 共享账本徽标 —— 复用洞察页那套处理和文案,同一个含义不该有两种说法。 */}
          {data?.restricted_to_creator ? (
            <Tooltip content={t('insights.restricted.hint')}>
              <Badge variant="outline" className="gap-1">
                <Users aria-hidden className="h-3 w-3" />
                {t('insights.restricted.label')}
              </Badge>
            </Tooltip>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Label className="text-xs text-muted-foreground" htmlFor="income-lookback">
            {t('insights.window.label')}
          </Label>
          {/* 运行中禁用 —— 换窗口会重挂内层组件并清掉 running,不禁用的话用户
              能在一次调用还没回来时再点一次「开始分析」,白花第二次。 */}
          <Select
            value={`${lookback}`}
            disabled={running}
            onValueChange={(v) => onLookbackChange(Number(v))}
          >
            <SelectTrigger id="income-lookback" className="h-9 w-32">
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
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              runAfterProfileSave.current = false
              setProfileOpen(true)
            }}
          >
            <ClipboardList aria-hidden className="mr-1 h-4 w-4" />
            {t('career.openDialog')}
          </Button>
          <Button type="button" size="sm" onClick={() => void run()} disabled={running}>
            {data === null ? t('income.run') : t('income.runAgain')}
          </Button>
        </div>
      </div>

      <p className="text-xs text-muted-foreground">{t('income.page.desc')}</p>

      {stale && data !== null ? (
        <p className="text-xs text-muted-foreground">{t('income.stale')}</p>
      ) : null}

      {data ? (
        <IncomeAssessmentPanel assessment={data.assessment} currency={data.currency || currency} />
      ) : (
        <Card className="bc-panel">
          <CardHeader>
            <CardTitle className="text-base">{t('income.assessment.title')}</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">{t('income.assessment.notRun')}</p>
          </CardContent>
        </Card>
      )}

      <IncomeSuggestionsPanel
        data={data}
        running={running}
        availableHoursPerWeek={availableHours}
        onRun={() => void run()}
        onStop={stop}
        onOpenAiSettings={() => navigate('/app/settings/ai')}
        onOpenCareerProfile={() => {
          runAfterProfileSave.current = true
          setProfileOpen(true)
        }}
        onOpenTransactions={() => navigate('/app/transactions')}
        onOpenGoals={() => navigate('/app/goals')}
      />

      <CareerProfileDialog
        open={profileOpen}
        onClose={() => {
          setProfileOpen(false)
          runAfterProfileSave.current = false
        }}
        onSaved={() => {
          if (!runAfterProfileSave.current) return
          runAfterProfileSave.current = false
          void run()
        }}
      />
    </div>
  )
}
