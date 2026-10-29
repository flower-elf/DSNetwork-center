// 账号页与任务页共用的操作流程：先调用接口，再把结果汇总成一条提示。
// apply(result) 在接口返回后、提示之前执行，负责把结果写回各自页面的状态。
import { confirmDialog } from '../../ui/dialog.js';
import { toast, toastError } from '../../ui/toast.js';
import * as activity from './activity.js';
import { anyOk, summarizeResults } from './logic.js';
import * as acct from './actions.js';

function report(results, title, { okText, withDetail = '', emptyText }) {
  if (!results.length) toast.warn(emptyText);
  else if (anyOk(results)) toast.success(title, summarizeResults(results, { okText, withDetail }));
  else toast.warn(title, summarizeResults(results));
}

/** Buddy 加油站签到；uid 留空时给全部国内版账号领取。 */
export async function runCheckin({ uid, apply } = {}) {
  try {
    const result = await acct.checkin(uid);
    if (apply) await apply(result);
    report(result.results || [], activity.BUDDY_LABEL + '签到',
      { okText: '已领取', emptyText: '没有可领的国内版账号' });
  } catch (err) {
    toastError('签到失败', err);
  }
}

/** 未活跃账号一键打卡：只发给今天还没打卡的已启用国际版账号。 */
export async function runDailyChatPending({ accounts, byUid, apply } = {}) {
  const pending = activity.pendingDailyChat(accounts, byUid);
  if (!pending.length) {
    toast.success('国际版账号今天都已打卡');
    return;
  }
  try {
    const result = await acct.dailyChatPending(pending.map((account) => account.uid));
    if (apply) await apply(result);
    report(result.results || [], '未活跃账号打卡',
      { okText: '打卡成功', emptyText: '名单里的账号没有执行' });
  } catch (err) {
    toastError('打卡失败', err);
  }
}

/** 网页通道打卡：每个已启用的国际版账号建一个网页端会话，会消耗少量积分。 */
export async function runDailyChatWeb({ apply } = {}) {
  const go = await confirmDialog({
    title: '确认网页通道打卡？',
    desc: '会为每个已启用的国际版账号各建一个网页端会话。这会实际发起一次任务并消耗该账号少量积分，用于计入官方「每日活跃」。',
    confirmText: '我已了解，继续',
  });
  if (!go) return;
  try {
    const result = await acct.dailyChatWeb();
    if (apply) await apply(result);
    report(result.results || [], '网页通道打卡',
      { okText: '已建会话', withDetail: 'conversation', emptyText: '没有可打卡的国际版账号' });
  } catch (err) {
    toastError('网页通道打卡失败', err);
  }
}
