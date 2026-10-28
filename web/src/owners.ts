import type { Managed } from './types'

/**
 * 「归属」文案。
 *
 * 服务器卡片、列表视图、换班组的成员选择器都显示这台机器现在归谁，
 * 措辞只在这里定义一次 —— 以前卡片和列表各写一套，结果出现
 * 「换班组「HK」」和「换班「HK」」两种说法。
 */
export function ownerBase(owner: Managed): string {
  return owner.kind === 'group' ? `换班组「${owner.name}」` : '单机保活'
}

/** 名字 + 托管状态；没有归属就是闲散手动 */
export function ownerLabel(owner: Managed | null | undefined): string {
  return owner ? `${ownerBase(owner)}${owner.active ? '托管' : '已暂停'}` : '闲散手动'
}
