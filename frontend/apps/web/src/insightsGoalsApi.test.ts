import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchGoalPlan, fetchGoals, fetchLedgerInsights } from '@beecount/api-client'

/**
 * 洞察 / 目标端点的 query 拼装。
 *
 * 值得单独钉住:参数名写错(`lookback` vs `lookback_periods`)或者时区符号
 * 传反,服务端只会回 422 / 悄悄按默认值算,前端看不出差别。
 */
function stubFetch(): ReturnType<typeof vi.fn> {
  const spy = vi.fn(
    async () =>
      new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
  )
  vi.stubGlobal('fetch', spy)
  return spy
}

function calledUrl(spy: ReturnType<typeof vi.fn>): string {
  return `${spy.mock.calls[0]?.[0]}`
}

describe('insights / goals request urls', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends lookback_periods and tz_offset_minutes on insights', async () => {
    const spy = stubFetch()
    await fetchLedgerInsights('tok', 'L_DUMP', { lookbackPeriods: 12, tzOffsetMinutes: -480 })
    expect(calledUrl(spy)).toBe('/api/v1/ledgers/L_DUMP/insights?lookback_periods=12&tz_offset_minutes=-480')
  })

  it('omits the query entirely when no options are given', async () => {
    const spy = stubFetch()
    await fetchLedgerInsights('tok', 'L_DUMP')
    expect(calledUrl(spy)).toBe('/api/v1/ledgers/L_DUMP/insights')
  })

  it('keeps tz_offset_minutes=0 instead of dropping it as falsy', async () => {
    const spy = stubFetch()
    await fetchLedgerInsights('tok', 'L1', { tzOffsetMinutes: 0 })
    expect(calledUrl(spy)).toBe('/api/v1/ledgers/L1/insights?tz_offset_minutes=0')
  })

  it('encodes the ledger id', async () => {
    const spy = stubFetch()
    await fetchGoals('tok', 'ledger a/b')
    expect(calledUrl(spy)).toBe('/api/v1/ledgers/ledger%20a%2Fb/goals')
  })

  it('passes the goal status filter through', async () => {
    const spy = stubFetch()
    await fetchGoals('tok', 'L1', 'active')
    expect(calledUrl(spy)).toBe('/api/v1/ledgers/L1/goals?status=active')
  })

  it('hits the plan sub-path', async () => {
    const spy = stubFetch()
    await fetchGoalPlan('tok', 'L1', { lookbackPeriods: 6 })
    expect(calledUrl(spy)).toBe('/api/v1/ledgers/L1/goals/plan?lookback_periods=6')
  })
})
