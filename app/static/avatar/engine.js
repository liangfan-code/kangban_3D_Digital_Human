/**
 * 康伴 3D 数字人渲染引擎（ES Module，浏览器端）
 *
 * 职责：
 * - 加载本地 VRM 模型与 VRMA 动作（全部本地资源，无 CDN）
 * - 待机循环 + 随机眨眼
 * - 通话时用远端音频的 AnalyserNode 能量驱动口型（不按文本时长估算）
 * - 按动作表随机选择 clip，使用 AnimationMixer crossFade 平滑过渡
 *
 * 前端只依赖 three / @pixiv/three-vrm / @pixiv/three-vrm-animation 的
 * 本地 vendored 构建产物，由页面 importmap 解析裸模块名。
 */

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import {
  VRMAnimationLoaderPlugin,
  createVRMAnimationClip,
} from '@pixiv/three-vrm-animation';

const TARGET_FPS = 30;
const BLINK_MIN_GAP = 2.4;
const BLINK_MAX_GAP = 4.8;
const BLINK_DURATION = 0.16;
const BASE_FADE = 0.5;
const ACTION_FADE = 0.25;
const MOUTH_ATTACK = 1.0;
const MOUTH_RELEASE = 3.6;
const SILENCE_HOLD = 0.45;

export class KangbanAvatar {
  /**
   * @param {object} options
   * @param {HTMLElement} options.host      画布宿主容器
   * @param {string} [options.actionsUrl]   动作表地址
   * @param {string} [options.avatarsUrl]   形象注册表地址
   * @param {string} [options.avatarId]     启动时指定形象 id（缺省用注册表默认）
   * @param {(status: object) => void} [options.onStatus] 加载状态回调
   */
  constructor({
    host,
    actionsUrl = '/actions.json',
    avatarsUrl = '/avatars.json',
    avatarId = null,
    onStatus = null,
  }) {
    this.host = host;
    this.actionsUrl = actionsUrl;
    this.avatarsUrl = avatarsUrl;
    this.initialAvatarId = avatarId;
    this.onStatus = onStatus;

    this.catalog = null;
    this.avatars = [];
    this.avatarsById = new Map();
    this.defaultAvatarId = null;
    this.avatarId = null;
    this.avatarDef = null;
    this.switching = false;
    this.baseRotationY = 0;
    this.unavailableActions = new Set();
    this.assetRoot = '/vrm/animations/common/';
    this.catalogActions = new Map();
    this.actions = new Map();
    this.baseClips = { idle: [], talking: [] };

    this.renderer = null;
    this.scene = null;
    this.camera = null;
    this.clock = null;
    this.vrm = null;
    this.mixer = null;
    this.canvas = null;
    this.resizeObserver = null;
    this.raf = null;
    this.lastFrame = 0;

    this.mode = 'idle'; // idle | calling | in_call | off
    this.speaking = false;
    this.mouthLevel = 0;
    this.silenceTime = 0;

    this.baseMode = 'idle';
    this.baseAction = null;
    this.idleAction = null;
    this.talkingActions = [];

    this.currentAction = null;
    this.currentDef = null;
    this.desiredBase = 'idle';
    this.lastPlayedAt = new Map();

    this.audioCtx = null;
    this.sourceNode = null;
    this.analyser = null;
    this.timeData = null;

    this.blinkGap = BLINK_MIN_GAP;
    this.blinkPhase = -1;
    this.blinkElapsed = 0;

    this.clipCache = new Map();
    this.actionCache = new Map();
    this.loadingClips = new Map();
    this.lastClipByAction = new Map();

    // 舞台背景层（注册表 stage_url 可选；纯装饰，加载失败不影响人物链路）
    this.stage = null;
    this.stageUrl = null;
    this.stageLoading = false;

    this.disposed = false;
  }

  _status(phase, message, extra = {}) {
    if (typeof this.onStatus === 'function') {
      try {
        this.onStatus({ phase, message, ...extra });
      } catch (_err) {
        // UI 回调异常不影响渲染
      }
    }
  }

  async init() {
    this._initScene();
    this._startLoop();
    await this._loadCatalog();
    await this._loadAvatars();
    await this._activateInitialAvatar();
    return this;
  }

