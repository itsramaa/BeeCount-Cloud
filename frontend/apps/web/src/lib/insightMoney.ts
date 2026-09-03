import { useCallback } from 'react'

import { useLocale, useT } from '@beecount/ui'
import { formatBalanceCompact } from '@beecount/web-features'

/**
 * 句子里内嵌金额时用的格式化器。
 *
 * 洞察 / 目标页的解释文案(诊断结论、可行性说明、"三条补齐差额的路")必须把
 * 真实数字写进句子里,不能只给一个形容词。这些位置放不进 `<Amount>` 组件
 * (它渲染 `<span>`,插到 `t(key, { amount })` 的模板参数里只会变成
 * `[object Object]`),所以统一走这个 hook 拿字符串。
 *
 * 口径跟全站 `<Amount>` 一致 —— 同一个 `formatBalanceCompact`,单位跟随 UI
 * 语言(中文「万/萬」、英文 k/M),不另起一套写法。
 */
export function useMoneyText(currency: string): (value: number | null | undefined) => string {
  const { locale } = useLocale()
  const t = useT()
  const chinese = locale.startsWith('zh')
  const wanUnit = t('common.unit.10k')
  return useCallback(
    (value: number | null | undefined) =>
      formatBalanceCompact(value, currency, { chinese, wanUnit }),
    [currency, chinese, wanUnit],
  )
}
