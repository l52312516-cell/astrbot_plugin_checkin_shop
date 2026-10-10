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
  adjustUserKey: '',
  assetUrls: {},
  rewardsLoaded: false,
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
  const label = button.textContent;
  const wasDisabled = button.disabled;
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
    if (busyText) button.textContent = label;
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
  if (!Number.isFinite(value) || !Number.isInteger(value)) {
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
  return String(value || '')
    .split(/[,，\s]+/)
    .filter((item) => item !== '')
    .map((item) => Number(item))
    .filter((item) => Number.isFinite(item));
}

async function post(endpoint, body) {
  return bridge.apiPost(endpoint, body || {});
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
  modal.hidden = false;
  okButton.focus();
  return new Promise((resolve) => {
    const finish = (value) => {
      modal.hidden = true;
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
    if (event.target === modal) close();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || modal.hidden) return;
    const confirm = $('confirm-modal');
    if (confirm && !confirm.hidden) return;
    close();
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
    ['用户数', formatStat(stats.user_count)],
    ['商品数', `${stats.enabled_product_count || 0} / ${stats.product_count || 0}`],
    ['今日打卡', formatStat(stats.checkin_today)],
    ['流通积分', formatStat(stats.total_points)],
    ['待发放', formatStat(stats.pending_redemption_count)],
    ['兑换总数', formatStat(stats.redemption_count)],
  ];
  $('stats').innerHTML = cards
    .map(
      ([label, value]) =>
        `<div class="stat"><b>${value}</b><span>${label}</span></div>`,
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
  const list = parseNumberList($('reward-list').value);
  const weights = parseNumberList($('reward-weights').value);
  if ($('reward-mode').value === 'custom' && list.length === 0) {
    setFieldError('reward-list', '自定义模式至少要填一个积分值，例如 5,10,20。');
    $('reward-list').focus();
    return;
  }
  if (weights.length && list.length && weights.length !== list.length) {
    setFieldError('reward-weights', '权重个数要和积分列表一致，或者整列留空表示等概率。');
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
    cells[3].textContent = `${product.cost} 积分`;
    cells[4].textContent = String(stock);
    cells[5].textContent = String(limit);
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
  $('p-desc').value = product ? product.description || '' : '';
  $('p-enabled').checked = product ? product.enabled !== false : true;
  $('p-image').value = '';
  clearErrors(['p-name', 'p-cost', 'p-stock', 'p-limit']);
  state.editingImagePath = product ? product.image_path || '' : '';
  state.previewProductId = editing ? product.id : '';
  // 编辑时保留原有 sort_order，否则"保存一下就跳到列表最后"（序号会跟着变）
  state.editingSortOrder = editing ? Number(product.sort_order) || 0 : null;
  state.pendingImage = null;
  renderProductPreview();
  $('product-modal').hidden = false;
  $('p-name').focus();
}

function closeProduct() {
  const modal = $('product-modal');
  if (modal.querySelector('.is-busy')) return;
  modal.hidden = true;
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
  clearErrors(['p-name', 'p-cost', 'p-stock', 'p-limit']);
  const name = $('p-name').value.trim();
  const cost = readInteger('p-cost', { allowEmpty: false, min: 0 });
  const stock = readInteger('p-stock', { allowEmpty: true, min: 0 });
  const limit = readInteger('p-limit', { allowEmpty: true, min: 0 });
  if (!name) setFieldError('p-name', '商品名称不能为空，用户端会用它显示商品。');
  if (cost.error) setFieldError('p-cost', cost.error);
  if (stock.error) setFieldError('p-stock', stock.error);
  if (limit.error) setFieldError('p-limit', limit.error);
  if (focusFirstInvalid(['p-name', 'p-cost', 'p-stock', 'p-limit'])) return;

  const payload = {
    id: state.previewProductId || '',
    name,
    cost: cost.value || 0,
    stock: stock.value,
    per_user_limit: limit.value,
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
  const file = $('p-image').files && $('p-image').files[0];
  if (file && productId) {
    await bridge.upload(`shop/products/image/upload?product_id=${encodeURIComponent(productId)}`, file);
  } else if (file && !productId) {
    say('商品已保存，但图片未上传：接口没有返回商品 ID，请重试', 'error');
    await loadSummary();
    return;
  }
  $('product-modal').hidden = true;
  state.assetUrls = {};
  await loadSummary();
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
    await bridge.download('shop/preview', {}, 'shop-preview.png');
    hint.textContent = '预览图已开始下载（浏览器下载栏可见）；商店图与用户在群里看到的完全一致。';
  } catch (error) {
    hint.textContent = `预览失败：${errorText(error)}`;
    say(`预览失败：${errorText(error)}`, 'error');
  }
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
  $('adjust-modal').hidden = false;
  $('a-delta').focus();
}

async function saveAdjust() {
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
  $('adjust-modal').hidden = true;
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
      : stamp.toLocaleString('zh-CN', { hour12: false });
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
    if (record.status !== '已发放') {
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
    say(`${result.message || '已更新'}${notice}${email}`, 'ok');
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
      : stamp.toLocaleString('zh-CN', { hour12: false });
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

// ---------------------------------------------------------------- 初始化

function bindTabs() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((item) => item.classList.remove('active'));
      document.querySelectorAll('.panel').forEach((item) => item.classList.remove('active'));
      tab.classList.add('active');
      const panel = document.querySelector(`.panel[data-panel="${tab.dataset.tab}"]`);
      if (panel) panel.classList.add('active');
      if (tab.dataset.tab === 'users') run(null, () => loadUsers(1));
      if (tab.dataset.tab === 'redemptions') run(null, () => loadRedemptions(1));
      if (tab.dataset.tab === 'ledger') run(null, () => loadLedger(1));
    });
  });
}

async function boot() {
  const context = await bridge.ready();
  if (context && context.i18n) {
    document.title = bridge.t('pages.shop.title', '打卡积分商店');
  }
  applyTheme(context ? context.isDark : undefined);
  bindTabs();
  bindModalDismiss('product-modal', closeProduct);
  bindModalDismiss('adjust-modal', () => {
    $('adjust-modal').hidden = true;
  });
  $('status-close').addEventListener('click', () => say(''));
  $('refresh').addEventListener('click', (event) => {
    run(event.currentTarget, async () => {
      await loadSummary();
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
  $('a-cancel').addEventListener('click', () => {
    $('adjust-modal').hidden = true;
  });
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
  };
}

if (bridge) {
  boot().catch((error) => say(errorText(error), 'error'));
} else {
  say('未检测到 AstrBot 插件页 bridge，请在 AstrBot 后台打开本页面。', 'error');
}
