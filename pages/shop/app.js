// 打卡积分商店 · WebUI 插件页
// 只通过 window.AstrBotPluginPage bridge 与插件后端通信，不直接访问 Dashboard。
const bridge = window.AstrBotPluginPage;

const state = {
  summary: null,
  productRows: [],
  productKeyword: '',
  productCategory: '',
  productStatus: '',
  productSort: '',
  productSortAsc: false,
  userRows: [],
  redeemRows: [],
  ledgerRows: [],
  userPage: 1,
  redeemPage: 1,
  ledgerPage: 1,
  userTotal: 0,
  redeemTotal: 0,
  ledgerTotal: 0,
  editingImagePath: '',
  editingSortOrder: null,
  pendingImage: null,
  previewProductId: '',
  previewUserKey: 'preview:0',
  adjustUserKey: '',
  assetUrls: {},
  rewardsLoaded: false,
  economy: null,
  economyLoaded: false,
  assetUserKey: '',
  assetRequest: 0,
};

const PAGE_SIZE = 20;

// 长名称的溢出保护：包一层 block 再裁剪（直接给 td 设 max-width 在自动表格布局里不可靠）
function setClipText(cell, text) {
  const value = text === null || text === undefined ? '' : String(text);
  cell.textContent = '';
  const span = document.createElement('span');
  span.className = 'cell-clip';
  span.textContent = value;
  if (value) span.title = value;
  cell.appendChild(span);
}

// 成功类提示 3 秒后自动消失；失败类提示一直留着，直到手动关闭。
const STATUS_TTL = { ok: 3000, info: 3000 };

const $ = (id) => document.getElementById(id);

function errorText(error) {
  if (!error) return '未知错误';
  return error.message ? String(error.message) : String(error);
}

// ---------------------------------------------------------------- 状态提示

function say(message, kind) {
  const box = $('status');
  const text = $('status-text');
  if (!box || !text) return;
  window.clearTimeout(say.timer);
  const value = message === undefined || message === null ? '' : String(message);
  // 操作失败时提示也留在当前对话框内，避免被遮罩挡住。
  const activeModal = topModal();
  const modalStatus = activeModal && activeModal.querySelector('.modal-status');
  if (modalStatus) {
    modalStatus.textContent = kind === 'error' ? value : '';
    modalStatus.hidden = kind !== 'error' || !value;
    if (!modalStatus.hidden) modalStatus.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  text.textContent = value;
  box.className = 'status' + (value && kind ? ` status-${kind}` : '');
  box.hidden = !value;
  const close = $('status-close');
  if (close) close.hidden = !value || kind !== 'error';
  if (!value) return;
  const ttl = STATUS_TTL[kind];
  if (ttl) {
    say.timer = window.setTimeout(() => say(''), ttl);
  }
}

// 异步按钮：进行中显示 spinner + 禁用，天然防重复点击。
async function withBusy(button, task, busyText) {
  if (!button) return task();
  if (button.dataset.busy === '1') return undefined;
  const labelNodes = Array.from(button.childNodes);
  const wasDisabled = button.disabled;
  const modal = button.closest('.modal');
  const closeButtons = modal ? Array.from(modal.querySelectorAll('[data-modal-close]')) : [];
  const closeStates = closeButtons.map((item) => item.disabled);
  closeButtons.forEach((item) => { item.disabled = true; });
  button.dataset.busy = '1';
  button.disabled = true;
  button.classList.add('is-busy');
  if (busyText) button.textContent = busyText;
  try {
    return await task();
  } finally {
    button.dataset.busy = '';
    button.disabled = wasDisabled;
    button.classList.remove('is-busy');
    if (busyText) button.replaceChildren(...labelNodes);
    closeButtons.forEach((item, index) => { item.disabled = closeStates[index]; });
  }
}

function run(button, task, busyText) {
  return withBusy(button, task, busyText).catch((error) => say(errorText(error), 'error'));
}

// ---------------------------------------------------------------- 表单校验

function setFieldError(inputId, message) {
  const input = $(inputId);
  const box = $(`${inputId}-error`);
  if (input) {
    if (message) input.setAttribute('aria-invalid', 'true');
    else input.removeAttribute('aria-invalid');
  }
  if (box) box.textContent = message || '';
}

function clearErrors(ids) {
  ids.forEach((id) => setFieldError(id, ''));
}

function readInteger(inputId, { allowEmpty, min = 0 }) {
  const input = $(inputId);
  if (!input) return { value: null, error: '' };
  const raw = String(input.value || '').trim();
  if (raw === '') {
    return allowEmpty
      ? { value: null, error: '' }
      : { value: null, error: '这一项不能为空，请填写一个不小于 0 的整数。' };
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value)) {
    return { value: null, error: '请填写整数。' };
  }
  if (value < min) {
    return { value: null, error: `请填写不小于 ${min} 的整数。` };
  }
  return { value, error: '' };
}

function focusFirstInvalid(ids) {
  const broken = ids.find((id) => {
    const input = $(id);
    return input && input.getAttribute('aria-invalid') === 'true';
  });
  if (broken && $(broken)) $(broken).focus();
  return Boolean(broken);
}

// ---------------------------------------------------------------- 主题

// 宿主 WebUI 有深色主题：能拿到 isDark 就按它来，拿不到才回落到系统 prefers-color-scheme。
function applyTheme(isDark) {
  const root = document.documentElement;
  if (isDark === true) root.setAttribute('data-theme', 'dark');
  else if (isDark === false) root.setAttribute('data-theme', 'light');
  else root.removeAttribute('data-theme');
}

function parseNumberList(value) {
  const parts = String(value || '')
    .split(/[,，\s]+/)
    .filter((item) => item !== '');
  if (parts.length > 30) return { values: [], error: '最多填写 30 项。' };
  const values = [];
  for (const [index, item] of parts.entries()) {
    if (!/^\d+$/.test(item) || !Number.isSafeInteger(Number(item))) {
      return { values: [], error: `第 ${index + 1} 项「${item}」必须是非负整数。` };
    }
    values.push(Number(item));
  }
  return { values, error: '' };
}

async function post(endpoint, body) {
  return bridge.apiPost(endpoint, body || {});
}

// 统一弹窗焦点与背景隔离；确认窗叠在其他弹窗上时只允许最上层交互。
const modalReturnFocus = new WeakMap();

function topModal() {
  if ($('confirm-modal') && !$('confirm-modal').hidden) return $('confirm-modal');
  return Array.from(document.querySelectorAll('.modal:not([hidden])')).at(-1) || null;
}

function modalFocusables(modal) {
  return Array.from(modal.querySelectorAll('button:not(:disabled), input:not(:disabled):not([type="hidden"]), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])'))
    .filter((item) => item.getClientRects().length > 0 && !item.closest('[hidden]'));
}

function syncModalState() {
  const active = topModal();
  document.body.classList.toggle('has-modal', Boolean(active));
  Array.from(document.body.children).forEach((item) => {
    if (item.tagName !== 'SCRIPT') item.inert = Boolean(active) && item !== active;
  });
}

function showModal(modalId, initialFocus) {
  const modal = $(modalId);
  modalReturnFocus.set(modal, document.activeElement);
  const status = modal.querySelector('.modal-status');
  if (status) { status.textContent = ''; status.hidden = true; }
  modal.hidden = false;
  syncModalState();
  (initialFocus || modal.querySelector('[role="dialog"], [role="alertdialog"]')).focus();
}

function hideModal(modalId) {
  const modal = $(modalId);
  modal.hidden = true;
  syncModalState();
  const previous = modalReturnFocus.get(modal);
  const active = topModal();
  if (previous && previous.isConnected && !previous.closest('[inert]') && !previous.disabled) previous.focus();
  else if (active) (modalFocusables(active)[0] || active.querySelector('[role="dialog"], [role="alertdialog"]')).focus();
  else document.querySelector('.tab.active')?.focus();
}

function bindModalFocus() {
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const modal = topModal();
    if (!modal) return;
    const controls = modalFocusables(modal);
    if (!controls.length) {
      event.preventDefault();
      modal.querySelector('[role="dialog"], [role="alertdialog"]').focus();
      return;
    }
    const first = controls[0];
    const last = controls.at(-1);
    const focused = document.activeElement;
    if (event.shiftKey && (focused === first || !modal.contains(focused))) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (focused === last || !modal.contains(focused))) {
      event.preventDefault(); first.focus();
    }
  });
}

// 插件页运行在 sandbox="allow-scripts allow-forms allow-downloads" 的 iframe 里，
// 没有 allow-modals：window.confirm()/alert() 会被浏览器直接忽略（confirm 恒为
// false），点了删除也永远进不去分支。所以二次确认一律走页面内对话框。
function askConfirm({ title, message, confirmText = '确定', danger = true }) {
  const modal = $('confirm-modal');
  const okButton = $('confirm-ok');
  const cancelButton = $('confirm-cancel');
  $('confirm-title').textContent = title || '请确认';
  $('confirm-text').textContent = message || '';
  okButton.textContent = confirmText;
  okButton.className = danger ? 'btn btn-danger' : 'btn btn-primary';
  showModal('confirm-modal', cancelButton);
  return new Promise((resolve) => {
    const finish = (value) => {
      hideModal('confirm-modal');
      okButton.removeEventListener('click', onOk);
      cancelButton.removeEventListener('click', onCancel);
      modal.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey);
      resolve(value);
    };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onBackdrop = (event) => {
      if (event.target === modal) finish(false);
    };
    const onKey = (event) => {
      if (event.key === 'Escape') finish(false);
    };
    okButton.addEventListener('click', onOk);
    cancelButton.addEventListener('click', onCancel);
    modal.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey);
  });
}

// 弹窗统一行为：点遮罩 / 按 Esc 关闭
function bindModalDismiss(modalId, close) {
  const modal = $(modalId);
  if (!modal) return;
  modal.addEventListener('click', (event) => {
    if (event.target === modal && !modal.querySelector('.is-busy')) close();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || modal.hidden) return;
    const confirm = $('confirm-modal');
    if (confirm && !confirm.hidden) return;
    if (!modal.querySelector('.is-busy')) close();
  });
}

// ---------------------------------------------------------------- 概览与统计

function formatStat(value) {
  if (value === undefined || value === null || value === '') return '-';
  return typeof value === 'number' ? value.toLocaleString('zh-CN') : String(value);
}

