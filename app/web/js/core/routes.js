// 全部页面的登记表，底部菜单栏、页内二级导航与命令面板都从这里读取。
// dock 为 true 的页面出现在菜单栏；section 相同的页面在页头显示为一组二级导航。
export const ROUTES = [
  { path: 'dashboard', title: '仪表盘', icon: 'BarChart3', dock: true, group: 'overview', groupLabel: '总览',
    keywords: '首页 总览 概况', load: () => import('../pages/dashboard.js') },
  { path: 'accounts', title: '账号', icon: 'Users', dock: true, group: 'ops', groupLabel: '运营', section: 'accounts',
    keywords: '账号池 积分 签到 加油站 今日活跃 登录 导入 导出', load: () => import('../pages/accounts.js') },
  { path: 'tasks', title: '任务', icon: 'ClipboardList', section: 'accounts',
    keywords: '成长任务 猫猫旅行 调度器 巡检 签到 加油站 打卡 今日活跃 积分变动', load: () => import('../pages/tasks.js') },
  { path: 'credits', title: '积分', icon: 'Coins', dock: true, group: 'ops',
    keywords: '积分 余额 套餐 到期 来源', load: () => import('../pages/credits.js') },
  { path: 'keys', title: '密钥', icon: 'KeyRound', dock: true, group: 'ops',
    keywords: 'API Key 密钥 配额 白名单', load: () => import('../pages/keys.js') },
  { path: 'models', title: '模型', icon: 'Boxes', dock: true, group: 'ops', section: 'models',
    keywords: '模型库 能力 上下文', load: () => import('../pages/models.js') },
  { path: 'playground', title: '测试台', icon: 'MessageSquare', section: 'models',
    keywords: '聊天 调试 对话', load: () => import('../pages/playground.js') },
  { path: 'stats', title: '用量', icon: 'TrendingUp', dock: true, group: 'govern', groupLabel: '治理', section: 'usage',
    keywords: '统计 Token 消耗 性能 分析', load: () => import('../pages/stats.js') },
  { path: 'estimate', title: '费用估算', icon: 'Calculator', section: 'usage',
    keywords: '费用 估算 官方价格 省了多少 积分换算', load: () => import('../pages/estimate.js') },
  { path: 'requests', title: '请求日志', dockTitle: '日志', icon: 'ScrollText', dock: true, group: 'govern', section: 'logs',
    keywords: '调用记录 审计 请求', load: () => import('../pages/requests.js') },
  { path: 'logs', title: '运行日志', icon: 'Terminal', section: 'logs',
    keywords: '网关日志 控制台 错误', load: () => import('../pages/logs.js') },
  { path: 'settings', title: '设置', icon: 'Settings', dock: true, group: 'govern',
    keywords: '配置 选项', load: () => import('../pages/settings.js') },
];

// 设置页的标签：地址为 #/settings/<id>。
export const SETTINGS_TABS = [
  { id: 'gateway', title: '网关行为', icon: 'Server', keywords: '保留积分 每日限额 自动切换 Messages 网络工具 接口测试模型 打卡' },
  { id: 'cost', title: '费用估算', icon: 'Calculator', keywords: '官方价格 积分 换算 省了多少' },
  { id: 'proxy', title: '代理槽', icon: 'Network', keywords: '代理 出口 槽位' },
  { id: 'security', title: '面板密码', icon: 'ShieldCheck', keywords: '密码 安全 登录' },
  { id: 'advanced', title: '高级', icon: 'Wrench', keywords: '环境变量 功能开关 缓存 连接复用 Anthropic 会话粘性' },
  { id: 'maintenance', title: '运维', icon: 'Gauge', keywords: '日志 保留 定价 刷新 超时 提示词 更新' },
  { id: 'about', title: '运行信息', icon: 'Info', keywords: '版本 目录 关于' },
];

export function findRoute(path) {
  return ROUTES.find((r) => r.path === path) || null;
}

/** 与 path 同组的二级导航项；不属于任何组时返回空数组。 */
export function sectionOf(path) {
  const route = findRoute(path);
  if (!route || !route.section) return [];
  return ROUTES.filter((r) => r.section === route.section);
}
