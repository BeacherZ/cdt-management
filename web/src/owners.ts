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

/**
 * 这台机器此刻在组里当不当班；不带归属 / 组已暂停 / 不是组员都返回空串。
 *
 * 换班组（按排班表轮）和均衡组（按评估点指派）都有当班概念，**两种组都要说**：
 * 以前只有均衡组露这一句，同一页里两种胶囊长得不一样；而且换班组非当班成员
 * 的按钮是锁着的，不写出来就只能靠猜。
 */
export function ownerDutyText(owner: Managed | null | undefined): string {
  if (!owner || !owner.active || owner.duty === undefined) return ''
  return owner.duty ? '当班' : '非当班'
}

/** 名字 + 托管状态；没有归属就是闲散手动。均衡组再带上当班/非当班，
    否则实例列表里看到按钮能点、不能点，还得再去均衡页找原因。 */
export function ownerLabel(owner: Managed | null | undefined): string {
  if (!owner) return '闲散手动'
  const duty = owner.kind === 'balance' && owner.active ? (owner.duty ? ' · 当班' : ' · 非当班') : ''
  return `${ownerBase(owner)}${owner.active ? '托管' : '已暂停'}${duty}`
}

/** 卡片胶囊里的短标签：当班/非当班要露在外面 —— 它是开关机按钮能不能点的唯一依据。
    换班组和均衡组共用一套写法（组名 + 「· 当班」），不再一种组有一句、另一种没有。 */
export function ownerTagText(owner: Managed): string {
  const duty = ownerDutyText(owner)
  return `${ownerBase(owner)}${duty ? ` · ${duty}` : ''}`
}

/**
 * 卡片上的开关机按钮能不能点。
 *
 * 换班组、单机保活托管中一律锁死：这两套流程每轮都对账，手动启停下一轮就被
 * 打回，点了只会多出两条反向指令（换班组本来就是为了省钱，排班表才是准的）。
 *
 * 均衡组的**非当班**成员是唯一豁免：评估点只比流量差值，不纠正任何人的开关机，
 * 手动开机不会被立刻打回（只有轮到它被换下来时，才按「下班后」设置处理），
 * 闲置时手动关掉也能省钱。当班仍然锁：关机等于中断对外服务，要换人请走
 * 「立即均衡」，那条路会先把新机器拉起来再切 DNS。
 */
export function ownerManualFree(owner: Managed | null | undefined): boolean {
  if (!owner || !owner.active) return true
  return owner.kind === 'balance' && !owner.duty
}

/** 启停按钮的提示语：锁着要说清为什么，放开要说清点下去之后会怎样 */
export function ownerActionHint(owner: Managed | null | undefined, running: boolean): string {
  if (!ownerManualFree(owner)) {
    if (owner?.kind === 'balance') {
      return `均衡组「${owner.name}」当班中，要换人请用「立即均衡」`
    }
    return `${owner ? ownerBase(owner) : ''}托管中，先暂停或移出后再手动启停`
  }
  if (owner?.kind === 'balance' && owner.active) {
    return running
      ? '关机（非当班成员，不影响对外服务）'
      : '开机（非当班成员，不会被自动拉起；轮到它当班时系统会自行开机）'
  }
  return running ? '关机（优先节省停机，不支持时转普通停机）' : '开机'
}