function renderStats() {
  const stats = (state.summary && state.summary.statistics) || {};
  const cards = [
    ['用户数', formatStat(stats.user_count), '参与打卡的用户', ''],
    ['商品数', `${stats.enabled_product_count || 0} / ${stats.product_count || 0}`, '已上架 / 全部商品', ''],
    ['今日打卡', formatStat(stats.checkin_today), '按每日重置时间统计', 'is-primary'],
    ['流通积分', formatStat(stats.total_points), '当前用户积分总额', 'is-primary'],
    ['待发放', formatStat(stats.pending_redemption_count), '等待处理的兑换', 'is-pending'],
    ['兑换总数', formatStat(stats.redemption_count), '全部兑换记录', ''],
  ];
  $('stats').innerHTML = cards
    .map(
      ([label, value, hint, className]) =>
        `<div class="stat ${className}"><b>${value}</b><span>${label}</span><small>${hint}</small></div>`,
    )
    .join('');
}

async function loadSummary() {
  const summary = await bridge.apiGet('shop/summary');
  state.summary = summary;
  fillRewardForm(summary.rewards);
  fillSettingsForm(summary.settings || {});
  state.productRows = summary.products || [];
  fillCategoryOptions();
  syncProductSortHeader();
  renderProducts();
  renderStats();
}

// ---------------------------------------------------------------- 打卡规则

// 每种积分模式下真正生效的输入项，其余置灰，避免管理员填了不生效的值
const REWARD_FIELDS = {
  fixed: ['reward-fixed'],
  random: ['reward-min', 'reward-max'],
  custom: ['reward-list', 'reward-weights'],
};

function syncRewardMode() {
  const mode = $('reward-mode').value;
  const active = new Set(REWARD_FIELDS[mode] || []);
  Object.values(REWARD_FIELDS)
    .flat()
    .forEach((id) => {
      const input = $(id);
      if (!input) return;
      const on = active.has(id);
      input.disabled = !on;
      if (input.parentElement) input.parentElement.classList.toggle('field-off', !on);
    });
}

function fillRewardForm(rewards) {
  const data = rewards && typeof rewards === 'object' ? rewards : null;
  const empty = $('rewards-empty');
  const form = document.querySelector('.panel[data-panel="rewards"] .form-grid');
  const actions = document.querySelector('.panel[data-panel="rewards"] .row-end');
  if (empty) empty.hidden = Boolean(data);
  if (form) form.hidden = !data;
  if (actions) actions.hidden = !data;
  state.rewardsLoaded = Boolean(data);
  if (!data) return;
  $('reward-mode').value = data.mode || 'fixed';
  $('reward-fixed').value = data.fixed ?? 10;
  $('reward-min').value = data.random_min ?? 5;
  $('reward-max').value = data.random_max ?? 20;
  $('reward-list').value = (data.custom_list || []).join(',');
  $('reward-weights').value = (data.custom_weights || []).join(',');
  $('bonus-enabled').checked = data.streak_bonus_enabled !== false;
  $('bonus-every').value = data.streak_bonus_every ?? 7;
  $('bonus-points').value = data.streak_bonus_points ?? 50;
  syncRewardMode();
}

async function saveRewards() {
  if (!state.rewardsLoaded) {
    say('还没有读到打卡规则配置，请先点右上角「刷新」重新拉取。', 'error');
    return;
  }
  clearErrors(['reward-list', 'reward-weights']);
  const parsedList = parseNumberList($('reward-list').value);
  const parsedWeights = parseNumberList($('reward-weights').value);
  if (parsedList.error) setFieldError('reward-list', parsedList.error);
  if (parsedWeights.error) setFieldError('reward-weights', parsedWeights.error);
  if (focusFirstInvalid(['reward-list', 'reward-weights'])) return;
  const list = parsedList.values;
  const weights = parsedWeights.values;
  if ($('reward-mode').value === 'custom' && list.length === 0) {
    setFieldError('reward-list', '自定义模式至少要填一个积分值，例如 5,10,20。');
    $('reward-list').focus();
    return;
  }
  if (weights.length && weights.length !== list.length) {
    setFieldError('reward-weights', '权重个数要和积分列表一致，或者整列留空表示等概率。');
    $('reward-weights').focus();
    return;
  }
  if (weights.length && weights.every((item) => item === 0)) {
    setFieldError('reward-weights', '权重不能全部为 0；整列留空表示等概率。');
    $('reward-weights').focus();
    return;
  }
  const payload = {
    mode: $('reward-mode').value,
    fixed: Number($('reward-fixed').value || 0),
    random_min: Number($('reward-min').value || 0),
    random_max: Number($('reward-max').value || 0),
    custom_list: list,
    custom_weights: weights,
    streak_bonus_enabled: $('bonus-enabled').checked,
    streak_bonus_every: Number($('bonus-every').value || 0),
    streak_bonus_points: Number($('bonus-points').value || 0),
  };
  const result = await post('shop/rewards/save', payload);
  await loadSummary();
  say(result.mirrored ? '打卡规则已保存（已同步到插件配置页）' : '打卡规则已保存', 'ok');
}

// ---------------------------------------------------------------- 展示设置

function fillSettingsForm(settings) {
  $('set-title').value = settings.shop_title || '';
  $('set-subtitle').value = settings.shop_subtitle || '';
  $('set-prefix').value = settings.display_prefix || '/';
  $('set-bare-command-hint').checked = settings.bare_command_hint_enabled === true;
  $('set-board-size').value = settings.leaderboard_size ?? 20;
  $('set-board-scope').value = settings.leaderboard_scope || 'platform';
  $('set-timeout').value = settings.confirm_timeout_seconds ?? 60;
  $('set-reset-hour').value = settings.reset_hour ?? 0;
  $('set-scope').value = settings.points_scope || 'platform_user';
  updatePrefixHint();
}

// 显示前缀只影响文案：这里的改动不会让机器人接受新的唤醒前缀
function updatePrefixHint() {
  const hint = $('prefix-hint');
  if (!hint) return;
  const prefix = $('set-prefix').value.trim() || '/';
  hint.textContent = '文案里会写成' + prefix + '打卡、' + prefix + '商店；只影响显示，实际触发仍看 AstrBot 的唤醒前缀。';
  const bind = $('bind-cmd');
  if (bind) bind.textContent = prefix + '绑定邮箱 邮箱';
}

async function saveSettings() {
  const result = await post('shop/settings/save', {
    shop_title: $('set-title').value,
    shop_subtitle: $('set-subtitle').value,
    display_prefix: $('set-prefix').value.trim(),
    bare_command_hint_enabled: $('set-bare-command-hint').checked,
    leaderboard_size: Number($('set-board-size').value || 20),
    leaderboard_scope: $('set-board-scope').value,
    confirm_timeout_seconds: Number($('set-timeout').value || 60),
    reset_hour: Number($('set-reset-hour').value || 0),
    points_scope: $('set-scope').value,
  });
  await loadSummary();
  say(result.mirrored ? '设置已保存（已同步到插件配置页）' : '设置已保存', 'ok');
}

// ---------------------------------------------------------------- 商品管理

function renderProducts() {
  const tbody = $('product-table').querySelector('tbody');
  const rows = productView();
  const empty = rows.length === 0;
  tbody.innerHTML = '';
  const wrap = $('product-table').closest('.table-wrap');
  if (wrap) {
    wrap.hidden = empty;
    // 表格自带滚动区：重新加载后回到顶部，避免"刷新完还停在半截"
    wrap.scrollTop = 0;
    wrap.scrollLeft = 0;
  }
  $('product-empty').hidden = !empty;
  if (state.productRows.length === 0) {
    $('product-empty-title').textContent = '还没有商品';
    $('product-empty-hint').textContent =
      '点右上角「新增商品」添加第一个商品，保存后商店图会立刻用上它。';
  } else {
    $('product-empty-title').textContent = '没有匹配的商品';
    $('product-empty-hint').textContent = '换个关键词或点「清空筛选」看全部商品。';
  }
  const count = $('product-count');
  if (count) {
    count.textContent = `共 ${state.productRows.length} 个 · 显示 ${rows.length} 个`;
  }
  // 序号以接口返回的 number 为准（服务端按全量商品算，和用户看到的编号一致）；
  // 万一旧数据没带 number，就按全量列表位置补一个等价的编号。
  const numbers = new Map(
    state.productRows.map((item, index) => [
      item.id,
      item.number || String(index + 1).padStart(2, '0'),
    ]),
  );
  rows.forEach((product) => {
    const row = document.createElement('tr');
    const stock = product.stock === null || product.stock === undefined ? '不限量' : product.stock;
    const limit =
      product.per_user_limit === null || product.per_user_limit === undefined
        ? '不限购'
        : product.per_user_limit;
    row.innerHTML = `
      <td class="num cell-number"></td>
      <td class="thumb-cell"></td>
      <td class="cell-name"></td>
      <td class="num"></td>
      <td class="num"></td>
      <td class="num"></td>
      <td></td>
      <td></td>
      <td class="cell-actions"><div class="row-actions">
        <button class="btn-link" type="button" data-action="up" title="上移">↑</button>
        <button class="btn-link" type="button" data-action="down" title="下移">↓</button>
        <button class="btn-link" type="button" data-action="edit">编辑</button>
        <button class="btn-link is-quiet" type="button" data-action="delete">删除</button>
      </div></td>`;
    const cells = row.querySelectorAll('td');
    cells[0].textContent = numbers.get(product.id) || '';
    setClipText(cells[2], product.name);
    if (product.product_type && product.product_type !== 'normal') {
      const type = document.createElement('small');
      type.className = 'product-type-label';
      type.textContent = product.product_type === 'checkin_boost' ? '签到增益卡' : '余额返利卡';
      cells[2].appendChild(type);
    }
    cells[3].textContent = `${product.cost} 积分`;
    cells[4].textContent = String(stock);
    cells[5].textContent = String(limit);
    if (product.per_user_daily_limit !== null && product.per_user_daily_limit !== undefined) {
      const daily = document.createElement('small');
      daily.className = 'product-type-label';
      daily.textContent = `每日 ${product.per_user_daily_limit}`;
      cells[5].appendChild(daily);
    }
    cells[6].textContent = product.category || '默认';
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = `tag tag-button ${product.enabled ? 'ok' : 'warn'}`;
    toggle.dataset.action = 'toggle';
    toggle.title = product.enabled ? '点一下下架' : '点一上架';
    toggle.textContent = product.enabled ? '上架' : '下架';
    cells[7].appendChild(toggle);
    const thumbCell = cells[1];
    if (product.image_path) {
      const img = document.createElement('img');
      img.className = 'thumb';
      img.alt = product.name;
      thumbCell.appendChild(img);
      loadImage(product.image_path).then((url) => {
        if (url) img.src = url;
      });
    } else {
      const span = document.createElement('span');
      span.className = 'thumb-fallback';
      span.textContent = (product.name || '?').slice(0, 1);
      thumbCell.appendChild(span);
    }
    row.querySelector('[data-action="edit"]').addEventListener('click', () => {
      openProduct(product);
    });
    row.querySelector('[data-action="delete"]').addEventListener('click', (event) => {
      // 注意：确认框等待期间不要显示 spinner，只有真正发请求时才算"进行中"
      deleteProduct(product, event.currentTarget).catch((error) => say(errorText(error), 'error'));
    });
    toggle.addEventListener('click', (event) => {
      toggleProduct(product, event.currentTarget).catch((error) => say(errorText(error), 'error'));
    });
    row.querySelector('[data-action="up"]').addEventListener('click', (event) => {
      moveProduct(product, -1, event.currentTarget).catch((error) =>
        say(errorText(error), 'error'),
      );
    });
    row.querySelector('[data-action="down"]').addEventListener('click', (event) => {
      moveProduct(product, 1, event.currentTarget).catch((error) =>
        say(errorText(error), 'error'),
      );
    });
    tbody.appendChild(row);
  });
}

