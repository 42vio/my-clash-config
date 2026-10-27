'use strict';
let timer;
function notify(message) {
  const toast = document.querySelector('#toast');
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => { toast.hidden = true; }, 3000);
}
document.addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const block = button.closest('.subscription-block');
  const code = block.querySelector('code');
  if (button.dataset.action === 'reveal') {
    const visible = button.getAttribute('aria-pressed') !== 'true';
    code.textContent = visible ? code.dataset.url : code.dataset.hidden;
    button.setAttribute('aria-pressed', String(visible));
  } else if (button.dataset.action === 'copy') {
    try {
      await navigator.clipboard.writeText(code.dataset.url);
      notify('已复制订阅链接');
    } catch {
      notify('无法访问剪贴板，请显示链接后手动复制');
    }
  } else if (button.dataset.action === 'import') {
    location.href = 'clash://install-config?url=' + encodeURIComponent(code.dataset.url);
  }
});
