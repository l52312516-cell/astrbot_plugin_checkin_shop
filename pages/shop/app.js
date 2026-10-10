// 打卡积分商店 · WebUI 插件页
// 只通过 window.AstrBotPluginPage bridge 与插件后端通信，不直接访问 Dashboard。
const bridge = window.AstrBotPluginPage;

const state = {
  summary: null,
  productRows: [],
  userRows: [],
  redeemRows: [],
  userPage: 1,
  redeemPage: 1,
  userTotal: 0,
  redeemTotal: 0,
  editingImagePath: '',
  pendingImage: null,
  previewProductId: '',
  adjustUserKey: '',
  assetUrls: {},
};

const $ = (id) => document.getElementById(id);

function say(message, kind) {
  const status = $('status');
  status.textContent = message || '';
  status.style.color =
    kind === 'error' ? 'var(--danger)' : kind === 'ok' ? 'var(--ok)' : 'var(--muted)';
  if (!message) return;
  window.clearTimeout(say.timer);
  say.timer = window.setTimeout(() => {
    status.textContent = '';
  }, 6000);
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
  okButton.className = danger ? 'btn danger' : 'btn primary';
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

// ---------------------------------------------------------------- 概览与统计

function renderStats() {
  const stats = (state.summary && state.summary.statistics) || {};
  const cards = [
    ['用户数', stats.user_count],
    ['商品数', `${stats.enabled_product_count || 0} / ${stats.product_count || 0}`],
    ['今日打卡', stats.checkin_today],
    ['流通积分', stats.total_points],
    ['待发放', stats.pending_redemption_count],
    ['兑换总数', stats.redemption_count],
  ];
  $('stats').innerHTML = cards
    .map(
      ([label, value]) =>
        `<div class="stat"><b>${value === undefined ? '-' : value}</b><span>${label}</span></div>`,
    )
    .join('');
}

async function loadSummary() {
  const summary = await bridge.apiGet('shop/summary');
  state.summary = summary;
  fillRewardForm(summary.rewards || {});
  fillSettingsForm(summary.settings || {});
  state.productRows = summary.products || [];
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
  $('reward-mode').value = rewards.mode || 'fixed';
  $('reward-fixed').value = rewards.fixed ?? 10;
  $('reward-min').value = rewards.random_min ?? 5;
  $('reward-max').value = rewards.random_max ?? 20;
  $('reward-list').value = (rewards.custom_list || []).join(',');
  $('reward-weights').value = (rewards.custom_weights || []).join(',');
  $('bonus-enabled').checked = rewards.streak_bonus_enabled !== false;
  $('bonus-every').value = rewards.streak_bonus_every ?? 7;
  $('bonus-points').value = rewards.streak_bonus_points ?? 50;
  syncRewardMode();
}

async function saveRewards() {
  const payload = {
    mode: $('reward-mode').value,
    fixed: Number($('reward-fixed').value || 0),
    random_min: Number($('reward-min').value || 0),
    random_max: Number($('reward-max').value || 0),
    custom_list: parseNumberList($('reward-list').value),
    custom_weights: parseNumberList($('reward-weights').value),
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
  $('set-board-size').value = settings.leaderboard_size ?? 20;
  $('set-board-scope').value = settings.leaderboard_scope || 'platform';
  $('set-timeout').value = settings.confirm_timeout_seconds ?? 60;
  $('set-reset-hour').value = settings.reset_hour ?? 0;
  $('set-scope').value = settings.points_scope || 'platform_user';
}

async function saveSettings() {
  const result = await post('shop/settings/save', {
    shop_title: $('set-title').value,
    shop_subtitle: $('set-subtitle').value,
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
  tbody.innerHTML = '';
  $('product-empty').hidden = state.productRows.length > 0;
  state.productRows.forEach((product) => {
    const row = document.createElement('tr');
    const stock = product.stock === null || product.stock === undefined ? '不限量' : product.stock;
    const limit =
      product.per_user_limit === null || product.per_user_limit === undefined
        ? '不限购'
        : product.per_user_limit;
    row.innerHTML = `
      <td class="thumb-cell"></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td></td>
      <td><span class="tag ${product.enabled ? 'ok' : 'warn'}">${product.enabled ? '上架' : '下架'}</span></td>
      <td><button class="btn ghost" data-action="edit">编辑</button>
          <button class="btn danger" data-action="delete">删除</button></td>`;
    const cells = row.querySelectorAll('td');
    cells[1].textContent = product.name;
    cells[2].textContent = `${product.cost} 积分`;
    cells[3].textContent = String(stock);
    cells[4].textContent = String(limit);
    cells[5].textContent = product.category || '默认';
    const thumbCell = cells[0];
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
      span.className = 'tag';
      span.textContent = (product.name || '?').slice(0, 1);
      thumbCell.appendChild(span);
    }
    row.querySelector('[data-action="edit"]').addEventListener('click', () => openProduct(product));
    row.querySelector('[data-action="delete"]').addEventListener('click', () => deleteProduct(product));
    tbody.appendChild(row);
  });
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
  state.editingImagePath = product ? product.image_path || '' : '';
  state.previewProductId = editing ? product.id : '';
  state.pendingImage = null;
  renderProductPreview();
  $('product-modal').hidden = false;
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
  const name = $('p-name').value.trim();
  if (!name) {
    say('商品名称不能为空', 'error');
    return;
  }
  const stockRaw = $('p-stock').value.trim();
  const limitRaw = $('p-limit').value.trim();
  const payload = {
    id: state.previewProductId || '',
    name,
    cost: Number($('p-cost').value || 0),
    stock: stockRaw === '' ? null : Number(stockRaw),
    per_user_limit: limitRaw === '' ? null : Number(limitRaw),
    category: $('p-category').value.trim() || '默认',
    description: $('p-desc').value.trim(),
    enabled: $('p-enabled').checked,
    sort_order: state.productRows.length * 10 + 10,
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

async function deleteProduct(product) {
  const confirmed = await askConfirm({
    title: '删除商品',
    message: `确定删除「${product.name}」吗？如果它还有待发放的兑换记录，会改为下架而不是删除，历史账目不会丢。`,
    confirmText: '删除',
  });
  if (!confirmed) return;
  try {
    const result = await post('shop/products/delete', { id: product.id });
    state.assetUrls = {};
    await loadSummary();
    say(result.message || '已删除', 'ok');
  } catch (error) {
    say(`删除失败：${error.message || error}`, 'error');
  }
}

async function previewShop() {
  const hint = $('preview-hint');
  try {
    await bridge.download('shop/preview', {}, 'shop-preview.png');
    hint.textContent = '已下载预览图（浏览器下载栏可见）。';
  } catch (error) {
    hint.textContent = `预览失败：${error.message}`;
    say(`预览失败：${error.message}`, 'error');
  }
}

// ---------------------------------------------------------------- 用户积分

async function loadUsers(page) {
  if (page) state.userPage = page;
  const result = await post('shop/users', {
    keyword: $('user-keyword').value.trim(),
    platform: $('user-platform').value.trim(),
    page: state.userPage,
    page_size: 20,
  });
  state.userRows = result.items || [];
  state.userTotal = result.total || 0;
  const tbody = $('user-table').querySelector('tbody');
  tbody.innerHTML = '';
  state.userRows.forEach((user) => {
    const row = document.createElement('tr');
    row.innerHTML = '<td></td><td></td><td></td><td></td><td></td><td></td><td></td>';
    const cells = row.querySelectorAll('td');
    cells[0].textContent = user.nickname || user.user_id;
    cells[1].textContent = user.platform;
    cells[2].textContent = String(user.points);
    cells[3].textContent = String(user.streak);
    cells[4].textContent = String(user.checkin_count);
    cells[5].textContent = user.last_checkin_date || '-';
    const button = document.createElement('button');
    button.className = 'btn ghost';
    button.textContent = '调整积分';
    button.addEventListener('click', () => openAdjust(user));
    cells[6].appendChild(button);
    tbody.appendChild(row);
  });
  $('user-page').textContent = `第 ${state.userPage} 页 · 共 ${state.userTotal} 人`;
}

function openAdjust(user) {
  state.adjustUserKey = user.user_key;
  $('adjust-user').textContent = `${user.nickname || user.user_id}（${user.user_key}）当前 ${user.points} 积分`;
  $('a-delta').value = 0;
  $('a-absolute').value = '';
  $('a-note').value = '';
  $('adjust-modal').hidden = false;
}

async function saveAdjust() {
  const absoluteRaw = $('a-absolute').value.trim();
  const payload = {
    user_key: state.adjustUserKey,
    delta: Number($('a-delta').value || 0),
    note: $('a-note').value.trim(),
  };
  if (absoluteRaw !== '') payload.absolute = Number(absoluteRaw);
  const result = await post('shop/users/adjust', payload);
  $('adjust-modal').hidden = true;
  await loadUsers();
  await loadSummary();
  say(result.message || '积分已调整', 'ok');
}

// ---------------------------------------------------------------- 兑换记录

async function loadRedemptions(page) {
  if (page) state.redeemPage = page;
  const result = await post('shop/redemptions', {
    status: $('redeem-status').value,
    page: state.redeemPage,
    page_size: 20,
  });
  state.redeemRows = result.items || [];
  state.redeemTotal = result.total || 0;
  const tbody = $('redeem-table').querySelector('tbody');
  tbody.innerHTML = '';
  state.redeemRows.forEach((record) => {
    const row = document.createElement('tr');
    row.innerHTML = '<td></td><td></td><td></td><td></td><td></td><td></td>';
    const cells = row.querySelectorAll('td');
    const stamp = new Date(Number(record.created_at) * 1000);
    cells[0].textContent = Number.isNaN(stamp.getTime())
      ? '-'
      : stamp.toLocaleString('zh-CN', { hour12: false });
    cells[1].textContent = record.nickname || record.user_id || record.user_key;
    cells[2].textContent = record.product_name;
    cells[3].textContent = `-${record.cost}`;
    const status = document.createElement('span');
    status.className = `tag ${record.status === '已发放' ? 'ok' : record.status === '已取消' ? 'warn' : ''}`;
    status.textContent = record.status;
    cells[4].appendChild(status);
    const actions = document.createElement('div');
    actions.className = 'row-end';
    if (record.status !== '已发放') {
      const done = document.createElement('button');
      done.className = 'btn ghost';
      done.textContent = '标记发放';
      done.addEventListener('click', () => updateRedemption(record, '已发放', false));
      actions.appendChild(done);
    }
    if (record.status !== '已取消') {
      const cancel = document.createElement('button');
      cancel.className = 'btn danger';
      cancel.textContent = '取消并返还';
      cancel.addEventListener('click', () => updateRedemption(record, '已取消', true));
      actions.appendChild(cancel);
    }
    const remove = document.createElement('button');
    remove.className = 'btn danger';
    remove.textContent = '删除';
    remove.addEventListener('click', () => deleteRedemption(record));
    actions.appendChild(remove);
    cells[5].appendChild(actions);
    tbody.appendChild(row);
  });
  $('redeem-page').textContent = `第 ${state.redeemPage} 页 · 共 ${state.redeemTotal} 条`;
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
    say(`操作失败：${error.message || error}`, 'error');
  }
}

async function sendTestEmail() {
  const hint = '测试邮件会发给插件配置页里的「兜底收件人」（没填就用 SMTP 账号）。';
  try {
    const result = await post('shop/email/test', {});
    say(`${result.message || '测试邮件已发送'}（${hint}）`, 'ok');
  } catch (error) {
    say(`测试邮件失败：${error.message || error}`, 'error');
  }
}

async function deleteRedemption(record) {
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
    const result = await post('shop/redemptions/delete', {
      id: record.id,
      force: pending,
    });
    await loadRedemptions();
    await loadSummary();
    say(result.message || '已删除', 'ok');
  } catch (error) {
    say(`删除失败：${error.message || error}`, 'error');
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
      if (tab.dataset.tab === 'users') loadUsers(1).catch((error) => say(error.message, 'error'));
      if (tab.dataset.tab === 'redemptions') {
        loadRedemptions(1).catch((error) => say(error.message, 'error'));
      }
    });
  });
}

