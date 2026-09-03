import { useEffect, useMemo, useState, type ReactNode } from 'react'

import type { GoalCreatePayload, GoalItem, GoalStatus } from '@beecount/api-client'
import {
  Button,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  useT,
} from '@beecount/ui'

interface Props {
  open: boolean
  /** null = 新建;有值 = 编辑。 */
  initial: GoalItem | null
  saving?: boolean
  onClose: () => void
  onSubmit: (payload: GoalCreatePayload) => Promise<void>
}

const NAME_MAX = 128
const PRIORITY_MAX = 100
const STATUS_OPTIONS: GoalStatus[] = ['active', 'achieved', 'archived']

type FieldErrors = Partial<Record<'name' | 'target' | 'saved' | 'deadline' | 'priority', string>>

/** deadline 必须严格晚于今天,所以 `min` 取明天 —— 服务端 POST / PATCH
 *  两条路径都会拒掉今天及更早的日期。 */
function tomorrowIso(): string {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  return d.toISOString().slice(0, 10)
}

/**
 * 目标创建 / 编辑弹窗。校验规则逐条对齐服务端(`routers/goals.py`),避免让
 * 用户提交后才吃到一个 422:name 去空白后 1..128、target > 0、saved >= 0
 * (允许超过 target)、deadline 严格晚于今天、priority 0..100。
 */
