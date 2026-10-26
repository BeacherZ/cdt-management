import type { SiteType } from './types'

/**
 * 地域候选。group 只用来在 <select> 里分组显示。
 *
 * sites 决定这个地域在哪个站点下可选：中国大陆地域只有中国站账号能开，
 * 国际站（alibabacloud.com 注册的账号）选不到。这是两个站点最实际的差别之一。
 *
 * 卡片、列表都从这里取中文名——不要再把 ap-northeast-1 这种 API 代码直接摆给用户看。
 */
export type RegionOption = {
  value: string
  label: string
  group: string
  sites: SiteType[]
}

export const REGIONS: RegionOption[] = [
  { value: 'cn-hangzhou', label: '华东 1（杭州）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-shanghai', label: '华东 2（上海）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-beijing', label: '华北 2（北京）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-zhangjiakou', label: '华北 3（张家口）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-qingdao', label: '华北 1（青岛）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-huhehaote', label: '华北 5（呼和浩特）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-shenzhen', label: '华南 1（深圳）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-guangzhou', label: '华南 3（广州）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-chengdu', label: '西南 1（成都）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-hongkong', label: '中国香港', group: '港澳与海外', sites: ['china', 'international'] },
  { value: 'ap-southeast-1', label: '新加坡', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-southeast-2', label: '澳大利亚（悉尼）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-southeast-3', label: '马来西亚（吉隆坡）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-southeast-5', label: '印度尼西亚（雅加达）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-southeast-6', label: '菲律宾（马尼拉）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-southeast-7', label: '泰国（曼谷）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-northeast-1', label: '日本（东京）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-northeast-2', label: '韩国（首尔）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-south-1', label: '印度（孟买）', group: '亚太', sites: ['china', 'international'] },
  { value: 'us-west-1', label: '美国（硅谷）', group: '北美', sites: ['china', 'international'] },
  { value: 'us-east-1', label: '美国（弗吉尼亚）', group: '北美', sites: ['china', 'international'] },
  { value: 'eu-west-1', label: '英国（伦敦）', group: '欧洲', sites: ['china', 'international'] },
  { value: 'eu-central-1', label: '德国（法兰克福）', group: '欧洲', sites: ['china', 'international'] },
  { value: 'me-east-1', label: '阿联酋（迪拜）', group: '中东', sites: ['china', 'international'] },
]

export const GROUP_ORDER = ['中国大陆', '港澳与海外', '亚太', '北美', '欧洲', '中东']

export const DEFAULT_REGION = 'cn-hongkong'

const LABELS = new Map(REGIONS.map((region) => [region.value, region.label]))

/** 地域代码 → 中文名。清单外的代码原样返回，不做猜测。 */
export function regionLabel(regionId?: string | null): string {
  const key = (regionId ?? '').trim()
  if (!key) return '未填地域'
  return LABELS.get(key) ?? key
}

export function regionsFor(site: SiteType): RegionOption[] {
  return REGIONS.filter((region) => region.sites.includes(site))
}
