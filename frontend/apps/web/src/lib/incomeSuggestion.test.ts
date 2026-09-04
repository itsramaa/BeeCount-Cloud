import { describe, expect, it } from 'vitest'

import {
  capacityColor,
  formatUnitNumber,
  potentialShape,
  showCapacityBar,
} from './incomeSuggestion'

/**
 * 收入建议卡片的两条正确性规则。写错了 UI 仍然渲染得出来,只是内容是错的 ——
 * 所以钉在这里。
 */
describe('potential shape never turns a missing estimate into zero', () => {
  it('needs both ends for a range', () => {
    expect(potentialShape(1000, 3000)).toBe('range')
  })

  it('says from / up to when only one end is estimated', () => {
    expect(potentialShape(1000, null)).toBe('from')
    expect(potentialShape(null, 3000)).toBe('upTo')
  })

  it('returns null (dash) when neither end is estimated', () => {
    expect(potentialShape(null, null)).toBeNull()
  })

  it('keeps a real 0 distinct from null', () => {
    // 服务端夹紧后 0 是一个真实的估值,不该跟「没估」混同。
    expect(potentialShape(0, null)).toBe('from')
  })
})

describe('capacity bar is fuller-is-worse, same thresholds as budget usage', () => {
  it('matches the BudgetUsagePanel breakpoints', () => {
    expect(capacityColor(0)).toBe('bg-green-500')
    expect(capacityColor(0.69)).toBe('bg-green-500')
    expect(capacityColor(0.7)).toBe('bg-orange-500')
    expect(capacityColor(0.89)).toBe('bg-orange-500')
    expect(capacityColor(0.9)).toBe('bg-red-500')
    expect(capacityColor(0.99)).toBe('bg-red-500')
    expect(capacityColor(1)).toBe('bg-red-700')
    expect(capacityColor(2.5)).toBe('bg-red-700')
  })

  it('only draws when both available hours and effort are known', () => {
    expect(showCapacityBar(10, 5)).toBe(true)
    expect(showCapacityBar(10, null)).toBe(false)
    expect(showCapacityBar(null, 5)).toBe(false)
    expect(showCapacityBar(0, 5)).toBe(false)
  })
})

describe('unit numbers drop meaningless decimals', () => {
  it('keeps integers bare and fractions to one place', () => {
    expect(formatUnitNumber(5)).toBe('5')
    expect(formatUnitNumber(7.5)).toBe('7.5')
    expect(formatUnitNumber(7.25)).toBe('7.3')
  })
})