export function GoalFormDialog({ open, initial, saving = false, onClose, onSubmit }: Props) {
  const t = useT()
  const isEdit = initial !== null

  const [name, setName] = useState('')
  const [target, setTarget] = useState('')
  const [saved, setSaved] = useState('')
  const [deadline, setDeadline] = useState('')
  const [priority, setPriority] = useState('0')
  const [status, setStatus] = useState<GoalStatus>('active')
  const [errors, setErrors] = useState<FieldErrors>({})

  useEffect(() => {
    if (!open) return
    setName(initial?.name ?? '')
    setTarget(initial ? `${initial.target_amount}` : '')
    setSaved(initial ? `${initial.saved_amount}` : '0')
    setDeadline(initial?.deadline ?? '')
    setPriority(initial ? `${initial.priority}` : '0')
    setStatus(initial?.status ?? 'active')
    setErrors({})
  }, [open, initial])

  const minDeadline = useMemo(() => tomorrowIso(), [])

  const validate = (): GoalCreatePayload | null => {
    const next: FieldErrors = {}
    const trimmedName = name.trim()
    if (trimmedName.length === 0 || trimmedName.length > NAME_MAX) {
      next.name = t('goals.form.error.name', { max: NAME_MAX })
    }
    const targetNum = Number(target)
    if (!Number.isFinite(targetNum) || targetNum <= 0) {
      next.target = t('goals.form.error.target')
    }
    const savedNum = saved.trim() === '' ? 0 : Number(saved)
    if (!Number.isFinite(savedNum) || savedNum < 0) {
      next.saved = t('goals.form.error.saved')
    }
    // 只在 deadline 真被改动时校验并发送。旧目标的 deadline 可能已经过期,
    // 原样回发会被服务端 422 —— 那样连改个名字都做不到。
    const deadlineChanged = deadline !== (initial?.deadline ?? '')
    if (deadlineChanged && deadline && deadline < minDeadline) {
      next.deadline = t('goals.form.error.deadline')
    }
    const priorityNum = priority.trim() === '' ? 0 : Number(priority)
    if (!Number.isInteger(priorityNum) || priorityNum < 0 || priorityNum > PRIORITY_MAX) {
      next.priority = t('goals.form.error.priority', { max: PRIORITY_MAX })
    }
    setErrors(next)
    if (Object.keys(next).length > 0) return null
    return {
      name: trimmedName,
      target_amount: targetNum,
      saved_amount: savedNum,
      // 空值不发字段 —— PATCH 是部分更新,`null` 被服务端当成"不动这个字段",
      // 空串会 422。清空 deadline 只能删掉目标重建(表单提示里已写明)。
      ...(deadlineChanged && deadline ? { deadline } : {}),
      priority: priorityNum,
      status,
    }
  }

  const handleSubmit = async () => {
    if (saving) return
    const payload = validate()
    if (!payload) return
    await onSubmit(payload)
  }

  return (
    <Dialog open={open} onOpenChange={(v) => (v ? undefined : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? t('goals.form.editTitle') : t('goals.form.createTitle')}</DialogTitle>
        </DialogHeader>

        <div className="space-y-3">
          <Field
            id="goal-name"
            label={t('goals.form.name')}
            error={errors.name}
          >
            <Input
              id="goal-name"
              value={name}
              maxLength={NAME_MAX}
              onChange={(e) => setName(e.target.value)}
              aria-invalid={errors.name ? true : undefined}
              aria-describedby={errors.name ? 'goal-name-error' : undefined}
            />
          </Field>

          <div className="grid gap-3 sm:grid-cols-2">
            <Field id="goal-target" label={t('goals.form.target')} error={errors.target}>
              <Input
                id="goal-target"
                type="number"
                inputMode="decimal"
                min="0"
                step="0.01"
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                aria-invalid={errors.target ? true : undefined}
                aria-describedby={errors.target ? 'goal-target-error' : undefined}
              />
            </Field>
            <Field
              id="goal-saved"
              label={t('goals.form.saved')}
              error={errors.saved}
              hint={t('goals.form.savedHint')}
            >
              <Input
                id="goal-saved"
                type="number"
                inputMode="decimal"
                min="0"
                step="0.01"
                value={saved}
                onChange={(e) => setSaved(e.target.value)}
                aria-invalid={errors.saved ? true : undefined}
                aria-describedby={
                  [errors.saved ? 'goal-saved-error' : '', 'goal-saved-hint']
                    .filter(Boolean)
                    .join(' ') || undefined
                }
              />
            </Field>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <Field
              id="goal-deadline"
              label={t('goals.form.deadline')}
              error={errors.deadline}
              hint={t('goals.form.deadlineHint')}
            >
              <Input
                id="goal-deadline"
                type="date"
                min={minDeadline}
                value={deadline}
                onChange={(e) => setDeadline(e.target.value)}
                aria-invalid={errors.deadline ? true : undefined}
                aria-describedby={
                  [errors.deadline ? 'goal-deadline-error' : '', 'goal-deadline-hint']
                    .filter(Boolean)
                    .join(' ') || undefined
                }
              />
            </Field>
            <Field
              id="goal-priority"
              label={t('goals.form.priority')}
              error={errors.priority}
              hint={t('goals.form.priorityHint')}
            >
              <Input
                id="goal-priority"
                type="number"
                inputMode="numeric"
                min="0"
                max={PRIORITY_MAX}
                step="1"
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
                aria-invalid={errors.priority ? true : undefined}
                aria-describedby={
                  [errors.priority ? 'goal-priority-error' : '', 'goal-priority-hint']
                    .filter(Boolean)
                    .join(' ') || undefined
                }
              />
            </Field>
          </div>

          <Field id="goal-status" label={t('goals.form.status')}>
            <Select value={status} onValueChange={(v) => setStatus(v as GoalStatus)}>
              <SelectTrigger id="goal-status">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {STATUS_OPTIONS.map((s) => (
                  <SelectItem key={s} value={s}>
                    {t(`goals.status.${s}`)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        </div>

        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onClose} disabled={saving}>
            {t('common.cancel')}
          </Button>
          <Button type="button" onClick={handleSubmit} disabled={saving}>
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function Field({
  id,
  label,
  error,
  hint,
  children,
}: {
  id: string
  label: string
  error?: string
  hint?: string
  children: ReactNode
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint ? (
        <p id={`${id}-hint`} className="text-[11px] text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} className="text-[11px] text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  )
}