// 商品表当前该显示哪些行：先筛选，再按表头排序（默认保持展示顺序 = 序号顺序）
function productView() {
  const keyword = state.productKeyword.trim().toLowerCase();
  const compact = keyword.replace(/\s+/g, '');
  const rows = state.productRows.filter((product) => {
    if (state.productCategory && (product.category || '默认') !== state.productCategory) {
      return false;
    }
    if (!matchProductStatus(product, state.productStatus)) return false;
    if (!compact) return true;
    const haystack = [product.name, product.category, product.description, product.number, `${product.cost}`]
      .map((value) => String(value === null || value === undefined ? '' : value).toLowerCase())
      .join(' ');
    return haystack.replace(/\s+/g, '').includes(compact);
  });
  if (!state.productSort) return rows;
  const field = state.productSort;
  const factor = state.productSortAsc ? 1 : -1;
  const value = (product) => {
    if (field === 'cost') return Number(product.cost) || 0;
    if (field === 'stock') return product.stock === null || product.stock === undefined ? -1 : Number(product.stock);
    if (field === 'limit') {
      return product.per_user_limit === null || product.per_user_limit === undefined
        ? -1
        : Number(product.per_user_limit);
    }
    if (field === 'status') return product.enabled ? 1 : 0;
    return String(product[field] || '');
  };
  return rows
    .map((product, index) => ({ product, index }))
    .sort((left, right) => {
      const a = value(left.product);
      const b = value(right.product);
      if (a === b) return left.index - right.index; // 同值时保持展示顺序，序号列才稳定
      if (typeof a === 'string' || typeof b === 'string') {
        return String(a).localeCompare(String(b), 'zh-Hans-CN') * factor;
      }
      return (a < b ? -1 : 1) * factor;
    })
    .map((item) => item.product);
}

// 状态筛选：上架/下架是商品自身状态，其余三种按"对某个用户能不能换"的规则判断，
// 后台没有具体用户，所以只按规则本身（库存、限购、价格）判断。
function matchProductStatus(product, status) {
  if (!status) return true;
  const stock = product.stock === null || product.stock === undefined ? null : Number(product.stock);
  if (status === 'enabled') return product.enabled !== false;
  if (status === 'disabled') return product.enabled === false;
  if (status === 'soldout') return stock !== null && stock <= 0;
  if (status === 'points') return Number(product.cost) > 0;
  if (status === 'redeemable') {
    return product.enabled !== false && (stock === null || stock > 0);
  }
  return true;
}

// 分类下拉的选项：从现有商品里自动收集（按展示顺序首次出现的顺序），不新增数据字段
function fillCategoryOptions() {
  const select = $('product-category');
  const list = $('category-options');
  if (!select) return;
  const seen = [];
  state.productRows.forEach((product) => {
    const value = product.category || '默认';
    if (!seen.includes(value)) seen.push(value);
  });
  const current = state.productCategory;
  select.innerHTML = '';
  const all = document.createElement('option');
  all.value = '';
  all.textContent = '全部分类';
  select.appendChild(all);
  seen.forEach((value) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  });
  select.value = seen.includes(current) ? current : '';
  state.productCategory = select.value;
  if (list) {
    list.innerHTML = '';
    seen.forEach((value) => {
      const option = document.createElement('option');
      option.value = value;
      list.appendChild(option);
    });
  }
}

function resetProductFilters() {
  state.productKeyword = '';
  state.productCategory = '';
  state.productStatus = '';
  state.productSort = '';
  state.productSortAsc = false;
  if ($('product-search')) $('product-search').value = '';
  if ($('product-category')) $('product-category').value = '';
  if ($('product-status')) $('product-status').value = '';
  syncProductSortHeader();
  renderProducts();
}

function syncProductSortHeader() {
  document.querySelectorAll('#product-table th.sortable').forEach((th) => {
    const active = th.dataset.sort === state.productSort;
    th.setAttribute('aria-sort', active ? (state.productSortAsc ? 'ascending' : 'descending') : 'none');
    th.classList.toggle('is-sorted', active);
  });
}

// 行内快速上下架：复用保存接口，提交完整商品对象（后端只认白名单字段）
async function toggleProduct(product, trigger) {
  const next = !(product.enabled !== false);
  try {
    const result = await withBusy(
      trigger,
      () =>
        post('shop/products/save', {
          id: product.id,
          name: product.name,
          cost: product.cost,
          stock: product.stock,
          per_user_limit: product.per_user_limit,
          per_user_daily_limit: product.per_user_daily_limit ?? null,
          product_type: product.product_type || 'normal',
          effect_config: product.effect_config || {},
          category: product.category || '默认',
          description: product.description || '',
          enabled: next,
          sort_order: product.sort_order,
        }),
      '切换中',
    );
    await loadSummary();
    say(`「${product.name}」已${next ? '上架' : '下架'}`, 'ok');
    return result;
  } catch (error) {
    say(`切换失败：${errorText(error)}`, 'error');
    throw error;
  }
}

// 上移/下移：序号就是展示顺序，所以这里改的是"谁能被编号选中"
async function moveProduct(product, offset, trigger) {
  const order = state.productRows.map((item) => item.id);
  const index = order.indexOf(product.id);
  const target = index + offset;
  if (index < 0 || target < 0 || target >= order.length) return;
  order.splice(target, 0, order.splice(index, 1)[0]);
  try {
    await withBusy(
      trigger,
      () => post('shop/products/reorder', { order }),
      '调整中',
    );
    await loadSummary();
    say(`「${product.name}」序号已更新`, 'ok');
  } catch (error) {
    say(`调整失败：${errorText(error)}`, 'error');
    throw error;
  }
}

async function loadImage(relativePath) {
  if (!relativePath) return '';
  if (state.assetUrls[relativePath]) return state.assetUrls[relativePath];
  try {
    const result = await bridge.apiGet('shop/products/image', { path: relativePath });
    const url = (result && result.data_url) || '';
    if (url) state.assetUrls[relativePath] = url;
    return url;
  } catch (error) {
    console.error('加载图片失败', error);
    return '';
  }
}

function openProduct(product) {
  const editing = Boolean(product && product.id);
  $('product-modal-title').textContent = editing ? `编辑商品：${product.name}` : '新增商品';
  $('p-name').value = product ? product.name || '' : '';
  $('p-cost').value = product ? product.cost ?? 0 : 0;
  $('p-stock').value =
    product && product.stock !== null && product.stock !== undefined ? product.stock : '';
  $('p-limit').value =
    product && product.per_user_limit !== null && product.per_user_limit !== undefined
      ? product.per_user_limit
      : '';
  $('p-category').value = product ? product.category || '' : '';
  $('p-type').value = product?.product_type || 'normal';
  $('p-daily-limit').value = product?.per_user_daily_limit ?? '';
  const effect = product?.effect_config || {};
  $('p-boost-multiplier').value = (effect.multiplier_bps ?? 20000) / 100;
  $('p-boost-days').value = effect.days ?? 7;
  $('p-boost-cap').value = effect.daily_bonus_cap ?? 20;
  $('p-rebate-rate').value = (effect.rebate_bps ?? 1500) / 100;
  $('p-rebate-cap').value = effect.rebate_cap ?? 350;
  $('p-rebate-cooldown').value = effect.cooldown_hours ?? 168;
  $('p-desc').value = product ? product.description || '' : '';
  $('p-enabled').checked = product ? product.enabled !== false : true;
  $('p-image').value = '';
  clearErrors(PRODUCT_FIELDS);
  state.editingImagePath = product ? product.image_path || '' : '';
  state.previewProductId = editing ? product.id : '';
  // 编辑时保留原有 sort_order，否则"保存一下就跳到列表最后"（序号会跟着变）
  state.editingSortOrder = editing ? Number(product.sort_order) || 0 : null;
  state.pendingImage = null;
  syncProductType();
  renderProductPreview();
  showModal('product-modal', $('p-name'));
}

const PRODUCT_FIELDS = ['p-name', 'p-cost', 'p-stock', 'p-limit', 'p-daily-limit', 'p-type', 'p-boost-multiplier', 'p-boost-days', 'p-boost-cap', 'p-rebate-rate', 'p-rebate-cap', 'p-rebate-cooldown'];

// 百分比最多两位小数，先按字符串拆分，避免浮点乘法改变基点值。
function readPercent(inputId, minBps = 0, maxBps = 10000) {
  const raw = String($(inputId).value).trim();
  if (!/^\d+(?:\.\d{1,2})?$/.test(raw)) return { value: null, error: '请填写非负百分比，最多两位小数。' };
  const [whole, fraction = ''] = raw.split('.');
  const value = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
  if (!Number.isSafeInteger(value) || value < minBps || value > maxBps) return { value: null, error: `请填写 ${minBps / 100}% 至 ${maxBps / 100}% 之间的数值。` };
  return { value, error: '' };
}

function syncProductType(applyDefaults = false) {
  const type = $('p-type').value;
  const boost = type === 'checkin_boost';
  const rebate = type === 'balance_rebate';
  $('p-effect-fields').hidden = !boost && !rebate;
  $('p-boost-fields').hidden = !boost;
  $('p-rebate-fields').hidden = !rebate;
  if (applyDefaults && !state.previewProductId && (boost || rebate)) $('p-cost').value = boost ? 50 : 300;
  updateProductEffectSummary();
}

function updateProductEffectSummary() {
  if ($('p-type').value === 'checkin_boost') {
    $('p-effect-summary').textContent = `兑换后次日生效，持续 ${$('p-boost-days').value || '—'} 个签到日；基础签到积分按 ${$('p-boost-multiplier').value || '—'}% 计算，额外积分每日最多 ${$('p-boost-cap').value || '0'} 分，不含连签奖励。已有待生效或生效中的卡时不能重复购买。`;
  } else {
    $('p-effect-summary').textContent = `兑换时按扣购卡价前的可用钱包余额返还 ${$('p-rebate-rate').value || '0'}%，最高 ${$('p-rebate-cap').value || '0'} 积分；存款、持仓与竞猜资金不计入。返还少于卡价时不可兑换，同类卡冷却 ${$('p-rebate-cooldown').value || '0'} 小时。`;
  }
}

