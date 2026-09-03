import { AlertTriangle, CheckCircle2, CircleDashed, XCircle } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import type { GoalFeasibility } from '@beecount/api-client'
import { Badge, type BadgeProps, useT } from '@beecount/ui'

/** 四个 code 各自专属图标 + 文字标签 —— 颜色不是唯一的语义载体。 */
const ICON_BY_FEASIBILITY: Record<GoalFeasibility, LucideIcon> = {
  feasible: CheckCircle2,
  tight: AlertTriangle,
  infeasible: XCircle,
  no_deadline: CircleDashed,
}

const VARIANT_BY_FEASIBILITY: Record<GoalFeasibility, BadgeProps['variant']> = {
  feasible: 'default',
  tight: 'secondary',
  infeasible: 'destructive',
  no_deadline: 'outline',
}

export function FeasibilityBadge({ feasibility }: { feasibility: GoalFeasibility }) {
  const t = useT()
  const Icon = ICON_BY_FEASIBILITY[feasibility]
  return (
    <Badge variant={VARIANT_BY_FEASIBILITY[feasibility]} className="gap-1">
      <Icon aria-hidden className="h-3 w-3" />
      {t(`goals.feasibility.${feasibility}.label`)}
    </Badge>
  )
}
