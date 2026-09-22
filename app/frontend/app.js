'use strict';
(function () {
  // 本页在飞牛网关下是 /app/scrcpy-fnos/，直连时是 /。
  // 从当前路径推 base，两种情形都能用。
  var BASE = location.pathname.replace(/\/(index\.html)?$/, '');
  var $ = function (id) { return document.getElementById(id); };

  var canvas = $('screen');
  var ctx = canvas.getContext('2d');
  var placeholder = $('placeholder');

  // ==================== 每台设备一份设置 ====================
  // 按 serial 存 localStorage：同一台设备下次连还是这套参数，
  // 换一台设备互不影响（竞品也是这个做法）。
  var SET_KEY = 'scrcpy-fnos.settings';
  var DEFAULTS = {
    // 画质
    bitRate: 8000000, maxFps: 60, maxSize: 0, codec: 'auto',
    angle: 0, crop: '',
    // 音频
    audio: true, audioBitRate: 128000, audioCodec: 'aac', audioSource: 'output',
    // 画面与设备
    showTouches: true, stayAwake: false, powerOffOnClose: false,
    keepActive: false, startScreenOff: false,
    powerOn: true, clipboardAutosync: true, screenOffTimeout: 0
  };
  // 推荐配置：**不再是一组写死的码率常数**，而是「每像素每帧给多少 bit」。
  // 竞品那套的问题就在这儿 —— 720p 和 4K 都给 8 Mbps：同一个数，放 720p 上绰绰有余，
  // 放 1080p 上根本喂不饱，于是「原画」反而糊。现在码率按「真正要编多少像素 × 帧率」算，
  // 屏幕分辨率一变，推荐值跟着变。
  //   bpp  = 每像素每帧分到多少 bit（H.264 High 约 0.10 就算好；Baseline 没有 CABAC/B 帧，
  //          同样的观感要多给三到五成，所以「画质优先」给到 0.20）
  //   size = 目标高度（0 = 原画）
  //   min/max = 兜底范围，免得算出个离谱的数
  var PRESETS = {
    'quality':   { bpp: 0.20, maxFps: 60, maxSize: 0,    min: 4000000, max: 60000000 },
    'balanced':  { bpp: 0.12, maxFps: 60, maxSize: 1080, min: 3000000, max: 25000000 },
    'saver':     { bpp: 0.08, maxFps: 30, maxSize: 720,  min: 1200000, max: 10000000 },
    'max-saver': { bpp: 0.05, maxFps: 15, maxSize: 480,  min: 400000,  max: 3000000 }
  };

  // 设备的原生屏尺寸（/api/screen 拿的）。算推荐码率必须知道它 ——
  // 会话报上来的尺寸还叠了 max_size 之类的缩放，拿它当「这台设备原生多少像素」是错的。
  var deviceScreen = null;          // {width, height, density} 或 null

  // 按目标高度缩放：把屏幕照 maxSize（**目标高度**）缩一遍，宽度按原生宽高比推出来，
  // 保持原始比例；目标高度大于原生高度时就不放大（等于原画）。缩完的长宽都取偶数，
  // 编码器对奇数尺寸不友好。
  function scaleForTargetHeight(screen, targetH) {
    var w = (screen && screen.width) || 0, h = (screen && screen.height) || 0;
    if (!w || !h) { w = 1280; h = 720; }        // 问不到就按 720p 估，宁可少算也别乱给
    if (!targetH || targetH >= h) return { width: w, height: h };   // 原画 / 不放大
    var k = targetH / h;
    return {
      width: Math.max(2, Math.round(w * k / 2) * 2),
      height: Math.max(2, Math.round(targetH / 2) * 2)
    };
  }

  // 按「实际会编多少像素」算码率（**按目标高度缩放码率**，不是按最长边）。
  // angle 是当前屏幕旋转：90/270 时宽高互换，目标高度落在原来的宽度上；不传就按屏幕原始方向。
  // 例：超宽屏 3440×1440 选 1080p → 按 2580×1080 估码率，而不是按最长边缩。
  function presetBitRate(p, screen, angle) {
    var sc = screen;
    if ((angle === 90 || angle === 270) && screen) {
      sc = { width: screen.height, height: screen.width };
    }
    var d = scaleForTargetHeight(sc, p.maxSize);
    var bps = d.width * d.height * (p.maxFps || 30) * p.bpp;
    return Math.max(p.min, Math.min(p.max, Math.round(bps / 100000) * 100000));
  }

  // 码率在界面上是「数值 + 单位下拉」，内部一律换算成 bps 再发给服务端。
  // （scrcpy 收的就是 bps，裸写 8000000 谁也读不出来是 8 Mbps。）
  function setRateField(bps, idIn, idUnit) {
    var n = Number(bps) || 0;
    var v = $(idIn || 'setBitRate'), u = $(idUnit || 'setBitRateUnit');
    if (!v || !u) return;
    if (n && n < 1000000 && u.querySelector('option[value="1000"]')) {
      u.value = '1000';
      v.value = String(Math.round(n / 1000));
    } else {
      u.value = '1000000';
      v.value = String(Math.round(n / 100000) / 10);
    }
  }
  function readRateField(idIn, idUnit, fallback) {
    var v = parseFloat($(idIn || 'setBitRate').value);
    var unit = parseInt($(idUnit || 'setBitRateUnit').value, 10) || 1000000;
    if (!isFinite(v) || v <= 0) return fallback;
    return Math.max(8000, Math.round(v * unit));
  }
  function fmtRate(bps) {
    var n = Number(bps) || 0;
    if (!n) return '默认码率';
    return n >= 1000000 ? (Math.round(n / 100000) / 10) + ' Mbps' : Math.round(n / 1000) + ' kbps';
  }

  function loadAllSettings() {
    try { return JSON.parse(localStorage.getItem(SET_KEY)) || {}; } catch (e) { return {}; }
  }
  function settingsOf(serial) {
    var all = loadAllSettings();
    var out = {};
    for (var k in DEFAULTS) if (DEFAULTS.hasOwnProperty(k)) out[k] = DEFAULTS[k];
    var mine = all[serial] || {};
    for (var k2 in mine) if (mine.hasOwnProperty(k2) && out.hasOwnProperty(k2)) out[k2] = mine[k2];
    return out;
  }
  function saveSettings(serial, s) {
    var all = loadAllSettings();
    all[serial] = s;
    try { localStorage.setItem(SET_KEY, JSON.stringify(all)); } catch (e) { /* 隐私模式，忽略 */ }
  }

  // ==================== 状态 ====================
  var devices = [];          // /api/devices 的结果
  var active = null;         // 当前选中/正在看的设备 serial
  var attached = null;       // 当前视频流已经挂上的设备 serial
  var streaming = false;
  var powerOn = {};          // serial -> 当前是否亮屏
  var videoWS = null, controlWS = null, keepaliveTimer = 0;
  var pollTimer = 0, listTimer = 0, fastPoll = 0;

  var devW = 0, devH = 0;
  var sessCodec = 'h264';    // 当前会话流里的实际编码（h264 / h265），挂流时从设备信息取
  var decoder = null, configured = false, decodeMode = '';
  var pendingFrame = null, drawRAF = 0;
  var MAX_POINTERS = 10;
  var activePointers = new Map();     // 浏览器 pointerId -> {cid}
  var movePending = new Map(), moveRAF = 0;

  // ==================== H.264 小工具 ====================
  // scrcpy 推出来的裸流是 Annex-B（00 00 01 起始码），WebCodecs 要 AVCC（长度前缀），
  // 所以要把每帧拆成 NAL 再重新打包。

  function nalUnits(buf) {
    var starts = [];
    for (var i = 0; i + 3 <= buf.length; i++) {
      if (buf[i] === 0 && buf[i + 1] === 0 && buf[i + 2] === 0 && i + 3 < buf.length && buf[i + 3] === 1) {
        starts.push([i + 4, i]); i += 3;
      } else if (buf[i] === 0 && buf[i + 1] === 0 && buf[i + 2] === 1) {
        starts.push([i + 3, i]); i += 2;
      }
    }
    var out = [];
    for (var k = 0; k < starts.length; k++) {
      var from = starts[k][0];
      var to = (k + 1 < starts.length) ? starts[k + 1][1] : buf.length;
      if (to > from) out.push(buf.subarray(from, to));
    }
    return out;
  }

  function annexBtoAVCC(units) {
    var total = 0, i;
    for (i = 0; i < units.length; i++) total += 4 + units[i].length;
    var out = new Uint8Array(total);
    var dv = new DataView(out.buffer);
    var o = 0;
    for (i = 0; i < units.length; i++) {
      dv.setUint32(o, units[i].length);
      o += 4;
      out.set(units[i], o);
      o += units[i].length;
    }
    return out;
  }

  function findNal(units, type) {
    for (var i = 0; i < units.length; i++) {
      if (units[i].length && (units[i][0] & 0x1f) === type) return units[i];
    }
    return null;
  }

  // 按 ISO/IEC 14496-15 拼 avcC
  function buildAvcC(sps, pps) {
    if (!sps) return null;
    var ppsN = pps || new Uint8Array(0);
    var out = new Uint8Array(8 + sps.length + 3 + ppsN.length);
    out[0] = 1;
    out[1] = sps[1]; out[2] = sps[2]; out[3] = sps[3];
    out[4] = 0xff;                       // lengthSizeMinusOne = 3
    out[5] = 0xe1;                       // 一条 SPS
    out[6] = (sps.length >> 8) & 0xff;
    out[7] = sps.length & 0xff;
    out.set(sps, 8);
    var o = 8 + sps.length;
    out[o] = 1;                          // 一条 PPS
    out[o + 1] = (ppsN.length >> 8) & 0xff;
    out[o + 2] = ppsN.length & 0xff;
    out.set(ppsN, o + 3);
    return out;
  }

  function hex2(v) { return ('0' + (v & 0xff).toString(16)).slice(-2); }

  function hexOf(u8, n) {
    if (!u8) return 'null';
    var s = '', k = Math.min(u8.length, n || 8);
    for (var i = 0; i < k; i++) s += hex2(u8[i]);
    return s;
  }

  // ==================== 状态条 / 日志 ====================
  function setStatus(text, cls) {
    var el = $('status');
    if (!el) return;
    el.textContent = text;
    el.className = 'st ' + (cls || '');
  }

  var LOG_MAX = 500;

  function addLog(text, level) {
    var body = $('logBody');
    if (!body) return;
    var t = new Date();
    var hh = ('0' + t.getHours()).slice(-2) + ':' + ('0' + t.getMinutes()).slice(-2)
      + ':' + ('0' + t.getSeconds()).slice(-2);
    var span = document.createElement('span');
    if (level) span.className = level;
    span.textContent = '[' + hh + '] ' + text + '\n';
    body.appendChild(span);
    while (body.childNodes.length > LOG_MAX) body.removeChild(body.firstChild);
    body.scrollTop = body.scrollHeight;
  }

  // 右侧几个抽屉（日志 / 状态 / 文件 / 终端）共用一套开关。
  // 一次只开一个：它们占的是同一块地方，叠在一起只会互相挡。
  // 抽屉**不铺模态遮罩**：它们只是侧边的一栏，画面和虚拟按键照旧可点。
  // （遮罩只属于真正的 modal —— 见 openModal/closeModal。）
  var DRAWER_IDS = ['logPanel', 'statPanel', 'filePanel', 'shellPanel'];
  var openDrawerId = null;

  // 抽屉是「占位」的，不是「浮层」——
  // 旧版它是 position:fixed 直接压在画面右侧，开着就永远看不见完整视频。
  // 现在打开时给 .app 挂上 has-drawer + data-drawer，CSS 那边按 data-drawer
  // 用 padding-right 让出同样的宽度（宽度值只在 CSS 的 --drawer-w-* 里定义一份），
  // 于是画面是「变窄」而不是「被盖住」。
  //
  // ⚠️ 每次都必须按 openDrawerId **重算**，不能只 add/remove 一个通用 class：
  // 「开着日志再点状态」这种切换，只 remove 会留下上一个抽屉的 stale 宽度。
  function syncDrawerLayout() {
    var app = document.querySelector('.app');
    if (!app) return;
    if (openDrawerId) {
      app.setAttribute('data-drawer', openDrawerId);
      app.classList.add('has-drawer');
    } else {
      app.removeAttribute('data-drawer');
      app.classList.remove('has-drawer');
    }
    // 舞台宽度变了，画面得重新贴合，不然会留黑边 / 被拉伸。
    resizeCanvas();
  }

  var appShell = document.querySelector('.app');
  if (appShell) {
    // 让位是带 0.2s 过渡的（和抽屉滑入同步），过渡期间舞台宽度一直在变；
    // 只在按下开关那一刻 resizeCanvas，画面是按「过渡前 / 过渡中」的宽度算的，
    // 结束之后就会因为外层 max-width:100% 被夹一下 —— 宽高比就不对了。
    // 所以过渡一结束再贴合一次。
    appShell.addEventListener('transitionend', function (e) {
      if (e.target === appShell && e.propertyName === 'padding-right') resizeCanvas();
    });
  }

  function setDrawer(id, show) {
    if (DRAWER_IDS.indexOf(id) < 0) return;
    if (show) {
      DRAWER_IDS.forEach(function (d) {
        if (d === id) return;
        var other = $(d);
        if (other) other.classList.remove('show');
      });
      openDrawerId = id;
    } else if (openDrawerId === id) {
      openDrawerId = null;
    }
    var el = $(id);
    if (el) el.classList.toggle('show', !!show);
    syncDrawerLayout();
    // 终端跟着抽屉开合：关掉就得把设备那边的 shell 一起放掉，别让它在后台空转。
    if (id === 'shellPanel') { if (show) shellOpen(); else shellClose(); }
    // 文件面板：**只有换了设备**才回默认目录。
    // 以前这里是无条件 fileReset()，于是每次打开都跳回 /sdcard ——
    // 用户刚翻到的子目录（还有他刚传上去的文件）全没了，看着像"上传的东西不见了"。
    // 打开时仍然刷新一次列表，拿的是最新内容，但目录保持不变。
    if (id === 'filePanel' && show) {
      if (fileCwdSerial !== active) { fileCwd = '/sdcard'; fileCwdSerial = active; }
      fileList(null);
    }
    // 状态面板平时不渲染（每秒白白重建 30 多行 DOM 不值当），打开时先把当前值填上，
    // 之后交给 statTick 每秒刷。
    if (id === 'statPanel' && show) { statRender(); statNeedNative(); }
  }

  function closeDrawers() {
    DRAWER_IDS.forEach(function (d) {
      var el = $(d);
      if (el) el.classList.remove('show');
    });
    if (openDrawerId === 'shellPanel') shellClose();
    openDrawerId = null;
    // 布局跟着收回来（把让出去的宽度还给画面）。#mask 是 modal 的东西，抽屉不碰它。
    syncDrawerLayout();
  }

  function showLog(show) { setDrawer('logPanel', show); }

  // 诊断：解码链路上每一环摊开，卡在哪一眼能看见
  var DIAG = {
    wsVideo: '未连', wsControl: '未连', frames: 0, configs: 0, posted: 0, dec: '未创建',
    // 排查花屏/黑屏用的计数（**累计**，重起步不清零 —— 长会话要看总量在不在涨）：
    //   dropped  = 客户端自己丢了多少帧
    //   keyAsks  = 向设备要了几次关键帧。⚠️ 修复后**必须恒为 0** ——
    //              这个字段留着就是当哨兵：一旦不为 0，说明又有人接回了 resetVideo。
    //   keyWaits = 进过几次「等自然 IDR」状态（丢帧等下一个关键帧）
    //   sessions / configs = 收到几次会话头 / 配置包
    //   reinits  = 主动重起步（只重订阅浏览器链路）次数
    //   stalls   = 判成「卡死」自愈的次数
    dropped: 0, keyAsks: 0, keyWaits: 0, sessions: 0,
    spsFirst: '', spsLast: '', spsSame: true,
    latSeeks: 0, stalls: 0, reinits: 0,
    // moveDropped = 因为控制通道积压而丢掉的 move 数（见 flushMove）。
    // 正常情况下应该是 0；一直在涨说明网络或 ADB 那边已经跟不上了。
    moveDropped: 0,
    audio: '未开始', audioDropped: 0
  };

  function diag(patch) {
    if (patch) {
      for (var k in patch) {
        if (Object.prototype.hasOwnProperty.call(patch, k)) DIAG[k] = patch[k];
      }
    }
    var el = $('logStat');
    if (!el) return;
    var secure = window.isSecureContext ? '是' : '否';
    var hasVD = (typeof VideoDecoder !== 'undefined') ? '有' : '无';
    var videoCls = DIAG.wsVideo === '已连' ? 'good' : (DIAG.wsVideo === '未连' ? 'bad' : '');
    var decCls = DIAG.dec.indexOf('不可用') === 0 ? 'bad'
      : (DIAG.dec.indexOf('MSE') === 0 || DIAG.dec.indexOf('WebCodecs 已') === 0 ? 'good' : '');
    el.innerHTML =
      '设备 <b>' + (active || '—') + '</b><br>' +
      '视频WS <b class="' + videoCls + '">' + DIAG.wsVideo + '</b>' +
      ' · 控制WS <b>' + DIAG.wsControl + '</b><br>' +
      '收到帧 <b>' + DIAG.frames + '</b>（config ' + DIAG.configs + '）' +
      ' · 已解码 <b>' + DIAG.posted + '</b><br>' +
      '解码器 <b class="' + decCls + '">' + DIAG.dec + '</b><br>' +
      'WebCodecs <b>' + hasVD + '</b> · 安全上下文 <b>' + secure + '</b><br>' +
      '地址 <b>' + location.host + location.pathname + '</b>';
  }

  // ==================== 实时状态面板 ====================
  // 刻意把「采集」和「渲染」拆开：
  //   · 采集跑在最热的那条路上（每帧一次），所以只做加法 —— 不碰 DOM、不拼字符串。
  //   · 计算 + 渲染每秒一次，而且是**面板打开时才渲染**（没开就只是攒着）。
  // 面板没开也照常采集：一打开看到的是热数据，不用先盯着它看一秒。
  var STATS = {
    winFrames: 0, winBytes: 0, winAt: 0,        // 当前窗口（用来算「每秒」的均值）
    fpsRx: 0, fpsDec: 0, kbps: 0,               // 上一个窗口算出来的结果
    peakKbps: 0,                                // 本次会话的码率峰值（看编码器有没有突然飙）
    rxFrames: 0, rxBytes: 0,                    // 自挂流起的累计
    startedAt: 0, decLast: 0, decLastAt: 0,
    zeroAt: 0, zeroBase: 0, zeroRxBase: 0, zeroWarned: false   // 零帧自诊断（见 zeroFrameCheck）
  };

  function statReset() {
    var now = Date.now();
    STATS.winFrames = STATS.winBytes = 0;
    STATS.fpsRx = STATS.fpsDec = STATS.kbps = STATS.peakKbps = 0;
    STATS.rxFrames = STATS.rxBytes = 0;
    STATS.startedAt = now;
    STATS.winAt = STATS.decLastAt = now;
    STATS.zeroAt = 0; STATS.zeroBase = 0; STATS.zeroRxBase = 0; STATS.zeroWarned = false;
    // 记下当前解码计数当基准：换流/重起步时 video 元素可能是新建的，计数会从 0 重来
    STATS.decLast = decodedFrames();
  }

  // ---- 「解码器配上了、却一帧都不上屏」的自诊断 ----
  // 这是最容易被误判成「网络卡住」的一种坏：configure 成功、帧也照收，
  // 就是不往画布上吐 —— 因为送进去的编码参数跟真实码流对不上，
  // 浏览器据此判「这条流我不支持」，然后安静地什么都不做。
  // 日志里一片祥和、画面全黑，用户只能瞎猜（2026-09-22 的 H.265 全黑就是它：
  // SPS 的防竞争字节没去掉，hvcC 里的 level 被读成 0）。
  // 所以这里主动把「配好了但 X 秒零帧」喊出来，并分清是收不到帧还是解不出帧。
  var ZERO_FRAME_MS = 4000;      // 配置成功之后多久还不出帧就报警
  var ZERO_MIN_RX = 30;          // 且至少收够这么多帧，才有资格说是「解码器的锅」
  function noteConfigured() {
    STATS.zeroAt = Date.now();
    STATS.zeroBase = DIAG.posted;
    STATS.zeroRxBase = DIAG.frames;
    STATS.zeroWarned = false;
  }
  function zeroFrameCheck(now) {
    if (!STATS.zeroAt || STATS.zeroWarned) return;
    if (DIAG.posted > STATS.zeroBase) { STATS.zeroWarned = true; return; }   // 出过帧了，不再管
    if (now - STATS.zeroAt < ZERO_FRAME_MS) return;
    STATS.zeroWarned = true;
    var rx = DIAG.frames - STATS.zeroRxBase;
    if (rx < ZERO_MIN_RX) {
      // 帧就没收到几帧，锅不在解码器 —— 别让人白折腾编码格式。
      addLog('解码器已配置，但这 ' + Math.round(ZERO_FRAME_MS / 1000)
        + ' 秒只收到 ' + rx + ' 帧，先查取流而不是解码', 'y');
      return;
    }
    addLog('解码器已配置、也收到 ' + rx + ' 帧，却没有一帧上屏 —— 这条 '
      + String(DIAG.codecStr || sessCodec || '').toUpperCase()
      + ' 流浏览器解不了（编码串 ' + (DIAG.codecStr || '?') + '）', 'r');
    setStatus('解码器配上了却不出画面：换 H.264 编码再试', 'err');
  }

  // 设备原生屏。**单独存一份、按 serial 记账**，不复用设置弹窗里那个全局 deviceScreen ——
  // 那个是「设备设置」的临时变量（每次开弹窗先清空再问），换设备时账对不上。
  var devNative = null, nativeAsked = null;
  function statNeedNative() {
    if (!active || nativeAsked === active) return;
    nativeAsked = active;
    api('/api/screen?serial=' + encodeURIComponent(active)).then(function (d) {
      if (d && d.ok && d.screen && active === nativeAsked) {
        devNative = d.screen;
        devNative.serial = active;
        if (openDrawerId === 'statPanel') statRender();
      }
    }).catch(function () { nativeAsked = null; /* 问不到就先算了，下次开面板再试 */ });
  }

  // 真正「上屏」了多少帧。MSE 模式下由 video 元素自己数（totalVideoFrames），
  // 比 DIAG.posted 准 —— 后者在 MSE 里数的是「送进 SourceBuffer 的段数」，不是上屏帧数。
  function decodedFrames() {
    if (decodeMode === 'mse' && videoEl && videoEl.getVideoPlaybackQuality) {
      try { return videoEl.getVideoPlaybackQuality().totalVideoFrames || 0; } catch (e) { /* 拿不到就退回 */ }
    }
    return DIAG.posted;
  }
  function browserDropped() {
    if (videoEl && videoEl.getVideoPlaybackQuality) {
      try { return videoEl.getVideoPlaybackQuality().droppedVideoFrames || 0; } catch (e) { /* 同上 */ }
    }
    return null;                                 // 这条路拿不到，显示「—」而不是假装是 0
  }

  function statTick() {
    var now = Date.now();
    zeroFrameCheck(now);                         // 跟窗口统计无关，别被下面的 early return 挡掉
    if (!STATS.winAt) STATS.winAt = STATS.decLastAt = now;
    var dt = (now - STATS.winAt) / 1000;
    if (dt < 0.25) return;                       // 被叫得太密就算了，别把均值算歪
    var dec = decodedFrames();
    var ddt = Math.max(0.001, (now - STATS.decLastAt) / 1000);
    STATS.fpsRx = STATS.winFrames / dt;
    STATS.fpsDec = Math.max(0, dec - STATS.decLast) / ddt;   // 换流时计数会重来，负数按 0 处理
    // ⚠️ 1 秒窗口量出来的码率天生跳 —— 编码器是按画面内容给码率的，静止和滚动能差一个数量级。
    // 直接显示读不出数，显示值上做一次轻平滑；**峰值仍取瞬时值**，不然"峰"就被抹平了。
    var inst = STATS.winBytes * 8 / dt / 1000;
    STATS.kbps = STATS.kbps ? (STATS.kbps * 0.55 + inst * 0.45) : inst;
    if (inst > STATS.peakKbps) STATS.peakKbps = inst;
    STATS.decLast = dec;
    STATS.decLastAt = now;
    STATS.winAt = now;
    STATS.winFrames = 0;
    STATS.winBytes = 0;
    if (openDrawerId === 'statPanel') statRender();
  }

  function fmtBytes(n) {
    if (n >= 1073741824) return (n / 1073741824).toFixed(2) + ' GB';
    if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
    if (n >= 1024) return Math.round(n / 1024) + ' KB';
    return Math.round(n || 0) + ' B';
  }
  function fmtDur(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    return (h ? h + ':' + ('0' + m).slice(-2) : String(m)) + ':' + ('0' + ss).slice(-2);
  }
  // 面板里有来自服务端的字符串（设备型号/名字），拼 innerHTML 前先过一道
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function statRow(k, v, cls, hero) {
    return '<div class="sr' + (hero ? ' hero' : '') + '">' +
      '<span class="sk">' + k + '</span>' +
      '<span class="sv' + (cls ? ' ' + cls : '') + '">' + v + '</span></div>';
  }
  function statSec(title, rows) {
    return '<div class="ssec"><div class="sh">' + title + '</div>' + rows.join('') + '</div>';
  }

  function statRender() {
    var el = $('statBody');
    if (!el) return;

    var d = null;
    for (var i = 0; i < devices.length; i++) {
      if (devices[i].serial === active) { d = devices[i]; break; }
    }
    var s = active ? settingsOf(active) : null;
    var on = streaming && !!active;
    var dash = '<span class="dim">—</span>';

    // ---- 实时（这三个是主角，放大一档）----
    var live = [];
    live.push(statRow('分辨率', (devW && devH) ? (devW + '×' + devH) : dash, '', true));
    live.push(statRow('接收帧率', on ? STATS.fpsRx.toFixed(1) + ' fps' : dash, '', true));
    live.push(statRow('下行码率', on ? (STATS.kbps >= 1000
      ? (STATS.kbps / 1000).toFixed(2) + ' Mbps'
      : Math.round(STATS.kbps) + ' kbps') : dash, '', true));

    live.push(statRow('上屏帧率', on ? STATS.fpsDec.toFixed(1) + ' fps' : dash));
    live.push(statRow('峰值码率', on ? (STATS.peakKbps >= 1000
      ? (STATS.peakKbps / 1000).toFixed(2) + ' Mbps'
      : Math.round(STATS.peakKbps) + ' kbps') : dash));

    // 画质密度：每像素每帧分到多少 bit。设备设置里那几档「推荐配置」就是用这个算的，
    // 所以这里显示的是**实测值**，能反过来验证"自动"选得对不对。
    var bpp = dash;
    if (on && devW && devH && STATS.fpsRx > 0.5 && STATS.kbps > 0) {
      bpp = (STATS.kbps * 1000 / (devW * devH * STATS.fpsRx)).toFixed(3) + ' bit/像素/帧';
    }
    live.push(statRow('画质密度', bpp));

    // 缓冲与延迟（只有 MSE 这条路有得算；WebCodecs 是逐帧直接上屏，没有中间缓冲）
    var buf = dash;
    if (on && decodeMode === 'mse' && videoEl && videoEl.buffered && videoEl.buffered.length) {
      var bf = videoEl.buffered;
      var bEnd = bf.end(bf.length - 1), bStart = bf.start(0);
      var lag = Math.max(0, bEnd - videoEl.currentTime);
      buf = lag.toFixed(2) + 's 落后 · 缓冲 ' + (bEnd - bStart).toFixed(1) + 's';
    }
    live.push(statRow('播放延迟', buf));

    var native = dash;
    if (devNative && devNative.serial === active && devNative.width) {
      native = devNative.width + '×' + devNative.height +
        (devNative.density ? ' @' + devNative.density : '');
    }
    live.push(statRow('设备原生屏', native));

    // ---- 链路 ----
    var link = [];
    link.push(statRow('设备', esc(active ? labelFor(active) : '未选择')));
    link.push(statRow('串号', esc(active || '—'), 'dim'));
    link.push(statRow('状态', d ? esc(phaseText(d)) : dash));
    if (d && d.error) link.push(statRow('错误', esc(d.error), 'bad'));
    var vCls = DIAG.wsVideo === '已连' ? 'good' : (DIAG.wsVideo === '未连' ? 'bad' : 'warn');
    var cCls = DIAG.wsControl === '已连' ? 'good' : (DIAG.wsControl === '未连' ? 'bad' : 'warn');
    link.push(statRow('视频通道', esc(DIAG.wsVideo), vCls));
    link.push(statRow('控制通道', esc(DIAG.wsControl), cCls));
    link.push(statRow('编码', esc(sessCodec.toUpperCase()) +
      (d && d.codecRequested ? ' <span class="dim">（请求 ' + esc(d.codecRequested) + '）</span>' : '')));
    var decCls = DIAG.dec.indexOf('不可用') === 0 ? 'bad'
      : (DIAG.dec.indexOf('MSE') === 0 || DIAG.dec.indexOf('WebCodecs 已') === 0 ? 'good' : 'warn');
    link.push(statRow('解码器', esc(DIAG.dec), decCls));
    link.push(statRow('音频', esc(DIAG.audio) +
      (DIAG.audioDropped ? ' <span class="bad">丢 ' + DIAG.audioDropped + '</span>' : '')));
    link.push(statRow('连接时长', on ? fmtDur(Date.now() - STATS.startedAt) : dash));
    if (s) {
      link.push(statRow('请求参数', esc((s.maxSize ? s.maxSize + 'p' : '原画') + ' · ' +
        s.maxFps + 'fps · ' + fmtRate(s.bitRate)), 'dim'));
    }

    // ---- 本次会话的累计计数 ----
    // ⚠️ keyAsks 是个**哨兵**：修复后它必须恒为 0。一旦不为 0，说明又有人把
    // 「自动向设备要关键帧（resetVideo）」那条路接回来了 —— 那正是「连一会儿就黑」的成因。
    var cnt = [];
    cnt.push(statRow('收到帧', DIAG.frames + ' <span class="dim">(config ' + DIAG.configs + ')</span>'));
    cnt.push(statRow('已解码', DIAG.posted));
    cnt.push(statRow('会话头', DIAG.sessions));
    cnt.push(statRow('客户端丢帧', DIAG.dropped, DIAG.dropped ? 'warn' : ''));
    cnt.push(statRow('等关键帧', DIAG.keyWaits));
    cnt.push(statRow('要关键帧', DIAG.keyAsks + (DIAG.keyAsks ? ' ← 不正常' : ' ✓'),
      DIAG.keyAsks ? 'bad' : 'good'));
    var bd = browserDropped();
    cnt.push(statRow('解码器丢帧', bd == null ? dash : String(bd), (bd ? 'warn' : '')));
    cnt.push(statRow('重起步', DIAG.reinits));
    cnt.push(statRow('卡死自愈', DIAG.stalls));
    cnt.push(statRow('延迟纠偏', DIAG.latSeeks));
    cnt.push(statRow('丢坐标', DIAG.moveDropped + (DIAG.moveDropped ? '（控制通道积压）' : ''),
      DIAG.moveDropped ? 'warn' : ''));
    cnt.push(statRow('累计流量', fmtBytes(STATS.rxBytes) +
      ' <span class="dim">(' + STATS.rxFrames + ' 帧)</span>'));
    cnt.push(statRow('SPS 一致性', DIAG.spsSame ? '与首个一致' : '与首个不同',
      DIAG.spsSame ? '' : 'warn'));

    // ---- 环境 ----
    var env = [];
    env.push(statRow('地址', esc(location.host + location.pathname), 'dim wrap'));
    env.push(statRow('安全上下文', window.isSecureContext ? '是' : '否',
      window.isSecureContext ? 'good' : 'bad'));
    env.push(statRow('WebCodecs', (typeof VideoDecoder !== 'undefined') ? '可用' : '没有',
      (typeof VideoDecoder !== 'undefined') ? 'good' : 'dim'));
    env.push(statRow('页面可见', document.visibilityState === 'visible' ? '是' : '否（后台会限帧）',
      document.visibilityState === 'visible' ? '' : 'warn'));

    el.innerHTML = statSec('实时', live) + statSec('链路', link) +
      statSec('本次会话', cnt) + statSec('环境', env);
  }

  // ==================== 解码与渲染 ====================
  var mediaSource = null, sourceBuffer = null, videoEl = null;
  var mseQueue = [], mseSeq = 1, mseDts = 0, mseDur = 33333, mseLastPts = 0;
  var mseResyncTo = 0;             // 重起步保留时间轴时，新链路要把播放点挪到的秒数
  // ⚠️ MSE 的硬规矩：init 段（ftyp+moov）必须先于任何媒体段进 SourceBuffer。
  // 一开始就踩了这个坑 —— 服务端补发的关键帧会先到（那时 sourceBuffer 还没建好，
  // 只能先压进队列），而 init 段是 sourceopen 里才入的队，结果排到了媒体段后面，
  // 第一次 appendBuffer 送过去的是「没有初始化信息的媒体段」→ SourceBuffer 立刻报错、
  // MediaSource 转 closed、缓冲被移出父对象，之后每帧都抛
  // "This SourceBuffer has been removed from the parent media source"，画面永远黑。
  // 所以 init 落地前，媒体段先攒在 msePending 里，等 init 追加完成再按序灌。
  var msePending = [], mseInitDone = false;
  // 解码器世代号：resetDecoder 一次就 +1。
  // 为什么要它：sourceopen 是异步回调，如果它迟到（比如尺寸变化触发了一次 reset、
  // 又火速建了新的 MediaSource），回调里读全局变量会拿到新的那个，
  // 于是把旧 MediaSource 的缓冲配到已经换代的对象上，整条链路就乱了。
  // 用世代号在回调里判废，迟到的那个直接扔掉。
  var decGen = 0;

  // ---------------- WebCodecs 的视频时钟 ----------------
  // MSE 那条路有 <video>，音频对齐直接读它的 currentTime 就行。
  // WebCodecs 没有 video 元素（画面是逐帧 drawImage 到 canvas 上的），
  // 于是音画同步完全没有「视频现在放到哪了」这个量 —— 以前 tickAudioSync
  // 开头就是 `if (!videoEl) return`，结果这条路上音频从头到尾没人管，一直漂。
  //
  // 这里补一条**相对视频时钟**：记下第一个输出帧的 timestamp 和那一刻的
  // performance.now()，之后用真实流逝的时间往外推。
  // 为什么是相对值而不是帧的绝对 pts：视频这条链没有 MSE 时间轴，音频的锚点
  // 和漂移比较都只在「同一会话内相对」这个意义上成立；拿设备开机微秒当绝对值，
  // 和音频的 MSE 时间轴差好几个数量级，会直接被判成「时间轴错开」而放弃同步。
  var wcClockBaseTs = null;   // 首个输出帧的 timestamp（微秒）
  var wcClockBaseNow = 0;     // 记下首帧那一刻的 performance.now()（毫秒）
  var wcClockLastTs = null;   // 最近一帧的 timestamp，诊断用

  function nowMs() {
    return (window.performance && window.performance.now)
      ? window.performance.now() : Date.now();
  }

  function wcClockReset() {
    wcClockBaseTs = null;
    wcClockBaseNow = 0;
    wcClockLastTs = null;
  }

  // 每个输出帧都过一下：只认**第一个**，它定这条相对时钟的零点。
  function wcClockNote(frame) {
    if (!frame) return;
    var ts = frame.timestamp;
    if (!isFinite(ts)) return;
    wcClockLastTs = ts;
    if (wcClockBaseTs == null) {
      wcClockBaseTs = ts;
      wcClockBaseNow = nowMs();
    }
  }

  // WebCodecs 的相对视频时钟（秒）：首帧 = 0，之后按真实时间往前走。
  // ⚠️ 这里**不许**出现 videoEl / currentTime —— MSE 的播放头是另一条时钟，
  // 混进来就会拿「MSE 的秒数」去比「音频的秒数」，两边根本不是一个时间轴。
  function wcClockSec() {
    if (wcClockBaseTs == null) return NaN;
    var d = (nowMs() - wcClockBaseNow) / 1000;
    return d > 0 ? d : 0;
  }

  // 统一的视频时钟入口：有 <video> 就用它的播放头（MSE，唯一权威）；
  // 没有（WebCodecs）就回落到上面那条相对时钟。返回 NaN = 现在还没有可用的视频时钟。
  function videoClockSec() {
    if (videoEl) {
      var ct = videoEl.currentTime;
      if (isFinite(ct)) return ct;
    }
    return wcClockSec();
  }

  // H.264 的帧之间是「参考」关系：随便丢一帧，后面所有帧都从残缺的参照里预测，
  // 画面会变成一片宏块花屏，而且得等到下一个关键帧才恢复。
  // 所以这里绝不盲目丢帧：一旦跟不上，就从这一刻起只收关键帧，
  // 一直等到编码器**自然**产出下一个 IDR 为止。
  //
  // ⚠️ 曾经这里会主动向设备发 MSG_RESET_VIDEO（type 17）去「催」一个关键帧，
  // 这条路是**单向的、走不通**：这台设备的 OMX.redroid.h264.encoder 被 reset 之后
  // 会重新出 IDR，但**不再补 CONFIG 包**；前端 resetDecoder 后 configured=false，
  // 而 H.264 又没有「拿关键帧当配置」的兜底 ⇒ 之后每一帧都被丢掉，画面永久黑。
  // 实测见 tools/_t29e_out.txt（reset 前 82.8 帧/秒，reset 后 30 秒 1 帧）。
  // 所以现在只做「等待」——本机自然 IDR 约 1.7 秒一个，够密。
  var mseNeedKey = true;
  var MSE_MAXQ = 30;        // 队列上限（60fps 下约半秒），超了按上面的策略等关键帧
  var latencyTimer = 0;
  // 「延迟纠偏」（把播放点推回直播边缘）的开关。
  // 这是唯一会去动播放位置的地方，出问题最难查，所以留个开关做 A/B。
  var LATENCY_ON = true;
  // ---- 低延迟的两个目标值（分开写，别让它们混成一个魔数）----
  //   TARGET：起播和每次纠偏之后，播放点落在「最新缓冲末端」往前这么多秒的地方。
  //           留一点点余量是为了不贴着缓冲边缘放 —— 贴太紧会因为下一帧还没到而顿一下。
  //   MAX   ：允许的最大可见延迟。超过就把播放点推回 TARGET 处。
  //           ⚠️ 旧版这里是「落后 1.5 秒才纠偏」，等于默认挂着 1.5 秒的延迟；
  //           实测 1.5 秒是可感知的「操作了画面才跟上」。目标最大可见延迟 0.4~0.6 秒。
  var MSE_TARGET_LATENCY = 0.15;   // 秒：纠偏点 = 缓冲末端 - 0.15
  var MSE_MAX_LATENCY = 0.5;       // 秒：落后超过这么多就纠偏（可见延迟上限约半秒）
  var MSE_SEEK_MIN_GAP_MS = 500;   // 两次纠偏之间至少隔这么久：seek 本身有代价，别每拍都动
  // 纠偏节拍。旧版 1000ms —— 一秒才发现一次落后，那半秒的阈值就没意义了。
  var LATENCY_TICK_MS = 250;

  function hideVideoSurface() {
    if (videoEl) {
      try { videoEl.pause(); } catch (e) { /* 忽略 */ }
      try { videoEl.removeAttribute('src'); videoEl.load(); } catch (e) { /* 忽略 */ }
      if (videoEl.parentNode) videoEl.parentNode.removeChild(videoEl);
      videoEl = null;
    }
    canvas.style.display = '';
  }

  // keepTimeline：只有「会话内重起步回收内存」才传 true —— 保留 mseDts，
  // 让新一条 MSE 接着旧的时间轴往下走。为什么必须保留：音频那条链是**连续**的，
  // 重起步时并不跟着重建（见 restartStream 的注释）。视频时间轴要是回到 0，
  // 两边就整整错开六十秒，音频对齐逻辑会拿视频的绝对时间去把声音倒回一分钟前重放。 
  // 换会话（attachStream / detach）一律 false —— 那时候两边本来就都从头开始。
  function resetDecoder(keepTimeline) {
    decGen++;
    configured = false;
    decodeMode = '';
    if (decoder) {
      try { decoder.close(); } catch (e) { /* 已经关了 */ }
      decoder = null;
    }
    if (pendingFrame) {
      try { pendingFrame.close(); } catch (e) { /* 忽略 */ }
      pendingFrame = null;
    }
    mseQueue.length = 0;
    msePending.length = 0;
    mseInitDone = false;
    if (keepTimeline) {
      // 时间轴接着走：新一条 MSE 的第一段媒体段就从 mseDts 开始，
      // 记住这个秒数，等它落地后把播放点挪过去（见 ensureMSE 的 updateend）。
      mseResyncTo = mseDts / 1000000;
    } else {
      mseSeq = 1; mseDts = 0; mseLastPts = 0; mseResyncTo = 0;
    }
    mseNeedKey = true;
    // 零帧自诊断跟着这条解码链一起重来：下一次配置成功时会重新打点
    STATS.zeroAt = 0; STATS.zeroWarned = false;
    // WebCodecs 的视频时钟：换会话（keepTimeline=false）一律归零，从头起算。
    // 重起步（keepTimeline=true）**故意不清** —— 音频那条链不重建，视频时钟要是跳回 0，
    // 音画同步立刻会把「声音比画面晚」误判成「早了整整一个重起步周期」而停摆。
    if (!keepTimeline) wcClockReset();
    if (sourceBuffer) {
      try { sourceBuffer.abort(); } catch (e) { /* 忽略 */ }
      sourceBuffer = null;
    }
    if (mediaSource) {
      try { mediaSource.endOfStream(); } catch (e) { /* 忽略 */ }
      try { URL.revokeObjectURL(videoEl && videoEl.src); } catch (e) { /* 忽略 */ }
      mediaSource = null;
    }
    hideVideoSurface();
    // ⚠️ 这里**不再**清零 frames/posted/configs。
    // 重起步现在是「每约 60 秒回收一次内存」的正常动作，清零会让诊断面板永远显示
    // 小数字，也没法判断「这一整条会话到底有没有在出画面」。改成累计，只看它涨不涨。
  }

  function ensureDecoder() {
    if (decoder) return true;
    if (typeof VideoDecoder === 'undefined') {
      DIAG.dec = window.isSecureContext === false
        ? '不可用：http 非安全上下文' : '不可用：浏览器不支持 WebCodecs';
      diag();
      return false;
    }
    try {
      var gen = decGen;
      decoder = new VideoDecoder({
        output: function (frame) {
          if (gen !== decGen) { try { frame.close(); } catch (e) { /* 忽略 */ } return; }
          // 每个输出帧都喂一次：第一个帧会定下这条链的相对视频时钟零点（音画同步要用）。
          wcClockNote(frame);
          queueFrame(frame);
        },
        error: function (e) { setStatus('解码错误：' + (e && e.message ? e.message : e), 'err'); }
      });
      return true;
    } catch (e) {
      setStatus('创建解码器失败：' + e.message, 'err');
      return false;
    }
  }

  // 编码族决定三件事：帧要不要按 NAL 拆、sample 要不要转 4 字节长度前缀、
  // 配置盒叫 avcC / hvcC / vpcC / av1C。写成一个函数，省得到处散落 if。
  //   h264 / h265 —— Annex-B 的 NAL 流，要拆、要转 AVCC
  //   vp8  / vp9  —— 自带边界，原样透传
  //   av1         —— OBU 流，原样透传
  function codecFamily(name) {
    if (name === 'h265') return 'hevc';
    if (name === 'vp8' || name === 'vp9') return 'vp';
    if (name === 'av1') return 'av1';
    return 'avc';
  }

  function finishConfigure(codec, config) {
    if (configured || !codec) return;
    DIAG.codecStr = codec;

    // 先试 WebCodecs（延迟最低）；它不可用时退到 MSE ——
    // VideoDecoder 是 SecureContext API，http 下浏览器不暴露它，
    // 而 MediaSource 没这个限制。两条路都试过才算尽力了。
    if (ensureDecoder()) {
      // ⚠️ WebCodecs 和 MP4 容器用的是两套 codec 字符串：VP8 在 WebCodecs 里就叫 'vp8'，
      // 而 'vp08.xx.xx.xx' 是 MP4 sample entry 那一套（只给 MSE 用）—— 直接喂过去 configure
      // 会失败，表现是「解码器没配上、画面一直黑」。vp09 / av01 / avc1 / hvc1 两套写法一致。
      var fam = codecFamily(sessCodec);
      var wcCodec = (sessCodec === 'vp8') ? 'vp8' : codec;
      var cfg = { codec: wcCodec, optimizeForLatency: true };
      // description 只有 h264/h265 需要 —— 它们的参数集在容器层，码流里没有。
      // vp8/vp9 没有这个概念；av1 的序列头是跟着帧走的（低开销 OBU），
      // 硬塞一个 av1C 进去反而可能被当成外部 extradata 去解析，不给最稳。
      if (config && (fam === 'avc' || fam === 'hevc')) cfg.description = config;
      if (devW) cfg.codedWidth = devW;
      if (devH) cfg.codedHeight = devH;
      try {
        decoder.configure(cfg);
        configured = true;
        decodeMode = 'webcodecs';
        DIAG.dec = 'WebCodecs 已配置 ' + wcCodec;
        noteConfigured();
        addLog('解码器已配置：WebCodecs / ' + wcCodec, 'g');
        diag();
        return;
      } catch (e) {
        DIAG.dec = 'WebCodecs 配置失败，转 MSE';
        addLog('WebCodecs 配置失败（' + e.message + '），改用 MSE', 'y');
        resetDecoder(true);        // 同一条会话，时间轴不动（音频那条链已经在跑）
      }
    }

    if (ensureMSE(codec, config)) {
      configured = true;
      decodeMode = 'mse';
      DIAG.dec = 'MSE ' + codec;
      noteConfigured();
      addLog('解码器已配置：MSE / ' + codec, 'g');
    } else if (DIAG.dec.indexOf('不可用') !== 0) {
      DIAG.dec = '两条解码路都不可用';
      addLog('两条解码路都不可用', 'r');
    }
    diag();
  }

  function configureFrom(units) {
    if (configured) return;
    // H.264 和 H.265 用的 NAL 类型号完全不同（SPS 在 H.264 是 7、H.265 是 33），
    // 配置盒也一样（avcC / hvcC）。这里先按当前会话报上来的 codec 分路走。
    var codec = '', config = null;
    if (sessCodec === 'h265') {
      // H.265 的解析在 mp4.js 里（那边才有带 2 字节 NAL 头处理的实现），
      // 这里必须走 Mp4. 前缀 —— 裸名字在 app.js 作用域里根本不存在。
      var vps = Mp4.findNalHevc(units, 32), hsps = Mp4.findNalHevc(units, 33), hpps = Mp4.findNalHevc(units, 34);
      config = Mp4.buildHvcC(vps, hsps, hpps);
      codec = Mp4.hevcCodecString(hsps) || '';
      if (!hsps || !config || !codec) {
        DIAG.dec = 'H.265 的配置包不完整（缺 VPS/SPS/PPS）';
        addLog('H.265 配置包不全，无法起步', 'r');
        diag();
        return;
      }
    } else {
      var sps = findNal(units, 7);
      var pps = findNal(units, 8);
      config = buildAvcC(sps, pps);
      if (!sps || !config) return;
      codec = 'avc1.' + hex2(sps[1]) + hex2(sps[2]) + hex2(sps[3]);
    }
    finishConfigure(codec, config);
  }

  // vp8 / vp9 / av1 没有 config 包：MediaCodec 不给这三种编码的 csd，
  // 设备端也就没东西可发。只能拿**第一个关键帧**当起点，从码流头里把
  // profile / 位深读出来，自己拼 vpcC / av1C。
  // 关键帧还没来（或者这帧头读不通）就先不配置，等下一帧 —— 半截数据凑出来的
  // 配置盒会让 MSE 直接拒收，那才是真正的「画面怎么都不出来」。
  function configureFromKeyFrame(payload) {
    if (configured) return;
    var info = Mp4.parseKeyFrame(sessCodec, payload, devW, devH);
    if (!info) {
      if (!DIAG.rawWait) DIAG.rawWait = 0;
      DIAG.rawWait++;
      if (DIAG.rawWait === 1 || DIAG.rawWait % 30 === 0) {
        addLog(sessCodec.toUpperCase() + ' 关键帧还没到（已跳 ' + DIAG.rawWait + ' 帧）', 'y');
      }
      return;
    }
    finishConfigure(info.codec, info.config);
  }

  function queueFrame(frame) {
    // 只留最新一帧：渲染慢的时候主动丢帧，换取低延迟
    if (pendingFrame) { try { pendingFrame.close(); } catch (e) { /* 忽略 */ } }
    pendingFrame = frame;
    if (drawRAF) return;
    drawRAF = requestAnimationFrame(function () {
      drawRAF = 0;
      var latest = pendingFrame;
      pendingFrame = null;
      if (latest) drawFrame(latest);
    });
  }

  function drawFrame(frame) {
    try {
      if (!canvas.width || !canvas.height) {
        canvas.width = devW || frame.displayWidth;
        canvas.height = devH || frame.displayHeight;
        resizeCanvas();
      }
      ctx.drawImage(frame, 0, 0, canvas.width, canvas.height);
      DIAG.posted++;
      if (DIAG.posted === 1) addLog('首帧已上屏 ' + canvas.width + '×' + canvas.height, 'g');
      if (DIAG.posted % 60 === 1) diag();
    } catch (e) {
      /* 尺寸切换的瞬间可能画失败，忽略这一帧 */
    } finally {
      try { frame.close(); } catch (e) { /* 忽略 */ }
    }
  }

  // 画面可用区域 = 舞台减去它自己的内边距。
  // getBoundingClientRect 给的是外框（含 padding），拿它算尺寸会比真正能用的地方
  // 大一圈，而外层有 max-width/max-height:100% 兜着 —— 被夹一下宽高比就变了，画面会拉伸。
  function stageContentBox() {
    var s = $('stage');
    var cs = getComputedStyle(s);
    var w = s.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
    var h = s.clientHeight - (parseFloat(cs.paddingTop) || 0) - (parseFloat(cs.paddingBottom) || 0);
    return { w: Math.max(1, w), h: Math.max(1, h) };
  }

  function fitSize() {
    var box = stageContentBox();
    var scale = Math.min(box.w / devW, box.h / devH);
    if (!isFinite(scale) || scale <= 0) scale = 1;
    return { w: Math.max(1, Math.floor(devW * scale)), h: Math.max(1, Math.floor(devH * scale)) };
  }

  function resizeCanvas() {
    if (!devW || !devH) return;
    var s = fitSize();
    canvas.style.width = s.w + 'px';
    canvas.style.height = s.h + 'px';
    if (videoEl) sizeVideoSurface();
  }

  // ---------------- MSE 降级路径 ----------------
  function switchToVideoSurface() {
    // src 被 revoke 过的残留元素不能接着用，否则永远停在 readyState 0
    if (videoEl && !videoEl.getAttribute('src')) videoEl = null;
    if (videoEl) return;
    videoEl = document.createElement('video');
    videoEl.muted = true;
    videoEl.autoplay = true;
    videoEl.playsInline = true;
    // object-fit 用 contain：尺寸哪怕因为什么原因没算准，也只会出现黑边，
    // 不会把画面拉变形（fill 会）。
    videoEl.style.cssText = 'display:block;background:#05070b;border-radius:8px;'
      + 'outline:none;object-fit:contain;touch-action:none;max-width:100%;max-height:100%;';
    canvas.style.display = 'none';
    var stage = $('stage');
    if (stage) stage.appendChild(videoEl);
    sizeVideoSurface();
  }

  function sizeVideoSurface() {
    if (!videoEl || !devW || !devH) return;
    var s = fitSize();
    videoEl.style.width = s.w + 'px';
    videoEl.style.height = s.h + 'px';
  }

  function pumpMSE() {
    if (!mediaSource || !sourceBuffer || sourceBuffer.updating || !mseQueue.length) return;
    try {
      sourceBuffer.appendBuffer(mseQueue.shift());
    } catch (e) {
      // 缓冲已经废了（比如被移出父 MediaSource）。这时候再喂只会每帧刷一条同样的错、
      // 白烧 CPU，画面也不会好 —— 停掉喂食，把状态亮出来，别假装还在放。
      DIAG.dec = 'MSE 追加失败：' + e.message;
      diag();
      mseQueue.length = 0;
      msePending.length = 0;
      configured = false;
      decodeMode = '';
      setStatus('画面解码中断，请重新连接设备', 'err');
      return;
    }
    // 只有真的被 SourceBuffer 收下才计数。以前把计数放在调用方、无条件自增，
    // 于是 append 全失败时也会打出「首帧已上屏」—— 这个假信号把 VP9 那条黑屏链路
    // 盖住了整整一轮排查（全都在看「首帧已上屏」以为已经好了）。
    DIAG.posted++;
    if (DIAG.posted === 1) addLog('首帧已上屏（MSE 通道）', 'g');
    if (DIAG.posted % 60 === 1) diag();
  }

  function ensureMSE(codec, avcC) {
    var MS = window.MediaSource || window.WebKitMediaSource;
    if (!MS) { DIAG.dec = '不可用：浏览器没有 MediaSource'; diag(); return false; }
    var mime = 'video/mp4; codecs="' + codec + '"';
    if (!MS.isTypeSupported(mime)) { DIAG.dec = '不可用：不支持 ' + codec; diag(); return false; }

    var gen = decGen;
    switchToVideoSurface();
    var ms = new MS();
    mediaSource = ms;
    videoEl.src = URL.createObjectURL(ms);
    ms.addEventListener('sourceopen', function () {
      // 这一代已经作废（中途 reset 过、或被更新的 MediaSource 顶替）→ 直接扔掉
      if (gen !== decGen || ms !== mediaSource) {
        try { ms.endOfStream(); } catch (e) { /* 忽略 */ }
        return;
      }
      var buf;
      try {
        buf = ms.addSourceBuffer(mime);
      } catch (e) {
        DIAG.dec = 'MSE 建缓冲失败：' + e.message; diag(); return;
      }
      sourceBuffer = buf;
      buf.mode = 'segments';
      buf.addEventListener('error', function () { DIAG.dec = 'MSE 缓冲错误'; diag(); });
      buf.addEventListener('updateend', function () {
        if (sourceBuffer !== buf) return;      // 这一代已经作废，别再动新链路的播放点
        if (mseInitDone) {
          // 重起步是接着旧时间轴走的（mseDts 没归零）：等第一条媒体段真落地之后，
          // 再把播放点挪到时间轴上。提前设没用 —— 那一刻 seekable 还是空的，会被夹回 0，
          // 元素就停在 0 上等一段永远不会有的数据（画面出不来，还会被看门狗反复重起步）。
          // mseResyncTo 就是这一代第一条媒体段的 dts（秒）。
          if (mseResyncTo > 0) {
            var t0 = mseResyncTo; mseResyncTo = 0;
            try { videoEl.currentTime = t0; } catch (e) { /* 忽略 */ }
          }
          pumpMSE();
          return;
        }
        // init 段落地了，才把之前攒下的媒体段放出来
        mseInitDone = true;
        if (msePending.length) {
          for (var i = 0; i < msePending.length; i++) mseQueue.push(msePending[i]);
          msePending.length = 0;
        }
        pumpMSE();
      });
      buf.appendBuffer(Mp4.makeInitSegment(codec, avcC, devW || 1, devH || 1));
    }, { once: true });
    videoEl.play().catch(function () { /* 等用户手势，忽略 */ });
    return true;
  }

  function feedMSE(avcc, isKey, pts) {
    // 正在等关键帧：这期间的 P 帧一律不要 —— 没有起点，解出来只会是花的。
    if (mseNeedKey && !isKey) return;
    if (isKey) mseNeedKey = false;

    // 队列满说明已经跟不上实时了。注意**只能丢新来的帧、不能丢队头**：
    // 队头那一帧是后面几帧的参考，丢了整段就烂；而丢新帧只是让画面停在
    // 最后一个完整帧上，等下面的关键帧一到就无缝接上（时间戳不为丢掉的帧推进，
    // 所以时间轴也不会出现空洞）。
    if (mseQueue.length >= MSE_MAXQ && !isKey) {
      // 只进「等关键帧」状态：丢弃后续非关键帧，等编码器自然产出下一个 IDR。
      // ⚠️ 绝不在这里向设备要关键帧（resetVideo）—— 见上面那段注释，
      // 那会把硬编重启成配不回来的状态，黑到底。等待自然 IDR 只丢一小段画面。
      if (!mseNeedKey) { mseNeedKey = true; DIAG.keyWaits++; }
      DIAG.dropped++;
      return;
    }

    if (pts > 0 && mseLastPts > 0) {
      var gap = pts - mseLastPts;
      if (gap >= 8000 && gap <= 100000) mseDur = gap;
    }
    if (pts > 0) mseLastPts = pts;
    var seg = Mp4.makeMediaSegment(mseSeq++, mseDts, mseDur, avcc, isKey);
    mseDts += mseDur;

    if (!mseInitDone) {
      // init 段还没落地 —— 这时候灌进去必然报错（见 msePending 那段注释），先攒着。
      if (msePending.length < 120) msePending.push(seg);
      return;
    }
    mseQueue.push(seg);
    pumpMSE();
  }

  // 这里原本有个 requestKeyFrame()：队列顶满/跟不上时发一条 MSG_RESET_VIDEO，
  // 想让设备端马上重出一个关键帧，把恢复时间压到几十毫秒。
  // **已删除** —— 实测这条路会把 OMX.redroid.h264.encoder 重启成「只出 IDR、不再补 CONFIG」
  // 的状态，前端再也配不上，就是主人报的「连上一会儿就黑」。
  // 现在只等编码器自然的 IDR（约 1.7s 一个），丢的是一小段画面，不是整条流。

  // 迟到的看门狗：MSE 一旦落后于缓冲末端，延迟只会越积越多（看着越来越卡）；
  // 播放链路一旦卡死，画面就永远冻在那儿不动 —— 收帧还在收，全白费。
  // 这两件事都归这个定时器盯。
  //
  // ⚠️ 这里原本还有一句 `sourceBuffer.remove(0, videoEl.currentTime - 4)` 用来
  // 清掉已播过的缓冲。实测在这套「每帧一个分片、duration 为无穷」的流上，
  // 它会把缓冲整个清空（buffered 从 1 段变 0 段）、播放位置被甩到缓冲之外，
  // 画面从那一刻起**永久冻结**：连上约 8 秒后必冻（因为触发条件是 currentTime > 8），
  // 之后收进来的几百帧全在往一个不动的播放点灌。
  // A/B 对照：开着它最长冻结 7.4 秒、缓冲被清空 39 次；关掉它最长冻结 0.6 秒、一次没清空。
  // 所以裁剪缓冲这条路整个不要了 —— 要回收内存就走重起步（重建链路，旧缓冲自然释放）。
  var latLastSeek = 0;
  var BUFFER_SPAN_LIMIT = 60;      // 缓冲跨度超过这么多秒就重起步，把内存放掉
  var latErr = '';
  var wdLastFrames = -1, wdLastT = -1, wdMuteUntil = 0;
  var reinitAt = 0;                // 刚重起步的时刻，用来发现「重起步后一直没画面」

  // 重起步：只重建**浏览器这一侧**的解码链路 —— 换一条视频 WebSocket，让服务端按
  // 「新订阅者」的既有规矩补发 session + config + GOP（`VideoHub.subscribe` 本来就这么做），
  // 前端拿到干净起点后自己配回来。
  //
  // ⚠️ 绝不再给设备发 MSG_RESET_VIDEO。旧版这里是 `resetDecoder(true)` + `sendControl resetVideo`，
  // 想「让设备重发关键帧」；实测那会把硬编重启成不再补 CONFIG 的状态，前端永久黑。
  // 设备编码器**从头到尾不用动**，流一直是好的，只是浏览器这边要重新挂上去。
  //
  // isStall：是「卡死自愈」还是「主动回收内存」。两者分开计数 ——
  // 主动回收是正常行为，不能算进失败次数里，否则长会话会被误判成「反复卡住」。
  function restartStream(reason, isStall) {
    wdMuteUntil = Date.now() + 6000;       // 重建期间别再看门狗，免得连环触发
    wdLastFrames = -1; wdLastT = -1; wdStuckMs = 0; wdLastTickMs = 0;
    if (isStall) {
      if (DIAG.stalls >= 5) {
        DIAG.dec = '画面反复卡住（已重起步 5 次）';
        setStatus('画面反复卡住，请重新连接设备', 'err');
        diag();
        return;
      }
      DIAG.stalls++;
      addLog('画面卡住（' + reason + '），自己重起步（第 ' + DIAG.stalls + ' 次）', 'y');
      setStatus('画面卡住，正在重新取画面…', 'warn');
    } else {
      DIAG.reinits++;
      addLog('缓冲攒到 ' + reason + '，重起步回收内存（第 ' + DIAG.reinits + ' 次）', 'm');
    }
    reinitAt = Date.now();
    // ⚠️ 必须传 true：只换视频那条链，时间轴接着走。
    // 音频那条链刻意不重建 —— 它没坏，重建只会白丢一段声音（而且 AAC 的配置包
    // 不会重发，重建后 audioAsc 没了，声音就再也回不来）。
    // 但音频不动，视频的时间轴就必须保持连续：一旦回到 0，两边差出整整一个重起步
    // 周期（实测 60 秒），音频对齐就会拿视频的绝对时间把声音倒回一分钟前重放。
    reopenVideoWS(true);
  }

  // 重建视频链路：先摘掉旧 socket（它的所有回调立刻作废，不会再往解码链路里送帧），
  // 再重建浏览器这一侧的解码链路，最后重新订阅 —— 服务端会当成一个新订阅者，
  // 把 `session + config + GOP` 补发过来，正好就是「给我一个干净的起点」。
  // keepTimeline：时间轴是否接着旧链走（见 restartStream 的注释）。
  function reopenVideoWS(keepTimeline) {
    var old = videoWS;
    videoWS = null;                        // 先摘牌：旧 socket 的 onmessage / onclose 全部作废
    if (old) { try { old.close(); } catch (e) { /* 忽略 */ } }
    resetDecoder(keepTimeline);
    openVideoWS();
  }

  // 这个定时器还兼着卡死自愈，而节拍从 1000ms 改成了 250ms，
  // 所以「不动多久算卡死」不能再按拍数数 —— 改成按真实毫秒累计，语义和以前一样。
  var WD_STUCK_MS = 3000;
  var wdStuckMs = 0, wdLastTickMs = 0;

  function tickLatency() {
    tickAudioSync();
    if (decodeMode !== 'mse' || !videoEl || !sourceBuffer) return;

    var b;
    try { b = sourceBuffer.buffered; } catch (e) { b = null; }
    if (b && b.length) {
      var start = 0, end = 0, ok = true;
      try { start = b.start(0); end = b.end(b.length - 1); } catch (e) { ok = false; }
      if (ok && LATENCY_ON) {
        // 播放点落后缓冲末端太多就把播放点推回直播边缘。阈值是 MSE_MAX_LATENCY ——
        // 落后超过它，用户就能看出「画面比操作慢半拍」，这时候立刻纠一次。
        // 纠偏点取 缓冲末端 - MSE_TARGET_LATENCY，留的一点点余量是给下一帧的到达时间。
        // 两次纠偏之间隔 MSE_SEEK_MIN_GAP_MS：seek 本身要解码器重排，动得太勤反而更花。
        var now = Date.now();
        if (end - videoEl.currentTime > MSE_MAX_LATENCY && now - latLastSeek > MSE_SEEK_MIN_GAP_MS) {
          latLastSeek = now;
          try {
            videoEl.currentTime = Math.max(start, end - MSE_TARGET_LATENCY);
            DIAG.latSeeks++;
            addLog('延迟纠偏：把播放点推回直播边缘（第 ' + DIAG.latSeeks + ' 次）', 'y');
          } catch (e) { latErr = String(e); }
        }
      }
      // 缓冲攒得太长就重起步回收内存。
      // ⚠️ 为什么不用 remove() 去裁：见上面那段注释，它会把这套流整死。
      if (ok && end - start > BUFFER_SPAN_LIMIT) {
        restartStream(Math.round(end - start) + ' 秒', false);
        return;
      }
    }

    var nowMs = Date.now();
    var dt = wdLastTickMs ? nowMs - wdLastTickMs : LATENCY_TICK_MS;
    wdLastTickMs = nowMs;

    var newFrames = (DIAG.frames !== wdLastFrames);
    wdLastFrames = DIAG.frames;

    // 重起步之后如果一直没有新帧进来，说明这次重起步没成功 —— 别就那么黑着，
    // 交给看门狗按「卡死」再修一次（这次会算进失败次数，避免无限打转）。
    if (reinitAt && !newFrames && nowMs - reinitAt > 8000) {
      reinitAt = 0;
      restartStream('重起步后 8 秒没收到画面', true);
      return;
    }
    if (newFrames) reinitAt = 0;

    // 卡死自愈。判据要分清两件事：
    //   · 静止画面本来就没有新帧 —— 播放位置不动是应该的，不算卡死；
    //   · 有新帧在进来、播放位置却不动 —— 这才是真卡死。
    // 所以只在「这一拍里收到了新帧」的前提下才累计不动的时间。
    if (nowMs < wdMuteUntil) return;
    var t = videoEl.currentTime;
    if (!newFrames) { wdStuckMs = 0; wdLastT = t; return; }
    if (t !== wdLastT) { wdStuckMs = 0; } else { wdStuckMs += dt; }
    wdLastT = t;
    if (wdStuckMs >= WD_STUCK_MS) {
      wdStuckMs = 0;
      restartStream('连续 ' + Math.round(WD_STUCK_MS / 1000) + ' 秒有帧进来但画面没推进', true);
    }
  }

  // ==================== 网络 ====================
  function api(path, options) {
    return fetch(BASE + path, options).then(function (r) { return r.json(); });
  }

  function postJSON(path, body) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  }

  function wsURL(path) {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + location.host + BASE + path;
  }

  // ==================== 设备列表 ====================
  function phaseClass(d) {
    if (d.phase === 'running') return ' running';
    if (d.phase === 'starting') return ' starting';
    if (d.phase === 'error') return ' error';
    return '';
  }

  function phaseText(d) {
    if (d.phase === 'running') {
      var dim = (d.width && d.height) ? (d.width + '×' + d.height + ' ') : '';
      return '投屏中 ' + dim + (d.codec || '');
    }
    if (d.phase === 'starting') return '连接中…';
    if (d.phase === 'error') return '失败：' + (d.error || '未知错误');
    if (d.state === 'device') return '已连接（未投屏）';
    if (d.state === 'offline') return '离线';
    if (d.state === 'unauthorized') return '未授权';
    return d.state || '未知';
  }

  // 设备标题：优先用型号名，但同型号撞车时（比如两台都是 redroid13_x86_64）
  // 光看型号分不清谁是谁，这种就退回 serial —— serial 天生唯一。
  // 列表和顶部标题共用这一套，免得两处显示不一致。
  function labelOf(d) {
    var mine = (d && d.name) || '';           // 自己起的名字最优先
    if (mine) return mine;
    var model = d.device || '';
    if (!model) return d.serial;
    var same = 0;
    for (var i = 0; i < devices.length; i++) if ((devices[i].device || '') === model) same++;
    return same > 1 ? d.serial : model;
  }

  function labelFor(serial) {
    for (var i = 0; i < devices.length; i++) if (devices[i].serial === serial) return labelOf(devices[i]);
    return serial;
  }

  function renderDevices() {
    var box = $('devices');
    if (!box) return;
    if (!devices.length) {
      box.innerHTML = '<div class="empty">还没有设备<br>点上面「添加设备」加一台</div>';
      syncSideActions();
      return;
    }
    box.textContent = '';
    devices.forEach(function (d) {
      var el = document.createElement('div');
      el.className = 'dev' + (d.serial === active ? ' active' : '') + phaseClass(d);
      el.title = d.serial;

      var dot = document.createElement('span');
      dot.className = 'sd';

      var txt = document.createElement('span');
      txt.className = 'txt';
      var nm = document.createElement('span');
      nm.className = 'nm';
      var label = labelOf(d);
      nm.textContent = label;
      var sub = document.createElement('span');
      sub.className = 'sub';
      // 标题已经是 serial 了就别在副标题里再重复一遍
      sub.textContent = (label === d.serial) ? phaseText(d) : (d.serial + ' · ' + phaseText(d));
      txt.appendChild(nm); txt.appendChild(sub);

      var x = document.createElement('button');
      x.className = 'x';
      x.textContent = '×';
      x.title = '断开投屏（设备保留在列表里）';
      x.addEventListener('click', function (ev) {
        ev.stopPropagation();
        // ⚠️ 这个 × 是「断开」，不是「删除」。
        // 以前它也走 askRemoveDevice —— 于是「×」和侧栏「删除设备」是同一个动作，
        // 想先停一下却把设备从列表里抹掉了。删除不可逆，只能走侧栏那个按钮并二次确认。
        disconnectDevice(d.serial);
      });

      el.appendChild(dot); el.appendChild(txt);
      // × 只在「确实有东西可断」的时候出现：没连上/已断开的设备挂个 × 出来只会误点
      if (d.phase === 'running' || d.phase === 'starting') el.appendChild(x);
      el.addEventListener('click', function () { activate(d.serial); });
      box.appendChild(el);
    });
    syncSideActions();
  }

  // 侧栏按钮状态跟着选中项走：
  //   「断开」只在选中项确实在投屏时可用（没连上的设备没什么可断的）
  //   「删除设备」只要选中了就可用（删除不需要先连接）
  function syncSideActions() {
    var me = null;
    for (var i = 0; i < devices.length; i++) if (devices[i].serial === active) me = devices[i];
    var live = !!(me && (me.phase === 'running' || me.phase === 'starting'));

    var dc = $('btnDisconnect');
    if (dc) {
      dc.disabled = !live;
      dc.title = !active ? '先选一台设备' : (live ? ('断开 ' + active) : '这台设备当前没在投屏');
    }
    var rm = $('btnRemove');
    if (rm) {
      rm.disabled = !active;
      rm.title = active ? ('删除 ' + active) : '先选一台设备';
    }
  }

  function refreshDevices() {
    return api('/api/devices').then(function (d) {
      if (!d.ok) return;
      devices = d.devices || [];
      renderDevices();
      // 设置弹窗开着的时候设备状态可能刚被读出来（连上之后那几秒），跟着刷一下。
      if (openModalId === 'setModal') renderDevInfo();
      reconcile();
      fillFoundDevices();
    }).catch(function () { /* 服务刚起来时会失败，下一轮再来 */ });
  }

  // 会话状态变了要把界面跟上去：连上了就挂流，停了就摘掉。
  function reconcile() {
    var me = null;
    for (var i = 0; i < devices.length; i++) {
      if (devices[i].serial === active) { me = devices[i]; break; }
    }
    updateHead(me);

    // 设备端明说采不到声音（Android 11 以下、或者没有音频输出设备）：
    // 那条音频 WS 会一直干等着，早点告诉用户，别让人以为是自己设置错了。
    if (me && me.audio === 'off' && audioWS && !audioOffWarned) {
      audioOffWarned = true;
      addLog('音频：设备端采不到声音，这次只有画面', 'y');
      DIAG.audio = '设备端采不到声音';
      try { audioWS.close(); } catch (e) { /* 忽略 */ }
    }

    if (active === null) return;
    if (!me) {                       // 设备从列表里消失了
      if (attached === active) detach('设备已从列表移除');
      return;
    }
    if (me.phase === 'running') {
      setStatus('已连接 ' + (me.device || '') + (me.width ? ' · ' + me.width + '×' + me.height : ''), 'ok');
      $('brand').classList.add('live');
      if (attached !== active) attachStream();
    } else if (me.phase === 'starting') {
      setStatus('正在启动投屏…');
      if (attached === active) detach(null);
    } else if (me.phase === 'error') {
      setStatus(me.error || '启动失败', 'err');
      $('brand').classList.remove('live');
      if (attached === active) detach(null);
    } else {
      setStatus('未连接');
      $('brand').classList.remove('live');
      if (attached === active) detach(null);
    }
  }

  function updateHead(me) {
    $('curName').textContent = active ? labelFor(active) : '未选择';
    $('curMeta').textContent = (me && me.width) ? (me.width + '×' + me.height + ' · ' + (me.codec || '')) : '';
    // 文件 / 终端都是「对着某台设备干活」的，没选设备时按钮直接灰掉
    $('btnSettings').disabled = !active;
    $('btnFiles').disabled = !active;
    $('btnShell').disabled = !active;
    // 设备被删掉（active 变 null）时，开着的文件/终端抽屉得跟着关 ——
    // 不然它会挂在一台已经不存在的设备上，点什么都报错。
    // （只关这两个：日志跟设备无关，别顺手把它也拍上。）
    if (!active) {
      if (openDrawerId === 'shellPanel') setDrawer('shellPanel', false);
      if (openDrawerId === 'filePanel') setDrawer('filePanel', false);
    }
  }

  // 「添加设备」弹窗里的候选列表 —— 数据来自 /api/adb（adb 扫到的设备），
  // 不是左侧那份「我添加过的设备」。点一下把 IP/端口填进去。
  function fillFoundDevices() {
    var box = $('connFound');
    if (!box) return;
    box.innerHTML = '<span class="hint" style="color:#5b6780">查询中…</span>';
    api('/api/adb').then(function (d) {
      var list = (d && d.devices) || [];
      box.textContent = '';
      if (!list.length) {
        box.innerHTML = '<span class="hint" style="color:#5b6780">adb 里没看到设备，手动填 IP 也行</span>';
        return;
      }
      list.forEach(function (d) {
        var b = document.createElement('button');
        b.type = 'button';
        b.textContent = d.serial + (d.added ? '（已添加）' : '');
        b.addEventListener('click', function () { fillAddr(d.serial); });
        box.appendChild(b);
      });
    }).catch(function () {
      box.innerHTML = '<span class="hint" style="color:#5b6780">查询失败，手动填吧</span>';
    });
  }

  // 从「adb 已发现的设备」点进来的原始 serial。
  // adb 里的 serial 不一定长成 ip:port —— 模拟器是 emulator-5554、USB 机器是一串硬件号，
  // 这些本来就没有端口。要还按「主机 + 默认 5555」去拼，就会拼出 emulator-5554:5555 这种
  // 根本不存在的地址，连过去只会得到 "device 'emulator-5554:5555' not found"。
  // 所以点候选时把原样记下来，提交优先用它；用户手动改了输入框就作废。
  var connExactSerial = '';

  // 把 "ip:port" 拆开填进两个输入框；没有端口就留空（默认端口在提交时补）。
  function fillAddr(serial) {
    var s = String(serial || '');
    connExactSerial = s;
    var i = s.lastIndexOf(':');
    if (i > 0 && /^\d+$/.test(s.slice(i + 1))) {
      $('connHost').value = s.slice(0, i);
      $('connPort').value = s.slice(i + 1);
    } else {
      $('connHost').value = s;
      $('connPort').value = '';
    }
  }

  // ==================== 连接 / 断开 ====================
  function connectDevice(serial, extraSettings) {
    var s = Object.assign({}, settingsOf(serial), extraSettings || {});
    addLog('连接 ' + serial + '（' + (s.maxSize ? s.maxSize + 'p' : '原画') + ' / ' + s.maxFps + 'fps / ' + fmtRate(s.bitRate) + '）');
    setStatus('正在连接…');
    return postJSON('/api/connect', { serial: serial, settings: s }).then(function (d) {
      if (!d.ok) {
        setStatus(d.error || '启动失败', 'err');
        addLog('连接失败：' + (d.error || ''), 'r');
        return;
      }
      startFastPoll();
      refreshDevices();
    }).catch(function (e) {
      setStatus('请求失败：' + e.message, 'err');
    });
  }

  // ---- 断开（可逆，不弹确认）----
  // 断开 = 只停这一台设备的投屏。设备记录、本机为它存的投屏设置全都留着，
  // 下次点一下就能接着连。语义上跟「删除设备」是两件事，入口也分开（见 renderDevices 的 ×）。
  function disconnectDevice(serial) {
    if (!serial) return Promise.resolve();
    return postJSON('/api/disconnect', { serial: serial }).then(function (d) {
      if (d && d.ok === false) { setStatus('断开失败：' + (d.error || ''), 'err'); return; }
      if (attached === serial) detach('已断开');
      setStatus('已断开 ' + serial + '（设备还在列表里）', 'ok');
      refreshDevices();
    }).catch(function (e) { setStatus('请求失败：' + e.message, 'err'); });
  }

  // ---- 删除设备（不可逆，必须确认）----
  // 删除 = 停掉投屏 + 把这条从「我添加过的设备」里抹掉 + 清掉本机存的投屏设置。
  // 以前这里只有「断开」，而列表来自 adb，所以断开后设备还在 adb 里、下一轮又刷回来，
  // 点多少次都删不掉。

  function askRemoveDevice(serial) {
    if (!serial) return;
    var cur = null;
    for (var i = 0; i < devices.length; i++) if (devices[i].serial === serial) cur = devices[i];
    askConfirm(
      '确定要删除设备「' + labelOf(cur || { serial: serial }) + '」吗？\n\n'
      + '删除会停掉它的投屏，把它从设备列表里移除，本机为它保存的投屏设置也一并清掉。\n'
      + '只是先停一下、以后还要用的话，请改点「断开」或者设备行上的 ×。',
      '删除',
      function (msgEl) {
        msgEl.textContent = '正在删除…';
        postJSON('/api/devices/remove', { serial: serial }).then(function (d) {
          if (d && d.ok === false) { msgEl.textContent = d.error || '删除失败'; return; }
          closeModal();
          forgetSettings(serial);
          if (attached === serial) detach(null);
          if (active === serial) active = null;
          // 抽屉不再被 closeModal 连坐关掉，所以这里得自己收一下：
          // 被删的那台要是正被文件面板看着，列表得退回"没选设备"的状态。
          if (openDrawerId === 'filePanel') fileList(null);
          addLog('已删除设备：' + serial, 'm');
          setStatus('已删除 ' + serial, 'ok');
          refreshDevices();
        }).catch(function (e) {
          msgEl.textContent = '请求失败：' + e.message;
        });
      });
  }

  function forgetSettings(serial) {
    var all = loadAllSettings();
    if (all && Object.prototype.hasOwnProperty.call(all, serial)) {
      delete all[serial];
      try { localStorage.setItem(SET_KEY, JSON.stringify(all)); } catch (e) { /* 忽略 */ }
    }
  }

  $('btnRemove').addEventListener('click', function () { askRemoveDevice(active); });
  $('btnDisconnect').addEventListener('click', function () { disconnectDevice(active); });

  function activate(serial) {
    if (serial === active && attached === serial) return;
    if (attached && attached !== serial) detach(null);
    var changed = (serial !== active);
    active = serial;
    // 换设备了：开着的终端是接在旧设备上的，得重新起一条；文件抽屉要回到根目录重读。
    // 放在 active 赋值**之后** —— 这两个动作都是拿 active 去连的。
    if (changed) onActiveChanged();
    renderDevices();
    updateHead(null);
    var me = null;
    for (var i = 0; i < devices.length; i++) if (devices[i].serial === serial) { me = devices[i]; break; }

    if (!me) { connectDevice(serial); return; }
    if (me.phase === 'running') { attachStream(); return; }
    if (me.phase === 'starting') { setStatus('正在启动投屏…'); startFastPoll(); return; }
    connectDevice(serial);
  }

  // 列表轮询：平时慢一点（每轮都要起一次 adb），有会话在起的时候快一点。
  function startFastPoll() {
    clearTimeout(fastPoll);
    fastPoll = setTimeout(function () {
      refreshDevices().then(function () {
        var busy = devices.some(function (d) { return d.phase === 'starting'; });
        if (busy) startFastPoll();
      });
    }, 700);
  }

  // ==================== 视频 / 控制通道 ====================
  function detach(reason) {
    streaming = false;
    attached = null;
    clearInterval(latencyTimer);
    closeWS();
    resetDecoder();
    resetAudio();
    devW = devH = 0;
    canvas.classList.add('off');
    placeholder.style.display = '';
    setBarEnabled(false);
    if (reason) addLog(reason, 'm');
  }

  function attachStream() {
    if (!active) return;
    // reconcile 和 activate 都会叫到这里，重复进来会把刚配好的解码器又掀一次
    if (attached === active && videoWS && controlWS) return;
    // 会话报上来的实际编码决定用哪套 NAL 解析 / 配置盒（H.264 用 avcC、H.265 用 hvcC），
    // 必须在 openVideoWS 之前定下来：config 包一到 configureFrom 就要靠它分路。
    // 注意 codec 是「实际编码」不是「请求值」—— 选 auto 时设备回落成 h264，这里拿到的就是 h264。
    for (var di = 0; di < devices.length; di++) {
      if (devices[di].serial === active) { sessCodec = devices[di].codec || 'h264'; break; }
    }
    attached = active;
    devW = devH = 0;
    resetDecoder();
    resetAudio();
    statReset();          // 换设备/重连就从零开始计量（峰值、累计流量、连接时长都归位）
    placeholder.style.display = 'none';
    canvas.classList.remove('off');
    streaming = true;
    setBarEnabled(true);
    openControlWS();
    openVideoWS();
    if (settingsOf(active).audio) openAudioWS();
    try { activeSurface().focus(); } catch (e) { /* 忽略 */ }
    clearInterval(latencyTimer);
    // 250ms 一拍：既要及时发现「落后到 0.5 秒」，也要够轻。一秒一拍的话，
    // 阈值再小也得等一整秒才反应过来，低延迟就成了纸面上的。
    latencyTimer = setInterval(tickLatency, LATENCY_TICK_MS);
  }

  function closeWS() {
    clearInterval(keepaliveTimer);
    [videoWS, controlWS, audioWS].forEach(function (ws) {
      if (ws) { try { ws.close(); } catch (e) { /* 忽略 */ } }
    });
    videoWS = controlWS = audioWS = null;
    DIAG.wsVideo = DIAG.wsControl = '未连';
    diag();
  }

  function openControlWS() {
    var serial = active;
    controlWS = new WebSocket(wsURL('/ws/control?serial=' + encodeURIComponent(serial)));
    controlWS.onopen = function () {
      DIAG.wsControl = '已连';
      diag();
      addLog('控制通道已连接', 'g');
      keepaliveTimer = setInterval(function () {
        if (controlWS && controlWS.readyState === 1) controlWS.send(new ArrayBuffer(0));
      }, 10000);
    };
    controlWS.onclose = function () {
      clearInterval(keepaliveTimer);
      DIAG.wsControl = '已断开';
      diag();
      if (streaming && active === serial) { addLog('控制通道已断开', 'y'); refreshDevices(); }
    };
    controlWS.onerror = function () { DIAG.wsControl = '出错'; diag(); addLog('控制通道出错', 'r'); };
  }

  function sendControl(obj) {
    if (!controlWS || controlWS.readyState !== 1) return;
    controlWS.send(JSON.stringify(obj));
  }

  function openVideoWS() {
    var serial = active;
    // ⚠️ 这条 socket 存局部变量，回调里先确认它还是「当前这一条」。
    // 只把全局 videoWS 换成新的、却不关旧的，旧的还会继续往解码链路里送帧 ——
    // 两路独立的 H.264 交错 append 进同一个 MSE，参考链互相踩，
    // 画面就是满屏宏块，而且不等到下一个 IDR 永不自愈。
    var ws = new WebSocket(wsURL('/ws/video?serial=' + encodeURIComponent(serial)));
    videoWS = ws;
    ws.binaryType = 'arraybuffer';
    ws.onmessage = function (ev) {
      if (ws !== videoWS) return;              // 作废的 socket：一个字都不许再进
      if (typeof ev.data === 'string') return;
      var buf = ev.data;
      if (buf.byteLength < 12) return;
      var dv = new DataView(buf);
      var hi = dv.getUint32(0);
      var lo = dv.getUint32(4);

      if (hi & 0x80000000) {                 // 会话头：宽高变了
        var w = dv.getUint32(4), h = dv.getUint32(8);
        DIAG.sessions++;
        addLog('会话头 #' + DIAG.sessions + ' ' + w + '×' + h
          + '（当前 ' + devW + '×' + devH + '）' + (w === devW && h === devH ? ' 尺寸未变' : ' 尺寸变了'));
        if (w !== devW || h !== devH) {
          devW = w; devH = h;
          canvas.width = w; canvas.height = h;
          resetDecoder(true);      // 尺寸变了要换 init 段，但时间轴要接着走
          resizeCanvas();
          addLog('会话尺寸 ' + w + '×' + h);
        }
        diag();
        return;
      }

      var isConfig = (hi & 0x40000000) !== 0;   // bit62
      var isKey = (hi & 0x20000000) !== 0;      // bit61
      var size = dv.getUint32(8);
      if (size <= 0 || buf.byteLength < 12 + size) return;
      // 状态面板的原始计数。只做加法（这条路上每帧一次，不能有别的开销）：
      // 码率算的是**码流字节**，所以 12 字节的 scrcpy 包头不算进去。
      STATS.winBytes += size;
      STATS.rxBytes += size;
      var payload = new Uint8Array(buf, 12, size);
      // vp8 / vp9 / av1 的载荷是自带边界的原始帧，**不能**按 NAL 去扫起始码 ——
      // 扫了会把帧切碎，MSE 收到一堆碎片直接黑屏。
      var family = codecFamily(sessCodec);
      var isNal = (family === 'avc' || family === 'hevc');
      var units = isNal ? nalUnits(payload) : null;

      if (isConfig) {
        DIAG.configs++;
        if (!isNal) {
          // 这三种编码设备端本来就给不出 csd，正常不会有 config 包。
          // 万一来了也直接丢 —— 当帧喂进去只会污染解码器。
          addLog('收到 ' + sessCodec.toUpperCase() + ' 的 config 包（无 extradata 概念，丢弃）', 'y');
          diag();
          return;
        }
        var spsNal = (sessCodec === 'h265') ? Mp4.findNalHevc(units, 33) : findNal(units, 7);
        var spsHex = hexOf(spsNal);
        if (!DIAG.spsFirst) DIAG.spsFirst = spsHex;
        DIAG.spsLast = spsHex;
        DIAG.spsSame = (DIAG.spsFirst === spsHex);
        addLog('config 包 #' + DIAG.configs + '（' + size + ' 字节）configured=' + configured
          + ' sps=' + spsHex + (DIAG.spsSame ? '（与本会话首个一致）' : '（与首个不同）'));
        configureFrom(units);
        diag();
        return;
      }
      DIAG.frames++;
      STATS.winFrames++;
      STATS.rxFrames++;
      if (DIAG.frames === 1) addLog('收到首个视频帧' + (isKey ? '（关键帧）' : ''));
      if (!configured) {
        // 没有 config 包的那几种，等第一个关键帧来当起点
        if (!isNal) configureFromKeyFrame(payload);
        if (!configured) return;
      }

      var pts = (hi & 0x1FFFFFFF) * 4294967296 + lo;
      var sample = isNal ? annexBtoAVCC(units) : payload;

      if (decodeMode === 'mse') { feedMSE(sample, isKey, pts); return; }
      if (!decoder) return;
      try {
        decoder.decode(new EncodedVideoChunk({
          type: isKey ? 'key' : 'delta',
          timestamp: pts,
          data: sample
        }));
      } catch (e) {
        DIAG.dec = '送解码失败：' + e.message;
        diag();
      }
    };
    ws.onopen = function () {
      if (ws !== videoWS) return;
      DIAG.wsVideo = '已连'; diag(); addLog('视频通道已连接', 'g');
    };
    ws.onclose = function () {
      if (ws !== videoWS) return;              // 主动换台时自己关的，不用报
      DIAG.wsVideo = '已断开';
      diag();
      if (streaming && active === serial) { addLog('视频通道已断开', 'y'); refreshDevices(); }
    };
    ws.onerror = function () {
      if (ws !== videoWS) return;
      DIAG.wsVideo = '出错'; diag(); addLog('视频通道出错', 'r');
    };
  }

  // ==================== 音频通道 ====================
  // 单独一条 MSE，挂在隐藏的 <video> 上 —— 刻意不跟视频共用 MediaSource。
  // 理由：视频那条路会在 WebCodecs / MSE 之间来回切（花屏那几轮改怕了），
  // 音频分开之后，视频怎么折腾都波及不到它。
  // 而且 AAC 每帧都能独立解，不需要视频那套「等关键帧」的机制。
  var audioEl = null, audioMS = null, audioSB = null, audioWS = null;
  var audioQueue = [], audioPending = [], audioInitDone = false;
  var audioSeq = 1, audioGen = 0;
  var audioAsc = null, audioCodecStr = '', audioDur = 21333;
  var audioBasePts = -1, audioDts = 0, audioDtsBase = 0, audioGot = 0, audioOffWarned = false;
  // 40 帧 ≈ 0.85 秒（AAC 一帧 1024 采样 / 48kHz = 21.3ms）。
  // 旧值是 90（约 2 秒）—— 音频在队列里攒两秒，就是「画面已经动了、声音还差两秒」，
  // 再怎么用 playbackRate 伺服也追不回来（±3% 追两秒要一分钟）。所以上限必须收进 1 秒以内：
  // 让「最坏情况下的滞后」本身就落在能接受的量级，伺服才有意义。
  // 满了丢最老的（音频每帧独立可解，丢一帧不会像 H.264 那样把后面的全毁掉）。
  var AUDIO_MAXQ = 40;

  function resetAudio() {
    audioGen++;
    audioQueue.length = 0;
    audioPending.length = 0;
    audioInitDone = false;
    audioSeq = 1; audioAsc = null; audioCodecStr = '';
    audioBasePts = -1; audioDts = 0; audioDtsBase = 0; audioDur = 21333;
    audioGot = 0; audioOffWarned = false;
    audioOffSkip = false; audioServoLogged = false; audioServoSince = 0; audioServoTripped = false;
    // 音频这条链重来 = 整条会话重来（resetAudio 只在 detach / attachStream 里调），
    // WebCodecs 的相对视频时钟也必须跟着归零，否则新会话会继承上一轮的流逝时间，
    // 音频锚点被顶到几十秒外，直接触发「时间轴错开」而不做任何同步。
    wcClockReset();
    if (audioSB) { try { audioSB.abort(); } catch (e) { /* 忽略 */ } audioSB = null; }
    if (audioMS) { try { audioMS.endOfStream(); } catch (e) { /* 忽略 */ } audioMS = null; }
    if (audioEl) {
      try { audioEl.pause(); } catch (e) { /* 忽略 */ }
      try { audioEl.removeAttribute('src'); audioEl.load(); } catch (e) { /* 忽略 */ }
      if (audioEl.parentNode) audioEl.parentNode.removeChild(audioEl);
      audioEl = null;
    }
    DIAG.audio = '未开始';
    diag();
  }

  function ensureAudioEl() {
    if (audioEl) return audioEl;
    var el = document.createElement('video');
    el.autoplay = true;
    el.playsInline = true;
    // 要出声，就不能 muted。能不能自动播由浏览器决定，播不了一按画面就恢复。
    el.muted = false;
    // 变速对齐时要保持音高。浏览器默认就是 true，这里写死是为了不被将来的默认值改动坑到 ——
    // 关掉的话 ±3% 的速率变化会变成 ±3% 的音高变化，那就成了能听出来的走调。
    try { el.preservesPitch = true; } catch (e) { /* 老浏览器没这个属性 */ }
    // 用 1px 透明元素而不是 display:none —— 隐藏元素上 MSE 的缓冲行为不保证一致
    el.style.cssText = 'position:absolute;width:1px;height:1px;opacity:0;'
      + 'pointer-events:none;left:-9px;top:-9px;';
    var stage = $('stage');
    (stage || document.body).appendChild(el);
    audioEl = el;
    return el;
  }

  function audioBitRateNow() {
    return active ? settingsOf(active).audioBitRate : 128000;
  }

  // 拿到 AudioSpecificConfig 才知道该用哪个 codec string，所以音频管线是这时候才建的
  function onAudioConfig(ascBytes) {
    audioAsc = ascBytes;
    var info = Mp4.parseAsc(ascBytes) || { objectType: 2, sampleRate: 48000, channels: 2 };
    audioCodecStr = Mp4.aacCodecString(info.objectType);
    // AAC 一帧固定 1024 个采样
    audioDur = Math.round(1024 * 1000000 / info.sampleRate);
    DIAG.audio = 'AAC ' + info.sampleRate + 'Hz / ' + info.channels + 'ch';
    addLog('音频已就绪：' + DIAG.audio, 'g');
    diag();
    buildAudioPipeline();
  }

  function buildAudioPipeline() {
    if (audioSB || !audioAsc) return;
    var MS = window.MediaSource || window.WebKitMediaSource;
    if (!MS) { DIAG.audio = '不可用：浏览器没有 MediaSource'; diag(); return; }
    var mime = 'audio/mp4; codecs="' + audioCodecStr + '"';
    if (!MS.isTypeSupported(mime)) {
      DIAG.audio = '浏览器不支持 ' + audioCodecStr;
      addLog('音频：浏览器不支持 ' + audioCodecStr + '，这次只有画面', 'y');
      diag();
      return;
    }
    var gen = audioGen;
    var el = ensureAudioEl();
    var ms = new MS();
    audioMS = ms;
    el.src = URL.createObjectURL(ms);
    ms.addEventListener('sourceopen', function () {
      if (gen !== audioGen || ms !== audioMS) {
        try { ms.endOfStream(); } catch (e) { /* 忽略 */ }
        return;
      }
      var buf;
      try {
        buf = ms.addSourceBuffer(mime);
      } catch (e) {
        DIAG.audio = '音频建缓冲失败：' + e.message; diag(); return;
      }
      audioSB = buf;
      buf.mode = 'segments';
      buf.addEventListener('error', function () { DIAG.audio = '音频缓冲错误'; diag(); });
      buf.addEventListener('updateend', function () {
        if (!audioInitDone) {
          audioInitDone = true;
          for (var i = 0; i < audioPending.length; i++) audioQueue.push(audioPending[i]);
          audioPending.length = 0;
        }
        pumpAudio();
      });
      // 和视频一样的硬规矩：init 段必须先于任何媒体段进 SourceBuffer
      buf.appendBuffer(Mp4.makeAudioInitSegment(audioAsc, audioBitRateNow()));
    }, { once: true });
    el.play().catch(function () {
      DIAG.audio = '浏览器拦着不让自动出声，点一下画面就恢复';
      diag();
    });
    diag();
  }

  function pumpAudio() {
    if (!audioSB || audioSB.updating || !audioQueue.length) return;
    try {
      audioSB.appendBuffer(audioQueue.shift());
    } catch (e) {
      DIAG.audio = '音频追加失败：' + e.message;
      diag();
      audioQueue.length = 0;
      audioPending.length = 0;
      audioSB = null;
    }
  }

  // 音视频各挂一个 <video>，两个元素的起播点可能对不齐。这里管的是**长期漂移**。
  //
  // ⚠️ 以前的做法是直接改 currentTime（= seek）：那是硬切。MSE 得从新位置重新取数据、
  // 输出要断一下 —— 听感就是「咔」一下，或者半拍空掉。基线实测它偏偏落在起播后 1.6 秒
  // 和 30.7 秒，正是「时不时卡一下」的来源。
  //
  // 现在改成**微调播放速率**：偏了就用 ±3%（偏得多时 ±6%）追一会儿，追进死区就回到 1.0。
  // 配合 preservesPitch，变速只改时长、不改音高，基本听不出来；关键是过程连续，
  // 不会产生任何空洞。速率伺服本来就是媒体播放器做 AV 同步的标准手段，seek 是最后手段。
  var audioOffSkip = false;       // 「差得太多、判定为时间轴错开、这次不追」只记一次
  var audioServoLogged = false;   // 伺服第一次启动记一条，方便取证
  var audioServoSince = 0;        // 本轮变速从什么时候开始，超时就收手
  var audioServoTripped = false;  // 超时收手过：只记一条日志，回到死区后重新武装
  var AUDIO_SYNC_MAX = 2.0;       // 超过这么多秒就不叫漂移了，是两边的时间轴起点不一样
  var AUDIO_STEP = 0.03;          // 常规追赶速率 ±3%
  var AUDIO_STEP_BIG = 0.06;      // 偏得较多时 ±6%
  var AUDIO_SERVO_TIMEOUT = 20000; // 追 20 秒还收不进去就别一直挂着变速
  // ⚠️ 这里的符号翻过一次车，写清楚：
  //   两个元素都是按真实时间往前放的。audioEl.currentTime **小于** videoEl.currentTime，
  //   意思是声音此刻正在放的是更早那一刻的内容 —— 先看到、后听到，也就是**声音落后于画面**。
  //   所以 late = video.ct - audio.ct，正数 = 声音比画面晚。
  //   晚 → 加速追（rate>1，会吃掉缓冲余量，但不同步更难受）；
  //   早 → 减速等（rate<1，缓冲余量反而变大，是安全的一边）。
  // 死区两边不一样宽：正常起播后声音就是结构性地晚一点（数据到得晚），
  // 这个「晚」不能去追（追它等于永远在抽缓冲余量）；而「早」很出戏，门槛要小。
  var AUDIO_LATE_TOL = 0.30;      // 声音最多允许比画面晚 300ms
  var AUDIO_EARLY_TOL = 0.12;     // 声音比画面早超过 120ms 就要等一等

  function setAudioRate(r) {
    if (!audioEl) return;
    if (Math.abs(audioEl.playbackRate - r) < 1e-3) return;
    try { audioEl.playbackRate = r; } catch (e) { /* 忽略 */ }
  }

  function tickAudioSync() {
    if (!audioEl || !audioSB) return;
    // ⚠️ 这里**不能**再写 `|| !videoEl`：WebCodecs 那条路本来就没有 videoEl，
    // 一 return 就等于「这条路上音频永远没人管」，只能眼睁睁看着它漂到几秒后。
    // 统一走 videoClockSec —— 它自己会按有没有 <video> 决定用哪条时钟。
    var vc = videoClockSec();
    if (!isFinite(vc)) return;                              // 视频时钟还没起来，这一拍先不动
    if (!audioEl.buffered.length || audioEl.currentTime < 1 || vc < 1) return;
    var late = vc - audioEl.currentTime;                    // >0 声音比画面晚

    // 差出好几秒就不是「漂移」了，是两边的时间轴起点不一样（视频重起步会把它的
    // 时间轴拉回 0，音频那条一直是连续的）。这时候按视频的绝对时间去追，
    // 等于把声音倒回一分钟前重放一整轮 —— 宁可先不动，也不能让它跳。
    if (Math.abs(late) > AUDIO_SYNC_MAX) {
      if (audioEl.playbackRate !== 1) setAudioRate(1);
      if (!audioOffSkip) {
        audioOffSkip = true;
        addLog('音频与画面时间轴错开 ' + Math.round(Math.abs(late)) + ' 秒，先不追', 'y');
      }
      return;
    }
    var want = late > AUDIO_LATE_TOL ? 1 : (late < -AUDIO_EARLY_TOL ? -1 : 0);
    if (!want) {
      if (audioEl.playbackRate !== 1) setAudioRate(1);
      audioServoSince = 0;
      audioServoTripped = false;      // 回到容忍范围内了，武装下一次（不是一票否决）
      return;
    }
    var now = Date.now();
    if (!audioServoSince) audioServoSince = now;
    // 追了 20 秒还在外面：要么设备音频时钟跟本机差得离谱，要么一直有东西在把它顶出去。
    // 先把变速停掉（长时间挂着一个非 1 的速率会把缓冲余量抽干），但**只是暂停**，不是放弃 ——
    // 等它自己回到容忍范围里会自动重新武装。
    if (now - audioServoSince > AUDIO_SERVO_TIMEOUT) {
      if (audioEl.playbackRate !== 1) setAudioRate(1);
      audioServoSince = 0;
      if (!audioServoTripped) {
        audioServoTripped = true;
        addLog('音频持续追不平（还差 ' + Math.round(Math.abs(late) * 1000) + ' ms），暂停变速', 'y');
      }
      return;
    }
    if (!audioServoLogged) {
      audioServoLogged = true;
      addLog('音频比画面' + (late > 0 ? '晚' : '早') + ' ' + Math.round(Math.abs(late) * 1000)
        + ' ms，用速率微调追赶（不动播放点）', 'm');
    }
    var step = Math.abs(late) > 0.4 ? AUDIO_STEP_BIG : AUDIO_STEP;
    setAudioRate(want > 0 ? 1 + step : 1 - step);
  }

  // 音频起播点该落在视频时间轴的哪个位置：
  //   视频时钟可用（MSE 的播放头 / WebCodecs 的相对时钟）→ 取它，再往后让 AUDIO_LEAD 一点点
  //   视频还没开动 → 退回 mseDts，那是此刻唯一有意义的位置
  // 为什么优先用视频时钟而不是 mseDts：mseDts 是视频缓冲的**写指针**，比播放头靠前
  // 半秒上下。锚在写指针上，音频一上来就领先画面，随后又得被切回来 —— 那一下就是断续。
  // ⚠️ 不要在这里加「提前量」。实测起播后声音本来就结构性地晚 100~150ms
  //（音频数据到得比画面晚），再加提前量只会让它更晚，那就奔着「能看出来」去了。
  // ⚠️ 必须走 videoClockSec 而不是直接看 videoEl：WebCodecs 那条路没有 videoEl，
  //    只看 videoEl 的话锚点会退化到 mseDts（WebCodecs 下恒为 0），两边时间轴对不上。
  var AUDIO_LEAD = 0;
  function audioAnchorSec() {
    var vc = videoClockSec();
    if (isFinite(vc) && vc > 0.05) return vc + AUDIO_LEAD;
    return mseDts / 1000000;
  }

  function feedAudio(isConfig, pts, payload) {
    if (isConfig) { onAudioConfig(payload); return; }
    if (!audioAsc) return;                 // 配置包一定在前面，没配置就还没开始

    var dts;
    if (pts > 0) {
      if (audioBasePts < 0) {
        audioBasePts = pts;
        // ⚠️ 起点必须落在**视频时间轴的当前位置**上，不能从 0 开始。
        // 视频那条 WS 订阅时会补发「最近一个关键帧起的一整段」，所以它的时间轴
        // 起点比「现在」早了整整一个补发段的长度（实测 ~1.5 秒）；音频只补发 config，
        // 起点就是「现在」。两边都从 0 算的话，画面前、声音后，差出整整一个补发段。
        audioDtsBase = Math.round(audioAnchorSec() * 1000000);
      }
      dts = audioDtsBase + (pts - audioBasePts);
      if (dts < audioDts) dts = audioDts;    // pts 偶尔抖回去的话不能让时间轴倒退
    } else {
      dts = audioDts;
    }
    if (dts < 0) dts = 0;
    audioDts = dts + audioDur;

    var seg = Mp4.makeMediaSegment(audioSeq++, dts, audioDur, payload, true);
    if (!audioInitDone) {
      if (audioPending.length < 400) audioPending.push(seg);
      return;
    }
    if (audioQueue.length >= AUDIO_MAXQ) {
      audioQueue.shift();                  // 音频不做「等关键帧」，直接丢最老的
      DIAG.audioDropped = (DIAG.audioDropped || 0) + 1;
    }
    audioQueue.push(seg);
    pumpAudio();
    audioGot++;
    if (audioGot === 1) {
      addLog('音频首包已上（' + audioCodecStr + '）', 'g');
      diag();
    }
  }

  function openAudioWS() {
    var serial = active;
    var ws = new WebSocket(wsURL('/ws/audio?serial=' + encodeURIComponent(serial)));
    audioWS = ws;
    ws.binaryType = 'arraybuffer';
    ws.onmessage = function (ev) {
      if (ws !== audioWS) return;          // 作废的 socket，一个字都不许再进
      if (typeof ev.data === 'string') return;
      var buf = ev.data;
      if (buf.byteLength < 12) return;
      var dv = new DataView(buf);
      var hi = dv.getUint32(0);
      if (hi & 0x80000000) return;         // 音频没有会话头
      var isConfig = (hi & 0x40000000) !== 0;
      var size = dv.getUint32(8);
      if (size <= 0 || buf.byteLength < 12 + size) return;
      feedAudio(isConfig, (hi & 0x1FFFFFFF) * 4294967296 + dv.getUint32(4),
        new Uint8Array(buf, 12, size));
    };
    ws.onopen = function () {
      if (ws !== audioWS) return;
      addLog('音频通道已连接', 'g');
    };
    ws.onclose = function () {
      if (ws !== audioWS) return;
      if (streaming && active === serial && audioGot === 0) {
        addLog('音频通道已断开（没收到过音频数据）', 'y');
      }
    };
    ws.onerror = function () { if (ws === audioWS) addLog('音频通道出错', 'r'); };
  }


  // ==================== 输入 ====================
  // 画面有两个「面」：WebCodecs 画在 <canvas> 上，MSE 是把 <video> 显示出来、canvas 藏起来。
  // 事件必须绑在**始终存在的舞台**上，坐标也按**当前真正显示的那个面**去算 ——
  // 之前绑在 canvas 上，MSE 模式下它是 display:none，触摸事件根本落不到它身上，
  // 而且它的 getBoundingClientRect() 是 0×0，算出来的坐标永远是 (0,0)，
  // 表现就是「能连上、能看见画面，但点什么都没反应」。
  var inputHost = $('stage');

  function activeSurface() {
    return (videoEl && videoEl.parentNode) ? videoEl : canvas;
  }

  function toDevice(e) {
    var rect = activeSurface().getBoundingClientRect();
    if (!rect.width || !rect.height || !devW || !devH) return { x: 0, y: 0 };
    var rx = (e.clientX - rect.left) / rect.width;
    var ry = (e.clientY - rect.top) / rect.height;
    rx = Math.max(0, Math.min(1, rx));
    ry = Math.max(0, Math.min(1, ry));
    return { x: Math.round(rx * devW), y: Math.round(ry * devH) };
  }

  function allocPointer() {
    var used = new Set();
    activePointers.forEach(function (t) { used.add(t.cid); });
    for (var i = 0; i < MAX_POINTERS; i++) if (!used.has(i)) return i;
    return -1;
  }

  // move 的背压阈值：控制通道里积压超过这么多字节就不再往里塞 move。
  // 60 条 move 差不多就是这个量级（一条 JSON 约 60~80 字节），够小看不出来、够大不误伤。
  var MOVE_BACKLOG_BYTES = 4096;

  function flushMove() {
    moveRAF = 0;
    var pending = movePending;
    if (!pending.size) return;
    // 网络 / ADB 背压保护：controlWS 里还压着一坨没发出去的时候，继续塞 move 只会
    // 让「手指已经抬起来了、设备还在执行几秒前的坐标」——延迟全堆在排队里。
    // move 是绝对坐标、无状态，丢中间任意几个都不影响最终结果，所以这里**丢掉这一批**，
    // 只留每个指针最新的那一个位置（movePending 本来就被 set 覆盖成最新），下一帧再试。
    // ⚠️ 只有 move 走这条路：down / up、按键、导航、滚动都是离散事件，
    //    丢一个就是「点了没反应」，它们各有各的发送路径，绝不经过这个判断。
    // ⚠️ 也绝不用 setTimeout / debounce 去「等一等」——那是在输入延迟上再加一层延迟。
    if (controlWS && controlWS.bufferedAmount > MOVE_BACKLOG_BYTES) {
      DIAG.moveDropped = (DIAG.moveDropped || 0) + pending.size;
      moveRAF = requestAnimationFrame(flushMove);   // 借下一帧重试，不额外加等待
      return;
    }
    movePending = new Map();
    pending.forEach(function (e, pid) {
      var t = activePointers.get(pid);
      if (!t) return;
      var p = toDevice(e);
      sendControl({ kind: 'touch', action: 2, pointerId: t.cid, x: p.x, y: p.y });
    });
  }

  inputHost.addEventListener('pointerdown', function (e) {
    e.preventDefault();
    try { activeSurface().focus(); } catch (err) { /* 忽略 */ }
    if (!streaming) return;
    if (e.pointerType !== 'touch' && e.isPrimary === false) return;
    if (activePointers.has(e.pointerId)) return;
    var cid = allocPointer();
    if (cid < 0) return;
    try { inputHost.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    activePointers.set(e.pointerId, { cid: cid });
    var p = toDevice(e);
    sendControl({ kind: 'touch', action: 0, pointerId: cid, x: p.x, y: p.y });
  });

  window.addEventListener('pointermove', function (e) {
    if (!activePointers.has(e.pointerId)) return;
    // 每个指针只留**最新**一个位置：movePending 是按 pointerId 的 Map，
    // set 直接把上一帧的坐标覆盖掉，所以卡顿的时候不会攒出一串旧坐标排队。
    // 真正发出去交给 rAF 的 flushMove，一帧最多发一次。
    movePending.set(e.pointerId, e);
    if (!moveRAF) moveRAF = requestAnimationFrame(flushMove);
  });

  function endPointer(e) {
    var t = activePointers.get(e.pointerId);
    if (!t) return;
    activePointers.delete(e.pointerId);
    movePending.delete(e.pointerId);
    try { inputHost.releasePointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    var p = toDevice(e);
    sendControl({ kind: 'touch', action: 1, pointerId: t.cid, x: p.x, y: p.y });
  }
  ['pointerup', 'pointercancel'].forEach(function (type) {
    inputHost.addEventListener(type, endPointer);
    window.addEventListener(type, endPointer);
  });

  inputHost.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  inputHost.addEventListener('wheel', function (e) {
    e.preventDefault();
    if (!streaming) return;
    var p = toDevice(e);
    sendControl({ kind: 'scroll', x: p.x, y: p.y, vscroll: e.deltaY > 0 ? -1 : 1 });
  }, { passive: false });

  var KEYCODES = {
    Backspace: 67, Enter: 66, Escape: 4,
    ArrowUp: 19, ArrowDown: 20, ArrowLeft: 21, ArrowRight: 22,
    Home: 3, End: 123, PageUp: 92, PageDown: 93, Tab: 61,
    ShiftLeft: 59, ShiftRight: 60
  };

  function isTyping(e) {
    var t = e.target;
    if (!t) return false;
    var tag = t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    // 抽屉里（终端 / 文件的面板）按键也不许再漏到底下的设备去 ——
    // 在终端里敲 ls，绝不该同时把 l、s 也当文本发给安卓。xterm 自己那个隐藏
    // textarea 已经被上面那条挡掉了，这条是给抽屉里其它可聚焦元素兜底。
    if (t.closest && t.closest('#shellPanel, #filePanel, #logPanel')) return true;
    return false;
  }

  document.addEventListener('keydown', function (e) {
    if (!streaming || isTyping(e)) return;
    var kc = KEYCODES[e.code];
    if (kc !== undefined) {
      e.preventDefault();
      sendControl({ kind: 'key', keycode: kc, action: 0 });
      return;
    }
    if (e.key && e.key.length === 1) {
      e.preventDefault();
      sendControl({ kind: 'text', text: e.key });
    }
  });

  document.addEventListener('keyup', function (e) {
    if (!streaming || isTyping(e)) return;
    var kc = KEYCODES[e.code];
    if (kc !== undefined) {
      e.preventDefault();
      sendControl({ kind: 'key', keycode: kc, action: 1 });
    }
  });

  // ==================== 右侧安卓按键 ====================
  // ⚠️ 只开关按键本身（.k），别把整根栏里的 button 一起关掉。
  // 「收起」那颗也是 #keybar 里的 button，被一起 disabled 之后点它不派发 click，
  // 表现就是右侧永远收不起来 —— 而且不报错，光看代码找不出来。
  function setBarEnabled(on) {
    var btns = $('keybar').querySelectorAll('button.k');
    for (var i = 0; i < btns.length; i++) btns[i].disabled = !on;
    $('btnPaste').disabled = !on;
  }

  // 一次"点一下"：按下 + 抬起各发一次
  function tapKey(code) {
    sendControl({ kind: 'key', keycode: code, action: 0 });
    setTimeout(function () { sendControl({ kind: 'key', keycode: code, action: 1 }); }, 40);
  }

  // 长按连发：先等 260ms 把"点一下"和"按住"分开，之后每 90ms 补一次。
  // 每次补的都是**完整的一对 按下/抬起** —— 只连发 DOWN 不给 UP，
  // 设备端会当成"重复键"处理，行为不确定；一对一对发才是稳的。
  var HOLD_DELAY = 260, HOLD_EVERY = 90;

  var powerBtn = null;
  Array.prototype.forEach.call($('keybar').querySelectorAll('button'), function (btn) {
    if (btn.dataset.key) {
      var code = parseInt(btn.dataset.key, 10);
      var holdCode = btn.dataset.holdKey ? parseInt(btn.dataset.holdKey, 10) : 0;
      var timer = null, repeater = null, fired = false;

      function stopHold() {
        if (timer) { clearTimeout(timer); timer = null; }
        if (repeater) { clearInterval(repeater); repeater = null; }
        btn.classList.remove('holding');
      }

      if (holdCode) {
        btn.addEventListener('pointerdown', function () {
          if (btn.disabled) return;
          fired = false;
          btn.classList.add('holding');
          timer = setTimeout(function () {
            fired = true;
            tapKey(holdCode);
            repeater = setInterval(function () { tapKey(holdCode); }, HOLD_EVERY);
          }, HOLD_DELAY);
        });
        ['pointerup', 'pointerleave', 'pointercancel'].forEach(function (ev) {
          btn.addEventListener(ev, stopHold);
        });
      }

      btn.addEventListener('click', function () {
        // 长按连发已经发过一串了，浏览器随后补的这个 click 得吞掉，
        // 不然松手的瞬间还会多走一格音量。
        if (fired) { fired = false; return; }
        tapKey(code);
      });
    } else if (btn.dataset.panel) {
      btn.addEventListener('click', function () { sendControl({ kind: 'panel', which: btn.dataset.panel }); });
    } else if (btn.dataset.act === 'rotate') {
      btn.addEventListener('click', function () { sendControl({ kind: 'rotate' }); });
    } else if (btn.dataset.act === 'power') {
      powerBtn = btn;
      btn.addEventListener('click', function () {
        var on = !powerOn[active];
        powerOn[active] = on;
        sendControl({ kind: 'power', on: on });
        // ⚠️ 图标换成 SVG 之后，"改 emoji 文本"那套没了 ——
        // 亮/熄是两颗 SVG，靠 .on 这个类切换显示（见 index.html 里的 .ico-on / .ico-off）。
        btn.querySelector('.br').textContent = on ? '亮屏' : '息屏';
        btn.classList.toggle('on', !on);
      });
    } else if (btn.dataset.act === 'clip') {
      btn.addEventListener('click', function () { openModal('clipModal'); });
    }
  });

  // ==================== 弹窗 ====================
  var openModalId = null;

  // 通用确认弹窗：删除设备、删除文件都走它。
  // 以前 #confirmOk 里写死了「删设备」那一套，多一个用途就得再抄一个弹窗。
  // 回调拿到的是那个提示文字节点 —— 出错时把话写回去，成功时自己 closeModal。
  var confirmCb = null;
  function askConfirm(text, okLabel, cb) {
    $('confirmText').textContent = text;
    $('confirmMsg').textContent = '';
    $('confirmOk').textContent = okLabel || '删除';
    confirmCb = cb;
    openModal('confirmModal');
  }
  $('confirmOk').addEventListener('click', function () {
    var cb = confirmCb;
    confirmCb = null;
    if (!cb) { closeModal(); return; }
    cb($('confirmMsg'));
  });

  // 通用单行输入：新建文件夹 / 重命名借它。
  var inputCb = null;
  function askInput(title, label, value, hint, cb) {
    $('inputTitle').textContent = title;
    $('inputLabel').textContent = label;
    $('inputValue').value = value || '';
    var h = $('inputHint');
    h.textContent = hint || '';
    h.style.display = hint ? '' : 'none';
    inputCb = cb;
    openModal('inputModal');
    setTimeout(function () { var v = $('inputValue'); if (v) { v.focus(); v.select(); } }, 60);
  }
  function submitInput() {
    var cb = inputCb;
    inputCb = null;
    if (!cb) { closeModal(); return; }
    var v = $('inputValue').value.trim();
    closeModal();
    cb(v);
  }
  $('inputOk').addEventListener('click', submitInput);
  $('inputValue').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); submitInput(); }
  });

  function openModal(id) {
    closeModal();
    openModalId = id;
    var m = $(id);
    if (m) m.classList.add('show');
    var mask = $('mask');
    if (mask) mask.classList.add('show');
  }

  function closeModal() {
    if (openModalId) {
      var m = $(openModalId);
      if (m) m.classList.remove('show');
      openModalId = null;
    }
    $('mask').classList.remove('show');
    // ⚠️ 这里以前还有一句 closeDrawers() —— 是个很坑的 bug：
    // 在文件管理里点「新建文件夹 / 重命名」，输入框一确认就把**整个抽屉**收起来了，
    // 用户刚走进去的目录也跟着白走（再打开又回根目录，会以为"东西没传上去"）。
    // modal 是盖在最上层的独立一层，抽屉该开着就开着，不该被它连坐。
  }

  $('mask').addEventListener('click', closeModal);
  Array.prototype.forEach.call(document.querySelectorAll('[data-close]'), function (b) {
    b.addEventListener('click', closeModal);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (openModalId) closeModal();
    else closeDrawers();
  });

  // ---- 添加设备 ----
  $('btnAdd').addEventListener('click', function () {
    $('connMsg').textContent = '';
    $('connHost').value = '';
    $('connPort').value = '5555';
    $('connName').value = '';
    connExactSerial = '';            // 每次开新的一轮，别把上次点的候选带过来
    $('connTop').checked = false;
    $('pairPort').value = '';
    $('pairCode').value = '';
    $('pairMsg').textContent = '';
    fillFoundDevices();
    openModal('connModal');
    setTimeout(function () { $('connHost').focus(); }, 60);
  });
  $('btnRefresh').addEventListener('click', function () { refreshDevices(); addLog('已刷新设备列表', 'm'); });

  // 把 IP + 端口拼成 serial；端口留空按 adb 默认 5555 补。
  function serialFromForm() {
    var host = $('connHost').value.trim();
    if (!host) return { err: '先填设备 IP' };
    // 候选点进来的：直接用 adb 里那个原样 serial，别再拼 :5555 了
    if (connExactSerial) return { serial: connExactSerial };
    if (host.indexOf(':') >= 0) return { serial: host };   // 允许直接粘整串地址
    var port = $('connPort').value.trim() || '5555';
    if (!/^\d+$/.test(port)) return { err: '端口只能是数字' };
    return { serial: host + ':' + port };
  }

  // 手一碰输入框，就说明用户想自己指定地址，候选那个原样 serial 作废。
  ['connHost', 'connPort'].forEach(function (id) {
    $(id).addEventListener('input', function () { connExactSerial = ''; });
  });

  $('connGo').addEventListener('click', function () {
    var r = serialFromForm();
    if (r.err) { $('connMsg').textContent = r.err; return; }
    var serial = r.serial;
    var name = $('connName').value.trim().slice(0, 60);
    var top = $('connTop').checked;
    closeModal();
    // 先落记录（带上名字/置顶），再连。记录是左侧列表的来源，
    // 只靠 /api/connect 的话名字和置顶就丢了。
    postJSON('/api/devices/add', { serial: serial, name: name, top: top })
      .catch(function () { /* 记录失败也照样去连，别把路堵死 */ })
      .then(function () {
        // 必须走 activate()：它会先把上一台 detach 掉（关掉那台的视频/控制 WS、解掉解码器）。
        // 以前这里直接 `active = serial; connectDevice(serial);`，绕过了 detach ——
        // 上一台的视频 WS 一直开着，两台设备的流一起灌进同一个 MSE，画面满屏宏块。
        activate(serial);
      });
  });
  $('connHost').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') $('connGo').click();
  });

  // ---- 无线配对（adb pair）----
  $('pairGo').addEventListener('click', function () {
    var host = $('connHost').value.trim();
    var port = $('pairPort').value.trim();
    var code = $('pairCode').value.trim();
    if (!host) { $('pairMsg').textContent = '先填上面的设备 IP'; return; }
    if (!port || !code) { $('pairMsg').textContent = '配对端口和配对码都要填'; return; }
    $('pairMsg').textContent = '正在配对…';
    postJSON('/api/adb/pair', { host: host, port: port, code: code }).then(function (d) {
      if (d && d.ok) {
        $('pairMsg').textContent = '配对成功。接着把「无线调试」页面上那个连接端口填到上面的端口，再点「添加并连接」';
        fillFoundDevices();
      } else {
        $('pairMsg').textContent = (d && d.error) || '配对失败';
      }
    }).catch(function (e) { $('pairMsg').textContent = '请求失败：' + e.message; });
  });

  // ---- 设备信息：连上时后端读回来、存在设备记录里的那一份 ----
  // 刻意不在打开设置时现问设备：那要跑好几条 adb，弹窗会僵住一两秒。
  // 数据是「每次连接成功都重读一遍」存下来的（见 server.py 的 running 分支），
  // 所以这里只是把记录里那份显示出来；一次都没连过就如实说还没读到，不编。
  function devRecord(serial) {
    for (var i = 0; i < devices.length; i++) {
      if (devices[i].serial === serial) return devices[i];
    }
    return null;
  }

  function agoText(ts) {
    if (!ts) return '';
    var sec = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    if (sec < 60) return '刚刚';
    if (sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
    if (sec < 86400) return Math.floor(sec / 3600) + ' 小时前';
    return Math.floor(sec / 86400) + ' 天前';
  }

  // 编码器的显示名：下拉框里写的是 H.264，别处却大写一下写成 H264 ——
  // 同一件事两种写法，看着像两个东西。
  function codecLabel(c) {
    var s = String(c).toLowerCase();
    if (s === 'h264') return 'H.264';
    if (s === 'h265') return 'H.265';
    return String(c).toUpperCase();
  }

  function devInfoRows(p) {
    var rows = [];
    function add(k, v, cls) {
      if (v !== 0 && !v) return;              // 空值不占一行（设备没这条属性）
      rows.push({ k: k, v: String(v), cls: cls || '' });
    }
    var brandModel = [p.brand, p.model].filter(function (x) { return x; }).join(' ');
    add('型号', brandModel || p.device);
    if (p.manufacturer && p.manufacturer !== p.brand) add('厂商', p.manufacturer);
    if (p.androidVersion) {
      add('系统', 'Android ' + p.androidVersion + (p.sdk ? '（SDK ' + p.sdk + '）' : ''));
    }
    if (p.width && p.height) {
      add('屏幕', p.width + '×' + p.height + (p.density ? '　·　' + p.density + ' dpi' : ''));
    }
    if (p.encoders && p.encoders.length) {
      add('视频编码器', p.encoders.map(codecLabel).join(' / '));
    }
    var code = [p.product, p.device, p.board].filter(function (x, i, a) {
      return x && a.indexOf(x) === i;
    });
    add('设备代号', code.join(' / '));
    add('处理器架构', p.abi);
    add('系统芯片', p.soc);
    add('安全补丁', p.securityPatch);
    add('系统版本号', p.fingerprint, 'mono');
    add('读取时间', agoText(p.updatedAt), 'refresh');
    return rows;
  }

  function renderDevInfo() {
    var box = $('devInfo');
    if (!box) return;
    var rec = active ? devRecord(active) : null;
    var p = (rec && rec.profile) || {};
    var rows = devInfoRows(p);
    box.textContent = '';
    if (!rows.length) {
      // 一次都没连过（或那次设备没答话）：说清楚什么时候会有，别摆一行「未知」吓人
      var tip = document.createElement('div');
      tip.className = 'drow';
      var tk = document.createElement('span');
      tk.className = 'dk';
      tk.textContent = '状态';
      var tv = document.createElement('span');
      tv.className = 'dv empty';
      tv.textContent = '连上这台设备后会自动读一遍';
      tip.appendChild(tk); tip.appendChild(tv);
      box.appendChild(tip);
      return;
    }
    rows.forEach(function (r) {
      var row = document.createElement('div');
      row.className = 'drow';
      var k = document.createElement('span');
      k.className = 'dk';
      k.textContent = r.k;
      var v = document.createElement('span');
      v.className = 'dv' + (r.cls ? ' ' + r.cls : '');
      v.textContent = r.v;
      row.appendChild(k); row.appendChild(v);
      box.appendChild(row);
    });
  }

  // ---- 每设备设置 ----
  function openSettings() {
    if (!active) return;
    var s = settingsOf(active);
    var rec = devRecord(active) || {};
    $('setWho').textContent = active;
    // 名字是「设备记录」上的字段，不在 settings 里，得从列表那条记录上取。
    $('setName').value = rec.name || '';
    renderDevInfo();
    setRateField(s.bitRate);
    setRateField(s.audioBitRate, 'setAudioBitRate', 'setAudioBitRateUnit');
    $('setMaxFps').value = s.maxFps;
    $('setCodec').value = s.codec;
    $('setMaxSize').value = String(s.maxSize);
    $('setAngle').value = String(s.angle);
    $('setCrop').value = s.crop || '';
    $('setAudio').checked = !!s.audio;
    $('setAudioCodec').value = s.audioCodec;
    $('setAudioSource').value = s.audioSource;
    $('setShowTouches').checked = !!s.showTouches;
    $('setStayAwake').checked = !!s.stayAwake;
    $('setPowerOffOnClose').checked = !!s.powerOffOnClose;
    $('setKeepActive').checked = !!s.keepActive;
    $('setStartScreenOff').checked = !!s.startScreenOff;
    $('setPowerOn').checked = !!s.powerOn;
    $('setClipboardAutosync').checked = !!s.clipboardAutosync;
    $('setScreenOffTimeout').value = String(s.screenOffTimeout);
    // 编码协议的可选项直接按设备的**真实能力**来：设备没有的灰掉，
    // 并把这台设备到底能编哪些写出来。
    // 以前那句「自动＝优先 H.265」是假的 —— auto 根本不传 codec 参数，
    // 设备端一直走的是它自己的默认（H.264），而提示还写着「优先 H.265」。
    var enc = rec.encoders || null;
    var note = rec.codecNote || '';
    var knownNoH265 = !!rec.noH265;
    // 会话没在跑的时候 encoders 是空的（那是会话里探出来的），但设备记录里存着
    // 上次连上时读到的那一份 —— 用它兜底，没在投屏时也能看清这台设备能编什么。
    var encStored = false;
    if (!enc && rec.profile && rec.profile.encoders && rec.profile.encoders.length) {
      enc = rec.profile.encoders;
      encStored = true;
    }
    Array.prototype.forEach.call($('setCodec').querySelectorAll('option'), function (o) {
      if (o.value === 'auto') { o.disabled = false; return; }
      if (enc && enc.length) o.disabled = enc.indexOf(o.value) < 0;
      else o.disabled = (o.value === 'h265' && knownNoH265);
    });
    var hint = $('codecHint');
    if (enc && enc.length) {
      hint.style.display = '';
      hint.textContent = '这台设备能编：'
        + enc.map(codecLabel).join(' / ')
        + (encStored ? '（上次连接时读到的）' : '')
        + (note ? '　·　' + note : '');
    } else {
      hint.style.display = 'none';
    }
    if (enc && enc.length && s.codec !== 'auto' && enc.indexOf(s.codec) < 0) $('setCodec').value = 'auto';
    // 顺手问一下设备原生屏幕多大，供「推荐配置」算码率用
    deviceScreen = null;
    api('/api/screen?serial=' + encodeURIComponent(active)).then(function (d) {
      if (d && d.ok && d.screen) {
        deviceScreen = d.screen;
        $('setMsg').textContent = '本机屏幕 ' + d.screen.width + '×' + d.screen.height
          + '，点上面的推荐配置会自动算码率';
      }
    }).catch(function () { /* 问不到就按 720p 估，不打扰用户 */ });
    syncAudioFields();
    $('setMsg').textContent = '';
    openModal('setModal');
  }

  // 「不传音频」的时候，音频那几个参数就没意义了，灰掉省得看花眼。
  function syncAudioFields() {
    var on = $('setAudio').checked;
    ['setAudioBitRate', 'setAudioBitRateUnit', 'setAudioCodec', 'setAudioSource']
      .forEach(function (id) { var e = $(id); if (e) e.disabled = !on; });
  }

  $('btnSettings').addEventListener('click', openSettings);
  $('setAudio').addEventListener('change', syncAudioFields);
  Array.prototype.forEach.call(document.querySelectorAll('[data-preset]'), function (b) {
    b.addEventListener('click', function () {
      var p = PRESETS[b.dataset.preset];
      if (!p) return;
      // 码率由「屏幕分辨率 × 帧率」算出来，不是查表抄来的 —— 见 PRESETS 上面那段注释。
      var bps = presetBitRate(p, deviceScreen, parseInt($('setAngle').value, 10) || 0);
      setRateField(bps);
      $('setMaxFps').value = p.maxFps;
      $('setMaxSize').value = String(p.maxSize);
      $('setMsg').textContent = '已套用「' + b.textContent + '」：'
        + (deviceScreen ? (deviceScreen.width + '×' + deviceScreen.height)
                        : '分辨率未知，按 720p 估')
        + ' → ' + fmtRate(bps) + '，点保存生效（数字可以自己改）';
    });
  });

  $('setSave').addEventListener('click', function () {
    if (!active) return;
    // 改名字和改投屏参数是两回事：名字只影响列表怎么显示，不需要重连，所以单独处理。
    var newName = $('setName').value.trim().slice(0, 60);
    var oldName = '';
    for (var ni = 0; ni < devices.length; ni++) {
      if (devices[ni].serial === active) { oldName = devices[ni].name || ''; break; }
    }
    if (newName !== oldName) {
      postJSON('/api/devices/rename', { serial: active, name: newName }).then(function () {
        addLog('设备改名：' + active + ' → ' + (newName || '（用设备型号）'), 'm');
        refreshDevices();
      }).catch(function () { /* 改名失败不该挡住下面的参数保存 */ });
    }
    var s = {
      bitRate: readRateField(),
      maxFps: parseInt($('setMaxFps').value, 10) || 0,
      maxSize: parseInt($('setMaxSize').value, 10) || 0,
      codec: $('setCodec').value || 'auto',
      angle: parseFloat($('setAngle').value) || 0,
      crop: $('setCrop').value.trim(),
      audio: $('setAudio').checked,
      audioBitRate: readRateField('setAudioBitRate', 'setAudioBitRateUnit', DEFAULTS.audioBitRate),
      audioCodec: $('setAudioCodec').value || 'aac',
      audioSource: $('setAudioSource').value || 'output',
      showTouches: $('setShowTouches').checked,
      stayAwake: $('setStayAwake').checked,
      powerOffOnClose: $('setPowerOffOnClose').checked,
      keepActive: $('setKeepActive').checked,
      startScreenOff: $('setStartScreenOff').checked,
      powerOn: $('setPowerOn').checked,
      clipboardAutosync: $('setClipboardAutosync').checked,
      screenOffTimeout: parseInt($('setScreenOffTimeout').value, 10) || 0
    };
    saveSettings(active, s);
    addLog('设置已保存：' + active + ' ' + JSON.stringify(s), 'm');

    // 保存完就把弹窗收掉：反馈走顶栏状态和日志，别让遮罩一直糊在画面上
    var cur = null;
    for (var i = 0; i < devices.length; i++) if (devices[i].serial === active) cur = devices[i];
    closeModal();
    if (cur && cur.phase === 'running') {
      setStatus('设置已保存，正在重连以生效…');
      disconnectDevice(active).then(function () { connectDevice(active, s); });
    } else {
      setStatus('设置已保存（下次连接生效）', 'ok');
    }
  });

  // ---- 剪贴板 ----
  $('clipSend').addEventListener('click', function () {
    var t = $('clipArea').value;
    if (!t) { $('clipMsg').textContent = '先输入内容'; return; }
    sendControl({ kind: 'clipboard', text: t, paste: true });
    $('clipMsg').textContent = '已发送';
  });

  // ---- 底部快速发送 ----
  function sendClipbar() {
    var t = $('clipText').value;
    if (!t) return;
    sendControl({ kind: 'clipboard', text: t, paste: true });
    $('clipText').value = '';
    addLog('已把文本发到设备剪贴板', 'm');
  }
  $('btnPaste').addEventListener('click', sendClipbar);
  $('clipText').addEventListener('keydown', function (e) { if (e.key === 'Enter') sendClipbar(); });

  // ---- 日志 / 文件 / 终端 / 全屏 / 侧栏 ----
  // 几个抽屉入口：点一下开、再点一下关。
  // 抽屉不铺遮罩，所以开着时画面和虚拟按键照旧可点；点顶栏那颗按钮也照旧是「再点一下关掉」，
  // 想切到另一个抽屉直接点它的按钮即可（setDrawer 会互斥关掉当前这个）。
  $('btnLog').addEventListener('click', function () { setDrawer('logPanel', openDrawerId !== 'logPanel'); });
  $('btnLogClose').addEventListener('click', function () { setDrawer('logPanel', false); });
  $('btnLogClear').addEventListener('click', function () {
    var b = $('logBody');
    if (b) b.textContent = '';
    addLog('日志已清空', 'm');
  });

  // 状态面板：和日志共用同一套抽屉行为（互斥、自带关闭按钮、Esc 关）
  $('btnStat').addEventListener('click', function () { setDrawer('statPanel', openDrawerId !== 'statPanel'); });
  $('btnStatClose').addEventListener('click', function () { setDrawer('statPanel', false); });

  // ==================== 文件管理抽屉 ====================
  // 后端的 sync 通道（自研，见 fileops.py）把设备当普通文件系统用：
  // 列目录 / 上传 / 下载 / 改名 / 删除 / 装 APK / 跑脚本，全走 /api/files*。
  var fileCwd = '/sdcard';
  // 当前 fileCwd 是「哪台设备的目录」。换设备必须回默认目录（旧路径在新设备上多半不存在），
  // 同一台设备再打开则停在原处 —— 靠它把「换设备」和「重开抽屉」分开。
  var fileCwdSerial = null;
  var failed = 0;                // 本轮上传里失败的个数（收尾要用真实成败报，不能一律说"完成"）

  var FILE_ICON = {
    dir: '<path d="M4 7.2A1.6 1.6 0 0 1 5.6 5.6h3.2l1.6 2h8A1.6 1.6 0 0 1 20 9.2v7.6a1.6 1.6 0 0 1-1.6 1.6H5.6A1.6 1.6 0 0 1 4 16.8z"/>',
    file: '<path d="M6.4 3.8h6.6l4.6 4.6v11.8H6.4z"/><path d="M13 3.8v4.6h4.6"/>',
    download: '<path d="M12 4v10"/><path d="M8 10.4 12 14.4l4-4"/><path d="M5 19h14"/>',
    install: '<path d="M12 3.6 19.4 7.8v8.4L12 20.4 4.6 16.2V7.8z"/><path d="M4.6 7.8 12 12l7.4-4.2M12 12v8.4"/>',
    run: '<path d="M8 5.6 18 12l-10 6.4z"/>',
    rename: '<path d="M4.6 19.4h4L19 9a2.1 2.1 0 0 0-3-3L4.6 17.4z"/><path d="M14.6 7.4l2 2"/>',
    del: '<path d="M5 7h14"/><path d="M9.5 7V5.4a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V7"/><path d="M7 7l.9 11.1a1.4 1.4 0 0 0 1.4 1.3h5.4a1.4 1.4 0 0 0 1.4-1.3L17 7"/>'
  };
  function svgIcon(name) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true">' + FILE_ICON[name] + '</svg>';
  }

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }
  function joinPath(dir, name) {
    return (dir === '/' ? '' : String(dir).replace(/\/+$/, '')) + '/' + name;
  }
  // 上传/下载这类消息要**留住**。以前写下的结果会被 fileList 连盖两次：
  // 它一进去先写「读取中…」，读完再写「N 项 · /sdcard」，而上传收尾恰好要刷新目录 ——
  // 于是「已上传 xx」只存在几十毫秒。实测推 40 MB 到手机只要 0.6 秒，
  // 整个过程连眨眼都来不及，用户看到的就是"不知道传成没传成"。
  // 所以给结果设一段保留期：期间只挡目录刷新那两条**过路消息**，不挡别的
  // （夹在中间的报错照样要能显示出来）。
  var fileMsgHoldUntil = 0;

  function setFileMsg(text, cls, holdMs) {
    var el = $('fileMsg');
    if (!el) return;
    if (holdMs) fileMsgHoldUntil = Date.now() + holdMs;
    el.textContent = text || '';
    el.className = 'fb-foot' + (cls ? ' ' + cls : '');
  }

  // 目录刷新用的低优先级消息：结果还在保留期内就闭嘴，别把结果盖掉
  function setFileMsgLow(text, cls) {
    if (Date.now() < fileMsgHoldUntil) return;
    setFileMsg(text, cls);
  }

  // 进度条：ratio 是 0..1 的确定进度；传 null 表示"在跑但不知道跑到哪"（来回扫）
  function showFileBar(ratio) {
    var bar = $('fileBar');
    if (!bar) return;
    bar.hidden = false;
    if (ratio === null) { bar.classList.add('indet'); return; }
    bar.classList.remove('indet');
    var fill = bar.firstElementChild;
    if (fill) fill.style.width = Math.max(0, Math.min(100, ratio * 100)).toFixed(1) + '%';
  }

  function hideFileBar() {
    var bar = $('fileBar');
    if (!bar) return;
    bar.hidden = true;
    bar.classList.remove('indet');
  }

  function renderBreadcrumb(p) {
    var box = $('fileCrumbs');
    if (!box) return;
    box.textContent = '';
    function crumb(label, target) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.title = target;
      b.addEventListener('click', function () { fileList(target); });
      return b;
    }
    box.appendChild(crumb('/', '/'));
    var acc = '';
    String(p || '/').split('/').filter(Boolean).forEach(function (seg) {
      var s = document.createElement('span');
      s.className = 'sep';
      s.textContent = '/';
      box.appendChild(s);
      acc += '/' + seg;
      box.appendChild(crumb(seg, acc));
    });
    box.scrollLeft = box.scrollWidth;        // 深层目录时把末尾那几段露出来
  }

  function renderFileList(entries) {
    var box = $('fileList');
    if (!box) return;
    box.textContent = '';
    if (!entries || !entries.length) {
      box.innerHTML = '<div class="fempty">这个文件夹是空的</div>';
      return;
    }
    entries.forEach(function (e) {
      var row = document.createElement('div');
      row.className = 'frow' + (e.directory ? ' dir' : '');

      var ic = document.createElement('span');
      ic.className = 'fi';
      ic.innerHTML = svgIcon(e.directory ? 'dir' : 'file');
      row.appendChild(ic);

      var nm = document.createElement('span');
      nm.className = 'fn';
      nm.textContent = e.name;
      nm.title = e.path || e.name;
      row.appendChild(nm);

      var meta = document.createElement('span');
      meta.className = 'fm';
      meta.textContent = e.directory ? '' : fmtSize(e.size);
      meta.title = e.mtime || '';
      row.appendChild(meta);

      var acts = document.createElement('span');
      acts.className = 'fa';
      row.appendChild(acts);

      function actBtn(icon, title, cls, fn) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'ri-btn' + (cls ? ' ' + cls : '');
        b.title = title;
        b.setAttribute('aria-label', title);
        b.innerHTML = svgIcon(icon);
        b.addEventListener('click', function (ev) { ev.stopPropagation(); fn(); });
        acts.appendChild(b);
      }

      if (e.directory) {
        nm.addEventListener('click', function () { fileList(e.path); });
        actBtn('rename', '重命名', '', function () { fileRename(e); });
        actBtn('del', '删除', 'danger', function () { fileDelete(e); });
      } else {
        actBtn('download', '下载到本机', '', function () { fileDownload(e); });
        // 装 APK / 跑脚本只对相应的后缀给出入口 —— 给每个文件都挂一排按钮，
        // 既看不过来，也容易点错。
        if (/\.apk$/i.test(e.name)) actBtn('install', '安装到设备', '', function () { fileInstall(e); });
        else if (/\.(sh|bash)$/i.test(e.name)) actBtn('run', '执行脚本', '', function () { fileRun(e); });
        actBtn('rename', '重命名', '', function () { fileRename(e); });
        actBtn('del', '删除', 'danger', function () { fileDelete(e); });
      }
      box.appendChild(row);
    });
  }

  function fileList(p) {
    if (!active) { setFileMsg('先在上面选一台设备', 'bad'); renderFileList([]); return; }
    if (p) fileCwd = p;
    setFileMsgLow('读取中…');
    api('/api/files?serial=' + encodeURIComponent(active) + '&path=' + encodeURIComponent(fileCwd))
      .then(function (d) {
        if (!d || d.ok === false) {
          // 目录可能已经不在了（设备重连换了挂载、或者被删掉/改名）。
          // 先自动退回默认目录救一次，别让用户卡在一个打不开的路径上；
          // 连 /sdcard 都打不开（设备掉线之类）才老实报错。
          if (fileCwd !== '/sdcard') {
            fileCwd = '/sdcard';
            fileList(null);
            setFileMsgLow('原目录打不开，已回到 /sdcard');
            return;
          }
          setFileMsg('打不开：' + ((d && d.error) || '未知错误'), 'bad');
          renderFileList([]);
          return;
        }
        fileCwd = d.path || fileCwd;         // 服务端会把路径规范化，以它回的为准
        fileCwdSerial = active;              // 这个目录属于这台设备，换设备时才该重置
        renderBreadcrumb(fileCwd);
        renderFileList(d.entries || []);
        // ⚠️ 这条必须走"低优先级"：上传刚成功时结果还挂在保留期里，
        // 用 setFileMsg 会把它直接盖掉（这就是"看不到上传成功"的真凶）。
        setFileMsgLow((d.entries || []).length + ' 项 · ' + fileCwd);
      })
      .catch(function (e) { setFileMsg('请求失败：' + e.message, 'bad'); });
  }
  function fileReset() { fileCwd = '/sdcard'; fileCwdSerial = active; fileList(null); }

  function fileUp() {
    var p = fileCwd.replace(/\/+$/, '');
    if (!p || p === '/') return;
    var i = p.lastIndexOf('/');
    fileList(i <= 0 ? '/' : p.slice(0, i));
  }

  function fileMkdir() {
    if (!active) return;
    askInput('新建文件夹', '文件夹名', '', '会在 ' + fileCwd + ' 下新建', function (name) {
      if (!name) return;
      postJSON('/api/files/mkdir', { serial: active, path: joinPath(fileCwd, name) })
        .then(function (d) {
          if (d && d.ok === false) { setFileMsg('新建失败：' + d.error, 'bad'); return; }
          fileList(null);
        }).catch(function (e) { setFileMsg('请求失败：' + e.message, 'bad'); });
    });
  }

  function fileRename(e) {
    if (!active) return;
    askInput('重命名', '新名字', e.name, '', function (name) {
      if (!name || name === e.name) return;
      postJSON('/api/files/rename', { serial: active, path: e.path, to: joinPath(fileCwd, name) })
        .then(function (d) {
          if (d && d.ok === false) { setFileMsg('重命名失败：' + d.error, 'bad'); return; }
          fileList(null);
        }).catch(function (er) { setFileMsg('请求失败：' + er.message, 'bad'); });
    });
  }

  function fileDelete(e) {
    if (!active) return;
    askConfirm(
      '确定要删除「' + e.name + '」吗？\n\n'
      + (e.directory ? '整个文件夹连同里面的内容都会被删掉，' : '')
      + '删掉之后从这边没法恢复。',
      '删除',
      function (msgEl) {
        msgEl.textContent = '正在删除…';
        postJSON('/api/files/delete', { serial: active, path: e.path })
          .then(function (d) {
            if (d && d.ok === false) { msgEl.textContent = d.error || '删除失败'; return; }
            closeModal();
            addLog('已删除 ' + e.path, 'm');
            fileList(null);
          }).catch(function (er) { msgEl.textContent = '请求失败：' + er.message; });
      });
  }

  function fileInstall(e) {
    if (!active) return;
    setFileMsg('正在安装 ' + e.name + '…');
    postJSON('/api/files/install', { serial: active, path: e.path })
      .then(function (d) {
        if (d && d.ok === false) { setFileMsg('安装失败：' + d.error, 'bad'); return; }
        setFileMsg(d.message || '已安装', 'good');
      }).catch(function (er) { setFileMsg('请求失败：' + er.message, 'bad'); });
  }

  function fileRun(e) {
    if (!active) return;
    setFileMsg('正在执行 ' + e.name + '…');
    postJSON('/api/files/execute', { serial: active, path: e.path })
      .then(function (d) {
        if (d && d.ok === false) { setFileMsg('执行失败：' + d.error, 'bad'); return; }
        setFileMsg((d.message || '已执行')
          + (d.code === undefined ? '' : '（退出码 ' + d.code + '）'), 'good');
      }).catch(function (er) { setFileMsg('请求失败：' + er.message, 'bad'); });
  }

  function fileDownload(e) {
    if (!active) return;
    // 服务端的响应带 Content-Disposition: attachment，直接让浏览器收下就行，
    // 不用把文件读进内存再拼 Blob —— 大文件那样会把页面撑爆。
    var a = document.createElement('a');
    a.href = BASE + '/api/files/download?serial=' + encodeURIComponent(active)
      + '&path=' + encodeURIComponent(e.path);
    a.download = e.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // 下载走浏览器自己的通道，拿不到进度；但至少让这句话留一会儿，
    // 别被紧接着的目录刷新盖掉（下载大文件时用户需要知道是自己在等）
    setFileMsg('正在下载 ' + e.name + '…（在浏览器下载里看进度）', '', 10000);
  }

  // 上传：请求体就是文件本身（不做 multipart）。用 XHR 而不是 fetch ——
  // 只有 XHR 拿得到 upload.onprogress，大文件拉到一半有没有在走，得看得见。
  //
  // ⚠️ 三条纪律（都是"用户看不到反馈"踩出来的）：
  //   1. `xhr.send()` **之前**就先写一句"正在上传…"。文件小的时候 onprogress
  //      可能只来一次、甚至来不及重绘，用户点了按钮屏幕纹丝不动，会以为没生效。
  //   2. 结果必须带 holdMs —— 收尾要刷新目录，而目录刷新的过路消息会把它盖掉。
  //   3. 失败也要留住（留得比成功久一点，错误要让人读得完）。
  function uploadOne(file, idx, total) {
    return new Promise(function (resolve) {
      var target = joinPath(fileCwd, file.name);
      var xhr = new XMLHttpRequest();
      xhr.open('POST', BASE + '/api/files/upload?serial=' + encodeURIComponent(active)
        + '&path=' + encodeURIComponent(target));
      // 设备上的修改时间跟着本机这份走；不带的话设备按"现在"落，列表里看着像刚生成的
      try {
        xhr.setRequestHeader('X-File-Mtime',
          String(Math.floor((file.lastModified || Date.now()) / 1000)));
      } catch (e) { /* 头设不上不影响上传 */ }

      var head = '上传 ' + file.name + (total > 1 ? '（' + idx + '/' + total + '）' : '');
      setFileMsg(head + ' …');
      showFileBar(0);

      xhr.upload.onprogress = function (ev) {
        if (!ev.lengthComputable) {        // 算不出总量就退化成"在动"
          setFileMsg(head + ' …');
          showFileBar(null);
          return;
        }
        var r = ev.total ? ev.loaded / ev.total : 0;
        setFileMsg(head + ' ' + Math.round(r * 100) + '%');
        showFileBar(r);
      };

      xhr.onload = function () {
        var d = null;
        try { d = JSON.parse(xhr.responseText); } catch (e) { /* 不是 JSON 就是服务端出了岔子 */ }
        if (!d || d.ok === false) {
          var why = (d && d.error) || ('HTTP ' + xhr.status);
          setFileMsg('上传失败：' + file.name + ' —— ' + why, 'bad', 20000);
          addLog('上传失败：' + file.name + '（' + why + '）', 'r');
          failed++;
        } else {
          var n = d.bytes || file.size;
          setFileMsg('已上传 ' + file.name + '（' + fmtSize(n) + '）', 'good', 8000);
          addLog('已上传：' + target + '（' + fmtSize(n) + '）', 'g');
        }
        resolve();
      };
      xhr.onerror = function () {
        setFileMsg('上传失败：' + file.name + ' —— 网络中断', 'bad', 20000);
        addLog('上传失败：' + file.name + '（网络中断）', 'r');
        failed++;
        resolve();
      };
      xhr.onabort = function () {
        setFileMsg('已取消上传：' + file.name, 'bad', 8000);
        resolve();
      };
      xhr.send(file);
    });
  }

  function fileUpload(files) {
    // 这两条以前是静默 return —— 上传这种"点了没反应会让人以为坏了"的动作，
    // 必须留句话在界面上，否则连排查的线索都没有。
    if (!active) { setFileMsg('先在上面选一台设备', 'bad'); return; }
    if (!files || !files.length) { setFileMsg('没有拿到文件，请重新选一次', 'bad', 8000); return; }
    var list = Array.prototype.slice.call(files);
    var i = 0;
    failed = 0;
    function next() {
      if (i >= list.length) {
        hideFileBar();
        if (list.length > 1) {
          // 以前这里无条件写「上传完成（N 个）」—— 有几个失败也照样这么说，
          // 属于骗人。现在按真实成败分开报。
          if (failed) {
            setFileMsg('上传结束：成功 ' + (list.length - failed) + ' 个，失败 ' + failed + ' 个',
                       'bad', 20000);
          } else {
            setFileMsg('已全部上传（' + list.length + ' 个）', 'good', 8000);
          }
        }
        fileList(null);          // 刷新列表用的是低优先级消息，盖不掉上面的结果
        return;
      }
      var f = list[i++];
      uploadOne(f, i, list.length).then(next);
    }
    next();
  }

  $('btnFiles').addEventListener('click', function () { setDrawer('filePanel', openDrawerId !== 'filePanel'); });
  $('btnFileClose').addEventListener('click', function () { setDrawer('filePanel', false); });
  $('btnFileRefresh').addEventListener('click', function () { fileList(null); });
  $('btnFileUp').addEventListener('click', fileUp);
  $('btnFileMkdir').addEventListener('click', fileMkdir);
  $('btnFileRoot').addEventListener('click', function () { fileList('/sdcard'); });
  $('btnFileTmp').addEventListener('click', function () { fileList('/data/local/tmp'); });
  $('btnFileUpload').addEventListener('click', function () {
    if (!active) { setFileMsg('先选一台设备', 'bad'); return; }
    $('filePick').click();
  });
  $('filePick').addEventListener('change', function () {
    // ⚠️ 顺序是致命的：`this.files` 是**活的** FileList。
    // 以前这里是 `var f = this.files; this.value = ''; fileUpload(f);` ——
    // 清空 input 的同时那个引用也空了，fileUpload 收到 length=0 的列表，
    // 第一行 `!files.length` 直接 return：**请求根本没发出去**，
    // 没有进度、没有日志、列表里也没有东西，而且一句提示都不给。
    // 实测（tools/_t144）：change 捕获阶段量到 files.length=1，
    // 但 XMLHttpRequest.send 被调用 0 次 —— 就是这个原因。
    // 必须**先把文件拷出来**，再清 input。
    var picked = Array.prototype.slice.call(this.files);
    this.value = '';            // 清掉，否则同一个文件第二次选不出来 change
    fileUpload(picked);
  });

  // ==================== 终端抽屉 ====================
  // xterm.js + FitAddon，离线内嵌在 lib/ 下（页面里那段加载脚本负责挂上）。
  // 协议（见 server.py 的 _ws_shell）：**二进制帧 = 键盘输入**，文本帧 = 控制指令
  // （{"t":"resize"} / {"t":"close"}）；回程二进制帧是 stdout+stderr，文本帧是
  // {"t":"exit"} 或 {"t":"error"}。
  var term = null, termFit = null, shellWS = null;

  function shellHint(text) {
    var el = document.querySelector('#shellPanel .term-hint');
    if (el) el.textContent = text;
  }
  function termWrite(data) {
    if (term) { try { term.write(data); } catch (e) { /* 已经 dispose 了 */ } }
  }
  function shellCtrl(obj) {
    if (shellWS && shellWS.readyState === 1) shellWS.send(JSON.stringify(obj));
  }

  function shellConnect() {
    if (shellWS) { try { shellWS.close(); } catch (e) { /* 忽略 */ } shellWS = null; }
    var rows = (term && term.rows) || 24, cols = (term && term.cols) || 80;
    var ws = new WebSocket(wsURL('/ws/shell?serial=' + encodeURIComponent(active)
      + '&rows=' + rows + '&cols=' + cols));
    shellWS = ws;
    ws.binaryType = 'arraybuffer';
    ws.onopen = function () {
      if (ws !== shellWS) return;
      addLog('终端已连接：' + active, 'g');
    };
    ws.onmessage = function (ev) {
      if (ws !== shellWS) return;          // 换代了的旧 socket 一个字都不许再写
      if (typeof ev.data === 'string') {
        var m = null;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (!m) return;
        if (m.t === 'exit') termWrite('\r\n\x1b[90m[进程已退出，代码 ' + m.code + ']\x1b[0m\r\n');
        else if (m.t === 'error') termWrite('\r\n\x1b[31m[终端打不开：' + (m.error || '') + ']\x1b[0m\r\n');
        return;
      }
      termWrite(new Uint8Array(ev.data));
    };
    ws.onclose = function () {
      if (ws !== shellWS) return;
      shellWS = null;
    };
    ws.onerror = function () {
      if (ws === shellWS) termWrite('\r\n\x1b[31m[终端连接出错]\x1b[0m\r\n');
    };
  }

  function shellOpen() {
    if (!active) { shellHint('先在上面选一台设备，再开终端。'); return; }
    if (typeof Terminal === 'undefined' || !window.FitAddon) {
      shellHint('终端组件没加载上（缺 lib/xterm.js），重新打开页面试试。');
      return;
    }
    if (!term) {
      term = new Terminal({
        convertEol: true, cursorBlink: true, fontSize: 13, scrollback: 2000,
        fontFamily: 'ui-monospace, Menlo, Consolas, monospace',
        theme: { background: '#0a0d13', foreground: '#dbe4f0', cursor: '#3b82f6' }
      });
      termFit = new FitAddon.FitAddon();
      term.loadAddon(termFit);
      term.open($('termHost'));
      // 键盘输入原样变成二进制帧发给设备端的 sh
      term.onData(function (d) {
        if (shellWS && shellWS.readyState === 1) shellWS.send(new TextEncoder().encode(d));
      });
      // 终端尺寸变了就告诉服务端，它会去 resize 设备那头的 PTY
      term.onResize(function (size) { shellCtrl({ t: 'resize', rows: size.rows, cols: size.cols }); });
    }
    shellHint('直接敲命令，回车执行。窗口大小会跟着这块面板自动调整。');
    // 抽屉刚 .show 出来时动画还没走完（transform 有 0.2s），但盒子尺寸已经是最终值；
    // 等一帧是为了拿到 flex 布局算完之后的真实高度，fit 才不会算错行列数。
    requestAnimationFrame(function () {
      if (!term || openDrawerId !== 'shellPanel') return;
      try { termFit.fit(); } catch (e) { /* 宿主还没尺寸，忽略 */ }
      term.focus();
      shellConnect();
    });
  }

  function shellClose() {
    if (shellWS) { try { shellWS.close(); } catch (e) { /* 忽略 */ } shellWS = null; }
    // 终端整个丢掉：下次打开（可能是另一台设备）从干净的会话开始
    if (term) { try { term.dispose(); } catch (e) { /* 忽略 */ } term = null; termFit = null; }
  }

  // 换设备时把终端和文件抽屉接到新设备上
  function onActiveChanged() {
    if (openDrawerId === 'shellPanel') {
      shellClose();
      if (active) shellOpen(); else setDrawer('shellPanel', false);
    } else {
      shellClose();
    }
    // 换设备：文件目录必须回默认（旧路径在新设备上多半不存在），并记下它现在属于谁。
    // 面板没开着也要记 —— 下次打开时 setDrawer 靠这个标记判断要不要重置。
    fileCwd = '/sdcard';
    fileCwdSerial = active;
    if (openDrawerId === 'filePanel') fileList(null);
  }

  $('btnShell').addEventListener('click', function () { setDrawer('shellPanel', openDrawerId !== 'shellPanel'); });
  $('btnShellClose').addEventListener('click', function () { setDrawer('shellPanel', false); });
  $('btnShellClear').addEventListener('click', function () { if (term) term.clear(); });
  // ---- 全屏（沉浸）----
  // 「全屏」要的是「画面铺满整个可视区」。可 Fullscreen API 只管浏览器/App 那一层窗口框，
  // 页面自己的顶栏和底部文本栏还在，上下各啃掉一条 —— 看着就"没全屏"。
  // 所以除了调 API，再切一个 .fs 布局类：把顶栏/底栏整块搬进左栏
  // （搬的是同两个节点，id 不变、事件不用重绑），中间那栏就只剩画面，铺满整个高度。
  // ⚠️ 别拿 requestFullscreen 成不成功来决定加不加 .fs —— 安卓 WebView 里这个 API 未必有，
  //    失败也得进沉浸布局，否则按钮点下去像没反应。退出则由 fullscreenchange（Esc/系统手势）
  //    和「退出全屏」按钮两条路一起兜。
  var headEl = document.querySelector('.stage-head');
  var clipEl = document.querySelector('.clipbar');
  var appEl = document.querySelector('.app');

  function inFs() { return !!(appEl && appEl.classList.contains('fs')); }

  function enterFs() {
    if (!appEl || inFs()) return;
    appEl.classList.add('fs');
    var s1 = $('sideSession'), s2 = $('sideCompose');
    if (s1 && headEl) s1.appendChild(headEl);
    if (s2 && clipEl) s2.appendChild(clipEl);
    if ($('btnFull')) $('btnFull').textContent = '退出全屏';
    resizeCanvas();
  }

  function exitFs() {
    if (!appEl || !inFs()) return;
    appEl.classList.remove('fs');
    var stage = document.querySelector('.stage');
    if (stage) {
      if (headEl) stage.insertBefore(headEl, stage.firstChild);
      if (clipEl) stage.appendChild(clipEl);
    }
    if ($('btnFull')) $('btnFull').textContent = '全屏';
    resizeCanvas();
  }

  $('btnFull').addEventListener('click', function () {
    if (inFs()) {
      // 真进了系统全屏就顺带退掉它，但别把收布局这件事押在 fullscreenchange 上 ——
      // 那个事件在 API 被静默忽略时不会来，押上去就是"点了退出全屏没反应"。
      // exitFs 自身幂等，事件真来了再走一次也无害。
      if (document.fullscreenElement && document.exitFullscreen) {
        try { document.exitFullscreen(); } catch (e) { /* 忽略 */ }
      }
      exitFs();
      return;
    }
    enterFs();
    var el = document.documentElement;
    if (el.requestFullscreen) {
      try {
        var p = el.requestFullscreen();
        if (p && p.catch) p.catch(function () { /* 不给用也不退：沉浸布局已经生效 */ });
      } catch (e) { /* 同上 */ }
    }
  });

  document.addEventListener('fullscreenchange', function () {
    if (document.fullscreenElement) enterFs();
    else exitFs();
  });
  // ---- 左右侧栏收起 / 展开 ----
  // 收起就是把 .app 加上 side-off / keys-off，CSS 里对应 display:none。
  // 状态存 localStorage：收起过的那一栏下次打开还是收起的，不用每次再点一遍。
  // 窄屏默认两栏都收起 —— 236 + 76 两个侧栏一摆，留给画面的地方就太少了。
  var PANEL_KEY = 'scrcpy-fnos.panels';
  var panels = (function () {
    try {
      var v = JSON.parse(localStorage.getItem(PANEL_KEY));
      if (v && typeof v === 'object') return { side: !!v.side, keys: !!v.keys };
    } catch (e) { /* 隐私模式，忽略 */ }
    var narrow = window.innerWidth <= 720;
    return { side: narrow, keys: narrow };
  })();

  function applyPanels() {
    var app = document.querySelector('.app');
    if (!app) return;
    app.classList.toggle('side-off', panels.side);
    app.classList.toggle('keys-off', panels.keys);
    $('btnSidebarShow').style.display = panels.side ? '' : 'none';
    $('btnKeysShow').style.display = panels.keys ? '' : 'none';
    try { localStorage.setItem(PANEL_KEY, JSON.stringify(panels)); } catch (e) { /* 忽略 */ }
    // 栏一收一放舞台宽度就变了，画面得重新贴合，不然会留黑边
    resizeCanvas();
  }

  $('btnSidebarHide').addEventListener('click', function () { panels.side = true; applyPanels(); });
  $('btnSidebarShow').addEventListener('click', function () { panels.side = false; applyPanels(); });
  // 全屏 + 左栏收起时的兜底键。顶栏那套按钮这会儿都躺在被收起的那一栏里，
  // 屏幕上再没有别的入口能把设备列表叫回来、也退不出全屏 —— 手机上进全屏就是个死胡同。
  var fsPillBtn = $('fsPill');
  if (fsPillBtn) fsPillBtn.addEventListener('click', function () { panels.side = false; applyPanels(); });
  // 直接复用「全屏」按钮的处理：程序化 click() 不看元素可不可见，
  // 所以哪怕它正躺在收起的侧栏里也照样能触发，省得把进退全屏的逻辑写两遍。
  var fsExitBtn = $('fsExit');
  if (fsExitBtn) fsExitBtn.addEventListener('click', function () { $('btnFull').click(); });
  $('btnKeysHide').addEventListener('click', function () { panels.keys = true; applyPanels(); });
  $('btnKeysShow').addEventListener('click', function () { panels.keys = false; applyPanels(); });
  applyPanels();

  window.addEventListener('resize', function () {
    resizeCanvas();
    // 终端开着的时候跟着重算行列 —— 不然窗口一变，xterm 还按老尺寸在画，
    // 右边会空一条、或者把内容挤出去。
    if (term && openDrawerId === 'shellPanel') {
      try { termFit.fit(); } catch (e) { /* 忽略 */ }
    }
  });

  // ==================== 启动 ====================
  function checkDecoderSupport() {
    if (typeof VideoDecoder !== 'undefined') {
      DIAG.dec = 'WebCodecs 可用';
      return true;
    }
    // WebCodecs 是 SecureContext API，http 下会被浏览器藏起来；
    // 但 MediaSource 没这个限制，所以那并不是死路。
    var MS = window.MediaSource || window.WebKitMediaSource;
    if (MS) {
      DIAG.dec = 'WebCodecs 不可用（'
        + (window.isSecureContext ? '浏览器不支持' : 'http 非安全上下文') + '），走 MSE';
      return true;
    }
    DIAG.dec = '不可用：WebCodecs 与 MediaSource 都没有';
    return false;
  }

  setBarEnabled(false);
  checkDecoderSupport();
  diag();
  addLog('页面就绪 · ' + location.href, 'm');
  addLog(DIAG.dec, DIAG.dec.indexOf('不可用') === 0 ? 'r' : 'm');
  if (DIAG.dec.indexOf('不可用') === 0) setStatus(DIAG.dec, 'err');

  // 页面一打开先拉一次列表；之后每 2.5 秒跟着（每轮会起一次 adb 查询，别太密）。
  // 只在「已经有会话在跑」时自动接管画面 —— 列着但没连的设备不自动连，
  // 免得一开页面就把设备全拉起来。
  refreshDevices().then(function () {
    var running = devices.filter(function (d) { return d.phase === 'running'; });
    if (running.length) activate(running[0].serial);
  });
  listTimer = setInterval(refreshDevices, 2500);
  // 状态面板的 1 秒窗口统计。开机就起，不跟着挂流走 —— 面板可以在没连设备时就打开，
  // 那时候也该显示设备列表/环境那几段，而不是一片空白。
  setInterval(statTick, 1000);

  window.addEventListener('beforeunload', function () { closeWS(); });

  // ==================== 测试钩子 ====================
  // 花屏这种事「看着像」不算数，得有个不依赖肉眼的判据。
  // 这里把当前上屏的那一帧缩到 32×32 灰度，交给外面的脚本比对：
  // 静止画面上 resetVideo 前后同一块画面必须一模一样，不一致就是解码真的被弄坏了。
  // （设备截图做真值那条路会被「时刻不同」干扰，静止时点差大反而说明不了问题。）
  window.__scrcpyTest = {
    stats: function () {
      var out = {};
      for (var k in DIAG) if (DIAG.hasOwnProperty(k)) out[k] = DIAG[k];
      out.devW = devW; out.devH = devH;
      out.attached = attached; out.streaming = streaming;
      out.surface = (videoEl && videoEl.parentNode) ? 'video' : (devW ? 'canvas' : 'none');
      out.mseQueue = mseQueue.length; out.msePending = msePending.length;
      out.mseInitDone = mseInitDone; out.mseNeedKey = mseNeedKey;
      out.srcBuffer = !!sourceBuffer;
      out.latOn = LATENCY_ON; out.maxQ = MSE_MAXQ;
      if (sourceBuffer) {
        try { out.sbRanges = sourceBuffer.buffered.length; } catch (e) { out.sbRanges = -2; }
        try { out.sbStart = sourceBuffer.buffered.length
          ? Math.round(sourceBuffer.buffered.start(0) * 1000) / 1000 : -1; }
        catch (e) { out.sbStart = -2; }
      }
      if (videoEl) {
        out.readyState = videoEl.readyState;
        out.currentTime = Math.round(videoEl.currentTime * 1000) / 1000;
        try { out.bufferedEnd = videoEl.buffered.length
          ? Math.round(videoEl.buffered.end(videoEl.buffered.length - 1) * 1000) / 1000 : -1; }
        catch (e) { out.bufferedEnd = -2; }
        try { out.vbRanges = videoEl.buffered.length; } catch (e) { out.vbRanges = -2; }
      }
      // ---- 音频 ----
      out.audioEl = !!audioEl;
      out.audioSB = !!audioSB;
      out.audioInitDone = audioInitDone;
      out.audioQueue = audioQueue.length;
      out.audioPending = audioPending.length;
      out.audioCodecStr = audioCodecStr;
      out.audioGot = audioGot;
      // ---- 实时状态面板的窗口统计（面板上显示的就是这几个数）----
      out.fpsRx = Math.round(STATS.fpsRx * 10) / 10;
      out.fpsDec = Math.round(STATS.fpsDec * 10) / 10;
      out.kbps = Math.round(STATS.kbps);
      out.peakKbps = Math.round(STATS.peakKbps);
      out.rxFrames = STATS.rxFrames; out.rxBytes = STATS.rxBytes;
      out.sinceMs = STATS.startedAt ? (Date.now() - STATS.startedAt) : 0;
      if (audioSB) {
        try { out.abRanges = audioSB.buffered.length; } catch (e) { out.abRanges = -2; }
      }
      if (audioEl) {
        out.aReady = audioEl.readyState;
        out.aPaused = audioEl.paused;
        out.aMuted = audioEl.muted;
        out.aTime = Math.round(audioEl.currentTime * 1000) / 1000;
        out.aVol = audioEl.volume;
        try { out.aBufferedEnd = audioEl.buffered.length
          ? Math.round(audioEl.buffered.end(audioEl.buffered.length - 1) * 1000) / 1000 : -1; }
        catch (e) { out.aBufferedEnd = -2; }
      }
      return out;
    },

    // 32×32 灰度指纹 + 全画面均值/方差
    fingerprint: function () {
      var face = activeSurface();
      var w = face.videoWidth || face.width;
      var h = face.videoHeight || face.height;
      if (!w || !h) return null;
      var cv = document.createElement('canvas');
      cv.width = 32; cv.height = 32;
      var c2 = cv.getContext('2d');
      try { c2.drawImage(face, 0, 0, 32, 32); } catch (e) { return { err: String(e) }; }
      var d = c2.getImageData(0, 0, 32, 32).data;
      var px = [], sum = 0, sq = 0;
      for (var i = 0; i < d.length; i += 4) {
        var g = Math.round(0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]);
        px.push(g); sum += g; sq += g * g;
      }
      var mean = sum / px.length;
      return { w: w, h: h, px: px, mean: Math.round(mean * 100) / 100,
               sd: Math.round(Math.sqrt(sq / px.length - mean * mean) * 100) / 100 };
    },

    // 整帧 PNG（和 scrcpy_frame_grab.js 同一套画法），需要时用来留证
    png: function () {
      var face = activeSurface();
      var w = face.videoWidth || face.width;
      var h = face.videoHeight || face.height;
      if (!w || !h) return null;
      var cv = document.createElement('canvas');
      cv.width = w; cv.height = h;
      cv.getContext('2d').drawImage(face, 0, 0, w, h);
      return cv.toDataURL('image/png');
    },

    // ⚠️ 排障专用：手动让设备重启编码器。**应用自己的任何路径都不再调它**
    // （会把这个硬编重启成「不再补 CONFIG」的状态，前端永久黑）。
    // 留着是给诊断脚本复现/取证用的（`tools/_t29*`），别接到界面上。
    resetVideo: function () { sendControl({ kind: 'resetVideo' }); return true; },

    // 让测试脚本能直接驱动设备做动作（不用借 adb），坐标是设备像素
    tap: function (x, y) {
      sendControl({ kind: 'touch', action: 0, pointerId: 9, x: x, y: y });
      setTimeout(function () { sendControl({ kind: 'touch', action: 1, pointerId: 9, x: x, y: y }); }, 60);
      return true;
    },
    swipe: function (x1, y1, x2, y2, ms) {
      ms = ms || 300;
      var steps = 12, i = 0;
      sendControl({ kind: 'touch', action: 0, pointerId: 9, x: x1, y: y1 });
      var timer = setInterval(function () {
        i++;
        if (i > steps) {
          clearInterval(timer);
          sendControl({ kind: 'touch', action: 1, pointerId: 9, x: x2, y: y2 });
          return;
        }
        var t = i / steps;
        sendControl({ kind: 'touch', action: 2, pointerId: 9,
          x: Math.round(x1 + (x2 - x1) * t), y: Math.round(y1 + (y2 - y1) * t) });
      }, Math.max(16, Math.round(ms / steps)));
      return true;
    },
    key: function (code) {
      sendControl({ kind: 'key', keycode: code, action: 0 });
      setTimeout(function () { sendControl({ kind: 'key', keycode: code, action: 1 }); }, 40);
      return true;
    },
    // 把 MSE 队列上限压到很小，逼出「跟不上 → 丢帧 → 要关键帧」那条路
    setMaxQ: function (n) { MSE_MAXQ = n; return MSE_MAXQ; },

    // 开关「延迟纠偏」，用来做 A/B：冻结到底是不是它造成的
    setLatency: function (on) { LATENCY_ON = !!on; return LATENCY_ON; },

    // 故意把播放链路弄死（就是曾经那句 remove 干的事），用来回归验证看门狗能不能自愈。
    // 只能在控制台/测试脚本里显式调用，正常路径永远不会碰它。
    _forceFreeze: function () {
      if (!sourceBuffer || !videoEl) return 'no buffer';
      try { sourceBuffer.remove(0, videoEl.currentTime - 4); return 'ok'; }
      catch (e) { return String(e); }
    },

    // ---- 抽屉 / 文件 / 终端的测试钩子 ----
    // 「点了没反应」这类问题肉眼看着像就说不清，得有能读回来的数字：
    // 抽屉到底开没开、文件列了几行、终端的 socket 到没到 OPEN。
    drawer: function (id, on) {
      setDrawer(id, on === undefined ? true : !!on);
      return openDrawerId;
    },
    closeDrawers: function () { closeDrawers(); return openDrawerId; },
    drawers: function () {
      var out = {};
      DRAWER_IDS.forEach(function (d) {
        var el = $(d);
        out[d] = !!(el && el.classList.contains('show'));
      });
      out.open = openDrawerId;
      return out;
    },
    filePath: function () { return fileCwd; },
    fileGo: function (p) { fileList(p); return fileCwd; },
    fileRows: function () { return document.querySelectorAll('#fileList .frow').length; },
    fileMsgText: function () { var e = $('fileMsg'); return e ? e.textContent : ''; },
    term: function () {
      // 逐行把 xterm 的缓冲区读成纯文本：它默认是 DOM 渲染器，直接看 textContent
      // 拿到的是拆碎的 span，没法比对 —— 缓冲区才是"屏幕上到底是什么"。
      var screen = '';
      if (term && term.buffer && term.buffer.active) {
        var b = term.buffer.active;
        for (var i = 0; i < b.length; i++) {
          var line = b.getLine(i);
          screen += (line ? line.translateToString(true) : '') + '\n';
        }
      }
      return {
        hasTerm: !!term,
        rows: term ? term.rows : 0, cols: term ? term.cols : 0,
        ws: shellWS ? shellWS.readyState : -1,   // 1 = OPEN
        screen: screen
      };
    },

    logs: function () {
      var b = $('logBody');
      return b ? b.textContent : '';
    }
  };
})();