function closeProduct() {
  const modal = $('product-modal');
  if (modal.querySelector('.is-busy')) return;
  hideModal('product-modal');
}

function renderProductPreview() {
  const img = $('p-preview');
  const path = state.editingImagePath;
  if (!path) {
    img.hidden = true;
    img.removeAttribute('src');
    return;
  }
  loadImage(path).then((url) => {
    if (url) {
      img.src = url;
      img.hidden = false;
    } else {
      img.hidden = true;
    }
  });
}

async function saveProduct() {
  $('product-modal-status').hidden = true;
  clearErrors(PRODUCT_FIELDS);
  const name = $('p-name').value.trim();
  const cost = readInteger('p-cost', { allowEmpty: false, min: 0 });
  const stock = readInteger('p-stock', { allowEmpty: true, min: 0 });
  const limit = readInteger('p-limit', { allowEmpty: true, min: 0 });
  const dailyLimit = readInteger('p-daily-limit', { allowEmpty: true, min: 0 });
  const type = $('p-type').value;
  const effect = {};
  if (!name) setFieldError('p-name', '商品名称不能为空，用户端会用它显示商品。');
  if (cost.error) setFieldError('p-cost', cost.error);
  if (stock.error) setFieldError('p-stock', stock.error);
  if (limit.error) setFieldError('p-limit', limit.error);
  if (dailyLimit.error) setFieldError('p-daily-limit', dailyLimit.error);
  if (!['normal', 'checkin_boost', 'balance_rebate'].includes(type)) setFieldError('p-type', '请选择有效的商品类型。');
  const effectFields = type === 'checkin_boost'
    ? [['p-boost-multiplier', 'multiplier_bps', 10000, true], ['p-boost-days', 'days', 1], ['p-boost-cap', 'daily_bonus_cap', 0]]
    : type === 'balance_rebate' ? [['p-rebate-rate', 'rebate_bps', 1, true], ['p-rebate-cap', 'rebate_cap', 0], ['p-rebate-cooldown', 'cooldown_hours', 0]] : [];
  effectFields.forEach(([id, key, min, percent]) => {
    const result = percent ? readPercent(id, min, key === 'multiplier_bps' ? 1000000 : 10000) : readInteger(id, { allowEmpty: false, min });
    const max = key === 'days' ? 365 : key === 'cooldown_hours' ? 87600 : Number.MAX_SAFE_INTEGER;
    if (!result.error && result.value > max) result.error = `请填写不大于 ${max} 的整数。`;
    if (result.error) setFieldError(id, result.error);
    else effect[key] = result.value;
  });
  if (focusFirstInvalid(PRODUCT_FIELDS)) return;

  const payload = {
    id: state.previewProductId || '',
    name,
    cost: cost.value || 0,
    stock: stock.value,
    per_user_limit: limit.value,
    per_user_daily_limit: dailyLimit.value,
    product_type: type,
    effect_config: effect,
    category: $('p-category').value.trim() || '默认',
    description: $('p-desc').value.trim(),
    enabled: $('p-enabled').checked,
    // 编辑时保持原来的位置；新增的排到最后
    sort_order:
      state.editingSortOrder === null
        ? state.productRows.length * 10 + 10
        : state.editingSortOrder,
  };
  const saved = await post('shop/products/save', payload);
  const productId = (saved.product && saved.product.id) || state.previewProductId;
  // 新商品已经落盘；后续上传失败时也必须按这个 ID 更新，不能再次新增。
  if (productId) {
    state.previewProductId = productId;
    state.editingSortOrder = Number(saved.product?.sort_order ?? payload.sort_order);
    $('product-modal-title').textContent = `编辑商品：${name}`;
  }
  const file = $('p-image').files && $('p-image').files[0];
  if (file && productId) {
    try {
      await bridge.upload(`shop/products/image/upload?product_id=${encodeURIComponent(productId)}`, file);
    } catch (error) {
      say(`商品已保存，图片上传失败，可重试：${errorText(error)}`, 'error');
      return;
    }
  } else if (file && !productId) {
    say('商品已保存，但图片未上传：接口没有返回商品 ID，请重试', 'error');
    await loadSummary();
    return;
  }
  state.assetUrls = {};
  await loadSummary();
  hideModal('product-modal');
  say('商品已保存', 'ok');
}

async function deleteProduct(product, trigger) {
  const confirmed = await askConfirm({
    title: '删除商品',
    message: `确定删除「${product.name}」吗？如果它还有待发放的兑换记录，会改为下架而不是删除，历史账目不会丢。`,
    confirmText: '删除',
  });
  if (!confirmed) return;
  try {
    const result = await withBusy(
      trigger,
      () => post('shop/products/delete', { id: product.id }),
      '删除中',
    );
    state.assetUrls = {};
    await loadSummary();
    say(result.message || '已删除', 'ok');
  } catch (error) {
    say(`删除失败：${errorText(error)}`, 'error');
  }
}

async function previewShop() {
  const hint = $('preview-hint');
  try {
    await bridge.download('shop/preview', { user_key: state.previewUserKey }, 'shop-preview.png');
    const selection = $('preview-user').selectedOptions[0]?.textContent || '模拟用户，0 积分';
    hint.textContent = `预览图已开始下载（浏览器下载栏可见），当前视角：${selection}。下架商品不出图，图片与可兑换状态使用用户端同一规则。`;
  } catch (error) {
    hint.textContent = `预览失败：${errorText(error)}`;
    say(`预览失败：${errorText(error)}`, 'error');
  }
}

async function searchPreviewUsers() {
  const result = await post('shop/users', {
    keyword: $('preview-user-keyword').value.trim(),
    page: 1,
    page_size: PAGE_SIZE,
  });
  const select = $('preview-user');
  select.replaceChildren(new Option('模拟用户，0 积分', 'preview:0'));
  for (const user of result.items || []) {
    const key = String(user.user_key || '');
    if (!key || key === 'preview:0') continue;
    const identity = `${user.platform || key.split(':')[0]}:${user.user_id || key.split(':').slice(1).join(':')}`;
    select.add(new Option(`${user.nickname || user.user_id || key} · ${identity} · ${user.points ?? 0} 积分`, key));
  }
  if (Array.from(select.options).some((option) => option.value === state.previewUserKey)) {
    select.value = state.previewUserKey;
  } else {
    state.previewUserKey = 'preview:0';
  }
  const count = (result.items || []).length;
  $('preview-user-result').textContent = Number(result.total || 0) > count
    ? `显示前 ${count} 位，请输入更具体的昵称或 ID 缩小范围。`
    : count ? `找到 ${count} 位用户，可选择其余额与限购视角。` : '没有匹配用户，可继续使用模拟视角。';
}

// ---------------------------------------------------------------- 用户积分

function syncUserPager() {
  $('user-prev').disabled = state.userPage <= 1;
  $('user-next').disabled = state.userPage * PAGE_SIZE >= state.userTotal;
  $('user-page').textContent = `第 ${state.userPage} 页 · 共 ${state.userTotal} 人`;
}

async function loadUsers(page) {
  if (page) state.userPage = page;
  const result = await post('shop/users', {
    keyword: $('user-keyword').value.trim(),
    platform: $('user-platform').value.trim(),
    page: state.userPage,
    page_size: PAGE_SIZE,
  });
  state.userRows = result.items || [];
  state.userTotal = result.total || 0;
  const tbody = $('user-table').querySelector('tbody');
  tbody.innerHTML = '';
  state.userRows.forEach((user) => {
    const row = document.createElement('tr');
    row.innerHTML =
      '<td class="cell-name"></td><td></td><td class="num"></td><td class="num"></td>' +
      '<td class="num"></td><td></td><td class="cell-actions"></td>';
    const cells = row.querySelectorAll('td');
    setClipText(cells[0], user.nickname || user.user_id);
    cells[1].textContent = user.platform;
    cells[2].textContent = String(user.points);
    cells[3].textContent = String(user.streak);
    cells[4].textContent = String(user.checkin_count);
    cells[5].textContent = user.last_checkin_date || '-';
    const button = document.createElement('button');
    button.className = 'btn btn-secondary';
    button.type = 'button';
    button.textContent = '调整积分';
    button.addEventListener('click', () => openAdjust(user));
    cells[6].appendChild(button);
    const assets = document.createElement('button');
    assets.className = 'btn btn-secondary';
    assets.type = 'button';
    assets.textContent = '资产详情';
    assets.dataset.action = 'assets';
    assets.addEventListener('click', () => openAssets(user));
    cells[6].classList.add('user-actions');
    cells[6].appendChild(assets);
    tbody.appendChild(row);
  });
  const empty = state.userRows.length === 0;
  const wrap = $('user-table').closest('.table-wrap');
  if (wrap) {
    wrap.hidden = empty;
    wrap.scrollTop = 0;
    wrap.scrollLeft = 0;
  }
  $('user-empty').hidden = !empty;
  syncUserPager();
}

function openAdjust(user) {
  state.adjustUserKey = user.user_key;
  $('adjust-user').textContent = `${user.nickname || user.user_id}（${user.user_key}）当前 ${user.points} 积分`;
  $('a-delta').value = 0;
  $('a-absolute').value = '';
  $('a-note').value = '';
  clearErrors(['a-delta', 'a-absolute']);
  showModal('adjust-modal', $('a-delta'));
}

function closeAdjust() {
  if ($('adjust-modal').querySelector('.is-busy')) return;
  hideModal('adjust-modal');
}

async function saveAdjust() {
  $('adjust-modal-status').hidden = true;
  clearErrors(['a-delta', 'a-absolute']);
  const absolute = readInteger('a-absolute', { allowEmpty: true, min: 0 });
  const delta = readInteger('a-delta', { allowEmpty: true, min: -1000000 });
  if (absolute.error) setFieldError('a-absolute', absolute.error);
  if (delta.error) setFieldError('a-delta', delta.error);
  const deltaValue = delta.value || 0;
  if (!delta.error && !absolute.error && absolute.value === null && deltaValue === 0) {
    setFieldError('a-delta', '请填写增量，或在右侧直接设置目标积分。');
  }
  if (focusFirstInvalid(['a-delta', 'a-absolute'])) return;

  const payload = {
    user_key: state.adjustUserKey,
    delta: deltaValue,
    note: $('a-note').value.trim(),
  };
  if (absolute.value !== null) payload.absolute = absolute.value;
  const result = await post('shop/users/adjust', payload);
  hideModal('adjust-modal');
  await loadUsers();
  await loadSummary();
  say(result.message || '积分已调整', 'ok');
}

// ---------------------------------------------------------------- 兑换记录

const REDEEM_TAG = { 已发放: 'ok', 待发放: 'warn', 已取消: '' };

