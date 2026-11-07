/**
 * 3D 数字人启动器：把渲染引擎包成 window.KangbanAvatar，
 * 供现有通话页的普通 <script> 调用（无需把整页改成 ES Module）。
 */

import { KangbanAvatar } from './engine.js';

const pending = {
  state: 'idle',
  stream: null,
  actions: [],
  hostId: 'avatarHomeSlot',
};

/** 用户形象偏好（localStorage）：切换成功后记录，下次进入自动加载 */
const AVATAR_PREF_KEY = 'kb_avatar_id';

let avatar = null;

function setHomeStatus(text) {
  const el = document.getElementById('avatarHomeStatus');
  if (el) el.textContent = text;
}

function loadPreferredAvatar() {
  try {
    return window.localStorage.getItem(AVATAR_PREF_KEY) || null;
  } catch (_err) {
    return null; // 隐私模式等 localStorage 不可用：忽略，用注册表默认
  }
}

function savePreferredAvatar(id) {
  try {
    window.localStorage.setItem(AVATAR_PREF_KEY, id);
  } catch (_err) {
    /* 存不进去不影响切换，只是下次不记忆 */
  }
}

/** 一键切换控件：按钮组渲染进 #kbAvatarSwitcher（voice.html / elder.html 共用）。
 *  切换期间禁用全部按钮防连点；成功后记忆偏好，失败回显当前形象。
 *  口型流（analyser）挂在引擎上而非模型快照，通话中切换无需重挂。 */
function renderAvatarSwitcher(container) {
  if (!container || container.dataset.rendered) return;
  const items = avatar.listAvatars();
  if (!items.length) return;
  container.dataset.rendered = '1';

  const row = document.createElement('div');
  row.style.cssText =
    'display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:8px;';
  const hint = document.createElement('div');
  hint.style.cssText = 'font-size:12px;color:#999;text-align:center;margin-top:4px;';

  const buttons = items.map((item) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = item.name;
    btn.title = item.description || item.id;
    btn.style.cssText =
      'font:inherit;font-size:15px;padding:8px 18px;border-radius:999px;' +
      'border:1px solid #ccc;background:#f7faf8;cursor:pointer;transition:all .15s;';
    btn.addEventListener('click', () => switchTo(item, btn));
    row.appendChild(btn);
    return btn;
  });

  function syncButtons() {
    const current = avatar.getAvatar();
    for (let i = 0; i < items.length; i++) {
      const active = items[i].id === current;
      buttons[i].classList.toggle('kb-avatar-active', active);
      buttons[i].style.background = active ? 'linear-gradient(135deg,#236455,#6db79a)' : '#f7faf8';
      buttons[i].style.color = active ? '#fff' : '#193a36';
      buttons[i].style.borderColor = active ? 'transparent' : '#ccc';
    }
    hint.textContent = '当前形象：' + (current || '无');
  }

  let switching = false;
  async function switchTo(item, btn) {
    if (switching) return;
    switching = true;
    buttons.forEach((b) => (b.disabled = true));
    btn.textContent = item.name + '…';
    try {
      const result = await avatar.setAvatar(item.id);
      if (result.ok) {
        savePreferredAvatar(result.id);
      } else if (result.busy) {
        setHomeStatus('正在切换形象，请稍后再试');
      } else {
        setHomeStatus('形象切换失败：' + (result.error || '未知错误'));
      }
    } finally {
      buttons.forEach((b, i) => {
        b.disabled = false;
        b.textContent = items[i].name;
      });
      syncButtons();
      switching = false;
    }
  }

  container.appendChild(row);
  container.appendChild(hint);
  syncButtons();
}

async function boot() {
  const host = document.getElementById('avatarHomeSlot');
  if (!host) return;

  try {
    avatar = new KangbanAvatar({
      host,
      avatarId: loadPreferredAvatar(), // 记住上次选择（非法 id 引擎自动回落默认）
      onStatus(status) {
        if (status.phase === 'ready') {
          document.body.classList.add('avatar3d-ready');
          setHomeStatus(`3D 数字人已就绪${status.avatar ? '（' + status.avatar + '）' : ''}`);
        } else if (status.phase === 'error') {
          setHomeStatus('数字人加载失败：' + (status.message || '未知错误'));
        } else {
          setHomeStatus(status.message || '数字人加载中…');
        }
      },
    });
    await avatar.init();
    savePreferredAvatar(avatar.getAvatar()); // 请求的 id 失效回落时，把实际形象写回

    if (pending.hostId && pending.hostId !== 'avatarHomeSlot') {
      const target = document.getElementById(pending.hostId);
      if (target) avatar.moveTo(target);
    }
    avatar.setState(pending.state);
    if (pending.stream) avatar.attachStream(pending.stream);
    for (const [actionId, param] of pending.actions.splice(0)) {
      avatar.playAction(actionId, param);
    }
    document.body.classList.add('avatar3d-ready');
    renderAvatarSwitcher(document.getElementById('kbAvatarSwitcher'));
  } catch (error) {
    console.error('[avatar] 初始化失败', error);
    setHomeStatus('3D 数字人不可用：' + (error?.message || error));
  }
}

window.KangbanAvatar = {
  get ready() {
    return Boolean(avatar);
  },
  /** idle / calling / in_call / off */
  setState(state) {
    pending.state = state || 'idle';
    avatar?.setState(pending.state);
  },
  /** 远端 WebRTC MediaStream，用于 AnalyserNode 口型驱动 */
  attachStream(stream) {
    pending.stream = stream || null;
    if (pending.stream) avatar?.attachStream(pending.stream);
  },
  detachStream() {
    pending.stream = null;
    avatar?.detachStream();
  },
  /** 信令 kind=action 触发的动作播放 */
  playAction(actionId, param = null) {
    if (!actionId) return;
    if (avatar) avatar.playAction(actionId, param);
    else pending.actions.push([actionId, param]);
  },
  /** 把画布移动到指定容器（首页卡片 / 通话浮层） */
  mount(hostId) {
    pending.hostId = hostId;
    if (!avatar) return;
    const target = document.getElementById(hostId);
    if (target) avatar.moveTo(target);
  },
  /** 运行时切换形象；注册表来自 /avatars.json */
  async setAvatar(id) {
    if (!avatar) return { ok: false, error: '数字人尚未初始化' };
    return avatar.setAvatar(id);
  },
  /** 当前形象 id */
  getAvatar() {
    return avatar?.avatarId || null;
  },
  /** 可切换的形象列表 */
  listAvatars() {
    return avatar?.listAvatars() || [];
  },
  /** 贴图/几何计数，用于切换前后对比 */
  getMemoryStats() {
    return avatar?.getMemoryStats() || null;
  },
  /** 调试用：控制台可执行 __kbAvatar.playAction('wave') */
  debug() {
    return avatar;
  },
};

boot();
