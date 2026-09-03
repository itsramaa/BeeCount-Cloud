import { useCallback, useEffect, useMemo, useState } from 'react'
import { Plus, Target } from 'lucide-react'

import {
  createGoal,
  deleteGoal,
  fetchGoalPlan,
  fetchGoals,
  updateGoal,
  type GoalCreatePayload,
  type GoalItem,
  type GoalPlanResponse,
} from '@beecount/api-client'
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  EmptyState,
  Skeleton,
  useT,
  useToast,
} from '@beecount/ui'
import { ConfirmDialog } from '@beecount/web-features'

import { GoalCard } from '../../components/goals/GoalCard'
import { GoalFormDialog } from '../../components/goals/GoalFormDialog'
import { GoalPlanSummary } from '../../components/goals/GoalPlanSummary'
import { useAuth } from '../../context/AuthContext'
import { useLedgers } from '../../context/LedgersContext'
import { usePageCache } from '../../context/PageDataCacheContext'
import { localizeError } from '../../i18n/errors'

/**
 * 攒钱目标页 —— 目标 CRUD + 结余分配计划。
 *
 * 目标是 server-only 实体,不进 `sync_changes`,所以:
 *   - mutation 不走 `useLedgerWrite` / `retryOnConflict` / `base_change_id`
 *   - 不订阅 `useSyncRefresh` —— 同步流里永远不会出现目标变更;自己改完自己刷
 *
 * plan 端点比 CRUD 端点新,失败时**降级不阻塞**:隐掉分配总览,目标卡照常
 * 从 CRUD 数据渲染,只留一行说明。
 */
export function GoalsPage() {
  const t = useT()
  const toast = useToast()
  const { token } = useAuth()
  const { activeLedgerId } = useLedgers()

  const bucket = activeLedgerId || '__none__'
  const [goals, setGoals] = usePageCache<GoalItem[]>(`goals:${bucket}:rows`, [])
  const [plan, setPlan] = usePageCache<GoalPlanResponse | null>(`goals:${bucket}:plan`, null)
  const [planFailed, setPlanFailed] = useState(false)
  const [loading, setLoading] = useState<boolean>(() => goals.length === 0)
  const [saving, setSaving] = useState(false)
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<GoalItem | null>(null)
  const [pendingDelete, setPendingDelete] = useState<GoalItem | null>(null)

  const notifyError = useCallback(
    (err: unknown) => toast.error(localizeError(err, t), t('notice.error')),
    [toast, t],
  )
  const notifySuccess = useCallback(
    (msg: string) => toast.success(msg, t('notice.success')),
    [toast, t],
  )

  const refresh = useCallback(async () => {
    if (!activeLedgerId) {
      setGoals([])
      setPlan(null)
      setLoading(false)
      return
    }
    try {
      const list = await fetchGoals(token, activeLedgerId)
      setGoals(list.items)
    } catch (err) {
      notifyError(err)
    } finally {
      setLoading(false)
    }
    // plan 单独 try —— 它挂了不能把目标列表一起带走。
    try {
      const p = await fetchGoalPlan(token, activeLedgerId, {
        tzOffsetMinutes: -new Date().getTimezoneOffset(),
      })
      setPlan(p)
      setPlanFailed(false)
    } catch (_err) {
      setPlan(null)
      setPlanFailed(true)
    }
    // setGoals / setPlan 来自 usePageCache,引用稳定
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, activeLedgerId, notifyError])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const planByGoalId = useMemo(() => {
    const map = new Map<string, GoalPlanResponse['items'][number]>()
    for (const item of plan?.items ?? []) map.set(item.goal_id, item)
    return map
  }, [plan])

  const handleSubmit = async (payload: GoalCreatePayload) => {
    if (!activeLedgerId) return
    setSaving(true)
    try {
      if (editing) {
        await updateGoal(token, activeLedgerId, editing.id, payload)
        notifySuccess(t('goals.notice.updated'))
      } else {
        await createGoal(token, activeLedgerId, payload)
        notifySuccess(t('goals.notice.created'))
      }
      setFormOpen(false)
      setEditing(null)
      await refresh()
    } catch (err) {
      notifyError(err)
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async () => {
    if (!activeLedgerId || !pendingDelete) return
    try {
      await deleteGoal(token, activeLedgerId, pendingDelete.id)
      notifySuccess(t('goals.notice.deleted'))
      setPendingDelete(null)
      await refresh()
    } catch (err) {
      notifyError(err)
    }
  }

  if (!activeLedgerId) {
    return (
      <Card className="bc-panel">
        <CardHeader>
          <CardTitle>{t('nav.goals')}</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">{t('shell.selectLedgerFirst')}</p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-semibold text-foreground">{t('nav.goals')}</h1>
        <Button
          type="button"
          size="sm"
          onClick={() => {
            setEditing(null)
            setFormOpen(true)
          }}
        >
          <Plus aria-hidden className="mr-1 h-4 w-4" />
          {t('goals.create')}
        </Button>
      </div>

      {plan ? <GoalPlanSummary plan={plan} /> : null}
      {planFailed ? (
        <p className="text-xs text-muted-foreground">{t('goals.plan.unavailable')}</p>
      ) : null}

      {loading && goals.length === 0 ? (
        <GoalsSkeleton />
      ) : goals.length === 0 ? (
        <Card className="bc-panel">
          <CardContent className="py-2">
            <EmptyState
              icon={<Target className="h-6 w-6" />}
              title={t('goals.empty.title')}
              description={t('goals.empty.desc')}
              action={
                <Button
                  type="button"
                  size="sm"
                  onClick={() => {
                    setEditing(null)
                    setFormOpen(true)
                  }}
                >
                  {t('goals.create')}
                </Button>
              }
            />
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
          {goals.map((goal) => (
            <GoalCard
              key={goal.id}
              goal={goal}
              planItem={planByGoalId.get(goal.id) ?? null}
              onEdit={() => {
                setEditing(goal)
                setFormOpen(true)
              }}
              onDelete={() => setPendingDelete(goal)}
            />
          ))}
        </div>
      )}

      <GoalFormDialog
        open={formOpen}
        initial={editing}
        saving={saving}
        onClose={() => {
          setFormOpen(false)
          setEditing(null)
        }}
        onSubmit={handleSubmit}
      />

      <ConfirmDialog
        open={pendingDelete !== null}
        title={t('goals.delete.title')}
        description={t('goals.delete.desc', { name: pendingDelete?.name ?? '' })}
        confirmText={t('common.delete')}
        cancelText={t('common.cancel')}
        onCancel={() => setPendingDelete(null)}
        onConfirm={handleDelete}
      />
    </div>
  )
}

function GoalsSkeleton() {
  return (
    <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
      {[0, 1, 2].map((i) => (
        <div key={i} className="space-y-3 rounded-xl border border-border/60 bg-card p-4">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-2 w-full rounded-full" />
          <Skeleton className="h-3 w-40" />
          <div className="grid grid-cols-2 gap-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        </div>
      ))}
    </div>
  )
}
