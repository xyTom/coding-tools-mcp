const localeButtons = [...document.querySelectorAll('[data-set-locale]')];
const localizedNodes = [...document.querySelectorAll('[data-lang]')];

function setLocale(locale) {
  const selected = locale === 'en' ? 'en' : 'zh';
  document.documentElement.dataset.locale = selected;
  document.documentElement.lang = selected === 'en' ? 'en' : 'zh-CN';
  for (const node of localizedNodes) node.hidden = node.dataset.lang !== selected;
  for (const button of localeButtons) {
    button.setAttribute('aria-pressed', String(button.dataset.setLocale === selected));
  }
}

for (const button of localeButtons) {
  button.addEventListener('click', () => setLocale(button.dataset.setLocale));
}

for (const button of document.querySelectorAll('[data-copy-target]')) {
  button.addEventListener('click', async () => {
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target) return;
    const original = button.textContent;
    try {
      await navigator.clipboard.writeText(target.textContent.trim());
      button.textContent = '已复制 / Copied';
    } catch {
      button.textContent = '复制失败 / Copy failed';
    }
    window.setTimeout(() => { button.textContent = original; }, 1600);
  });
}

setLocale('zh');
