import { el, button, muted, errorBox, notify } from './dom.js';

let nextDialogId = 0;

// showModal supplies background inertness; explicitly trap Tab and restore focus.
export function dialog(title, subtitle, { wide = false, drawer = false, onClose } = {}) {
  const previous = document.activeElement;
  const body = el('div', { class: 'modal-body' });
  const footer = el('div', { class: 'modal-footer' });
  const heading = el('h2', { id: `dialog-${++nextDialogId}` }, title);
  const modal = el('dialog', { class: `modal ${wide ? 'wide' : ''} ${drawer ? 'drawer' : ''}`, 'aria-labelledby': heading.id },
    el('div', { class: 'modal-head' }, el('div', {}, heading, subtitle ? muted(subtitle) : null),
      button('×', () => modal.close(), 'icon-btn', { 'aria-label': '关闭对话框' })), body, footer);
  modal.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const nodes = [...modal.querySelectorAll('button, input, select, textarea, a[href], summary, [tabindex="0"]')]
      .filter((node) => !node.disabled && node.getClientRects().length);
    const first = nodes[0];
    const last = nodes.at(-1);
    if (!first) { event.preventDefault(); modal.focus(); return; }
    if (event.shiftKey && (document.activeElement === first || document.activeElement === modal)) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  });
  modal.addEventListener('close', () => {
    onClose?.();
    modal.remove();
    if (previous?.isConnected) previous.focus();
    else {
      const replacement = previous?.dataset.focusKey
        ? document.querySelector(`[data-focus-key="${CSS.escape(previous.dataset.focusKey)}"]`) : null;
      (replacement || document.getElementById('main'))?.focus();
    }
  }, { once: true });
  return {
    body, footer, modal,
    close: () => modal.close(),
    open: (focus) => { document.body.append(modal); modal.showModal(); (focus || body.querySelector('input, textarea, select') || footer.querySelector('button'))?.focus(); },
  };
}

export function confirmAction({ title, description, content, confirmText = '确认删除', action, onSuccess }) {
  const view = dialog(title, '请确认操作对象；此操作不可撤销。');
  const errors = el('div');
  const cancel = button('取消', view.close);
  const submit = button(confirmText, async () => {
    submit.disabled = true;
    cancel.disabled = true;
    submit.textContent = '处理中…';
    errors.replaceChildren();
    try {
      const result = await action();
      view.close();
      await onSuccess?.(result);
    } catch (error) {
      if (view.modal.open) errors.replaceChildren(errorBox(error));
      else notify(error.message || '操作未完成，请刷新后重试。', true);
    } finally {
      submit.disabled = false;
      cancel.disabled = false;
      submit.textContent = confirmText;
    }
  }, 'danger');
  view.body.append(el('p', { class: 'mb' }, description), content || el('span'), errors);
  view.footer.append(cancel, submit);
  view.open(cancel);
}