async function boot() {
  const context = await bridge.ready();
  if (context && context.i18n) {
    document.title = bridge.t('pages.shop.title', '打卡积分商店');
  }
  bindTabs();
  $('refresh').addEventListener('click', () => {
    loadSummary()
      .then(() => say('已刷新', 'ok'))
      .catch((error) => say(error.message, 'error'));
  });
  $('save-rewards').addEventListener('click', () => {
    saveRewards().catch((error) => say(error.message, 'error'));
  });
  $('save-settings').addEventListener('click', () => {
    saveSettings().catch((error) => say(error.message, 'error'));
  });
  $('reward-mode').addEventListener('change', syncRewardMode);
  $('new-product').addEventListener('click', () => openProduct(null));
  $('p-save').addEventListener('click', () => {
    saveProduct().catch((error) => say(error.message, 'error'));
  });
  $('p-cancel').addEventListener('click', () => {
    $('product-modal').hidden = true;
  });
  $('p-image').addEventListener('change', () => {
    const file = $('p-image').files && $('p-image').files[0];
    const img = $('p-preview');
    if (!file) return;
    img.src = URL.createObjectURL(file);
    img.hidden = false;
  });
  $('preview-shop').addEventListener('click', () => {
    previewShop().catch((error) => say(error.message, 'error'));
  });
  $('user-search').addEventListener('click', () => {
    loadUsers(1).catch((error) => say(error.message, 'error'));
  });
  $('user-prev').addEventListener('click', () => {
    if (state.userPage > 1) loadUsers(state.userPage - 1).catch((error) => say(error.message, 'error'));
  });
  $('user-next').addEventListener('click', () => {
    if (state.userPage * 20 < state.userTotal) {
      loadUsers(state.userPage + 1).catch((error) => say(error.message, 'error'));
    }
  });
  $('a-save').addEventListener('click', () => {
    saveAdjust().catch((error) => say(error.message, 'error'));
  });
  $('a-cancel').addEventListener('click', () => {
    $('adjust-modal').hidden = true;
  });
  $('redeem-search').addEventListener('click', () => {
    loadRedemptions(1).catch((error) => say(error.message, 'error'));
  });
  $('email-test').addEventListener('click', () => {
    sendTestEmail();
  });
  $('redeem-prev').addEventListener('click', () => {
    if (state.redeemPage > 1) {
      loadRedemptions(state.redeemPage - 1).catch((error) => say(error.message, 'error'));
    }
  });
  $('redeem-next').addEventListener('click', () => {
    if (state.redeemPage * 20 < state.redeemTotal) {
      loadRedemptions(state.redeemPage + 1).catch((error) => say(error.message, 'error'));
    }
  });
  bridge.onContext((next) => {
    if (next && next.isDark !== undefined) {
      document.documentElement.setAttribute('data-theme', next.isDark ? 'dark' : 'light');
    }
  });
  try {
    await loadSummary();
  } catch (error) {
    say(error.message, 'error');
  }
  // 供页面 smoke 测试使用
  window.__shopPage = { state, loadSummary, renderProducts, saveRewards, saveProduct, loadUsers, loadRedemptions };
}

if (bridge) {
  boot().catch((error) => say(error.message || String(error), 'error'));
} else {
  say('未检测到 AstrBot 插件页 bridge，请在 AstrBot 后台打开本页面。', 'error');
}