function syncRedeemPager() {
  $('redeem-prev').disabled = state.redeemPage <= 1;
  $('redeem-next').disabled = state.redeemPage * PAGE_SIZE >= state.redeemTotal;
  $('redeem-page').textContent = `第 ${state.redeemPage} 页 · 共 ${state.redeemTotal} 条`;
}

async function loadRedemptions(page) {
  if (page) state.redeemPage = page;
  const result = await post('shop/redemptions', {
    status: $('redeem-status').value,
    page: state.redeemPage,
    page_size: PAGE_SIZE,
  });
  state.redeemRows = result.items || [];
  state.redeemTotal = result.total || 0;
  const tbody = $('redeem-table').querySelector('tbody');
  tbody.innerHTML = '';
  state.redeemRows.forEach((record) => {
    const row = document.createElement('tr');
    row.innerHTML =
      '<td></td><td class="cell-name"></td><td class="cell-name"></td><td class="num"></td>' +
      '<td></td><td class="cell-actions"></td>';
    const cells = row.querySelectorAll('td');
    const stamp = new Date(Number(record.created_at) * 1000);
    cells[0].textContent = Number.isNaN(stamp.getTime())
      ? '-'
      : stamp.toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
    setClipText(cells[1], record.nickname || record.user_id || record.user_key);
    setClipText(cells[2], record.product_name);
    cells[3].textContent = `-${record.cost}`;
    const status = document.createElement('span');
    const tone = REDEEM_TAG[record.status] || '';
    status.className = `tag${tone ? ` ${tone}` : ''}`;
    status.textContent = record.status;
    cells[4].appendChild(status);

    const actions = document.createElement('div');
    actions.className = 'row-actions';
    if (record.status === '待发放') {
      const done = document.createElement('button');
      done.className = 'btn-link';
      done.type = 'button';
      done.textContent = '标记发放';
      done.addEventListener('click', (event) => {
        run(event.currentTarget, () => updateRedemption(record, '已发放', false), '处理中');
      });
      actions.appendChild(done);
    }
    if (record.status !== '已取消') {
      const cancel = document.createElement('button');
      cancel.className = 'btn-link is-quiet';
      cancel.type = 'button';
      cancel.textContent = '取消并返还';
      cancel.addEventListener('click', (event) => {
        run(event.currentTarget, () => updateRedemption(record, '已取消', true), '处理中');
      });
      actions.appendChild(cancel);
    }
    const remove = document.createElement('button');
    remove.className = 'btn-link is-quiet';
    remove.type = 'button';
    remove.textContent = '删除';
    remove.addEventListener('click', (event) => {
      deleteRedemption(record, event.currentTarget).catch((error) =>
        say(errorText(error), 'error'),
      );
    });
    actions.appendChild(remove);
    cells[5].appendChild(actions);
    tbody.appendChild(row);
  });
  const empty = state.redeemRows.length === 0;
  const wrap = $('redeem-table').closest('.table-wrap');
  if (wrap) {
    wrap.hidden = empty;
    wrap.scrollTop = 0;
    wrap.scrollLeft = 0;
  }
  $('redeem-empty').hidden = !empty;
  syncRedeemPager();
}

async function updateRedemption(record, status, restore) {
  try {
    const result = await post('shop/redemptions/status', {
      id: record.id,
      status,
      restore,
      note: '',
    });
    await loadRedemptions();
    await loadSummary();
    const notice = result.notice ? `，${result.notice}` : '';
    const email = result.email ? `，${result.email}` : '';
    // 邮件失败时后端返回的是"邮件发送失败：…"，不能塞进绿色成功提示里当好消息
    const emailFailed = Boolean(result.email) && /失败|错误|不完整|没有可用/.test(result.email);
    say(
      `${result.message || '已更新'}${notice}${email}`,
      emailFailed ? 'error' : 'ok',
    );
  } catch (error) {
    say(`操作失败：${errorText(error)}`, 'error');
  }
}

async function sendTestEmail() {
  const hint = '测试邮件会发给插件配置页里的「兜底收件人」（没填就用 SMTP 账号）。';
  try {
    const result = await post('shop/email/test', {});
    say(`${result.message || '测试邮件已发送'}（${hint}）`, 'ok');
  } catch (error) {
    say(`测试邮件失败：${errorText(error)}`, 'error');
  }
}

async function deleteRedemption(record, trigger) {
  const pending = record.status === '待发放';
  const message = pending
    ? `「${record.product_name}」还是待发放状态。删除只清理这条记录，不会退还 ${record.cost} 积分、也不会回补库存；想要退积分请用「取消并返还」。`
    : `确定删除「${record.product_name}」这条兑换记录吗？删除只清理历史，不影响积分与库存。`;
  const confirmed = await askConfirm({
    title: pending ? '强制删除待发放记录' : '删除兑换记录',
    message,
    confirmText: '删除',
  });
  if (!confirmed) return;
  try {
    const result = await withBusy(
      trigger,
      () =>
        post('shop/redemptions/delete', {
          id: record.id,
          force: pending,
        }),
      '删除中',
    );
    await loadRedemptions();
    await loadSummary();
    say(result.message || '已删除', 'ok');
  } catch (error) {
    say(`删除失败：${errorText(error)}`, 'error');
  }
}

// ---------------------------------------------------------------- 账单流水

const LEDGER_TAG = { 打卡: 'ok', 兑换: 'warn', 返还: 'danger', 调整: '' };

function ledgerFilters() {
  return {
    keyword: $('ledger-keyword').value.trim(),
    type: $('ledger-type').value,
    start: $('ledger-start').value,
    end: $('ledger-end').value,
  };
}

function syncLedgerPager() {
  $('ledger-prev').disabled = state.ledgerPage <= 1;
  $('ledger-next').disabled = state.ledgerPage * PAGE_SIZE >= state.ledgerTotal;
  $('ledger-page').textContent = `第 ${state.ledgerPage} 页 · 共 ${state.ledgerTotal} 条`;
}

function renderLedgerSummary(result) {
  const income = Number(result.income) || 0;
  const expense = Number(result.expense) || 0;
  const net = Number(result.net) || 0;
  const netText = net >= 0 ? `+${net}` : String(net);
  $('ledger-summary').textContent =
    `筛选结果：收入 ${income} · 支出 ${expense} · 净变动 ${netText}`;
}

async function loadLedger(page) {
  if (page) state.ledgerPage = page;
  const result = await post('shop/ledger', {
    ...ledgerFilters(),
    page: state.ledgerPage,
    page_size: PAGE_SIZE,
  });
  state.ledgerRows = result.items || [];
  state.ledgerTotal = result.total || 0;
  if (Array.isArray(result.types)) {
    const selected = $('ledger-type').value;
    const types = [...new Set(['打卡', '兑换', '返还', '调整', ...result.types, ...(selected ? [selected] : [])])];
    $('ledger-type').replaceChildren();
    const all = document.createElement('option'); all.value = ''; all.textContent = '全部类型'; $('ledger-type').appendChild(all);
    types.forEach((value) => { const option = document.createElement('option'); option.value = String(value); option.textContent = String(value); $('ledger-type').appendChild(option); });
    $('ledger-type').value = selected;
  }
  renderLedgerSummary(result);
  const tbody = $('ledger-table').querySelector('tbody');
  tbody.innerHTML = '';
  state.ledgerRows.forEach((entry) => {
    const row = document.createElement('tr');
    row.innerHTML =
      '<td></td><td class="cell-name"></td><td></td><td class="num"></td>' +
      '<td class="num"></td><td class="cell-name"></td>';
    const cells = row.querySelectorAll('td');
    const stamp = new Date(Number(entry.created_at) * 1000);
    cells[0].textContent = Number.isNaN(stamp.getTime())
      ? '-'
      : stamp.toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
    setClipText(cells[1], entry.nickname || entry.user_id || entry.user_key);
    const tag = document.createElement('span');
    const tone = LEDGER_TAG[entry.type] || '';
    tag.className = `tag${tone ? ` ${tone}` : ''}`;
    tag.textContent = entry.type || '-';
    cells[2].appendChild(tag);
    const points = Number(entry.points) || 0;
    cells[3].textContent = points > 0 ? `+${points}` : String(points);
    cells[3].className = `num ${points >= 0 ? 'delta-in' : 'delta-out'}`;
    cells[4].textContent = String(entry.balance ?? '-');
    setClipText(cells[5], entry.note || '-');
    const details = [];
    if (entry.source !== null && entry.source !== undefined && String(entry.source).trim()) details.push(String(entry.source));
    if (typeof entry.fee === 'number' && Number.isFinite(entry.fee)) details.push(`费用 ${entry.fee}`);
    if (typeof entry.profit === 'number' && Number.isFinite(entry.profit)) details.push(`收益 ${entry.profit > 0 ? '+' : ''}${entry.profit}`);
    if (details.length) {
      const meta = textNode('span', details.join(' / '), 'ledger-meta');
      meta.title = meta.textContent;
      cells[5].appendChild(meta);
    }
    tbody.appendChild(row);
  });
  const empty = state.ledgerRows.length === 0;
  const wrap = $('ledger-table').closest('.table-wrap');
  if (wrap) {
    wrap.hidden = empty;
    wrap.scrollTop = 0;
    wrap.scrollLeft = 0;
  }
  $('ledger-empty').hidden = !empty;
  const filtered = Boolean(
    ($('ledger-keyword').value || '').trim() ||
      $('ledger-type').value ||
      $('ledger-start').value ||
      $('ledger-end').value,
  );
  $('ledger-empty-title').textContent = filtered ? '没有匹配的流水' : '还没有流水';
  $('ledger-empty-hint').textContent = filtered
    ? '换个关键词/类型/时间区间，或点「筛选」前先清空条件。'
    : '有人打卡或用积分兑换之后，这里就有记录了；只有从本版本起发生的变动才会被记录，历史兑换请看「兑换记录」页。';
  syncLedgerPager();
}

async function exportLedger() {
  const filters = ledgerFilters();
  try {
    // busy 态由外面的 run() 负责，这里不要再套一层 withBusy
    await bridge.download('shop/ledger/export', filters, 'checkin-ledger.csv');
    say('导出已开始：CSV 按当前筛选条件导出，浏览器下载栏可见。', 'ok');
  } catch (error) {
    say(`导出失败：${errorText(error)}`, 'error');
  }
}

// ---------------------------------------------------------------- 玩法配置（仅管理员配置与只读资产，不提供玩家交易入口）

const ECONOMY_SWITCHES = {
  stocks_enabled: 'e-stocks-enabled', savings_enabled: 'e-savings-enabled',
  predictions_enabled: 'e-predictions-enabled', auto_predictions_enabled: 'e-auto-predictions-enabled',
  lottery_enabled: 'e-lottery-enabled', checkin_cards_enabled: 'e-checkin-cards-enabled',
  rebate_cards_enabled: 'e-rebate-cards-enabled',
};

