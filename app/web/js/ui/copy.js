import { toast, toastError } from './toast.js';

// navigator.clipboard 只在安全上下文可用，并且可能因权限或页面失焦被拒绝；
// 这两种情况都回退到隐藏文本框 + execCommand。
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    const written = await navigator.clipboard.writeText(text).then(() => true, () => false);
    if (written) return true;
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:-1000px;left:-1000px;opacity:0';
  document.body.appendChild(area);
  const selection = document.getSelection();
  const saved = selection && selection.rangeCount ? selection.getRangeAt(0) : null;
  area.select();
  const ok = document.execCommand('copy');
  area.remove();
  if (saved) {
    selection.removeAllRanges();
    selection.addRange(saved);
  }
  return ok;
}

/** 复制并给出结果提示；text 可以是返回字符串的异步函数。 */
export async function copyWithToast(text, label = '已复制到剪贴板') {
  let value;
  try {
    value = typeof text === 'function' ? await text() : text;
  } catch (err) {
    toastError('复制失败', err);
    return false;
  }
  const ok = await copyText(value);
  if (ok) toast.success(label);
  else toast.error('复制失败', '请手动选中文本后复制');
  return ok;
}
