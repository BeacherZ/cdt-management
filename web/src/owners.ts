import type { Managed } from './types'

/**
 * 「归属」文案。
 *
 * 服务器卡片、列表视图、换班组的成员选择器都显示这台机器现在归谁，
 * 措辞只在这里定义一次 —— 以前卡片和列表各写一套，结果出现
 * 「换班组「HK」」和「换班「HK」」两种说法。
 */
export function ownerBase(owner: Managed): string {
  if (owner.kind === 'group') return `换班组「${owner.name}」`
  if (owner.kind === 'balance') return `均衡组「${owner.name}」`
  return '单机保活'
}

/** 归属用的图标类别：卡片和列表按它挑图标，别在每处再写一串三元 */
export function ownerIcon(owner: Managed): 'shift' | 'balance' | 'keepalive' {
  if (owner.kind === 'group') return 'shift'
  if (owner.kind === 'balance') return 'balance'
  return 'keepalive'
}

/**
 * 归属胶囊的配色：由**归属类型**决定，不再按「托管中 / 已暂停」上色。
 *
 * 之前按状态取色，换班组和均衡组都落进 .locked 的蓝，一眼分不出是哪套逻辑；
 * 现在约定：换班组绿、均衡组蓝、单机保活紫，闲散手动不上色（中性灰描边）。
 * 颜色只表示「被哪个流程接管」，没归属的机器没有颜色才最不容易看错。
 * 暂停只在外面多一个 .paused 描边，不改色相。
 */
export function ownerTone(owner: Managed | null | undefined): string {
  if (!owner) return 'free'
  if (owner.kind === 'group') return 'kind-shift'
  if (owner.kind === 'balance') return 'kind-balance'
  return 'kind-keepalive'
}

/** 名字 + 托管状态；没有归属就是闲散手动 */
export function ownerLabel(owner: Managed | null | undefined): string {
  return owner ? `${ownerBase(owner)}${owner.active ? '托管' : '已暂停'}` : '闲散手动'
}
