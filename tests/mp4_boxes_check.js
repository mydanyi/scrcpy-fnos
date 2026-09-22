'use strict';
// 把**真设备**抓到的 h265 参数集喂给 mp4.js，打印结果给 Python 侧断言。
// 夹具来源：黑鲨 SHARK PRS-A0 的真实 scrcpy config 包（tools/_t129_phone_sps.py 抓的）。
// 为什么用真数据：编造的 SPS 不会有防竞争字节，正好绕开要防的那个坑。
global.window = global;
var path = require('path');
require(path.join(__dirname, '..', 'app', 'frontend', 'mp4.js'));
var M = global.Mp4;

var VPS = '40010c01ffff016000000300b00000030000030096ac09';
var SPS = '420101016000000300b00000030000030096a00220800961cbe5aee4c92ea520a0c0c05da14250';
var PPS = '4401c0e30f09418f6108';

function u(hex) { return new Uint8Array(Buffer.from(hex, 'hex')); }
function hex(u8) { return Buffer.from(u8).toString('hex'); }

var out = {};
try {
  var sps = u(SPS);
  out.spsLen = sps.length;
  out.rawEscapeByte = sps[7];            // 原始字节里那个 0x03（防竞争字节）
  out.unescaped = hex(M.hevcUnescape(sps).slice(2, 16));
  out.fields = M.hevcSpsFields(sps);
  out.codec = M.hevcCodecString(sps);
  out.reverse60000000 = M.reverseBits32(0x60000000);
  var hvcc = M.buildHvcC(u(VPS), sps, u(PPS));
  out.hvcc = hex(hvcc);
  out.hvccHead = hex(hvcc.slice(0, 23));
  out.hvccLevel = hvcc[12];
  out.hvccCompat = hex(hvcc.slice(2, 6));
  out.hvccConstraints = hex(hvcc.slice(6, 12));
  out.hvccByte21 = hvcc[21];
  out.hvccNumArrays = hvcc[22];

  // 解析器不能把「本来就没有防竞争字节的 SPS」也改坏。
  // 这串就是真 SPS 去转义后的样子（42 01 | 01 01 60 00 00 00 b0 00 00 00 00 00 96 a0）
  var noEsc = u('4201010160000000b0000000000096a0');
  out.plainFields = M.hevcSpsFields(noEsc);
} catch (e) {
  out.error = e && e.stack ? e.stack : String(e);
}
console.log('RESULT ' + JSON.stringify(out));
