import { useEffect, useRef, useState, type ReactNode } from 'react'
import { X } from 'lucide-react'

import {
  patchProfileMe,
  type CareerEmploymentType,
  type CareerProfile,
} from '@beecount/api-client'
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
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
  Textarea,
  useT,
  useToast,
} from '@beecount/ui'

import { useAuth } from '../../context/AuthContext'
import { localizeError } from '../../i18n/errors'

interface Props {
  open: boolean
  onClose: () => void
  /** 保存成功后回调。收入增长页在「没填档案」空态里打开时用它自动跑一次分析。 */
  onSaved?: () => void
}

// 上界逐条对齐服务端 `schemas.CareerProfile`,提交前就拦住,不让用户吃 422。
const OCCUPATION_MAX = 128
const SKILLS_MAX = 20
const SKILL_LEN_MAX = 64
const EXPERIENCE_YEARS_MAX = 80
const HOURS_PER_WEEK_MAX = 168
const REGION_MAX = 64
const NOTES_MAX = 500

const EMPLOYMENT_TYPES: CareerEmploymentType[] = [
  'full_time',
  'part_time',
  'freelance',
  'self_employed',
  'student',
  'unemployed',
  'other',
]

/** Radix Select 的 item value 不能是空串(空串是「清空」的内部信号),所以
 *  「未指定」需要一个真实的哨兵值,提交时映射成「不发这个字段」。 */
const EMPLOYMENT_UNSET = '__unset__'

type FieldErrors = Partial<
  Record<'occupation' | 'skills' | 'experience' | 'hours' | 'region' | 'notes', string>
>

/**
 * 职业档案编辑弹窗。两个入口共用这一个组件:收入增长页的页头 / 它的
 * `no_career_profile` 空态,以及设置-个人资料页的 `CareerProfileCard`。
 *
 * ## 为什么这个弹窗自己存,而 GoalFormDialog 把 onSubmit 交给上层
 *
 * 目标弹窗只有一个宿主(目标页),存盘逻辑放宿主里没有重复。职业档案有两个宿主,
 * 而且 `PATCH /profile/me` 的 `career_profile` 是**整体替换 + 空对象清空**语义
 * —— 让两个宿主各写一份「哪些字段该发、清空要不要发 `{}`」的逻辑,迟早分叉成
 * 「在设置页清空成功、在收入页清空却只清掉一半」。所以存盘收在这里,宿主只负责
 * 开关和 `onSaved`。
 *
 * `Field` 是本文件私有的一份副本,没有抽成共享组件 —— GoalFormDialog 和
 * ProviderEditDialog 各有自己的一份,抽公共件要动那两个已经稳定的弹窗,收益不值。
 */
