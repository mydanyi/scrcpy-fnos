# scrcpy-fnos

我手头安卓设备不少——手机、平板，还有一台常年挂在 NAS 上的云手机。
想在浏览器里随时打开就能看、就能操作，但 scrcpy 是桌面程序，每台电脑都得装一个，
手机上还压根用不了。

所以就把 scrcpy 搬到了飞牛 NAS 上，做成了一个应用：打开网页就是投屏，不用再装客户端。
装完之后桌面上会多一个 **牛牛牧场** 图标，点开就是控制台——真机、云手机、redroid 容器
都能连，手机浏览器打开也一样用。

---

## 怎么装

1. 到 [Releases](https://github.com/mydanyi/scrcpy-fnos/releases) 下载最新的 `scrcpy-fnos-v*.fpk`
2. 打开飞牛 **应用中心**，点左下角的 **手动安装**
3. 选好存储空间，点 **从电脑上传**，把刚下载的 fpk 选上
   （fpk 已经在 NAS 上，就点 **从 NAS 添加**）
4. 装完在应用中心点启动，再点桌面上的 **牛牛牧场** 图标

它挂在飞牛自己的统一网关后面，不用记端口，也不占端口。

设备连接记录这些存在 `/vol1/@appdata/scrcpy-fnos`，重装不会动它。

### 装之前要有什么

- 飞牛 fnOS 1.1.3100 以上
- x86_64 架构的机器
- 宿主机上要有 `python3` 和 `adb`（装的时候会自动检查，缺了会直接提示）
- 一个安卓设备：真机（USB 插上、或开「无线调试」）、云手机，或者一个 redroid 容器。
  连真机就够，Docker 不是必需的；想跑 redroid 容器才需要先把 Docker 装好

## 用起来

1. **连设备**
   - 真机：USB 插上就认；或者开「无线调试」，页面上填地址和端口就能连，也能无线配对
   - redroid 容器 / 云手机：把 adb 端口映射出来，填 `<NAS_IP>:<端口>`
2. **点设备 → 投屏**。画质有几档（原画 / 均衡 / 省流 / 极限省流），
   声音、麦克风、要不要显示触摸点都能单独开关。
3. 设备行为可以调：连接时唤醒、连上后自动关屏（省电）、断开时恢复、屏幕不熄这些。
4. 面板里有四个抽屉：**文件**（浏览 / 上传 / 下载 / 装 APK / 跑脚本）、**终端**、
   **日志**、**状态**。

> 画面在浏览器里用 MSE + WebCodecs 解码，能走硬解就走硬解，不额外占 CPU。

## 架构

服务直接跑在飞牛宿主机上（不是 Docker），通过 unix socket 接进飞牛统一网关，
对外由网关转发和鉴权，不单独开端口。

设备那头还是 scrcpy 的老办法：把 `scrcpy-server.jar` 推进设备、adb 连上去，
视频、音频、控制各走一条通道。

## 安卓环境

能被 `adb connect` 连上的设备都能用：

- **真机**：USB 或 WiFi 调试
- **云手机 / 模拟器**：填地址端口
- **redroid 容器**：怎么起容器可以参考 MAA 那个仓库里
  [「安卓环境」一节](https://github.com/mydanyi/MAA-FnOS#安卓环境redroid)

## 自己构建

想自己打包：仓库根目录的 `assemble.sh` 在装了 fnpack 的机器上跑一下就行，
产物是 `scrcpy-fnos.fpk`。

## 致谢

引擎和工具都是现成的，我只是把它们拼到了一起。

- [scrcpy](https://github.com/Genymobile/scrcpy) —— 投屏引擎
- [adb / platform-tools](https://developer.android.com/tools/releases/platform-tools) —— 连安卓的那条线
- [xterm.js](https://xtermjs.org/) —— 网页里的终端
- [ERSTT/redroid](https://github.com/ERSTT/redroid) —— 容器化安卓（跑 redroid 容器时用）

## 许可证

代码以 **GPL-3.0 + 附加条款** 发布：你可以免费用、改、分享，但**不能拿它赚钱**，
而且**改了之后对外发布必须同样开源**。完整条款见 [LICENSE](LICENSE)。

`app/tool/` 下的 adb 与 scrcpy-server 是第三方组件，遵循它们各自的协议
（见 `app/tool/LICENSE`、`app/tool/adb-NOTICE.txt`）。

## 免责声明

这是我给自己用的小工具，只做个人设备的投屏与管理。
用它去操控不属于自己的设备、或者绕开应用 / 游戏的限制，风险请自己评估。
