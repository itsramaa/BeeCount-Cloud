import { afterEach, describe, expect, it, vi } from 'vitest'

import { configureHttp, requestIncomeGrowth } from '@beecount/api-client'

/**
 * `POST /ai/income-growth` 的请求组装 + AbortSignal 透传。
 *
 * 值得单独钉住的三件事,写错了服务端和 UI 都不会明显报错:
 *   1. 字段名是 snake_case(`lookback_periods` / `tz_offset_minutes`)—— 写成
 *      camelCase 服务端会**静默按默认值算**,前端看不出差别。
 *   2. 时区符号 —— 传反了周期边界就错一天,数字仍然「看起来正常」。
 *   3. signal 必须落到 fetch 上,而且 401 重放那次也得带 —— 否则「停止等待」
 *      在 token 刚过期时会静默失效,用户点了没反应。
 */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function stubFetch(...responses: Response[]): ReturnType<typeof vi.fn> {
  const queue = [...responses]
  const spy = vi.fn(async () => queue.shift() ?? jsonResponse({}))
  vi.stubGlobal('fetch', spy)
  return spy
}

function bodyOf(spy: ReturnType<typeof vi.fn>, call = 0): Record<string, unknown> {
  const init = spy.mock.calls[call]?.[1] as RequestInit | undefined
  return JSON.parse(`${init?.body ?? '{}'}`)
}

describe('income growth request', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    configureHttp({ refreshToken: null, onLogout: null })
  })

  it('posts to /ai/income-growth with snake_case fields', async () => {
    const spy = stubFetch()
    await requestIncomeGrowth('tok', {
      ledgerId: 'L_DUMP',
      locale: 'zh-TW',
      lookbackPeriods: 12,
      tzOffsetMinutes: -480,
    })
    expect(`${spy.mock.calls[0]?.[0]}`).toBe('/api/v1/ai/income-growth')
    expect(bodyOf(spy)).toEqual({
      ledger_id: 'L_DUMP',
      locale: 'zh-TW',
      lookback_periods: 12,
      tz_offset_minutes: -480,
    })
  })

  it('keeps tz_offset_minutes=0 instead of dropping it as falsy', async () => {
    const spy = stubFetch()
    await requestIncomeGrowth('tok', { ledgerId: 'L1', locale: 'en', tzOffsetMinutes: 0 })
    expect(bodyOf(spy)).toEqual({ ledger_id: 'L1', locale: 'en', tz_offset_minutes: 0 })
  })

  it('threads the abort signal into fetch', async () => {
    const spy = stubFetch()
    const controller = new AbortController()
    await requestIncomeGrowth('tok', { ledgerId: 'L1', locale: 'en' }, { signal: controller.signal })
    const init = spy.mock.calls[0]?.[1] as RequestInit
    expect(init.signal).toBe(controller.signal)
  })

  it('keeps the abort signal on the 401 replay', async () => {
    const spy = stubFetch(jsonResponse({ detail: 'expired' }, 401), jsonResponse({ generated: false }))
    configureHttp({ refreshToken: async () => 'fresh', onLogout: null })
    const controller = new AbortController()
    await requestIncomeGrowth('tok', { ledgerId: 'L1', locale: 'en' }, { signal: controller.signal })
    expect(spy).toHaveBeenCalledTimes(2)
    const replay = spy.mock.calls[1]?.[1] as RequestInit
    expect(replay.signal).toBe(controller.signal)
    expect((replay.headers as Record<string, string>).Authorization).toBe('Bearer fresh')
  })

  it('surfaces the 429 rate limit as an ApiError with its code', async () => {
    stubFetch(
      jsonResponse(
        { error_code: 'AI_INCOME_GROWTH_RATE_LIMITED', detail: 'at most 10 requests per 300s' },
        429,
      ),
    )
    await expect(
      requestIncomeGrowth('tok', { ledgerId: 'L1', locale: 'en' }),
    ).rejects.toMatchObject({ status: 429, code: 'AI_INCOME_GROWTH_RATE_LIMITED' })
  })
})