export function CareerProfileDialog({ open, onClose, onSaved }: Props) {
  const t = useT()
  const toast = useToast()
  const { token, profileMe, refreshProfile } = useAuth()

  const [occupation, setOccupation] = useState('')
  const [skills, setSkills] = useState<string[]>([])
  const [skillDraft, setSkillDraft] = useState('')
  const [experience, setExperience] = useState('')
  const [hours, setHours] = useState('')
  const [employment, setEmployment] = useState<string>(EMPLOYMENT_UNSET)
  const [region, setRegion] = useState('')
  const [notes, setNotes] = useState('')
  const [errors, setErrors] = useState<FieldErrors>({})
  const [saving, setSaving] = useState(false)

  // 只在弹窗「打开的那一刻」灌初值,之后 profileMe 再变都不重灌 —— server 会为
  // 别的原因广播 `profile_change`(另一台设备改了外观、mobile 推了 ai_config),
  // 跟着重灌会把用户正在输入的内容清掉。`profileMe` 从 ref 读,不进依赖数组。
  const profileRef = useRef(profileMe)
  profileRef.current = profileMe
  useEffect(() => {
    if (!open) return
    const career = profileRef.current?.career_profile ?? null
    setOccupation(career?.occupation ?? '')
    setSkills(career?.skills ?? [])
    setSkillDraft('')
    setExperience(career?.experience_years == null ? '' : `${career.experience_years}`)
    setHours(
      career?.available_hours_per_week == null ? '' : `${career.available_hours_per_week}`,
    )
    setEmployment(career?.employment_type ?? EMPLOYMENT_UNSET)
    setRegion(career?.region ?? '')
    setNotes(career?.notes ?? '')
    setErrors({})
  }, [open])

  const commitSkill = (raw: string) => {
    const value = raw.trim()
    if (!value) return
    // 大小写不敏感去重,静默忽略 —— 用户重复输入同一个技能不是错误,不值一条报错。
    if (skills.some((s) => s.toLowerCase() === value.toLowerCase())) {
      setSkillDraft('')
      return
    }
    if (skills.length >= SKILLS_MAX) return
    setSkills([...skills, value])
    setSkillDraft('')
  }

  const handleSkillKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault()
      commitSkill(skillDraft)
      return
    }
    // 输入框空着按退格 = 删掉最后一个 chip,标准 chips 交互。
    if (event.key === 'Backspace' && skillDraft === '' && skills.length > 0) {
      event.preventDefault()
      setSkills(skills.slice(0, -1))
    }
  }

  const clearAll = () => {
    setOccupation('')
    setSkills([])
    setSkillDraft('')
    setExperience('')
    setHours('')
    setEmployment(EMPLOYMENT_UNSET)
    setRegion('')
    setNotes('')
    setErrors({})
  }

  /**
   * 提交时才校验(不做逐键校验 —— 一个还没填完的数字不该先弹红)。
   *
   * 数字按「有限且在区间内」判,**不要求整数**:服务端是 `float`,3.5 年经验和
   * 每周 7.5 小时都是合法输入。
   */
  const validate = (): CareerProfile | null => {
    const next: FieldErrors = {}
    const trimmedOccupation = occupation.trim()
    if (trimmedOccupation.length > OCCUPATION_MAX) {
      next.occupation = t('career.error.occupation', { max: OCCUPATION_MAX })
    }
    if (skills.some((s) => s.length > SKILL_LEN_MAX)) {
      next.skills = t('career.error.skills', { max: SKILL_LEN_MAX })
    }
    const experienceNum = experience.trim() === '' ? null : Number(experience)
    if (
      experienceNum !== null &&
      (!Number.isFinite(experienceNum) || experienceNum < 0 || experienceNum > EXPERIENCE_YEARS_MAX)
    ) {
      next.experience = t('career.error.experienceYears', { max: EXPERIENCE_YEARS_MAX })
    }
    const hoursNum = hours.trim() === '' ? null : Number(hours)
    if (
      hoursNum !== null &&
      (!Number.isFinite(hoursNum) || hoursNum < 0 || hoursNum > HOURS_PER_WEEK_MAX)
    ) {
      next.hours = t('career.error.availableHours', { max: HOURS_PER_WEEK_MAX })
    }
    const trimmedRegion = region.trim()
    if (trimmedRegion.length > REGION_MAX) {
      next.region = t('career.error.region', { max: REGION_MAX })
    }
    const trimmedNotes = notes.trim()
    if (trimmedNotes.length > NOTES_MAX) {
      next.notes = t('career.error.notes', { max: NOTES_MAX })
    }
    setErrors(next)
    if (Object.keys(next).length > 0) return null

    // 没填的字段根本不出现在 payload 里。全部没填 → `{}` → 服务端清空整个档案。
    const payload: CareerProfile = {}
    if (trimmedOccupation) payload.occupation = trimmedOccupation
    if (skills.length > 0) payload.skills = skills
    if (experienceNum !== null) payload.experience_years = experienceNum
    if (hoursNum !== null) payload.available_hours_per_week = hoursNum
    if (employment !== EMPLOYMENT_UNSET) {
      payload.employment_type = employment as CareerEmploymentType
    }
    if (trimmedRegion) payload.region = trimmedRegion
    if (trimmedNotes) payload.notes = trimmedNotes
    return payload
  }

  const handleSubmit = async () => {
    if (saving) return
    const payload = validate()
    if (!payload) return
    setSaving(true)
    try {
      await patchProfileMe(token, { career_profile: payload })
      await refreshProfile()
      toast.success(
        Object.keys(payload).length === 0 ? t('career.notice.cleared') : t('career.notice.saved'),
        t('notice.success'),
      )
      // onSaved **先于** onClose:宿主的 onClose 会清掉「存完自动跑一次」的标记
      // (取消时必须清),反过来调用户就永远等不到那次自动分析。
      onSaved?.()
      onClose()
    } catch (err) {
      toast.error(localizeError(err, t), t('notice.error'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => (v ? undefined : onClose())}>
      {/* 七个字段 + 一行 chips 在手机上放不下一屏,所以显式给高度上限 + 内部滚动。 */}
      <DialogContent className="max-w-lg max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t('career.dialog.title')}</DialogTitle>
          <DialogDescription>{t('career.dialog.desc')}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <Field id="career-occupation" label={t('career.field.occupation')} error={errors.occupation}>
            <Input
              id="career-occupation"
              value={occupation}
              maxLength={OCCUPATION_MAX}
              onChange={(e) => setOccupation(e.target.value)}
              aria-invalid={errors.occupation ? true : undefined}
              aria-describedby={errors.occupation ? 'career-occupation-error' : undefined}
            />
          </Field>

          <Field
            id="career-skills"
            label={t('career.field.skills')}
            error={errors.skills}
            hint={t('career.field.skills.hint', { max: SKILLS_MAX, len: SKILL_LEN_MAX })}
          >
            <div className="space-y-2">
              {skills.length > 0 ? (
                <ul className="flex flex-wrap gap-1.5">
                  {skills.map((skill) => (
                    <li
                      key={skill}
                      className="inline-flex items-center gap-1 rounded-full border border-border px-2.5 py-0.5 text-xs"
                    >
                      <span className="break-all">{skill}</span>
                      {/* 真 <button>,带 aria-label —— 键盘能 tab 到,读屏器读得出删的是哪个。 */}
                      <button
                        type="button"
                        aria-label={t('career.field.skills.remove', { skill })}
                        onClick={() => setSkills(skills.filter((s) => s !== skill))}
                        className="rounded text-muted-foreground transition hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        <X aria-hidden className="h-3 w-3" />
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
              <Input
                id="career-skills"
                value={skillDraft}
                maxLength={SKILL_LEN_MAX}
                placeholder={t('career.field.skills.placeholder')}
                disabled={skills.length >= SKILLS_MAX}
                onChange={(e) => setSkillDraft(e.target.value)}
                onKeyDown={handleSkillKeyDown}
                onBlur={() => commitSkill(skillDraft)}
                aria-invalid={errors.skills ? true : undefined}
                aria-describedby={
                  [errors.skills ? 'career-skills-error' : '', 'career-skills-hint']
                    .filter(Boolean)
                    .join(' ') || undefined
                }
              />
              {skills.length >= SKILLS_MAX ? (
                <p className="text-[11px] text-muted-foreground">
                  {t('career.field.skills.full', { max: SKILLS_MAX })}
                </p>
              ) : null}
            </div>
          </Field>

          <div className="grid gap-3 sm:grid-cols-2">
            <Field
              id="career-experience"
              label={t('career.field.experienceYears')}
              error={errors.experience}
            >
              <Input
                id="career-experience"
                type="number"
                inputMode="decimal"
                min="0"
                max={EXPERIENCE_YEARS_MAX}
                step="0.5"
                value={experience}
                onChange={(e) => setExperience(e.target.value)}
                aria-invalid={errors.experience ? true : undefined}
                aria-describedby={errors.experience ? 'career-experience-error' : undefined}
              />
            </Field>
            <Field
              id="career-hours"
              label={t('career.field.availableHours')}
              error={errors.hours}
              hint={t('career.field.availableHours.hint', { max: HOURS_PER_WEEK_MAX })}
            >
              <Input
                id="career-hours"
                type="number"
                inputMode="decimal"
                min="0"
                max={HOURS_PER_WEEK_MAX}
                step="0.5"
                value={hours}
                onChange={(e) => setHours(e.target.value)}
                aria-invalid={errors.hours ? true : undefined}
                aria-describedby={
                  [errors.hours ? 'career-hours-error' : '', 'career-hours-hint']
                    .filter(Boolean)
                    .join(' ') || undefined
                }
              />
            </Field>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <Field id="career-employment" label={t('career.field.employmentType')}>
              <Select value={employment} onValueChange={setEmployment}>
                <SelectTrigger id="career-employment">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={EMPLOYMENT_UNSET}>
                    {t('career.field.employmentType.unset')}
                  </SelectItem>
                  {EMPLOYMENT_TYPES.map((type) => (
                    <SelectItem key={type} value={type}>
                      {t(`career.employment.${type}`)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field
              id="career-region"
              label={t('career.field.region')}
              error={errors.region}
              hint={t('career.field.region.hint')}
            >
              <Input
                id="career-region"
                value={region}
                maxLength={REGION_MAX}
                onChange={(e) => setRegion(e.target.value)}
                aria-invalid={errors.region ? true : undefined}
                aria-describedby={
                  [errors.region ? 'career-region-error' : '', 'career-region-hint']
                    .filter(Boolean)
                    .join(' ') || undefined
                }
              />
            </Field>
          </div>

          <Field
            id="career-notes"
            label={t('career.field.notes')}
            error={errors.notes}
            hint={t('career.field.notes.hint', { max: NOTES_MAX })}
          >
            <Textarea
              id="career-notes"
              value={notes}
              maxLength={NOTES_MAX}
              onChange={(e) => setNotes(e.target.value)}
              aria-invalid={errors.notes ? true : undefined}
              aria-describedby={
                [errors.notes ? 'career-notes-error' : '', 'career-notes-hint']
                  .filter(Boolean)
                  .join(' ') || undefined
              }
            />
          </Field>
        </div>

        <DialogFooter>
          {/* 清空只清本地表单;真正的清空发生在随后那次 Save —— 它会发 `{}`。 */}
          <Button type="button" variant="ghost" onClick={clearAll} disabled={saving}>
            {t('career.clearAll')}
          </Button>
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
