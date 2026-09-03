import { HelpCircle, PiggyBank, TrendingDown, TrendingUp, Wallet } from 'lucide-react'

import type { InsightBaseline } from '@beecount/api-client'
import { Tooltip, useT } from '@beecount/ui'
import { Amount, type AmountTone } from '@beecount/web-features'

interface Props {
  baseline: InsightBaseline
  currency: string
}

/**
 * 基线四宫格 + 单独一行的 allocatable。
 *
 * 四个数是"你平常什么样",allocatable 是"据此每月能拿出多少",两者不是同一
 * 类信息 —— 拆两行避免用户把可分配额度当成第五个统计量扫过去。
 */
export function BaselineTiles({ baseline, currency }: Props) {
  const t = useT()

  const tiles: Array<{
    key: string
    label: string
    value: number
    icon: typeof Wallet
    tone: AmountTone
    hint?: string
  }> = [
    {
      key: 'income',
      label: t('insights.baseline.medianIncome'),
      value: baseline.median_income,
      icon: TrendingUp,
      tone: 'positive',
    },
    {
      key: 'expense',
      label: t('insights.baseline.medianExpense'),
      value: baseline.median_expense,
      icon: TrendingDown,
      tone: 'negative',
    },
    {
      key: 'surplus',
      label: t('insights.baseline.medianSurplus'),
      value: baseline.median_surplus,
      icon: Wallet,
      tone: baseline.median_surplus >= 0 ? 'positive' : 'negative',
    },
    {
      key: 'buffer',
      label: t('insights.baseline.buffer'),
      value: baseline.buffer,
      icon: PiggyBank,
      tone: 'default',
      // buffer 最容易被误读成"平台留的 10%",必须说清它来自用户自己的波动。
      hint: t('insights.baseline.bufferHint'),
    },
  ]

  return (
    <div className="space-y-3">
      <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
        {tiles.map((tile) => (
          <div key={tile.key} className="rounded-xl border border-border/60 bg-card p-4">
            <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-muted-foreground">
              <tile.icon aria-hidden className="h-4 w-4" />
              <span>{tile.label}</span>
              {tile.hint ? (
                <Tooltip content={tile.hint}>
                  {/* 真 <button> 而不是可聚焦的 div/span —— 键盘用户 tab 得到,
                      屏幕阅读器读 aria-label,不需要另起一套 role。 */}
                  <button
                    type="button"
                    aria-label={tile.hint}
                    className="text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <HelpCircle aria-hidden className="h-3.5 w-3.5" />
                  </button>
                </Tooltip>
              ) : null}
            </div>
            <div className="mt-1.5">
              <Amount
                value={tile.value}
                currency={currency}
                showCurrency
                bold
                size="2xl"
                tone={tile.tone}
              />
            </div>
          </div>
        ))}
      </div>
      <div className="rounded-xl border border-primary/40 bg-primary/5 p-4">
        <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
          {t('insights.baseline.allocatable')}
        </div>
        <div className="mt-1.5">
          <Amount
            value={baseline.allocatable}
            currency={currency}
            showCurrency
            bold
            size="2xl"
          />
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          {t('insights.baseline.allocatableHint')}
        </p>
      </div>
    </div>
  )
}