const ECONOMY_FIELDS = [
  ['stock_fee_bps', 'stock', '交易手续费（%）', 100, 0, 10000, true],
  ['stock_hourly_bps', 'stock', '每小时最大涨跌（%）', 200, 0, 10000, true],
  ['stock_daily_bps', 'stock', '每日最大涨跌（%）', 1000, 0, 10000, true],
  ['stock_daily_trades', 'stock', '每人每日交易次数', 20, 0, Number.MAX_SAFE_INTEGER],
  ['stock_daily_buy_limit', 'stock', '每人每日买入积分上限', 1000, 0, Number.MAX_SAFE_INTEGER],
  ['savings_rate_bps', 'savings', '每日收益率（%）', 5, 0, 10000, true],
  ['savings_principal_cap', 'savings', '每人本金上限（积分）', 5000, 0, Number.MAX_SAFE_INTEGER],
  ['prediction_min', 'prediction', '单次最低投入（积分）', 5, 1, Number.MAX_SAFE_INTEGER],
  ['prediction_max', 'prediction', '单次最高投入（积分）', 50, 1, Number.MAX_SAFE_INTEGER],
  ['prediction_daily_limit', 'prediction', '每人每日投入上限（积分）', 100, 0, Number.MAX_SAFE_INTEGER],
  ['prediction_payout_bps', 'prediction', '奖池返还比例（%）', 9500, 0, 10000, true],
  ['prediction_timeout_days', 'prediction', '未判定自动作废天数', 7, 1, Number.MAX_SAFE_INTEGER],
  ['auto_close_minutes', 'prediction', '下一整点前封盘（分钟）', 5, 1, 59],
  ['lottery_cost', 'lottery', '每张消耗（积分）', 10, 1, Number.MAX_SAFE_INTEGER],
  ['lottery_daily_limit', 'lottery', '每人每日购买次数', 3, 0, Number.MAX_SAFE_INTEGER],
  ['checkin_card_daily_limit', 'card', '签到增益卡同类每日限购', 1, 0, Number.MAX_SAFE_INTEGER],
  ['rebate_card_daily_limit', 'card', '余额返利卡同类每日限购', 1, 0, Number.MAX_SAFE_INTEGER],
];

const DEFAULT_LOTTERY_PRIZES = [{ points: 0, weight: 45 }, { points: 5, weight: 20 }, { points: 10, weight: 25 }, { points: 40, weight: 9 }, { points: 100, weight: 1 }];
const economyInputId = (key) => `e-${key.replaceAll('_', '-')}`;

function textNode(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value === null || value === undefined ? '—' : String(value);
  if (className) node.className = className;
  return node;
}

function beijingTime(value) {
  if (value === null || value === undefined || value === '') return '—';
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
}

function stockPrice(stock) {
  if (stock.price_text !== undefined) return String(stock.price_text);
  const cent = Number(stock.price_cent ?? stock.price);
  return Number.isFinite(cent) ? (cent / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—';
}

function setupEconomyFields() {
  ECONOMY_FIELDS.forEach(([key, group, title, fallback, min, max, percent]) => {
    const id = economyInputId(key);
    const label = document.createElement('label');
    label.appendChild(textNode('span', title, 'field-label'));
    const input = document.createElement('input');
    Object.assign(input, { id, type: 'number', min: String(percent ? min / 100 : min), max: String(percent ? max / 100 : max), step: percent ? '0.01' : '1' });
    input.setAttribute('aria-describedby', `${id}-error`);
    label.appendChild(input);
    const error = textNode('span', '', 'field-error');
    error.id = `${id}-error`;
    label.appendChild(error);
    $(`economy-${group}-fields`).appendChild(label);
  });
}

let prizeSequence = 0;
function addLotteryPrize(prize = { points: 0, weight: 1 }) {
  const list = $('lottery-prizes');
  if (list.children.length >= 30) { $('lottery-prizes-error').textContent = '最多添加 30 项奖项。'; return; }
  const row = document.createElement('div');
  row.className = 'prize-row';
  const sequence = prizeSequence++;
  [['points', '中奖积分'], ['weight', '权重']].forEach(([key, title]) => {
    const label = document.createElement('label');
    label.appendChild(textNode('span', title, 'field-label'));
    const input = document.createElement('input');
    Object.assign(input, { id: `lottery-${key}-${sequence}`, type: 'number', min: '0', step: '1', value: String(prize[key] ?? '') });
    input.dataset.prizeField = key;
    input.addEventListener('input', updateLotteryProbabilities);
    label.appendChild(input);
    const error = textNode('span', '', 'field-error');
    error.id = `${input.id}-error`;
    input.setAttribute('aria-describedby', error.id);
    label.appendChild(error);
    row.appendChild(label);
  });
  row.appendChild(textNode('span', '', 'prize-probability'));
  const remove = textNode('button', '移除', 'btn-link is-quiet');
  remove.type = 'button'; remove.setAttribute('aria-label', '移除此奖项');
  remove.addEventListener('click', () => {
    row.remove(); updateLotteryProbabilities();
    $('lottery-prize-add').focus();
  });
  row.appendChild(remove); list.appendChild(row);
  updateLotteryProbabilities();
}

function updateLotteryProbabilities() {
  const rows = Array.from($('lottery-prizes').children);
  const weights = rows.map((row) => Number(row.querySelector('[data-prize-field="weight"]').value));
  const valid = weights.every((value) => Number.isSafeInteger(value) && value >= 0);
  const total = weights.reduce((sum, value) => sum + value, 0);
  rows.forEach((row, index) => { row.querySelector('.prize-probability').textContent = valid && total > 0 ? `${(weights[index] / total * 100).toFixed(2)}%` : '概率 —'; });
  $('lottery-prize-add').disabled = rows.length >= 30;
}

function fillEconomySettings(settings) {
  Object.entries(ECONOMY_SWITCHES).forEach(([key, id]) => { $(id).checked = settings[key] === true; });
  $('prediction-new').disabled = settings.predictions_enabled !== true;
  $('prediction-new').title = settings.predictions_enabled === true ? '创建管理员事件竞猜' : '启用并保存竞猜设置后可创建';
  ECONOMY_FIELDS.forEach(([key, , , fallback, , , percent]) => {
    const value = settings[key] ?? fallback;
    $(economyInputId(key)).value = percent ? Number(value) / 100 : value;
    setFieldError(economyInputId(key), '');
  });
  const select = $('e-auto-stock-code');
  select.replaceChildren();
  const stocks = Array.isArray(state.economy?.stocks) ? state.economy.stocks : [];
  const chosen = String(settings.auto_stock_code || 'ST01');
  const codes = new Set();
  stocks.forEach((stock) => {
    if (!stock.code || codes.has(stock.code)) return;
    codes.add(stock.code);
    const option = textNode('option', `${stock.code} · ${stock.name || stock.code}`);
    option.value = stock.code; select.appendChild(option);
  });
  if (!codes.has(chosen)) { const option = textNode('option', `${chosen}（当前配置）`); option.value = chosen; select.appendChild(option); }
  select.value = chosen;
  $('lottery-prizes').replaceChildren();
  (Array.isArray(settings.lottery_prizes) ? settings.lottery_prizes : DEFAULT_LOTTERY_PRIZES).forEach(addLotteryPrize);
  $('lottery-prizes-error').textContent = '';
}

function renderEconomyOverview() {
  const data = state.economy || {};
  const statistics = data.statistics || {};
  const metrics = $('economy-statistics'); metrics.replaceChildren();
  [['积分通本金', statistics.savings_principal], ['股票持有人数', statistics.stock_holders], ['未结算竞猜', statistics.open_predictions], ['竞猜锁定积分', statistics.locked_prediction]].forEach(([title, value]) => {
    const card = document.createElement('div'); card.append(textNode('span', title), textNode('b', formatStat(value))); metrics.appendChild(card);
  });
  const stocks = Array.isArray(data.stocks) ? data.stocks : [];
  const body = $('economy-stock-table').querySelector('tbody'); body.replaceChildren();
  stocks.forEach((stock) => {
    const row = document.createElement('tr');
    row.append(textNode('td', `${stock.code || '—'} · ${stock.name || '—'}`), textNode('td', `${stockPrice(stock)} 积分`, 'num'), textNode('td', Number.isFinite(Number(stock.change_bps)) ? `${Number(stock.change_bps) > 0 ? '+' : ''}${(Number(stock.change_bps) / 100).toFixed(2)}%` : '—', 'num'), textNode('td', beijingTime(stock.last_updated)));
    body.appendChild(row);
  });
  $('economy-stock-table').closest('.table-wrap').hidden = !stocks.length;
  $('economy-stocks-empty').hidden = Boolean(stocks.length);
  renderPredictions(Array.isArray(data.predictions) ? data.predictions : []);
}

async function loadEconomy() {
  $('economy-load-status').textContent = '正在加载玩法配置…';
  try {
    const result = await bridge.apiGet('shop/economy/overview');
    if (!result?.settings || typeof result.settings !== 'object' || Array.isArray(result.settings)) throw new Error('接口未返回有效玩法配置');
    state.economy = result; state.economyLoaded = true;
    fillEconomySettings(result.settings); renderEconomyOverview();
    $('economy-config').hidden = false; $('save-economy').disabled = false;
    $('economy-load-status').textContent = '配置已加载。保存设置后生效；行情与资产由机器人自动维护。';
    return true;
  } catch (error) {
    $('economy-load-status').textContent = `玩法配置加载失败：${errorText(error)}。请点击「刷新玩法」重试。`;
    $('save-economy').disabled = !state.economyLoaded;
    return false;
  }
}

async function saveEconomy() {
  if (!state.economyLoaded) { say('请先成功加载玩法配置。', 'error'); return; }
  const payload = {};
  const ids = [];
  Object.entries(ECONOMY_SWITCHES).forEach(([key, id]) => { payload[key] = $(id).checked; });
  ECONOMY_FIELDS.forEach(([key, , , , min, max, percent]) => {
    const id = economyInputId(key); ids.push(id); setFieldError(id, '');
    const result = percent ? readPercent(id, min, max) : readInteger(id, { allowEmpty: false, min });
    if (!result.error && result.value > max) result.error = `请填写不大于 ${max} 的整数。`;
    if (result.error) setFieldError(id, result.error);
    else payload[key] = result.value;
  });
  if (payload.prediction_min > payload.prediction_max) setFieldError(economyInputId('prediction_max'), '最高投入不能小于最低投入。');
  payload.auto_stock_code = $('e-auto-stock-code').value;
  ids.push('e-auto-stock-code'); setFieldError('e-auto-stock-code', /^ST0[1-4]$/.test(payload.auto_stock_code) ? '' : '请选择 ST01 至 ST04 中的一只股票。');
  const prizes = []; let prizeError = '';
  Array.from($('lottery-prizes').children).forEach((row, index) => {
    const prize = {};
    row.querySelectorAll('[data-prize-field]').forEach((input) => {
      ids.push(input.id); setFieldError(input.id, '');
      const raw = input.value.trim();
      const result = /^\d+$/.test(raw) && Number.isSafeInteger(Number(raw)) ? Number(raw) : null;
      if (result === null) { setFieldError(input.id, `第 ${index + 1} 项必须是非负整数。`); prizeError ||= '请修正奖项中的非法积分或权重。'; }
      else prize[input.dataset.prizeField] = result;
    });
    prizes.push(prize);
  });
  if (!prizes.length || prizes.length > 30) prizeError = '奖表需包含 1 至 30 项。';
  else if (!prizeError && !prizes.some((item) => item.weight > 0)) prizeError = '至少一项奖项权重必须大于 0。';
  $('lottery-prizes-error').textContent = prizeError;
  const invalid = ids.find((id) => $(id)?.getAttribute('aria-invalid') === 'true');
  if (invalid || prizeError) {
    const input = invalid ? $(invalid) : $('lottery-prizes-error');
    const details = input.closest('details'); if (details) details.open = true;
    input.scrollIntoView({ block: 'center' }); if (invalid) input.focus();
    say('玩法设置未保存，请修正标记的输入项。', 'error'); return;
  }
  payload.lottery_prizes = prizes;
  const result = await post('shop/economy/settings', payload);
  if (result.settings) { state.economy.settings = result.settings; fillEconomySettings(result.settings); }
  say(result.message || '玩法设置已保存', 'ok');
}

function renderPredictions(predictions) {
  const list = $('economy-prediction-list'); list.replaceChildren();
  $('economy-predictions-empty').hidden = Boolean(predictions.length);
  predictions.forEach((prediction) => {
    const card = document.createElement('article'); card.className = 'prediction-item';
    const title = textNode('h4', prediction.title || '未命名竞猜');
    const closeAt = Number(prediction.close_at ?? prediction.closes_at);
    const status = prediction.status || 'open';
    const terminal = ['resolved', 'settled', 'cancelled', 'canceled', 'void', '已结算', '已作废'].includes(status);
    const closed = status === 'closed' || (Number.isFinite(closeAt) && closeAt <= Date.now() / 1000);
    const auto = prediction.source === 'auto';
    const statusTitle = terminal ? (status === 'void' ? '无胜方，已返还' : ['cancelled', 'canceled', '已作废'].includes(status) ? '已作废' : '已判定') : closed ? '已封盘' : '进行中';
    const heading = document.createElement('div'); heading.className = 'section-heading'; heading.append(title, textNode('span', `${auto ? '自动' : '管理员'} · ${statusTitle}`, 'tag')); card.appendChild(heading);
    card.appendChild(textNode('p', `编号 ${prediction.id || '—'} · 封盘 ${beijingTime(closeAt)} · 参与 ${prediction.participants ?? 0} 人 · 投入 ${prediction.total_stake ?? 0} 积分`, 'muted'));
    const options = Array.isArray(prediction.options) ? prediction.options : [];
    card.appendChild(textNode('p', `选项：${options.join(' / ') || '—'}${prediction.winner ? ` · 获胜：${prediction.winner}` : ''}`, 'prediction-options'));
    if (!terminal && prediction.id) {
      const actions = document.createElement('div'); actions.className = 'prediction-actions';
      if (!auto) {
        const select = document.createElement('select'); select.setAttribute('aria-label', `为${prediction.title || prediction.id}选择获胜选项`);
        const blank = textNode('option', '选择获胜选项'); blank.value = ''; select.appendChild(blank);
        options.forEach((value) => { const option = textNode('option', value); option.value = String(value); select.appendChild(option); });
        const resolve = textNode('button', '判定结果', 'btn btn-primary'); resolve.type = 'button';
        resolve.disabled = !closed; resolve.title = closed ? '选择获胜选项后判定' : '竞猜封盘后才能判定';
        resolve.addEventListener('click', () => {
          if (!select.value) { say('请先选择获胜选项。', 'error'); select.focus(); return; }
          resolvePrediction(prediction, select.value, false, resolve).catch((error) => say(errorText(error), 'error'));
        });
        actions.append(select, resolve);
      } else actions.appendChild(textNode('span', '股市竞猜将自动判定', 'muted'));
      const cancel = textNode('button', '作废并返还', 'btn btn-secondary'); cancel.type = 'button';
      cancel.addEventListener('click', () => resolvePrediction(prediction, '', true, cancel).catch((error) => say(errorText(error), 'error')));
      actions.appendChild(cancel); card.appendChild(actions);
    }
    list.appendChild(card);
  });
}

function openPrediction() {
  clearErrors(['prediction-title', 'prediction-options', 'prediction-closes']);
  $('prediction-title').value = ''; $('prediction-options').value = '';
  $('prediction-closes').value = '';
  showModal('prediction-modal', $('prediction-title'));
}

function closePrediction() { if (!$('prediction-modal').querySelector('.is-busy')) hideModal('prediction-modal'); }

function parseBeijingLocal(value) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value)) return NaN;
  const date = new Date(`${value}+08:00`);
  return date.getTime() / 1000;
}

