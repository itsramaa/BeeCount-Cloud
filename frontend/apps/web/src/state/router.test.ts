import { describe, expect, it } from 'vitest'

import { parseRoute, routePath } from './router'

describe('router path mapping', () => {
  it('parses login path', () => {
    expect(parseRoute('/login')).toEqual({ kind: 'login' })
    expect(parseRoute('/')).toEqual({ kind: 'login' })
  })

  it('parses app route with section', () => {
    expect(parseRoute('/app/ledger-1/transactions')).toEqual({
      kind: 'app',
      ledgerId: 'ledger-1',
      section: 'transactions'
    })
expect(parseRoute('/app/workspace/transactions')).toEqual({
      kind: 'app',
      ledgerId: '',
      section: 'transactions'
    })
    expect(parseRoute('/app/transactions')).toEqual({
      kind: 'app',
      ledgerId: '',
      section: 'transactions'
    })
    expect(parseRoute('/app/settings/health')).toEqual({
      kind: 'app',
      ledgerId: '',
      section: 'settings-health'
    })
  })

  it('falls back to overview for unknown section', () => {
    expect(parseRoute('/app/ledger-1/unknown')).toEqual({
      kind: 'app',
      ledgerId: 'ledger-1',
      section: 'transactions'
    })
  })

  it('creates path from app route', () => {
    expect(
      routePath({
        kind: 'app',
        ledgerId: 'ledger a',
        section: 'settings-devices'
      })
    ).toBe('/app/settings/devices')
    expect(
      routePath({
        kind: 'app',
        ledgerId: 'ledger a',
        section: 'settings-health'
      })
    ).toBe('/app/settings/health')
    // insights / goals 是 routePath 这个穷举 switch 的新分支 —— 漏掉的话
    // tsc -b 直接失败,这里再钉一层运行时断言。
    expect(
      routePath({
        kind: 'app',
        ledgerId: '',
        section: 'insights'
      })
    ).toBe('/app/insights')
    expect(
      routePath({
        kind: 'app',
        ledgerId: '',
        section: 'goals'
      })
    ).toBe('/app/goals')
    expect(
      routePath({
        kind: 'app',
        ledgerId: '',
        section: 'income-growth'
      })
    ).toBe('/app/income-growth')
  })

  // `/app/income-growth` 必须进 parseRoute 的 root-section 白名单。漏了的话
  // 它会被当成 legacy 的 `:ledgerId`,静默渲染成 transactions —— tsc 抓不到。
  it('parses the income-growth root section instead of treating it as a ledger id', () => {
    expect(parseRoute('/app/income-growth')).toEqual({
      kind: 'app',
      ledgerId: '',
      section: 'income-growth'
    })
  })
})
