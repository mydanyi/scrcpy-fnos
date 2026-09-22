'use strict';
// fMP4 分片封装：把 Annex-B 的 H.264 裸流包成 MSE 能吃的 fragmented MP4。
//
// 只在 WebCodecs 不可用时才走这条路（VideoDecoder 是 SecureContext API，
// http 访问时浏览器根本不暴露它；而 MediaSource 没有这个限制）。
// 全部按 ISO/IEC 14496-12 / 14496-15 手拼 box，没有依赖。
(function (global) {

  function bytes() {
    return new Uint8Array(Array.prototype.slice.call(arguments));
  }
  function u16(v) { return bytes((v >>> 8) & 255, v & 255); }
  function u24(v) { return bytes((v >>> 16) & 255, (v >>> 8) & 255, v & 255); }
  function u32(v) { return bytes((v >>> 24) & 255, (v >>> 16) & 255, (v >>> 8) & 255, v & 255); }

  function concat() {
    var parts = Array.prototype.slice.call(arguments), i, n = 0, o = 0;
    for (i = 0; i < parts.length; i++) n += parts[i].length;
    var out = new Uint8Array(n);
    for (i = 0; i < parts.length; i++) { out.set(parts[i], o); o += parts[i].length; }
    return out;
  }

  function str(s) {
    var out = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }

  function box(type) {
    var payloads = Array.prototype.slice.call(arguments, 1), i, n = 0;
    for (i = 0; i < payloads.length; i++) n += payloads[i].length;
    return concat(u32(8 + n), str(type), concat.apply(null, payloads));
  }

  function fullBox(type, ver, flags) {
    var payloads = Array.prototype.slice.call(arguments, 3);
    return box.apply(null, [type, bytes(ver), u24(flags)].concat(payloads));
  }

  // 把 Annex-B（00 00 01 起始码）拆成一个个 NAL 单元
  function nalUnits(buf) {
    var starts = [], i;
    for (i = 0; i + 3 <= buf.length; i++) {
      if (buf[i] === 0 && buf[i + 1] === 0 && buf[i + 2] === 0 && i + 3 < buf.length && buf[i + 3] === 1) {
        starts.push([i + 4, i]); i += 3;
      } else if (buf[i] === 0 && buf[i + 1] === 0 && buf[i + 2] === 1) {
        starts.push([i + 3, i]); i += 2;
      }
    }
    var out = [];
    for (i = 0; i < starts.length; i++) {
      var from = starts[i][0];
      var to = (i + 1 < starts.length) ? starts[i + 1][1] : buf.length;
      if (to > from) out.push(buf.subarray(from, to));
    }
    return out;
  }

  // Annex-B → AVCC（4 字节长度前缀），WebCodecs 与 MSE 都要这个形式
  function annexBtoAVCC(buf) {
    var units = nalUnits(buf), i, n = 0, o = 0;
    for (i = 0; i < units.length; i++) n += 4 + units[i].length;
    var out = new Uint8Array(n), dv = new DataView(out.buffer);
    for (i = 0; i < units.length; i++) {
      dv.setUint32(o, units[i].length); o += 4;
      out.set(units[i], o); o += units[i].length;
    }
    return out;
  }

  function findNal(units, type) {
    for (var i = 0; i < units.length; i++) {
      if (units[i].length && (units[i][0] & 0x1f) === type) return units[i];
    }
    return null;
  }

  // HEVC 的 NAL 头是 2 字节，类型在第一个字节的高 6 位（H.264 只有 1 字节、低 5 位）。
  // 拿 H.264 那套去找 HEVC 的 SPS 是永远找不到的 —— 这就是选 H.265 后画面全黑的原因。
  function nalTypeHevc(nal) {
    return nal && nal.length ? (nal[0] >> 1) & 0x3f : -1;
  }
  function findNalHevc(units, type) {
    for (var i = 0; i < units.length; i++) {
      if (nalTypeHevc(units[i]) === type) return units[i];
    }
    return null;
  }

  function hex2(v) { return ('0' + (v & 255).toString(16)).slice(-2); }

  function codecString(sps) {
    return 'avc1.' + hex2(sps[1]) + hex2(sps[2]) + hex2(sps[3]);
  }

  // ==================== HEVC 的 SPS 解析 ====================
  // ⚠️ 这里是「硬解黑屏、软解能出画面」的真正成因，务必整段读完再动。
  //
  // H.265 的参数集（VPS/SPS/PPS）在码流里带着**防竞争字节**：
  // 只要 RBSP 里出现 00 00 00/01/02/03，编码器就会在中间插一个 0x03，
  // 变成 00 00 03 xx —— 好让起始码 00 00 01 永远不出现在载荷里。
  // 解析器必须先把这个 0x03 拿掉，才是字段真正的偏移。
  //
  // 拿黑鲨 SHARK PRS-A0 的**真** h265 配置包看过（tools/_t129 抓的）：
  //   SPS 原始  42 01 | 01 01 60 00 00 03 00 b0 00 00 03 00 00 03 00 96 …
  //   去防竞争  42 01 | 01 01 60 00 00 00 00 b0 00 00 00 00 00 96 …
  // 按原始字节读，从 profile_tier_level 往后**每一项都错位**：
  //   兼容位  60000003（应 60000000）
  //   约束位  00 b0 00 00 03 00（应 b0 00 00 00 00 00）
  //   level   0 —— **非法值**（应 150 = L5.0）
  // 后果不是抛错，而是**解码器「配上了」却一帧都不吐**：画面全黑、控制台安静。
  // 宽松的软件解码器会自己去码流里重新读参数集，所以还能出画面 ——
  // 这正是主人报的「硬件解码全黑、软件解码有画面」。
  function hevcUnescape(nal) {
    var out = [], zeros = 0, i;
    for (i = 0; i < nal.length; i++) {
      var b = nal[i];
      // 连续两个 0 之后的 03 是转义字节（规范上它后面一定是 <= 03 的值）
      if (zeros >= 2 && b === 0x03 && i + 1 < nal.length && nal[i + 1] <= 0x03) {
        zeros = 0;
        continue;
      }
      out.push(b);
      zeros = (b === 0x00) ? zeros + 1 : 0;
    }
    return new Uint8Array(out);
  }

  // 从 HEVC 的 SPS 里把 hvcC 与 codec string 要的字段**一次性**抽出来。
  // 入参是 nalUnits() 切出来的、**带 2 字节 NAL 头**的完整 SPS NAL。
  // 只此一处解析，buildHvcC 与 hevcCodecString 都从这里取 ——
  // 两处各读一遍迟早会漂。
  function hevcSpsFields(sps) {
    if (!sps || sps.length < 15) return null;
    var r = hevcUnescape(sps);
    // r[0..1] 是 NAL 头，RBSP 从 r[2] 起：
    //   r[2] = vps_id(4) | max_sub_layers_minus1(3) | temporal_id_nesting_flag(1)
    //   r[3] = profile_space(2) | tier_flag(1) | profile_idc(5)
    var sp = r[3];
    return {
      profileSpace: (sp >> 6) & 0x03,
      tierFlag: (sp >> 5) & 0x01,
      profileIdc: sp & 0x1f,
      compat: ((r[4] << 24) | (r[5] << 16) | (r[6] << 8) | r[7]) >>> 0,
      constraints: [r[8], r[9], r[10], r[11], r[12], r[13]],
      levelIdc: r[14],
      // 这两个也要抄真的：hvcC 第 22 字节用它们，
      // 写死 0 等于告诉解码器「这条流没有时域分层」——和真实 SPS 自相矛盾。
      numTemporalLayers: ((r[2] >> 1) & 0x07) + 1,
      temporalIdNested: r[2] & 0x01
    };
  }

  // hvcC（HEVCDecoderConfigurationRecord，ISO/IEC 14496-15 §8.3.3.1）。
  // profile / 兼容位 / 约束位 / level 全都是从 SPS 的 profile_tier_level 里照抄出来的，
  // 自己编没有任何意义 —— 播放器就是靠这些做的能力匹配。
  function buildHvcC(vps, sps, pps) {
    var f = hevcSpsFields(sps);
    if (!f) return null;

    var arrays = [];
    function pushArray(nalType, nal) {
      if (!nal) return;
      arrays.push(bytes(0x80 | nalType));                       // array_completeness = 1
      arrays.push(u16(1));
      arrays.push(u16(nal.length));
      arrays.push(nal);                                         // NAL 原样带进去（防竞争字节保留）
    }
    pushArray(32, vps);       // VPS
    pushArray(33, sps);       // SPS
    pushArray(34, pps);       // PPS

    return concat(
      bytes(1),                                                 // configurationVersion
      bytes((f.profileSpace << 6) | (f.tierFlag << 5) | f.profileIdc),
      bytes(f.compat >>> 24, (f.compat >>> 16) & 255,
            (f.compat >>> 8) & 255, f.compat & 255),
      bytes(f.constraints[0], f.constraints[1], f.constraints[2],
            f.constraints[3], f.constraints[4], f.constraints[5]),
      bytes(f.levelIdc),
      u16(0xf000),                                              // min_spatial_segmentation_idc = 0
      bytes(0xfc),                                              // parallelismType = 0
      bytes(0xfd),                                              // chromaFormat = 1（4:2:0）
      bytes(0xf8),                                              // bitDepthLumaMinus8 = 0
      bytes(0xf8),                                              // bitDepthChromaMinus8 = 0
      u16(0),                                                   // avgFrameRate
      // constantFrameRate(2) | numTemporalLayers(3) | temporalIdNested(1) | lengthSizeMinusOne(2)
      bytes((f.numTemporalLayers << 3) | (f.temporalIdNested << 2) | 0x03),
      bytes(arrays.length / 4),                                 // numOfArrays
      concat.apply(null, arrays));
  }

  // RFC 7798 要求 general_profile_compatibility_flags 按**位反转**写成十六进制，
  // 而不是把 32 位大端整数直译出来。
  //   0x60000000（Main + Main10 兼容）→ 反转成 0x00000006 → 写成 "6"
  // 直译会得到 "60000000"，那是另一种东西：严格的解析器（Chrome 就是）据此
  // 判「不支持」，MSE 直接拒收 —— 表现同样是黑屏，但锅在编码串这一层。
  // 用乘法逐位反转，不用位移：JS 的位移是 32 位有符号运算，最高位会翻车。
  function reverseBits32(v) {
    var out = 0, i;
    for (i = 0; i < 32; i++) out = out * 2 + ((v >>> i) & 1);
    return out;
  }

  // RFC 7798 的 codec string：hvc1.<profile>.<compat>.<tier+level>.<constraints>
  function hevcCodecString(sps) {
    var f = hevcSpsFields(sps);
    if (!f) return null;
    var compatHex = reverseBits32(f.compat).toString(16).toUpperCase().replace(/^0+/, '') || '0';
    var c = f.constraints.slice();
    while (c.length && c[c.length - 1] === 0) c.pop();          // 末尾的 0 字节去掉
    var constraintHex = c.map(function (b) { return hex2(b); }).join('').toUpperCase();
    return 'hvc1.'
      + (f.profileSpace ? 'ABC'.charAt(f.profileSpace - 1) : '')
      + f.profileIdc + '.'
      + compatHex + '.'
      + (f.tierFlag ? 'H' : 'L') + f.levelIdc
      + (constraintHex ? '.' + constraintHex : '');
  }

  // avcC（AVCDecoderConfigurationRecord）
  function buildAvcC(sps, pps) {
    if (!sps) return null;
    var p = pps || new Uint8Array(0);
    var out = new Uint8Array(8 + sps.length + 3 + p.length);
    out[0] = 1;
    out[1] = sps[1]; out[2] = sps[2]; out[3] = sps[3];
    out[4] = 0xff;                    // lengthSizeMinusOne = 3
    out[5] = 0xe1;                    // 1 条 SPS
    out[6] = (sps.length >> 8) & 255; out[7] = sps.length & 255;
    out.set(sps, 8);
    var o = 8 + sps.length;
    out[o] = 1;                       // 1 条 PPS
    out[o + 1] = (p.length >> 8) & 255; out[o + 2] = p.length & 255;
    out.set(p, o + 3);
    return out;
  }

  function hex2(v) { return ('0' + (v & 255).toString(16)).slice(-2); }

  function codecString(sps) {
    return 'avc1.' + hex2(sps[1]) + hex2(sps[2]) + hex2(sps[3]);
  }

  // hex2 之上再补一层：把范围里的字节拼成小写十六进制串
  function hexOfBytes(buf) {
    var s = '', i;
    if (!buf) return '';
    for (i = 0; i < buf.length; i++) s += hex2(buf[i]);
    return s;
  }

  // ==================== 位读取 ====================
  // VP9 / AV1 的码流头都是按位读的，先垫一个最小的 MSB-first 读取器。
  // 用 *2 而不是 <<1：要读 32 位的字段（AV1 的 timing_info 里有），
  // 位移在 JS 里是 32 位有符号运算，会翻车；乘法不会。
  function BitReader(buf) {
    this.b = buf || new Uint8Array(0);
    this.pos = 0;                       // 单位是 bit
  }
  BitReader.prototype.read = function (n) {
    var v = 0;
    for (var i = 0; i < n; i++) {
      var byte = this.b[this.pos >> 3];
      this.pos++;
      if (byte === undefined) { v = v * 2; continue; }      // 越界按 0 补，别抛
      v = v * 2 + ((byte >> (7 - ((this.pos - 1) & 7))) & 1);
    }
    return v;
  };
  // AV1 的 uvlc()：数前导 0，再读同样多个 bit
  BitReader.prototype.uvlc = function () {
    var zeros = 0;
    while (this.read(1) === 0) {
      zeros++;
      if (zeros > 32) return 2147483647;
    }
    var v = 0;
    for (var i = 0; i < zeros; i++) v += this.read(1) * Math.pow(2, i);
    return v + Math.pow(2, zeros) - 1;
  };

  // ==================== VP8 / VP9 ====================
  // VP8/VP9 没有 H.264 那种「参数集」，关键帧自带全部信息；
  // 但 MP4 的 sample entry 从 avc1/hvc1 换成 vp08/vp09 + vpcC，
  // 盒里的 profile / 位深写错的后果是「MSE 直接拒收」，所以宁可回落到最保守的值。

  // VP9 无压缩帧头（VP9 spec 6.2）。只读到我们需要的 profile 和位深就停。
  function parseVp9Header(frame) {
    if (!frame || frame.length < 2) return null;
    var r = new BitReader(frame);
    if (r.read(2) !== 2) return null;                 // frame_marker 必须是 0b10
    var low = r.read(1), high = r.read(1);
    var profile = (high << 1) | low;                  // 低位先读，别把顺序写反
    if (profile === 3) r.read(1);                     // reserved_zero
    if (r.read(1)) return { profile: profile, bitDepth: 8, subX: 1, subY: 1, key: false };
    var key = (r.read(1) === 0);                      // frame_type: 0 = KEY_FRAME
    r.read(1);                                        // show_frame
    r.read(1);                                        // error_resilient_mode
    var bitDepth = 8, subX = 1, subY = 1;
    if (key) {
      if (r.read(24) !== 0x498342) return { profile: profile, bitDepth: 8, subX: 1, subY: 1, key: true };
      if (profile > 0) {
        var colorSpace = r.read(3);
        if (colorSpace !== 7) {                       // 7 = CS_RGB
          r.read(1);                                  // color_range
          if (profile === 1 || profile === 3) { subX = 1; subY = 1; r.read(1); }
          else { subX = 0; subY = 0; }
          if (profile >= 2) bitDepth = r.read(1) ? 12 : 10;
        }
      }
    }
    return { profile: profile, bitDepth: bitDepth, subX: subX, subY: subY, key: key };
  }

  // VP8 没有 profile / 位深的概念：永远是 8bit 4:2:0。
  // 这里只验一下关键帧起始码，确认拿到的是真 VP8 帧而不是半截数据。
  // ⚠️ 帧头结构（RFC 6386 §9.1）：**3 字节** frame tag，其中 bit0 = 0 才表示关键帧；
  // 起始码 9d 01 2a 跟在它后面，所以落在**第 4~6 字节**，不是第 2~4。
  // 实测设备端关键帧开头就是 70 88 09 | 9d 01 2a —— 偏移写错会永远等不到关键帧（画面全黑）。
  function isVp8KeyFrame(frame) {
    if (!frame || frame.length < 6) return false;
    return (frame[0] & 0x01) === 0 && frame[3] === 0x9d && frame[4] === 0x01 && frame[5] === 0x2a;
  }

  // vpcC（VPCodecConfigurationBox，ISO/IEC 14496-15 §12.2.4）
  // 第 3 字节 = bitDepth(4) | chromaSubsampling(3) | videoFullRangeFlag(1)
  //   4:2:0 colocated 的 chromaSubsampling = 1
  function buildVpcC(profile, bitDepth, subX, subY) {
    var chroma = 1;                                   // 4:2:0
    if (subX && !subY) chroma = 2;                    // 4:2:2
    else if (!subX && !subY) chroma = 3;              // 4:4:4
    return concat(
      bytes(profile & 0x0f),
      bytes(0),                                       // level：只作提示，填 0 不影响解码
      bytes(((bitDepth & 0x0f) << 4) | (chroma << 1)),
      bytes(1),                                       // colourPrimaries = BT.709
      bytes(1),                                       // transferCharacteristics
      bytes(1),                                       // matrixCoefficients
      u16(0));                                        // codecInitializationDataSize = 0
  }

  function vpCodecString(kind, profile, bitDepth, width, height) {
    function two(n) { n = n | 0; return (n < 10 ? '0' : '') + n; }
    // level 只是个能力声明，不影响解码；按分辨率给个像样的值，别写死 10
    var px = (width || 0) * (height || 0);
    var level = px > 2560 * 1440 ? 51 : px > 1920 * 1080 ? 41 : px > 1280 * 720 ? 31 : px > 854 * 480 ? 21 : 10;
    return (kind === 'vp8' ? 'vp08' : 'vp09') + '.' + two(profile) + '.' + two(level) + '.' + two(bitDepth);
  }

  // ==================== AV1 ====================
  // 低开销码流格式的 OBU：头 1 字节 = forbidden(1) obu_type(4) extension(1) has_size(1) reserved(1)
  function av1Obus(buf) {
    var out = [], i = 0;
    while (i < buf.length) {
      var hdr = buf[i];
      if (hdr & 0x80) break;                          // forbidden 位置了 → 不是 OBU 流
      var type = (hdr >> 3) & 0x0f;
      var ext = (hdr >> 2) & 1, hasSize = (hdr >> 1) & 1;
      var start = i, p = i + 1 + (ext ? 1 : 0), size = 0, shift = 0;
      if (hasSize) {
        while (p < buf.length) {
          var b = buf[p++];
          size += (b & 0x7f) * Math.pow(2, shift);
          shift += 7;
          if (!(b & 0x80)) break;
        }
      } else {
        size = buf.length - p;
      }
      // 越界就截到末尾。注意 size = 0 是**合法**的（temporal delimiter OBU 就没载荷），
      // 不能把它当成「这条 OBU 没有长度字段、吃掉剩下全部」—— 那样后面的 OBU 全看不见。
      if (p + size > buf.length) size = Math.max(0, buf.length - p);
      out.push({ type: type, hasSize: !!hasSize, start: start, payloadStart: p, size: size });
      if (p + size <= start) break;                   // 推不动了，收手，别死循环
      i = p + size;
    }
    return out;
  }

  // 把 sequence_header_obu 的载荷解成 av1C 需要的那几位（AV1 spec §5.5.1 / 5.5.2）
  function av1SeqHeader(p) {
    var r = new BitReader(p);
    var seqProfile = r.read(3);
    r.read(1);                                        // still_picture
    var reduced = r.read(1);
    var level = 0, tier = 0;
    if (reduced) {
      level = r.read(5);
    } else {
      if (r.read(1)) {                                // timing_info_present_flag → timing_info()
        r.read(32); r.read(32);                       // num_units_in_display_tick / time_scale
        if (r.read(1)) r.uvlc();                      // equal_picture_interval → num_ticks_per_picture_minus_1
      }
      var initialDelay = r.read(1);                   // initial_display_delay_present_flag
      var opCnt = r.read(5) + 1;
      for (var i = 0; i < opCnt; i++) {
        r.read(12);                                   // operating_point_idc
        var lv = r.read(5), tr = 0;
        if (lv > 7) tr = r.read(1);                   // seq_tier
        if (i === 0) { level = lv; tier = tr; }
        if (initialDelay) { if (r.read(1)) r.read(4); }   // initial_display_delay_minus_1
      }
    }
    var wBits = r.read(4) + 1, hBits = r.read(4) + 1;   // frame_width/height_bits_minus_1
    r.read(wBits); r.read(hBits);                       // max_frame_width/height_minus_1（值用不上）
    var frameIdPresent = reduced ? 0 : r.read(1);
    if (frameIdPresent) { r.read(4); r.read(3); }       // delta_frame_id_length / additional_frame_id_length
    r.read(1); r.read(1); r.read(1);                    // superblock / filter_intra / intra_edge_filter
    if (!reduced) {
      r.read(1); r.read(1); r.read(1); r.read(1);       // interintra / masked / warped / dual_filter
      var orderHint = r.read(1);
      if (orderHint) { r.read(1); r.read(1); }          // jnt_comp / ref_frame_mvs
      var forceSCT = r.read(1) ? 2 : r.read(1);         // seq_choose_screen_content_tools
      if (forceSCT > 0) { if (!r.read(1)) r.read(1); }  // seq_choose_integer_mv（选了就 1 位，没选 2 位）
      if (orderHint) r.read(3);                         // order_hint_bits_minus_1
      r.read(1); r.read(1); r.read(1);                  // superres / cdef / restoration
    }
    // color_config()：只读到 matrix_coefficients 就够 ——
    // av1C 要的那几项（位深、色度采样）到这儿已经全定了，后面那些位读不读都不影响。
    var highBitdepth = r.read(1);
    var twelveBit = (seqProfile === 2 && highBitdepth) ? r.read(1) : 0;
    var mono = seqProfile === 1 ? 0 : r.read(1);
    var matrix = -1;
    if (r.read(1)) { r.read(8); r.read(8); matrix = r.read(8); }   // color_description_present_flag
    var subX = 1, subY = 1;
    if (mono) { subX = 1; subY = 1; }
    else if (matrix === 0) { subX = 0; subY = 0; }      // MC_IDENTITY → 4:4:4
    else if (seqProfile === 0) { subX = 1; subY = 1; }
    else if (seqProfile === 1) { subX = 0; subY = 0; }
    else { subX = twelveBit ? 0 : 1; subY = 0; }
    var bitDepth = highBitdepth ? (twelveBit ? 12 : 10) : 8;
    return { seqProfile: seqProfile, level: level, tier: tier, bitDepth: bitDepth,
             subX: subX, subY: subY, mono: mono };
  }

  // av1C（AV1CodecConfigurationRecord，AV1-ISOBMFF §2.3.3）
  function buildAv1C(info, configObus) {
    var obus = configObus || new Uint8Array(0);
    return concat(
      bytes(0x81),                                    // marker=1 | version=1
      bytes(((info.seqProfile & 0x07) << 5) | (info.level & 0x1f)),
      bytes((info.tier ? 0x80 : 0) | (info.bitDepth > 8 ? 0x40 : 0)
        | (info.bitDepth === 12 ? 0x20 : 0) | (info.mono ? 0x10 : 0)
        | (info.subX ? 0x08 : 0) | (info.subY ? 0x04 : 0)),
      bytes(0),                                       // reserved(3)=0 | initial_delay_present=0 | delay=0
      obus);
  }

  function av1CodecString(info) {
    function two(n) { n = n | 0; return (n < 10 ? '0' : '') + n; }
    return 'av01.' + (info.seqProfile | 0) + '.' + two(info.level) + (info.tier ? 'H' : 'M')
      + '.' + two(info.bitDepth);
  }

  // 从一帧（或一串 OBU）里把 AV1 的起手信息挖出来。
  // 优先用真正的 sequence header OBU（type = 1）；实在没有就退回保守默认值 ——
  // 保守值能解出来的画面就是对的，参数写错才是真正的坑。
  function parseAv1Frame(frame) {
    var obus = av1Obus(frame);
    for (var i = 0; i < obus.length; i++) {
      if (obus[i].type !== 1) continue;               // OBU_SEQUENCE_HEADER
      var payload = frame.subarray(obus[i].payloadStart, obus[i].payloadStart + obus[i].size);
      var info = av1SeqHeader(payload);
      // configOBUs 要的是「带长度字段的 OBU」。原流里没有长度字段就自己补一个。
      var obu;
      if (obus[i].hasSize) {
        obu = frame.subarray(obus[i].start, obus[i].payloadStart + obus[i].size);
      } else {
        var hdr = bytes(frame[obus[i].start] | 0x02);  // 置上 obu_has_size_field
        obu = concat(hdr, leb128(obus[i].size), payload);
      }
      return { info: info, config: obu };
    }
    return null;
  }

  function leb128(n) {
    var out = [], v = n >>> 0;                        // 注意用无符号，否则大数会变成负数
    do {
      var b = v % 128;
      v = Math.floor(v / 128);
      out.push(v > 0 ? (b | 0x80) : b);
    } while (v > 0);
    return bytes.apply(null, out);
  }

  // 给 app.js 的统一入口：从「第一个关键帧」把编码参数读出来。
  // H.264/H.265 用不着它（它们有独立的 config 包），vp8/vp9/av1 没有——
  // MediaCodec 对这三种不给 csd，只能自己从关键帧的码流头里挖。
  // 读不出来就返回 null，让调用方等下一个关键帧，别拿半截数据硬凑。
  function parseKeyFrame(codec, frame, width, height) {
    if (codec === 'vp8') {
      if (!isVp8KeyFrame(frame)) return null;
      return { codec: vpCodecString('vp8', 0, 8, width, height), config: buildVpcC(0, 8, 1, 1) };
    }
    if (codec === 'vp9') {
      var v = parseVp9Header(frame);
      if (!v || !v.key) return null;
      return { codec: vpCodecString('vp9', v.profile, v.bitDepth, width, height),
               config: buildVpcC(v.profile, v.bitDepth, v.subX, v.subY) };
    }
    if (codec === 'av1') {
      var a = parseAv1Frame(frame);
      if (!a) return null;
      return { codec: av1CodecString(a.info), config: buildAv1C(a.info, a.config) };
    }
    return null;
  }

  // moov（初始化分片）：告诉播放器这是什么流、分辨率多少
  // codec 的前 4 个字符就是 sample entry 的类型（avc1 / hvc1 / vp09 / vp08 / av01），
  // 配置盒的名字由它推出来 —— 不用再为每种编码各写一条 if。
  function makeInitSegment(codec, config, width, height) {
    var w = width || 1, h = height || 1;
    var fourcc = String(codec || 'avc1').slice(0, 4).toLowerCase();
    if (fourcc === 'hev1') fourcc = 'hvc1';
    var cfgType = { avc1: 'avcC', hvc1: 'hvcC', vp08: 'vpcC', vp09: 'vpcC', av01: 'av1C' }[fourcc];
    if (!cfgType) { fourcc = 'avc1'; cfgType = 'avcC'; }
    var ftyp = box('ftyp', str('iso5'), u32(1), str('iso5'), str('iso6'), str('mp41'));

    // mvhd：注意 matrix(9×u32=36) 之后是 pre_defined **6×u32=24**，最后才是 next_track_ID。
    // 少写一个 u32 会让整个 moov 的 box 边界错位，MSE 直接报 stream parsing failed。
    var mvhd = fullBox('mvhd', 0, 0,
      u32(0), u32(0), u32(1000000), u32(0xffffffff),
      u32(0x00010000), u16(0x0100), u16(0),
      u32(0), u32(0),
      u32(0x00010000), u32(0), u32(0), u32(0),
      u32(0x00010000), u32(0), u32(0), u32(0),
      u32(0x40000000),
      u32(0), u32(0), u32(0), u32(0), u32(0), u32(0),
      u32(2));

    var tkhd = fullBox('tkhd', 0, 7,
      u32(0), u32(0), u32(1), u32(0), u32(0xffffffff),
      u32(0), u32(0), u16(0), u16(0), u16(0), u16(0),
      u32(0x00010000), u32(0), u32(0), u32(0),
      u32(0x00010000), u32(0), u32(0), u32(0),
      u32(0x40000000), u32(w << 16), u32(h << 16));

    var mdhd = fullBox('mdhd', 0, 0,
      u32(0), u32(0), u32(1000000), u32(0xffffffff), u16(0x55c4), u16(0));

    var hdlr = fullBox('hdlr', 0, 0,
      u32(0), str('vide'), u32(0), u32(0), u32(0), str('VideoHandler\0'));

    var vmhd = fullBox('vmhd', 0, 1, u16(0), u16(0), u16(0), u16(0));
    var dref = fullBox('dref', 0, 0, u32(1), fullBox('url ', 0, 1));
    var dinf = box('dinf', dref);

    // ⚠️ vpcC 是 **FullBox**（ISO/IEC 14496-15 §12.2.4，version = 1）；
    // avcC / hvcC / av1C 都是普通 Box。少写这 4 字节 version+flags，
    // MSE 就会把 profile 当成 version 读、整个 sample entry 跟着错位 ——
    // 表现是 init 段一 append 缓冲立刻作废（"removed from the parent media source"）。
    var configBox = (cfgType === 'vpcC')
      ? fullBox('vpcC', 1, 0, config)
      : box(cfgType, config);
    var sampleEntry = box(fourcc,
      bytes(0, 0, 0, 0, 0, 0), u16(1),
      u16(0), u16(0), u32(0), u32(0), u32(0),
      u16(w), u16(h),
      u32(0x00480000), u32(0x00480000), u32(0),
      u16(1), str('\0'), bytes(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
      u16(0x0018), u16(0xffff), configBox);

    var stsd = fullBox('stsd', 0, 0, u32(1), sampleEntry);
    var stbl = box('stbl', stsd,
      fullBox('stts', 0, 0, u32(0)),
      fullBox('stsc', 0, 0, u32(0)),
      fullBox('stsz', 0, 0, u32(0), u32(0)),
      fullBox('stco', 0, 0, u32(0)));

    var minf = box('minf', vmhd, dinf, stbl);
    var mdia = box('mdia', mdhd, hdlr, minf);
    var trak = box('trak', tkhd, mdia);
    var mvex = box('mvex', fullBox('trex', 0, 0, u32(1), u32(1), u32(33333), u32(0), u32(0)));

    return concat(ftyp, box('moov', mvhd, trak, mvex));
  }

  // moof + mdat（媒体分片）：一帧一个
  function makeMediaSegment(seq, dts, duration, sampleAVCC, isKey) {
    var flags = isKey ? 0x02000000 : 0x01010000;
    var mfhd = fullBox('mfhd', 0, 0, u32(seq));
    var tfhd = fullBox('tfhd', 0, 0x020000, u32(1));
    var tfdt = fullBox('tfdt', 0, 0, u32(dts >>> 0));

    // moof 的长度取决于 trun 里的 data_offset，所以要算两遍（第二遍才是对的）
    function trun(offset) {
      return fullBox('trun', 0, 0x000701, u32(1), u32(offset),
        u32(duration), u32(sampleAVCC.length), u32(flags));
    }
    var traf = box('traf', tfhd, tfdt, trun(0));
    var moof = box('moof', mfhd, traf);
    traf = box('traf', tfhd, tfdt, trun(moof.length + 8));
    moof = box('moof', mfhd, traf);

    return concat(moof, box('mdat', sampleAVCC));
  }

  // ==================== 音频（AAC）====================
  // 音频单独一条 MSE 管线，只用得上「音频版 init 段 + 和视频一样的媒体段」。
  // 采样率、声道数都在设备端给的 AudioSpecificConfig 里，不用另外问。

  var AAC_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
                   16000, 12000, 11025, 8000, 7350];

  // AudioSpecificConfig（ISO/IEC 14496-3）：前两个字节就够解出我们要的东西
  //   audioObjectType  = b0[7:3]
  //   samplingFreqIdx  = b0[2:0] << 1 | b1[7]
  //   channelConfig    = b1[6:3]
  function parseAsc(asc) {
    if (!asc || asc.length < 2) return null;
    var aot = (asc[0] >> 3) & 0x1f;
    var sfi = ((asc[0] & 0x07) << 1) | ((asc[1] >> 7) & 0x01);
    var ch = (asc[1] >> 3) & 0x0f;
    return { objectType: aot || 2, sampleRate: AAC_RATES[sfi] || 48000, channels: ch || 2 };
  }

  function aacCodecString(objectType) {
    return 'mp4a.40.' + (objectType || 2);      // AAC-LC = mp4a.40.2
  }

  // MPEG-4 描述符：tag + 变长长度（每字节 7 位，最高位表示「后面还有」）
  function descLen(n) {
    var out = [], v = n;
    out.unshift(v & 0x7f); v >>= 7;
    while (v > 0) { out.unshift((v & 0x7f) | 0x80); v >>= 7; }
    return bytes.apply(null, out);
  }
  function desc(tag, payload) {
    return concat(bytes(tag), descLen(payload.length), payload);
  }

  // esds：告诉播放器这是 MPEG-4 音频，并把 AudioSpecificConfig 带上
  function buildEsds(asc, bitRate) {
    var br = bitRate || 128000;
    var dsi = desc(0x05, asc);                                    // DecoderSpecificInfo
    var dcd = desc(0x04, concat(bytes(0x40, 0x15), u24(0),         // 0x40 = MPEG-4 Audio
      u32(br), u32(br), dsi));                                     // 0x15 = 音频流
    var sl = desc(0x06, bytes(0x02));                              // SLConfigDescriptor
    var es = desc(0x03, concat(u16(1), bytes(0x00), dcd, sl));
    return fullBox('esds', 0, 0, es);
  }

  function makeAudioInitSegment(asc, bitRate) {
    var info = parseAsc(asc) || { objectType: 2, sampleRate: 48000, channels: 2 };
    var rate = info.sampleRate, ch = info.channels;
    var ftyp = box('ftyp', str('iso5'), u32(1), str('iso5'), str('iso6'), str('mp41'));

    var mvhd = fullBox('mvhd', 0, 0,
      u32(0), u32(0), u32(1000000), u32(0xffffffff),
      u32(0x00010000), u16(0x0100), u16(0),
      u32(0), u32(0),
      u32(0x00010000), u32(0), u32(0), u32(0),
      u32(0x00010000), u32(0), u32(0), u32(0),
      u32(0x40000000),
      u32(0), u32(0), u32(0), u32(0), u32(0), u32(0),
      u32(2));

    // 音频的 tkhd 和视频只差两处：volume 要给满（0x0100 = 1.0），宽高留 0
    var tkhd = fullBox('tkhd', 0, 7,
      u32(0), u32(0), u32(1), u32(0), u32(0xffffffff),
      u32(0), u32(0),
      u16(0), u16(0), u16(0x0100), u16(0),
      u32(0x00010000), u32(0), u32(0), u32(0),
      u32(0x00010000), u32(0), u32(0), u32(0),
      u32(0x40000000), u32(0), u32(0));

    var mdhd = fullBox('mdhd', 0, 0,
      u32(0), u32(0), u32(1000000), u32(0xffffffff), u16(0x55c4), u16(0));

    var hdlr = fullBox('hdlr', 0, 0,
      u32(0), str('soun'), u32(0), u32(0), u32(0), str('SoundHandler\0'));

    var smhd = fullBox('smhd', 0, 0, u16(0), u16(0));
    var dref = fullBox('dref', 0, 0, u32(1), fullBox('url ', 0, 1));
    var dinf = box('dinf', dref);

    // AudioSampleEntry：8 字节 SampleEntry 头 + 8 字节保留 + 声道/位深/采样率 + esds
    // 采样率是 16.16 定点，超过 32767 的（比如 48000）在 JS 里左移会溢出成负数，
    // 但 u32 用的是无符号右移，位模式照样是对的。
    var sampleEntry = box('mp4a',
      bytes(0, 0, 0, 0, 0, 0), u16(1),
      u32(0), u32(0),
      u16(ch), u16(16), u16(0), u16(0),
      u32(rate << 16),
      buildEsds(asc, bitRate));

    var stsd = fullBox('stsd', 0, 0, u32(1), sampleEntry);
    var stbl = box('stbl', stsd,
      fullBox('stts', 0, 0, u32(0)),
      fullBox('stsc', 0, 0, u32(0)),
      fullBox('stsz', 0, 0, u32(0), u32(0)),
      fullBox('stco', 0, 0, u32(0)));

    var minf = box('minf', smhd, dinf, stbl);
    var mdia = box('mdia', mdhd, hdlr, minf);
    var trak = box('trak', tkhd, mdia);
    // 默认采样时长 21333µs ≈ 1024/48000，只是兜底，真正的时长每个分片都写了
    var mvex = box('mvex', fullBox('trex', 0, 0, u32(1), u32(1), u32(21333), u32(0), u32(0)));

    return concat(ftyp, box('moov', mvhd, trak, mvex));
  }

  global.Mp4 = {
    nalUnits: nalUnits,
    annexBtoAVCC: annexBtoAVCC,
    findNal: findNal,
    findNalHevc: findNalHevc,
    nalTypeHevc: nalTypeHevc,
    buildAvcC: buildAvcC,
    buildHvcC: buildHvcC,
    // 参数集解析也导出去：单测要断言「从真 SPS 读出来的字段」，
    // 而不是只断言拼出来的字节（字节对了、字段错了照样是黑屏）
    hevcUnescape: hevcUnescape,
    hevcSpsFields: hevcSpsFields,
    reverseBits32: reverseBits32,
    codecString: codecString,
    hevcCodecString: hevcCodecString,
    // vp8 / vp9 / av1：没有 config 包，靠关键帧自己起手
    parseKeyFrame: parseKeyFrame,
    buildVpcC: buildVpcC,
    buildAv1C: buildAv1C,
    parseVp9Header: parseVp9Header,
    parseAv1Frame: parseAv1Frame,
    makeInitSegment: makeInitSegment,
    makeMediaSegment: makeMediaSegment,
    parseAsc: parseAsc,
    aacCodecString: aacCodecString,
    makeAudioInitSegment: makeAudioInitSegment
  };
})(window);