  _initScene() {
    const canvas = document.createElement('canvas');
    canvas.className = 'kb-avatar-canvas';
    canvas.setAttribute('aria-hidden', 'true');
    this.canvas = canvas;
    this.host.appendChild(canvas);

    this.renderer = new THREE.WebGLRenderer({
      canvas,
      alpha: true,
      antialias: true,
      powerPreference: 'low-power',
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.NoToneMapping;

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(32, 1, 0.05, 50);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8a9099, 2.2));
    const key = new THREE.DirectionalLight(0xffffff, 1.8);
    key.position.set(1.2, 2.4, 2.2);
    this.scene.add(key);
    const fill = new THREE.DirectionalLight(0xd7f0e4, 0.6);
    fill.position.set(-1.5, 1.4, 1.8);
    this.scene.add(fill);

    this.clock = new THREE.Clock();
    this.resizeObserver = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      if (box) this.resize(box.width, box.height);
    });
    this.resizeObserver.observe(this.canvas);
    this._resizeToHost();
    // 首帧布局可能尚未完成，下一帧再校正一次
    requestAnimationFrame(() => this._resizeToHost());
  }

  async _loadCatalog() {
    const response = await fetch(this.actionsUrl, { cache: 'no-cache' });
    if (!response.ok) {
      throw new Error(`动作表加载失败: HTTP ${response.status}`);
    }
    const data = await response.json();
    this.catalog = data;
    this.assetRoot = data.asset_root || '/vrm/animations/common/';
    for (const item of data.actions || []) {
      this.catalogActions.set(item.id, item);
      this.actions.set(item.id, item);
    }
  }

  // ============================== 形象注册表 ==============================

  async _loadAvatars() {
    let data = null;
    try {
      const response = await fetch(this.avatarsUrl, { cache: 'no-cache' });
      if (!response.ok) {
        throw new Error(`形象注册表加载失败: HTTP ${response.status}`);
      }
      data = await response.json();
    } catch (error) {
      console.error('[avatar] 形象注册表不可用，使用动作表兜底形象', error);
      this._applyFallbackAvatars(`形象注册表不可用：${error?.message || error}`);
      return;
    }
    this._applyAvatars(data);
  }

  _applyFallbackAvatars(reason) {
    // 注册表整体不可用时的兜底：只从 actions.json 的 model_url 推导单个形象，
    // 不在 JS 里硬编码任何形象列表。
    const modelUrl = this.catalog?.model_url;
    if (!modelUrl) {
      this._status('error', `无法加载任何形象：${reason}`);
      return;
    }
    const def = {
      id: 'default',
      name: '默认形象',
      model_url: modelUrl,
      animation_root: this.catalog.asset_root || '/vrm/animations/common/',
    };
    this.avatars = [def];
    this.avatarsById = new Map([[def.id, def]]);
    this.defaultAvatarId = def.id;
    this._status('error', `${reason}，已回落到动作表内置形象`);
  }

  _applyAvatars(data) {
    // 舞台背景层：注册表顶层可选 stage_url（纯装饰，失败只记日志不报状态错误）；
    // stage_scale=目标脚印宽度(米,默认6)、stage_floor=缩放后舞台最低点高度(默认-0.55)
    if (typeof data.stage_url === 'string' && data.stage_url) {
      this.stageUrl = data.stage_url;
      this._stageScale = Number(data.stage_scale);
      this._stageFloor = Number(data.stage_floor);
      this._loadStage();
    }
    const errors = this._validateAvatarSchema(data);
    for (const message of errors) {
      console.error('[avatar] avatars.json 校验失败：' + message);
    }
    if (!this.avatars.length) {
      this._applyFallbackAvatars('avatars.json 没有可用形象');
      return;
    }
    if (errors.length) {
      this._status('error', `形象注册表有 ${errors.length} 处问题，已使用可用形象`);
    }
  }

  /** 校验注册表并只保留合法条目；返回所有错误描述 */
  _validateAvatarSchema(data) {
    const errors = [];
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      errors.push('根节点必须是对象');
      this.avatars = [];
      this.avatarsById = new Map();
      this.defaultAvatarId = null;
      return errors;
    }
    if (data.version !== 1) {
      errors.push(`version 必须为 1（当前 ${JSON.stringify(data.version)}）`);
    }
    if (!Array.isArray(data.avatars)) {
      errors.push('avatars 必须是数组');
    }
    const list = Array.isArray(data.avatars) ? data.avatars : [];
    const seen = new Set();
    const valid = [];
    list.forEach((item, index) => {
      const itemErrors = [];
      if (!item || typeof item !== 'object' || Array.isArray(item)) {
        errors.push(`avatars[${index}] 不是对象`);
        return;
      }
      const id = typeof item.id === 'string' ? item.id.trim() : '';
      if (!id) {
        itemErrors.push('缺少 id');
      } else if (!/^[a-z0-9_-]+$/.test(id)) {
        itemErrors.push(`id「${id}」格式非法（只允许小写字母/数字/_/-）`);
      } else if (seen.has(id)) {
        itemErrors.push(`id「${id}」重复`);
      }
      if (typeof item.name !== 'string' || !item.name.trim()) {
        itemErrors.push('缺少 name');
      }
      if (typeof item.model_url !== 'string' || !item.model_url.trim()) {
        itemErrors.push('缺少 model_url');
      } else if (!/\.vrm(?:$|\?)/i.test(item.model_url)) {
        itemErrors.push(`model_url「${item.model_url}」不是 .vrm 文件`);
      }
      if (item.scale !== undefined && !(Number.isFinite(item.scale) && item.scale > 0)) {
        itemErrors.push('scale 必须是正数');
      }
      if (item.rotation_y !== undefined && !Number.isFinite(item.rotation_y)) {
        itemErrors.push('rotation_y 必须是数字');
      }
      if (item.camera !== undefined && (typeof item.camera !== 'object' || item.camera === null)) {
        itemErrors.push('camera 必须是对象');
      }
      if (item.supported_actions !== undefined && !Array.isArray(item.supported_actions)) {
        itemErrors.push('supported_actions 必须是数组');
      }
      if (item.action_clips !== undefined && (typeof item.action_clips !== 'object' || item.action_clips === null)) {
        itemErrors.push('action_clips 必须是对象');
      }
      if (item.base_clips !== undefined && (typeof item.base_clips !== 'object' || item.base_clips === null)) {
        itemErrors.push('base_clips 必须是对象');
      }
      if (itemErrors.length) {
        errors.push(`avatars[${index}]：${itemErrors.join('；')}`);
        return;
      }
      seen.add(id);
      valid.push(item);
    });

    const defaultId = typeof data.default_avatar_id === 'string' ? data.default_avatar_id : '';
    let effectiveDefault = defaultId;
    if (!defaultId) {
      errors.push('缺少 default_avatar_id');
    } else if (!seen.has(defaultId)) {
      errors.push(`default_avatar_id「${defaultId}」指向不存在的形象`);
    }
    if (!seen.has(effectiveDefault)) {
      effectiveDefault = valid[0]?.id || '';
    }

    this.avatars = valid;
    this.avatarsById = new Map(valid.map((item) => [item.id, item]));
    this.defaultAvatarId = effectiveDefault || null;
    return errors;
  }

  getAvatar() {
    return this.avatarId;
  }

  listAvatars() {
    return this.avatars.map((item) => ({
      id: item.id,
      name: item.name || item.id,
      description: item.description || '',
      model_url: item.model_url,
    }));
  }

  getMemoryStats() {
    const memory = this.renderer?.info?.memory;
    return {
      avatar: this.avatarId,
      textures: memory?.textures ?? 0,
      geometries: memory?.geometries ?? 0,
      programs: this.renderer?.info?.programs?.length ?? 0,
    };
  }

  /** 调试用：当前形象的包围盒、相机与网格统计 */
  describeAvatar() {
    if (!this.vrm) return { avatar: this.avatarId, missing: true };
    const box = new THREE.Box3().setFromObject(this.vrm.scene);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    let meshes = 0;
    let visibleMeshes = 0;
    let skinned = 0;
    this.vrm.scene.traverse((obj) => {
      if (!obj.isMesh) return;
      meshes += 1;
      if (obj.visible) visibleMeshes += 1;
      if (obj.isSkinnedMesh) skinned += 1;
    });
    const deg = (rad) => Math.round(THREE.MathUtils.radToDeg(rad) * 10) / 10;
    const wrap = (d) => ((((d + 180) % 360) + 360) % 360) - 180;
    const baseDeg = deg(this.baseRotationY);
    const totalDeg = deg(this.vrm.scene.rotation.y);
    return {
      avatar: this.avatarId,
      size: [size.x, size.y, size.z].map((v) => Math.round(v * 100) / 100),
      center: [center.x, center.y, center.z].map((v) => Math.round(v * 100) / 100),
      camera: [this.camera.position.x, this.camera.position.y, this.camera.position.z].map(
        (v) => Math.round(v * 100) / 100,
      ),
      facing: {
        base_deg: baseDeg,
        offset_deg: Number.isFinite(this.avatarDef?.rotation_y) ? this.avatarDef.rotation_y : 0,
        total_deg: totalDeg,
        // rotateVRM0 之后的姿态即该格式/该模型的「默认正面」，偏移 0 表示正对相机
        at_default_orientation: Math.abs(wrap(totalDeg - baseDeg)) <= 1,
      },
      meshes,
      visibleMeshes,
      skinned,
      memory: this.getMemoryStats(),
    };
  }

  _modelLoader() {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    return loader;
  }

  _animationLoader() {
    // 与参考工程一致：VRMA 只注册动画插件，避免 VRM 扩展再次介入
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMAnimationLoaderPlugin(parser));
    return loader;
  }

  _newLoader() {
    return this._modelLoader();
  }

  // ============================== 形象切换 ==============================

  async _activateInitialAvatar() {
    const requested =
      (this.initialAvatarId && this.avatarsById.get(this.initialAvatarId)) || null;
    const preferred =
      requested || this.avatarsById.get(this.defaultAvatarId) || this.avatars[0] || null;
    if (!preferred) {
      this._status('error', '没有可用形象');
      return { ok: false };
    }
    const queue = [preferred];
    for (const item of this.avatars) {
      if (item !== preferred) queue.push(item);
    }
    for (const def of queue) {
      const result = await this._activateAvatar(def);
      if (result.ok) return result;
      this._status('error', `形象「${def.name}」加载失败：${result.error}`, {
        avatar: def.id,
      });
    }
    this._status('error', '所有形象均加载失败，页面仍可操作但不显示模型');
    return { ok: false };
  }

  /**
   * 运行时切换形象。
   * @param {string} id 注册表里的形象 id
   * @returns {Promise<{ok:boolean,id?:string,error?:string,switch_ms?:number,fallback?:boolean}>}
   */
  async setAvatar(id) {
    const target = this.avatarsById.get(String(id || ''));
    if (!target) {
      const message = `未知形象 id：${id}`;
      console.error('[avatar] ' + message);
      this._status('error', message);
      return { ok: false, error: message };
    }
    const result = await this._activateAvatar(target);
    if (!result.ok && !result.busy && target.id !== this.defaultAvatarId) {
      const fallback = this.avatarsById.get(this.defaultAvatarId);
      if (fallback) {
        this._status(
          'error',
          `形象「${target.name}」切换失败，回落到默认形象「${fallback.name}」`,
          { avatar: target.id, error: result.error },
        );
        const retry = await this._activateAvatar(fallback);
        return { ...retry, fallback: true, requested: target.id };
      }
    }
    return result;
  }

  /** 先加载新模型，成功后才销毁旧模型；任何失败都回滚，不白屏 */
  async _activateAvatar(def) {
    if (this.switching) {
      return { ok: false, error: '正在切换形象，请稍后再试', busy: true };
    }
    if (this.avatarId === def.id && this.vrm) {
      return { ok: true, id: def.id, reused: true, switch_ms: 0 };
    }
    this.switching = true;
    const started = performance.now();
    const before = this.getMemoryStats();
    this._status('switching', `正在加载形象「${def.name}」…`, { avatar: def.id });
    let loaded = null;
    let snapshot = null;
    try {
      const resolved = this._resolveAvatarAnimations(def);
      await this._validateClipAvailability(def, resolved);
      loaded = await this._loadVrm(def);
      snapshot = this._snapshotCurrent();
      this._installLoaded(def, resolved, loaded);
      const base = await this._loadBaseClips(resolved);
      if (!base.ok) {
        throw new Error('该形象没有可用的待机动画');
      }
      const switchMs = Math.round(performance.now() - started);
      if (snapshot?.vrm) this._disposeSnapshot(snapshot);
      this._applyAvatarState();
      this._status('ready', `数字人已就绪（${def.name}）`, {
        avatar: def.id,
        switch_ms: switchMs,
        actions: this.actions.size,
        unavailable_actions: [...this.unavailableActions],
      });
      return {
        ok: true,
        id: def.id,
        switch_ms: switchMs,
        before,
        after: this.getMemoryStats(),
      };
    } catch (error) {
      console.error(`[avatar] 形象「${def.name}」加载失败`, error);
      this._rollbackAvatar(loaded, snapshot);
      const message = error?.message || String(error);
      this._status('error', `形象「${def.name}」加载失败：${message}`, { avatar: def.id });
      return { ok: false, id: def.id, error: message, before, after: this.getMemoryStats() };
    } finally {
      this.switching = false;
    }
  }

  async _loadVrm(def) {
    const loader = this._newLoader();
    const url = def.model_url;
    const gltf = await new Promise((resolve, reject) => {
      loader.load(
        url,
        resolve,
        (event) => {
          const total = event.total || 0;
          const pct = total ? Math.round((event.loaded / total) * 100) : 0;
          this._status('loading', `正在加载形象「${def.name}」${pct}%`, {
            progress: pct,
            avatar: def.id,
          });
        },
        (error) => reject(error),
      );
    });

    const vrm = gltf.userData.vrm;
    if (!vrm) {
      throw new Error(`模型不是有效的 VRM：${url}`);
    }
    VRMUtils.removeUnnecessaryVertices(gltf.scene);
    VRMUtils.combineSkeletons(gltf.scene);
    VRMUtils.rotateVRM0(vrm);
    // rotateVRM0 会对 VRM 0.0 模型绕 Y 轴转 π（VRM0 原始面向 -Z，相机在 +Z，
    // 必须转半圈才正面朝人）。注册表的 rotation_y 是「相对该默认朝向的校准偏移」，
    // 只能叠加在 rotateVRM0 的结果上；直接赋值会把这个 π 覆盖成 0，模型就会背对镜头。
    this.baseRotationY = vrm.scene.rotation.y;
    const scale = Number.isFinite(def.scale) && def.scale > 0 ? def.scale : 1;
    vrm.scene.scale.setScalar(scale);
    const rotationOffset = THREE.MathUtils.degToRad(
      Number.isFinite(def.rotation_y) ? def.rotation_y : 0,
    );
    vrm.scene.rotation.y = this.baseRotationY + rotationOffset;
    vrm.scene.traverse((obj) => {
      obj.frustumCulled = false;
    });
    const mixer = new THREE.AnimationMixer(vrm.scene);
    return { vrm, mixer, gltf };
  }

  _frameCamera(def = null) {
    const cam = (def && def.camera) || {};
    const box = new THREE.Box3().setFromObject(this.vrm.scene);
    const size = new THREE.Vector3();
    box.getSize(size);
    // 注意不得设最低身高钳制：Q 版模型（小鲨鱼 0.78m / 阿乔魔女 0.88m）被按 1.2m
    // 取景会整体错位，只拍到一个头（Day4 实测）；仅防退化值
    const height = size.y > 0.3 ? size.y : 1.6;
    // 取景一律基于包围盒实际最低点：若某模型网格原点不在脚底，可自动校正
    const baseY = Number.isFinite(box.min.y) ? box.min.y : 0;
    // 默认取景 = 全身构图（视窗高 ≈1.26×身高，头脚各留 ~13%）；Day4 起所有模型统一全身
    const lookAtRatio = Number.isFinite(cam.look_at_ratio) ? cam.look_at_ratio : 0.5;
    const heightRatio = Number.isFinite(cam.height_ratio) ? cam.height_ratio : 0.5;
    const distanceRatio = Number.isFinite(cam.distance_ratio) ? cam.distance_ratio : 2.2;
    this.camera.fov = Number.isFinite(cam.fov) ? cam.fov : 32;
    this.camera.position.set(0, baseY + height * heightRatio, height * distanceRatio);
    this.camera.lookAt(0, baseY + height * lookAtRatio, 0);
    this.camera.updateProjectionMatrix();
  }

  /** 加载舞台背景层（注册表 stage_url）；纯装饰：失败仅 console，不影响人物链路 */
  async _loadStage() {
    if (this.stageLoading || this.stage || !this.scene) return;
    this.stageLoading = true;
    try {
      const gltf = await new Promise((resolve, reject) => {
        this._newLoader().load(this.stageUrl, resolve, undefined, reject);
      });
      if (this.disposed) return;
      const root = gltf.scene;
      const box = new THREE.Box3().setFromObject(root);
      const size = new THREE.Vector3();
      box.getSize(size);
      // 自动缩放到可用占位（默认脚印 6m），注册表 stage_scale/stage_floor 可校准
      const targetFootprint = Number.isFinite(this._stageScale) ? this._stageScale : 6;
      const floorY = Number.isFinite(this._stageFloor) ? this._stageFloor : -0.55;
      const footprint = Math.max(size.x, size.z, 0.001);
      const scale = targetFootprint / footprint;
      root.scale.setScalar(scale);
      const scaledBox = new THREE.Box3().setFromObject(root);
      const scaledSize = new THREE.Vector3();
      const scaledCenter = new THREE.Vector3();
      scaledBox.getSize(scaledSize);
      scaledBox.getCenter(scaledCenter);
      root.position.set(-scaledCenter.x, floorY - scaledBox.min.y, -scaledCenter.z);
      root.traverse((obj) => {
        obj.frustumCulled = false;
      });
      this.stage = root;
      this.scene.add(root);
      console.info('[avatar] 舞台背景已加载', this.stageUrl, 'footprint≈' + (footprint * scale).toFixed(1) + 'm');
    } catch (error) {
      console.error('[avatar] 舞台背景加载失败（不影响人物）', error);
    } finally {
      this.stageLoading = false;
    }
  }

  _snapshotCurrent() {
    return {
      avatarId: this.avatarId,
      avatarDef: this.avatarDef,
      baseRotationY: this.baseRotationY,
      assetRoot: this.assetRoot,
      actions: this.actions,
      baseClips: this.baseClips,
      unavailableActions: this.unavailableActions,
      vrm: this.vrm,
      mixer: this.mixer,
      clipCache: this.clipCache,
      actionCache: this.actionCache,
      loadingClips: this.loadingClips,
      lastClipByAction: this.lastClipByAction,
      lastPlayedAt: this.lastPlayedAt,
      idleAction: this.idleAction,
      talkingActions: this.talkingActions,
      baseAction: this.baseAction,
      baseMode: this.baseMode,
      currentAction: this.currentAction,
      currentDef: this.currentDef,
      desiredBase: this.desiredBase,
    };
  }

  _restoreSnapshot(snapshot) {
    this.avatarId = snapshot.avatarId;
    this.avatarDef = snapshot.avatarDef;
    this.baseRotationY = snapshot.baseRotationY ?? 0;
    this.assetRoot = snapshot.assetRoot;
    this.actions = snapshot.actions;
    this.baseClips = snapshot.baseClips;
    this.unavailableActions = snapshot.unavailableActions;
    this.clipCache = snapshot.clipCache;
    this.actionCache = snapshot.actionCache;
    this.loadingClips = snapshot.loadingClips;
    this.lastClipByAction = snapshot.lastClipByAction;
    this.lastPlayedAt = snapshot.lastPlayedAt;
    this.idleAction = snapshot.idleAction;
    this.talkingActions = snapshot.talkingActions;
    this.baseAction = snapshot.baseAction;
    this.baseMode = snapshot.baseMode;
    this.currentAction = snapshot.currentAction;
    this.currentDef = snapshot.currentDef;
    this.desiredBase = snapshot.desiredBase;
    this.vrm = snapshot.vrm;
    this.mixer = snapshot.mixer;
    if (this.vrm && !this.vrm.scene.parent) {
      this.scene.add(this.vrm.scene);
    }
    this._setMouth(this.mouthLevel);
  }

  _installLoaded(def, resolved, loaded) {
    if (this.vrm) this.scene.remove(this.vrm.scene);
    this.avatarId = def.id;
    this.avatarDef = def;
    this.assetRoot = resolved.animationRoot;
    this.actions = resolved.actions;
    this.baseClips = resolved.base;
    this.unavailableActions = resolved.unavailable;
    this.clipCache = new Map();
    this.actionCache = new Map();
    this.loadingClips = new Map();
    this.lastClipByAction = new Map();
    this.lastPlayedAt = new Map();
    this.idleAction = null;
    this.talkingActions = [];
    this.baseAction = null;
    this.baseMode = 'idle';
    this.currentAction = null;
    this.currentDef = null;
    this.vrm = loaded.vrm;
    this.mixer = loaded.mixer;
    this.mixer.addEventListener('finished', (event) => this._onActionFinished(event));
    if (this.vrm.lookAt) {
      this.vrm.lookAt.target = this.camera;
    }
    this.scene.add(this.vrm.scene);
    this._frameCamera(def);
  }

  _rollbackAvatar(loaded, snapshot) {
    const newVrm = loaded?.vrm;
    if (newVrm) {
      try {
        this.scene.remove(newVrm.scene);
      } catch (_error) {
        // ignore
      }
      try {
        VRMUtils.deepDispose(newVrm.scene);
      } catch (_error) {
        // ignore
      }
    }
    if (this.mixer && this.mixer !== snapshot?.mixer) {
      try {
        this.mixer.stopAllAction();
      } catch (_error) {
        // ignore
      }
    }
    if (!snapshot) {
      this.vrm = null;
      this.mixer = null;
      this.avatarId = null;
      this.avatarDef = null;
      this.idleAction = null;
      this.talkingActions = [];
      this.baseAction = null;
      this.currentAction = null;
      this.currentDef = null;
      this.clipCache = new Map();
      this.actionCache = new Map();
      this.loadingClips = new Map();
      return;
    }
    this._restoreSnapshot(snapshot);
  }

  _disposeSnapshot(snapshot) {
    if (!snapshot?.vrm) return;
    try {
      snapshot.mixer?.stopAllAction();
      snapshot.mixer?.uncacheRoot(snapshot.vrm.scene);
    } catch (_error) {
      // ignore
    }
    try {
      this.scene.remove(snapshot.vrm.scene);
    } catch (_error) {
      // ignore
    }
    try {
      VRMUtils.deepDispose(snapshot.vrm.scene);
    } catch (error) {
      console.warn('[avatar] 旧模型释放失败', error);
    }
  }

  _applyAvatarState() {
    const desired = this.mode === 'in_call' && this.speaking ? 'talking' : 'idle';
    this.desiredBase = desired;
    this.baseMode = null;
    this.blinkPhase = -1;
    this.blinkGap = BLINK_MIN_GAP;
    this._setMouth(this.mouthLevel);
    this._setBase(desired, { force: true });
  }

  _absUrl(file, root) {
    if (/^(https?:)?\/\//.test(file) || file.startsWith('/')) return file;
    return `${root}${file}`;
  }

  /**
   * 把 catalog 的动作解析成该形象可用的副本。
   * - supported_actions 限定可用动作；action_clips 可覆盖 clip 列表
   * - base_clips 可覆盖 idle/talking 池
   * 不修改 actions.json 的协议语义，action id 不变。
   */
  _resolveAvatarAnimations(def) {
    let root = def.animation_root || this.catalog?.asset_root || '/vrm/animations/common/';
    if (!root.endsWith('/')) root += '/';
    const toClip = (entry) => {
      const file = typeof entry === 'string' ? entry : entry?.file;
      return file ? { file: this._absUrl(file, root) } : null;
    };
    const supported = Array.isArray(def.supported_actions)
      ? new Set(def.supported_actions)
      : null;
    const unavailable = new Set();
    const actions = new Map();
    for (const [id, item] of this.catalogActions) {
      if (supported && !supported.has(id)) {
        unavailable.add(id);
        continue;
      }
      const override = def.action_clips && def.action_clips[id];
      const source = Array.isArray(override) && override.length ? override : item.clips || [];
      const clips = source.map(toClip).filter(Boolean);
      if (!clips.length) {
        unavailable.add(id);
        continue;
      }
      actions.set(id, { ...item, clips });
    }
    const baseOverride = def.base_clips || {};
    const idleSource =
      Array.isArray(baseOverride.idle) && baseOverride.idle.length
        ? baseOverride.idle
        : this.catalog?.base?.idle?.clips || [];
    const talkingSource =
      Array.isArray(baseOverride.talking) && baseOverride.talking.length
        ? baseOverride.talking
        : this.catalog?.base?.talking?.clips || [];
    return {
      animationRoot: root,
      actions,
      base: {
        idle: idleSource.map(toClip).filter(Boolean),
        talking: talkingSource.map(toClip).filter(Boolean),
      },
      unavailable,
    };
  }

  /** 用 HEAD 检查声明的动画文件是否存在；缺失的明确打日志并从动作表跳过 */
  async _validateClipAvailability(def, resolved) {
    const owners = new Map();
    for (const [id, item] of resolved.actions) {
      for (const clip of item.clips) {
        if (!owners.has(clip.file)) owners.set(clip.file, new Set());
        owners.get(clip.file).add(id);
      }
    }
    for (const clip of resolved.base.idle) {
      if (!owners.has(clip.file)) owners.set(clip.file, new Set());
      owners.get(clip.file).add('__base_idle');
    }
    for (const clip of resolved.base.talking) {
      if (!owners.has(clip.file)) owners.set(clip.file, new Set());
      owners.get(clip.file).add('__base_talking');
    }
    const missing = await Promise.all(
      [...owners.keys()].map(async (url) => ((await this._probe(url)) ? null : url)),
    );
    for (const url of missing.filter(Boolean)) {
      const ownersOfUrl = owners.get(url);
      for (const id of ownersOfUrl) {
        if (id === '__base_idle' || id === '__base_talking') {
          console.error(
            `[avatar] 形象「${def.name}」缺少${id === '__base_idle' ? '待机' : '说话'}动画：${url}`,
          );
          continue;
        }
        console.warn(
          `[avatar] 形象「${def.name}」缺少动画文件 ${url}，动作 ${id} 已跳过`,
        );
        resolved.actions.delete(id);
        resolved.unavailable.add(id);
      }
    }
  }

  async _probe(url) {
    try {
      const response = await fetch(url, { method: 'HEAD', cache: 'no-store' });
      // 405/501 表示服务端不支持 HEAD，交给实际加载阶段判断
      if (response.ok || response.status === 405 || response.status === 501) {
        return true;
      }
      return false;
    } catch (_error) {
      return false;
    }
  }

  _clipUrl(file) {
    if (/^https?:\/\//.test(file) || file.startsWith('/')) {
      return file;
    }
    return `${this.assetRoot}${file}`;
  }

  async _loadClip(file) {
    if (this.clipCache.has(file)) {
      return this.clipCache.get(file);
    }
    if (this.loadingClips.has(file)) {
      return this.loadingClips.get(file);
    }
    const task = (async () => {
      const gltf = await new Promise((resolve, reject) => {
        this._animationLoader().load(this._clipUrl(file), resolve, undefined, reject);
      });
      const animations = gltf.userData.vrmAnimations;
      if (!animations || !animations.length) {
        throw new Error(`动画不是有效的 VRMA: ${file}`);
      }
      const clip = createVRMAnimationClip(animations[0], this.vrm);
      this.clipCache.set(file, clip);
      return clip;
    })();
    this.loadingClips.set(file, task);
    try {
      return await task;
    } finally {
      this.loadingClips.delete(file);
    }
  }

  async _actionForClip(file) {
    if (this.actionCache.has(file)) {
      return this.actionCache.get(file);
    }
    const clip = await this._loadClip(file);
    const action = this.mixer.clipAction(clip);
    this.actionCache.set(file, action);
    return action;
  }

  async _loadBaseClips(resolved) {
    const idleClips = [...(resolved?.base?.idle || [])];
    this.idleAction = null;
    this.talkingActions = [];
    this.baseAction = null;
    this.baseMode = 'idle';
    let idleLoaded = false;
    while (idleClips.length && !idleLoaded) {
      const clip = idleClips.splice(Math.floor(Math.random() * idleClips.length), 1)[0];
      try {
        const action = await this._actionForClip(clip.file);
        action.setLoop(THREE.LoopRepeat, Infinity);
        action.clampWhenFinished = false;
        action.setEffectiveWeight(1);
        action.reset().play();
        this.idleAction = action;
        this.baseAction = action;
        idleLoaded = true;
      } catch (error) {
        console.error(`[avatar] 待机动画加载失败，尝试下一个：${clip.file}`, error);
      }
    }
    if (!idleLoaded) {
      console.error(`[avatar] 形象「${this.avatarDef?.name || this.avatarId}」没有可用待机动画`);
    }
    // 说话动画在后台预加载，避免首次开口时卡顿
    for (const clip of resolved?.base?.talking || []) {
      this._actionForClip(clip.file)
        .then((action) => {
          action.setLoop(THREE.LoopRepeat, Infinity);
          action.setEffectiveWeight(0);
          this.talkingActions.push(action);
        })
        .catch((error) =>
          console.warn(`[avatar] 说话动画加载失败，已跳过：${clip.file}`, error),
        );
    }
    // 高频动作后台预加载；失败只移除该 clip，不阻塞页面
    const preload = ['nod', 'shake', 'think', 'tilt', 'wave', 'clap'];
    for (const actionId of preload) {
      const item = this.actions.get(actionId);
      for (const clip of item?.clips || []) {
        this._actionForClip(clip.file).catch((error) => {
          console.warn(
            `[avatar] 形象「${this.avatarDef?.name || this.avatarId}」动作 ${actionId} ` +
              `动画加载失败，已跳过：${clip.file}`,
            error,
          );
          this._dropClip(actionId, clip.file);
        });
      }
    }
    return { ok: idleLoaded, idle: idleLoaded, talking: this.talkingActions.length > 0 };
  }

  /** 移除某个动作下加载失败的 clip；全部失败时该动作不可用 */
  _dropClip(actionId, file) {
    const item = this.actions.get(actionId);
    if (!item) return;
    const remaining = (item.clips || []).filter((clip) => clip.file !== file);
    if (remaining.length) {
      this.actions.set(actionId, { ...item, clips: remaining });
    } else {
      this.actions.delete(actionId);
      this.unavailableActions.add(actionId);
    }
  }

  _pickRandom(list, avoid = null) {
    if (!list.length) return null;
    if (list.length === 1) return list[0];
    for (let i = 0; i < 4; i += 1) {
      const item = list[Math.floor(Math.random() * list.length)];
      if (item !== avoid) return item;
    }
    return list[0];
  }

  _pickClip(def) {
    const clips = def?.clips || [];
    if (!clips.length) return null;
    if (clips.length === 1) return clips[0];
    const last = this.lastClipByAction.get(def.id);
    const chosen = this._pickRandom(clips, last);
    this.lastClipByAction.set(def.id, chosen);
    return chosen;
  }

  _setBase(mode, { force = false } = {}) {
    if (!this.vrm || !this.mixer) return;
    if (this.currentAction && !force) {
      this.desiredBase = mode;
      return;
    }
    if (this.baseMode === mode && this.baseAction && !force) return;

    let next = null;
    if (mode === 'talking') {
      next = this._pickRandom(this.talkingActions, this.baseAction);
      // 该形象没有可用的说话动画时退回 idle，避免停住不动
      if (!next) {
        mode = 'idle';
        next = this.idleAction;
      }
    } else {
      next = this.idleAction;
    }
    if (!next) return;

    next.reset();
    next.setLoop(THREE.LoopRepeat, Infinity);
    next.clampWhenFinished = false;
    next.setEffectiveTimeScale(1);
    next.setEffectiveWeight(1);
    next.fadeIn(BASE_FADE).play();
    if (this.baseAction && this.baseAction !== next) {
      this.baseAction.fadeOut(BASE_FADE);
    }
    this.baseAction = next;
    this.baseMode = mode;
  }

  _restoreBase() {
    let desired = this.desiredBase || (this.speaking ? 'talking' : 'idle');
    let base =
      desired === 'talking'
        ? this._pickRandom(this.talkingActions, this.baseAction)
        : this.idleAction;
    if (!base && desired === 'talking') {
      desired = 'idle';
      base = this.idleAction;
    }
    if (!base) return;
    base.reset();
    base.setLoop(THREE.LoopRepeat, Infinity);
    base.clampWhenFinished = false;
    base.setEffectiveTimeScale(1);
    base.setEffectiveWeight(1);
    base.fadeIn(0.3).play();
    this.baseAction = base;
    this.baseMode = desired;
  }

  /**
   * 播放一个动作；同一语义下从 clips 里随机选一条。
   * @param {string} actionId
   * @param {string|number|null} param 可选重复次数 1-3
   * @returns {Promise<boolean>}
   */
  async playAction(actionId, param = null) {
    const def = this.actions.get(String(actionId || '').toLowerCase());
    if (!def) {
      console.warn('[avatar] 未知动作或当前形象不支持，已忽略', actionId);
      return false;
    }
    if (!this.vrm || !this.mixer) return false;

    const now = performance.now() / 1000;
    const last = this.lastPlayedAt.get(def.id) || 0;
    if (now - last < (def.min_interval_seconds || 0)) return false;

    if (def.stackable === false && this.speaking) {
      console.info('[avatar] 动作仅允许空闲播放，当前正在说话，跳过', def.id);
      return false;
    }
    if (
      this.currentAction &&
      this.currentDef &&
      !this.currentDef.interruptible &&
      (def.priority || 0) <= (this.currentDef.priority || 0)
    ) {
      console.info('[avatar] 当前动作不可打断且优先级更高，跳过', def.id);
      return false;
    }

    let repeats = 1;
    if (param !== null && param !== undefined && param !== '') {
      const parsed = Number.parseInt(String(param), 10);
      if (Number.isFinite(parsed)) {
        repeats = Math.min(3, Math.max(1, parsed));
      }
    }

    const clip = this._pickClip(def);
    if (!clip) return false;

    let action;
    try {
      action = await this._actionForClip(clip.file);
    } catch (error) {
      console.error(`[avatar] 动作动画加载失败，已跳过 ${def.id}：${clip.file}`, error);
      this._dropClip(def.id, clip.file);
      return false;
    }

    const previous = this.baseAction;
    action.reset();
    action.setLoop(repeats > 1 ? THREE.LoopRepeat : THREE.LoopOnce, repeats);
    action.clampWhenFinished = true;
    action.setEffectiveTimeScale(1);
    action.setEffectiveWeight(1);
    action.fadeIn(ACTION_FADE).play();
    if (previous && previous !== action) {
      previous.fadeOut(ACTION_FADE);
    }

    this.currentAction = action;
    this.currentDef = def;
    this.lastPlayedAt.set(def.id, now);
    this.desiredBase = this.speaking ? 'talking' : 'idle';
    return true;
  }

  _onActionFinished(event) {
    if (event.action !== this.currentAction) return;
    event.action.fadeOut(ACTION_FADE);
    this.currentAction = null;
    this.currentDef = null;
    this._restoreBase();
  }

  /**
   * 切换页面状态：idle / calling / in_call / off
   * - idle/calling：强制待机（拨号等待期间不说话）
   * - in_call：由音频能量自动切 idle/talking
   * - off：关闭口型并回到待机
   */
  setState(state) {
    const next = ['idle', 'calling', 'in_call', 'off'].includes(state) ? state : 'idle';
    this.mode = next;
    if (next === 'idle' || next === 'calling' || next === 'off') {
      this.speaking = false;
      this.silenceTime = 0;
      this._setMouth(0);
      if (this.currentAction) {
        this.currentAction.fadeOut(ACTION_FADE);
        this.currentAction = null;
        this.currentDef = null;
      }
      this.desiredBase = 'idle';
      this._setBase('idle', { force: true });
    }
  }

  async attachStream(stream) {
    if (!stream) return;
    try {
      if (!this.audioCtx) {
        this.audioCtx = new AudioContext();
      }
      if (this.audioCtx.state === 'suspended') {
        await this.audioCtx.resume();
      }
      this.detachStream();
      this.sourceNode = this.audioCtx.createMediaStreamSource(stream);
      this.analyser = this.audioCtx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.smoothingTimeConstant = 0.5;
      this.timeData = new Uint8Array(this.analyser.fftSize);
      // 只做分析，不接 destination，避免和页面 <audio> 重复播放
      this.sourceNode.connect(this.analyser);
    } catch (error) {
      console.warn('[avatar] 音频分析接入失败', error);
    }
  }

  detachStream() {
    try {
      this.sourceNode?.disconnect();
    } catch (_error) {
      // 已断开
    }
    this.sourceNode = null;
    this.analyser = null;
    this.timeData = null;
    this._setMouth(0);
  }

  _setMouth(level) {
    this.mouthLevel = Math.max(0, Math.min(1, level));
    const em = this.vrm?.expressionManager;
    if (!em) return;
    try {
      em.setValue('aa', this.mouthLevel * 0.85);
      em.setValue('ih', this.mouthLevel * 0.12);
    } catch (_error) {
      // 模型未定义该表情时忽略
    }
  }

  _updateMouth(dt) {
    let target = 0;
    if (this.analyser && this.timeData) {
      this.analyser.getByteTimeDomainData(this.timeData);
      let sum = 0;
      for (let i = 0; i < this.timeData.length; i += 1) {
        const value = (this.timeData[i] - 128) / 128;
        sum += value * value;
      }
      const rms = Math.sqrt(sum / this.timeData.length);
      target = Math.max(0, Math.min(1, (rms - 0.012) * 6.5));
    }
    if (target > this.mouthLevel) {
      this.mouthLevel += (target - this.mouthLevel) * Math.min(1, dt * 18);
    } else {
      this.mouthLevel = Math.max(target, this.mouthLevel - dt * MOUTH_RELEASE);
    }
    this._setMouth(this.mouthLevel);

    const speaking = this.mouthLevel > 0.06;
    this.speaking = speaking;
    if (this.mode !== 'in_call') return;
    if (speaking) {
      this.silenceTime = 0;
      this._setBase('talking');
    } else {
      this.silenceTime += dt;
      if (this.silenceTime >= SILENCE_HOLD) {
        this._setBase('idle');
      }
    }
  }

  _updateBlink(dt) {
    const em = this.vrm?.expressionManager;
    if (!em) return;
    if (this.blinkPhase < 0) {
      this.blinkGap -= dt;
      if (this.blinkGap <= 0) {
        this.blinkPhase = 0;
        this.blinkElapsed = 0;
      }
      return;
    }
    this.blinkElapsed += dt;
    const p = Math.min(1, this.blinkElapsed / BLINK_DURATION);
    const value = p < 0.5 ? p * 2 : (1 - p) * 2;
    try {
      em.setValue('blink', Math.max(0, value));
    } catch (_error) {
      // 模型未定义 blink 时忽略
    }
    if (p >= 1) {
      this.blinkPhase = -1;
      this.blinkGap = BLINK_MIN_GAP + Math.random() * (BLINK_MAX_GAP - BLINK_MIN_GAP);
      try {
        em.setValue('blink', 0);
      } catch (_error) {
        // ignore
      }
    }
  }

  _resizeToHost() {
    const width = this.canvas.clientWidth || this.host.clientWidth || 320;
    const height = this.canvas.clientHeight || this.host.clientHeight || 240;
    this.resize(width, height);
  }

  resize(width, height) {
    if (!this.renderer || !this.camera) return;
    const w = Math.max(1, Math.round(width || 1));
    const h = Math.max(1, Math.round(height || 1));
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  moveTo(host) {
    if (!host || host === this.host) return;
    this.host = host;
    host.appendChild(this.canvas);
    this.resizeObserver?.disconnect();
    this.resizeObserver = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      if (box) this.resize(box.width, box.height);
    });
    this.resizeObserver.observe(this.canvas);
    requestAnimationFrame(() => this._resizeToHost());
  }

  _startLoop() {
    if (this.raf !== null) return;
    this.lastFrame = performance.now();
    const tick = (now) => {
      if (this.disposed) return;
      this.raf = requestAnimationFrame(tick);
      if (now - this.lastFrame < 1000 / TARGET_FPS - 1) return;
      if (document.hidden) {
        this.lastFrame = now;
        return;
      }
      const dt = Math.min(0.1, (now - this.lastFrame) / 1000);
      this.lastFrame = now;
      if (this.vrm && this.mixer) {
        this._updateMouth(dt);
        this._updateBlink(dt);
        this.mixer.update(dt);
        this.vrm.update(dt);
      }
      this.renderer.render(this.scene, this.camera);
    };
    this.raf = requestAnimationFrame(tick);
  }

  dispose() {
    this.disposed = true;
    if (this.raf !== null) cancelAnimationFrame(this.raf);
    this.raf = null;
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    this.detachStream();
    try {
      this.audioCtx?.close();
    } catch (_error) {
      // ignore
    }
    this.audioCtx = null;
    this.mixer?.stopAllAction();
    if (this.vrm) {
      try {
        this.mixer?.uncacheRoot(this.vrm.scene);
        this.scene?.remove(this.vrm.scene);
        VRMUtils.deepDispose(this.vrm.scene);
      } catch (error) {
        console.warn('[avatar] 模型释放失败', error);
      }
      this.vrm = null;
      this.mixer = null;
    }
    this.renderer?.dispose();
    this.canvas?.remove();
  }
}
