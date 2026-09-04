import { ClipboardList } from 'lucide-react'

import type { CareerProfile } from '@beecount/api-client'
import { Button, Card, CardContent, CardHeader, CardTitle, EmptyState, useT } from '@beecount/ui'

interface Props {
  /** null / 空档案 = 用户没填过。 */
  career: CareerProfile | null
  onEdit: () => void
}

/** 小时 / 年数去掉无意义的小数尾巴。 */
function formatUnitNumber(value: number): string {
  return Number.isInteger(value) ? `${value}` : value.toFixed(1)
}

/**
 * 设置-个人资料页里的职业档案只读卡片。编辑一律走 `CareerProfileDialog`,
 * 这里不放任何输入控件 —— 两处可编辑就会有两套「哪些字段该发」的逻辑。
 *
 * 档案本身只喂给收入增长建议端点,不影响任何统计数字,所以这张卡不做任何计算。
 */
export function CareerProfileCard({ career, onEdit }: Props) {
  const t = useT()
  const dash = t('common.dash')
  const filled =
    career != null &&
    (career.occupation != null ||
      (career.skills?.length ?? 0) > 0 ||
      career.experience_years != null ||
      career.available_hours_per_week != null ||
      career.employment_type != null ||
      career.region != null ||
      career.notes != null)

  return (
    <Card className="bc-panel">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>{t('career.card.title')}</CardTitle>
          {filled ? (
            <Button type="button" variant="outline" size="sm" onClick={onEdit}>
              {t('common.edit')}
            </Button>
          ) : null}
        </div>
        <p className="text-xs text-muted-foreground">{t('career.card.desc')}</p>
      </CardHeader>
      <CardContent>
        {filled && career ? (
          <dl className="grid gap-3 sm:grid-cols-2">
            <Row label={t('career.field.occupation')} value={career.occupation ?? dash} />
            <Row
              label={t('career.field.employmentType')}
              value={
                career.employment_type ? t(`career.employment.${career.employment_type}`) : dash
              }
            />
            <Row
              label={t('career.field.experienceYears')}
              value={
                career.experience_years == null
                  ? dash
                  : t('career.value.years', { n: formatUnitNumber(career.experience_years) })
              }
            />
            <Row
              label={t('career.field.availableHours')}
              value={
                career.available_hours_per_week == null
                  ? dash
                  : t('career.value.hoursPerWeek', {
                      n: formatUnitNumber(career.available_hours_per_week),
                    })
              }
            />
            <Row label={t('career.field.region')} value={career.region ?? dash} />
            <Row
              label={t('career.field.skills')}
              value={career.skills?.length ? career.skills.join(' · ') : dash}
            />
            {career.notes ? (
              <div className="sm:col-span-2">
                <dt className="text-[11px] uppercase tracking-wide text-muted-foreground">
                  {t('career.field.notes')}
                </dt>
                <dd className="mt-0.5 whitespace-pre-wrap break-words text-sm text-foreground">
                  {career.notes}
                </dd>
              </div>
            ) : null}
          </dl>
        ) : (
          <EmptyState
            icon={<ClipboardList className="h-6 w-6" />}
            title={t('career.card.empty.title')}
            description={t('career.card.empty.desc')}
            action={
              <Button type="button" size="sm" onClick={onEdit}>
                {t('career.card.fill')}
              </Button>
            }
          />
        )}
      </CardContent>
    </Card>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 break-words text-sm text-foreground">{value}</dd>
    </div>
  )
}