async function createPrediction() {
  if (state.economy?.settings?.predictions_enabled !== true) { say('请先启用并保存竞猜设置。', 'error'); return; }
  clearErrors(['prediction-title', 'prediction-options', 'prediction-closes']);
  const title = $('prediction-title').value.trim();
  const options = $('prediction-options').value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  const closesAt = parseBeijingLocal($('prediction-closes').value);
  if (!title || title.length > 100) setFieldError('prediction-title', '请填写 1 至 100 字的事件标题。');
  if (options.length < 2 || options.length > 6 || options.some((value) => value.length > 40) || new Set(options).size !== options.length) setFieldError('prediction-options', '请填写 2 至 6 个不同的选项，每项不超过 40 字。');
  if (!Number.isFinite(closesAt) || closesAt <= Date.now() / 1000) setFieldError('prediction-closes', '请填写未来的北京时间。');
  if (focusFirstInvalid(['prediction-title', 'prediction-options', 'prediction-closes'])) return;
  const result = await post('shop/economy/predictions/create', { title, options, closes_at: closesAt });
  hideModal('prediction-modal'); await loadEconomy();
  say(result.message || '管理员竞猜已创建', 'ok');
}

async function resolvePrediction(prediction, option, cancel, button) {
  const confirmed = await askConfirm({ title: cancel ? '作废竞猜并返还' : '判定竞猜结果', message: cancel ? `确定作废「${prediction.title}」并返还参与积分吗？` : `确定将「${prediction.title}」的获胜选项判为「${option}」吗？判定后将按规则结算。`, confirmText: cancel ? '作废并返还' : '确认判定', danger: true });
  if (!confirmed) return;
  await withBusy(button, async () => {
    const result = await post('shop/economy/predictions/resolve', { prediction_id: prediction.id, ...(cancel ? { cancel: true } : { option }) });
    await loadEconomy(); say(result.message || (cancel ? '竞猜已作废' : '竞猜已判定'), 'ok');
  }, '处理中');
}

function assetDefinition(title, entries) {
  const section = document.createElement('section'); section.className = 'asset-section'; section.appendChild(textNode('h4', title));
  const list = document.createElement('dl'); list.className = 'asset-definition';
  entries.forEach(([label, value]) => list.append(textNode('dt', label), textNode('dd', value)));
  section.appendChild(list); return section;
}

async function openAssets(user) {
  const requestId = ++state.assetRequest;
  state.assetUserKey = user.user_key;
  $('asset-user').textContent = `${user.nickname || user.user_id || '用户'} · ${user.user_key}`;
  $('asset-content').replaceChildren(textNode('p', '正在读取资产…', 'muted'));
  showModal('asset-modal', $('asset-close-top'));
  try {
    const result = await post('shop/economy/user', { user_key: user.user_key });
    if (requestId !== state.assetRequest || $('asset-modal').hidden) return;
    renderAssets(result || {});
  } catch (error) {
    if (requestId === state.assetRequest && !$('asset-modal').hidden) say(`资产加载失败：${errorText(error)}`, 'error');
  }
}

function closeAssets() { state.assetRequest += 1; hideModal('asset-modal'); }

function renderAssets(data) {
  const content = $('asset-content'); content.replaceChildren();
  const savings = data.savings || {};
  content.appendChild(assetDefinition('钱包与积分通', [['可用钱包', `${formatStat(data.wallet)} 积分`], ['积分通本金', `${formatStat(savings.principal)} 积分`], ['累计收益', `${formatStat(savings.interest_earned)} 积分`], ['当前每日收益率', savings.rate_bps === undefined ? '—' : `${Number(savings.rate_bps) / 100}%`], ['预计下次整积分收益', beijingTime(savings.next_settlement_at)], ['收益计算截至', beijingTime(savings.last_accrual_at)], ['竞猜锁定积分', `${formatStat(data.locked_prediction)} 积分`], ['股票已实现盈亏', `${formatStat(data.stock_realized_profit ?? 0)} 积分`]]));
  const positions = Array.isArray(data.positions) ? data.positions : [];
  const positionSection = document.createElement('section'); positionSection.className = 'asset-section'; positionSection.appendChild(textNode('h4', '股票持仓'));
  if (!positions.length) positionSection.appendChild(textNode('p', '暂无持仓', 'muted'));
  positions.forEach((position) => positionSection.appendChild(assetDefinition(`${position.code || '—'} · ${position.name || '—'}`, [['数量', position.quantity ?? '—'], ['当前单价', `${stockPrice(position)} 积分`], ['持仓市值', position.market_value_text ?? (position.market_value === undefined ? '—' : `${position.market_value} 积分`)], ['累计成本', position.cost_total_text ?? (position.cost_total === undefined ? '—' : `${position.cost_total} 积分`)]])));
  content.appendChild(positionSection);
  const cards = data.cards || {};
  const cardEntries = [];
  const boost = cards.checkin_boost || cards.boost;
  const usage = data.daily_usage && typeof data.daily_usage === 'object' ? data.daily_usage : {};
  const today = usage.date || new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' });
  if (boost) {
    const status = boost.start_date && today < boost.start_date ? '待生效' : boost.end_date && today >= boost.end_date ? '已到期' : boost.status || '生效中';
    cardEntries.push(['签到增益卡', `${status}${boost.multiplier_bps !== undefined ? ` · ${Number(boost.multiplier_bps) / 100}%` : ''}${boost.start_date ? ` · 生效日 ${boost.start_date}` : ''}${boost.end_date ? ` · 截至 ${boost.end_date}（不含该日）` : ''}${boost.daily_bonus_cap !== undefined ? ` · 每日额外最多 ${boost.daily_bonus_cap} 分` : ''}`]);
  }
  if (cards.rebate_last_at) cardEntries.push(['上次余额返利', beijingTime(cards.rebate_last_at)]);
  if (cards.rebate_cooldown_until) cardEntries.push(['余额返利卡', Number(cards.rebate_cooldown_until) > Date.now() / 1000 ? `冷却至 ${beijingTime(cards.rebate_cooldown_until)}` : '冷却已结束']);
  content.appendChild(assetDefinition('卡券状态', cardEntries.length ? cardEntries : [['当前状态', '暂无有效卡券或冷却记录']]));
  const usageLabels = { date: '签到日', stock_trades: '股票交易次数', stock_buy_total: '股票买入积分', prediction_stake: '竞猜投入积分', lottery_count: '刮刮乐次数' };
  const usageEntries = Object.entries(usage).filter(([key, value]) => usageLabels[key] && (typeof value === 'number' || typeof value === 'string')).map(([key, value]) => [usageLabels[key], value]);
  Object.entries(usage.families || {}).forEach(([key, value]) => {
    if (['checkin_boost', 'balance_rebate'].includes(key)) usageEntries.push([key === 'checkin_boost' ? '签到增益卡购买' : '余额返利卡购买', value]);
  });
  content.appendChild(assetDefinition('今日使用情况', usageEntries.length ? usageEntries : [['当前状态', '暂无今日使用记录']]));
}

