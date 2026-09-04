import { useState } from 'react'

import { useAuth } from '../../context/AuthContext'

import { CareerProfileCard } from './CareerProfileCard'
import { CareerProfileDialog } from './CareerProfileDialog'

/**
 * 设置-个人资料页里的职业档案小节 —— 只读卡片 + 共用的编辑弹窗。
 *
 * 存在的理由是把「卡片 + 弹窗 + open 状态」这三件事收成一个挂载点,让
 * `SettingsProfileAppearanceSection` 只加一行 JSX。弹窗自己存盘,所以这里
 * 不需要 onSaved —— `refreshProfile()` 已经把新档案刷回 context,卡片自动更新。
 */
export function CareerProfileSection() {
  const { profileMe } = useAuth()
  const [open, setOpen] = useState(false)

  return (
    <>
      <CareerProfileCard
        career={profileMe?.career_profile ?? null}
        onEdit={() => setOpen(true)}
      />
      <CareerProfileDialog open={open} onClose={() => setOpen(false)} />
    </>
  )
}