// ---------------------------------------------------------------- 初始化

function bindTabs() {
  const tabs = Array.from(document.querySelectorAll('.tab'));
  const panels = Array.from(document.querySelectorAll('.panel'));
  const compact = window.matchMedia('(max-width: 900px)');
  const syncOrientation = () => document.querySelector('.tabs').setAttribute('aria-orientation', compact.matches ? 'horizontal' : 'vertical');
  syncOrientation();
  compact.addEventListener('change', syncOrientation);
  tabs.forEach((tab, index) => {
    const name = tab.dataset.tab;
    const panel = document.querySelector(`.panel[data-panel="${name}"]`);
    tab.id = `tab-${name}`;
    tab.setAttribute('aria-controls', `panel-${name}`);
    tab.setAttribute('aria-selected', String(tab.classList.contains('active')));
    tab.tabIndex = tab.classList.contains('active') ? 0 : -1;
    if (panel) {
      panel.id = `panel-${name}`;
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', tab.id);
    }
    tab.addEventListener('keydown', (event) => {
      let next;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index + tabs.length - 1) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next === undefined) return;
      event.preventDefault();
      tabs.forEach((item, itemIndex) => { item.tabIndex = itemIndex === next ? 0 : -1; });
      tabs[next].focus();
    });
    tab.addEventListener('click', () => {
      tabs.forEach((item) => {
        item.classList.remove('active');
        item.setAttribute('aria-selected', String(item === tab));
        item.tabIndex = item === tab ? 0 : -1;
      });
      panels.forEach((item) => item.classList.remove('active'));
      tab.classList.add('active');
      if (panel) panel.classList.add('active');
      if (tab.dataset.tab === 'users') run(null, () => loadUsers(1));
      if (tab.dataset.tab === 'redemptions') run(null, () => loadRedemptions(1));
      if (tab.dataset.tab === 'ledger') run(null, () => loadLedger(1));
      if (tab.dataset.tab === 'economy') run(null, loadEconomy);
    });
  });
}

async function boot() {
  const context = await bridge.ready();
  if (context && context.i18n) {
    document.title = bridge.t('pages.shop.title', '打卡积分商店');
  }
  applyTheme(context ? context.isDark : undefined);
  const logo = $('brand-logo');
  logo.addEventListener('error', () => { logo.hidden = true; });
  if (logo.complete && !logo.naturalWidth) logo.hidden = true;
  bindTabs();
  setupEconomyFields();
  bindModalFocus();
  bindModalDismiss('product-modal', closeProduct);
  bindModalDismiss('adjust-modal', closeAdjust);
  bindModalDismiss('prediction-modal', closePrediction);
  bindModalDismiss('asset-modal', closeAssets);
  $('status-close').addEventListener('click', () => say(''));
  $('refresh').addEventListener('click', (event) => {
    run(event.currentTarget, async () => {
      await loadSummary();
      if (document.querySelector('.tab.active')?.dataset.tab === 'economy') await loadEconomy();
      say('已刷新', 'ok');
    }, '刷新中');
  });
  $('save-rewards').addEventListener('click', (event) => {
    run(event.currentTarget, saveRewards, '保存中');
  });
  $('save-settings').addEventListener('click', (event) => {
    run(event.currentTarget, saveSettings, '保存中');
  });
  $('reward-mode').addEventListener('change', syncRewardMode);
  $('set-prefix').addEventListener('input', updatePrefixHint);
  $('new-product').addEventListener('click', () => openProduct(null));
  $('p-type').addEventListener('change', () => syncProductType(true));
  ['p-boost-multiplier', 'p-boost-days', 'p-boost-cap', 'p-rebate-rate', 'p-rebate-cap', 'p-rebate-cooldown'].forEach((id) => $(id).addEventListener('input', updateProductEffectSummary));
  $('economy-refresh').addEventListener('click', (event) => run(event.currentTarget, loadEconomy, '加载中'));
  $('save-economy').addEventListener('click', (event) => run(event.currentTarget, saveEconomy, '保存中'));
  $('lottery-prize-add').addEventListener('click', () => addLotteryPrize());
  $('prediction-new').addEventListener('click', openPrediction);
  $('prediction-save').addEventListener('click', (event) => run(event.currentTarget, createPrediction, '创建中'));
  $('prediction-cancel').addEventListener('click', closePrediction);
  $('asset-close').addEventListener('click', closeAssets);
  $('asset-close-top').addEventListener('click', closeAssets);
  // 筛选/排序都在前端完成：商品本来就是一次性加载的，这样点一下就有反馈、不再多打一次接口
  $('product-search').addEventListener('input', () => {
    state.productKeyword = $('product-search').value;
    renderProducts();
  });
  $('product-category').addEventListener('change', () => {
    state.productCategory = $('product-category').value;
    renderProducts();
  });
  $('product-status').addEventListener('change', () => {
    state.productStatus = $('product-status').value;
    renderProducts();
  });
  $('product-filter-reset').addEventListener('click', resetProductFilters);
  document.querySelectorAll('#product-table th.sortable').forEach((th) => {
    th.addEventListener('click', () => {
      const field = th.dataset.sort;
      if (state.productSort === field) {
        state.productSortAsc = !state.productSortAsc;
      } else {
        state.productSort = field;
        state.productSortAsc = true;
      }
      syncProductSortHeader();
      renderProducts();
    });
  });
  $('p-save').addEventListener('click', (event) => {
    run(event.currentTarget, saveProduct, '保存中');
  });
  $('p-cancel').addEventListener('click', closeProduct);
  $('p-image').addEventListener('change', () => {
    const file = $('p-image').files && $('p-image').files[0];
    const img = $('p-preview');
    if (!file) return;
    img.src = URL.createObjectURL(file);
    img.hidden = false;
  });
  $('preview-shop').addEventListener('click', (event) => {
    run(event.currentTarget, previewShop, '导出中');
  });
  $('preview-user-search').addEventListener('click', (event) => {
    run(event.currentTarget, searchPreviewUsers, '搜索中');
  });
  $('preview-user-keyword').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      run($('preview-user-search'), searchPreviewUsers, '搜索中');
    }
  });
  $('preview-user').addEventListener('change', () => {
    state.previewUserKey = $('preview-user').value || 'preview:0';
  });
  $('user-search').addEventListener('click', (event) => {
    run(event.currentTarget, () => loadUsers(1), '搜索中');
  });
  $('user-prev').addEventListener('click', (event) => {
    if (state.userPage <= 1) return;
    run(event.currentTarget, () => loadUsers(state.userPage - 1)).finally(syncUserPager);
  });
  $('user-next').addEventListener('click', (event) => {
    if (state.userPage * PAGE_SIZE >= state.userTotal) return;
    run(event.currentTarget, () => loadUsers(state.userPage + 1)).finally(syncUserPager);
  });
  $('a-save').addEventListener('click', (event) => {
    run(event.currentTarget, saveAdjust, '提交中');
  });
  $('a-cancel').addEventListener('click', closeAdjust);
  $('redeem-search').addEventListener('click', (event) => {
    run(event.currentTarget, () => loadRedemptions(1), '筛选中');
  });
  $('email-test').addEventListener('click', (event) => {
    run(event.currentTarget, sendTestEmail, '发送中…');
  });
  $('redeem-prev').addEventListener('click', (event) => {
    if (state.redeemPage <= 1) return;
    run(event.currentTarget, () => loadRedemptions(state.redeemPage - 1)).finally(syncRedeemPager);
  });
  $('redeem-next').addEventListener('click', (event) => {
    if (state.redeemPage * PAGE_SIZE >= state.redeemTotal) return;
    run(event.currentTarget, () => loadRedemptions(state.redeemPage + 1)).finally(syncRedeemPager);
  });
  $('ledger-search').addEventListener('click', (event) => {
    run(event.currentTarget, () => loadLedger(1), '筛选中');
  });
  $('ledger-export').addEventListener('click', (event) => {
    run(event.currentTarget, exportLedger, '导出中');
  });
  $('ledger-prev').addEventListener('click', (event) => {
    if (state.ledgerPage <= 1) return;
    run(event.currentTarget, () => loadLedger(state.ledgerPage - 1)).finally(syncLedgerPager);
  });
  $('ledger-next').addEventListener('click', (event) => {
    if (state.ledgerPage * PAGE_SIZE >= state.ledgerTotal) return;
    run(event.currentTarget, () => loadLedger(state.ledgerPage + 1)).finally(syncLedgerPager);
  });
  bridge.onContext((next) => {
    if (next && next.isDark !== undefined) applyTheme(next.isDark);
  });
  try {
    await loadSummary();
  } catch (error) {
    say(errorText(error), 'error');
  }
  // 供页面 smoke 测试使用
  window.__shopPage = {
    state,
    loadSummary,
    renderProducts,
    productView,
    resetProductFilters,
    fillCategoryOptions,
    saveRewards,
    saveProduct,
    loadUsers,
    loadRedemptions,
    loadLedger,
    exportLedger,
    loadEconomy,
    saveEconomy,
    openAssets,
    parseBeijingLocal,
  };
}

if (bridge) {
  boot().catch((error) => say(errorText(error), 'error'));
} else {
  say('未检测到 AstrBot 插件页 bridge，请在 AstrBot 后台打开本页面。', 'error');
}
