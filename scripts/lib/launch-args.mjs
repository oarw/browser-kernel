// 把一个环境配置翻译成 fingerprint-chromium 的命令行参数。
//
// 单独成模块是为了可测试:launcher.js 依赖 store.js,而后者在模块顶层就调
// app.getPath('userData'),在 node --test 下导入即崩。这里是纯函数,不碰 Electron 与文件系统。

// 内核只认这四个 WebRTC IP 处理策略。拼错不会报错,会被 ToWebRTCIPHandlingPolicy() 静默
// 回落到最宽松的 default —— 所以必须白名单校验,不能把用户输入直接拼进命令行。
export const WEBRTC_IP_HANDLING_POLICIES = Object.freeze([
  'default',
  'default_public_and_private_interfaces',
  'default_public_interface_only',
  'disable_non_proxied_udp'
])

export const FINGERPRINT_PLATFORMS = Object.freeze(['windows', 'macos', 'linux'])

// 字体伪装开关。内核的 --disable-spoofing 从 Chrome 144 起提供,接受
// font / audio / canvas / clientrects / gpu 五个值(逗号分隔),我们只用 font 这一个。
//
// 'disable' = 下发 --disable-spoofing=font,关掉字体伪装;'spoof' = 不下发,保持内核默认伪装。
export const FONT_MODES = Object.freeze(['disable', 'spoof'])

const WEBRTC_POLICY_SET = new Set(WEBRTC_IP_HANDLING_POLICIES)

// 这些归一化函数同时被 profile manifest 使用。启动参数和持久化快照必须对同一个脏值作出
// 完全相同的决定，否则界面显示的配置会与真正传给内核的行为分叉。
export function normalizeFontMode(value) {
  const normalized = typeof value === 'string' ? value.trim().toLowerCase() : ''
  return FONT_MODES.includes(normalized) ? normalized : 'disable'
}

export function normalizeWebrtcMode(value) {
  const normalized = typeof value === 'string' ? value.trim().toLowerCase() : ''
  if (normalized === 'off') return 'off'
  return WEBRTC_POLICY_SET.has(normalized) ? normalized : 'disable_non_proxied_udp'
}

// Number('')、Number(null)、Number(false) 都是 0，而 0 恰好是一个看似合法的 seed。
// 明确只接受数字或非空数字字符串，避免缺失字段静默塌缩成 --fingerprint=0。
export function parseFingerprintSeed(value) {
  const raw = typeof value === 'string' ? value.trim() : value
  if (raw === '' || raw === null || raw === undefined || typeof raw === 'boolean') return null
  if (typeof raw !== 'number' && typeof raw !== 'string') return null
  const seed = Number(raw)
  return Number.isInteger(seed) && seed >= 0 && seed <= 0xffffffff ? seed : null
}

// proxyServerUrl:若走本地桥接,这里传入本地代理 url;否则为空,使用环境自身代理。
// windowBounds:可选窗口位置/大小(用于平铺)。
export function buildArgs(profile, userDataDir, proxyServerUrl, windowBounds) {
  const fp = profile.fingerprint || {}
  const args = [
    `--user-data-dir=${userDataDir}`,
    '--no-first-run',
    '--no-default-browser-check'
  ]

  // seed 是 canvas / clientRects / measureText / 字体 / GPU / 音频 / deviceMemory 全部噪声维度
  // 的总闸门。它缺失的后果不是「少一维指纹」,而是所有维度同时静默失效 —— 环境退化成一个只改
  // 了 UA 的普通 Chromium,而界面上完全看不出异常。所以宁可拒绝启动,也不允许省略后继续。
  //
  // 上界校验到 0xffffffff 而不是 0x7fffffff:早期版本的 randomSeed() 生成到 0xffffffff,存量
  // 环境里确实有超过 INT_MAX 的 seed。它们今天是能启动的(只是 UA-CH 那条路径上撞
  // base::StringToInt 溢出、补丁版本号塌缩到 INT_MAX),这里不能反过来把它们锁死。
  // 新生成的 seed 由 randomSeed() 收在 0x7fffffff 以内,存量交给 profile 迁移逐步纠正。
  // 先挡住会被 Number() 悄悄转成 0 的那几类值(null / '' / 空白串 / false / []),否则
  // seed 缺失的环境会静默拿到 --fingerprint=0 —— 而所有这类环境会塌缩到同一个指纹,
  // 正是跨账号关联检测最想看到的信号。
  const seed = parseFingerprintSeed(fp.seed)
  if (seed === null) {
    throw new Error('指纹种子必须是 0 到 4294967295 的整数,当前值无法生成可用指纹')
  }
  args.push(`--fingerprint=${seed}`)

  const platform = String(fp.platform ?? '').trim().toLowerCase()
  if (platform) {
    if (!FINGERPRINT_PLATFORMS.includes(platform)) {
      throw new Error(`不支持的指纹平台:${fp.platform}`)
    }
    args.push(`--fingerprint-platform=${platform}`)
  }

  // platformVersion 的空值语义按平台分岔,不能一刀切:
  //
  // - Linux:空串是**正确取值**。真实 Chrome on Linux 的 Sec-CH-UA-Platform-Version 恒为空,
  //   而不传参数会让内核回落到它自己那个非空的未文档化默认值(kLinuxVersions)。所以必须显式
  //   下发等号后为空,原来那句 if (fp.platformVersion) 恰好把唯一正确的取值短路掉了。
  // - Windows / macOS:空串表示「使用内核默认」(UI 上输入框的 placeholder 就是这么写的)。
  //   这里整条不下发,让内核按 seed 从自带表里挑 —— 真实 Chrome 在这两个平台上必定上报非空值,
  //   显式发空反而是个破绽。
  const platformVersion = typeof fp.platformVersion === 'string'
    ? fp.platformVersion.trim()
    : fp.platformVersion == null ? '' : String(fp.platformVersion).trim()
  if (platform === 'linux') {
    args.push(`--fingerprint-platform-version=${platformVersion ?? ''}`)
  } else if (platformVersion !== undefined && platformVersion !== null && platformVersion !== '') {
    args.push(`--fingerprint-platform-version=${platformVersion}`)
  }

  const brand = typeof fp.brand === 'string' ? fp.brand.trim() : ''
  const brandVersion = typeof fp.brandVersion === 'string' ? fp.brandVersion.trim() : ''
  if (brand) args.push(`--fingerprint-brand=${brand}`)
  if (brandVersion) args.push(`--fingerprint-brand-version=${brandVersion}`)

  // 内核用无异常保护的 std::stoul 解析这个值,而 Blink 以 -fno-exceptions 编译:非数字会让
  // 每个 renderer 进程直接崩。脏值宁可整条不下发,让内核走 seed 派生。
  const cores = Number(fp.hardwareConcurrency)
  if (Number.isInteger(cores) && cores >= 1 && cores <= 256) {
    args.push(`--fingerprint-hardware-concurrency=${cores}`)
  }

  const timezone = typeof fp.timezone === 'string' ? fp.timezone.trim() : ''
  const language = typeof fp.language === 'string' ? fp.language.trim() : ''
  const acceptLanguages = typeof fp.acceptLanguages === 'string' ? fp.acceptLanguages.trim() : ''
  if (timezone) args.push(`--timezone=${timezone}`)
  if (language) args.push(`--lang=${language}`)
  if (acceptLanguages) args.push(`--accept-lang=${acceptLanguages}`)

  // WebRTC 策略:默认禁止非代理 UDP,避免真实 IP 泄漏。
  //
  // 这里原来下发的是 --disable-non-proxied-udp,而那个参数在 fingerprint-chromium 和上游
  // Chromium 里都不存在 —— Chromium 对未知开关静默忽略,所以它一直是空转。真正的开关是
  // --force-webrtc-ip-handling-policy。
  //
  // 另外 'off' 不等于「不设防」:内核把 kWebRTCIPHandlingPolicy 的默认 pref 就改成了
  // disable_non_proxied_udp,所以不下发任何参数时防护依然生效。'off' 只是「不覆盖内核默认」。
  const webrtcMode = normalizeWebrtcMode(fp.webrtcMode)
  if (webrtcMode !== 'off') {
    const policy = webrtcMode
    args.push(`--force-webrtc-ip-handling-policy=${policy}`)
  }

  // 字体伪装。**默认关掉**,因为它会把中文渲染成方框。
  //
  // 内核的字体伪装由 --fingerprint=<seed> 驱动(见上面 seed 那段注释里列的维度),做法是按 seed
  // 过滤页面可见的字体集合。宿主装着微软雅黑/宋体/黑体也没用 —— 一旦 CJK 字体被过滤掉,
  // 中文就没有任何字形来源,整页汉字变成 □□□,而拉丁字母因为总有兜底字体所以看着正常。
  // 「只有中文是方框、英文正常」正是这个机制的特征。
  //
  // 权衡:关掉它意味着字体列表变成一个可被指纹的维度。但一个显示不了中文的浏览器对用户来说
  // 是直接不可用,而 canvas / audio / clientrects / GPU / WebGL 全部维度仍然由 seed 正常伪装,
  // 只让出字体这一维。想要完整伪装的用户可以在环境里改成「伪装」。
  //
  // 注意 flag 拼法:是 --disable-spoofing=font(Chrome 144+),**不是** --disable-font-spoofing。
  // Chromium 对未知开关静默忽略,拼错不会报错,只会让这个修复空转 —— 仓库里
  // --disable-non-proxied-udp 已经踩过一次这个坑。
  if (normalizeFontMode(fp.fontMode) === 'disable') {
    args.push('--disable-spoofing=font')
  }

  // 代理:优先使用桥接后的本地代理;否则用环境自身的无认证代理
  const proxy = profile.proxy
  if (proxyServerUrl) {
    args.push(`--proxy-server=${proxyServerUrl}`)
  } else {
    const proxyHost = typeof proxy?.host === 'string' ? proxy.host.trim() : ''
    const proxyPort = proxy?.port === null || proxy?.port === undefined ? '' : String(proxy.port).trim()
    if (proxy && proxy.enabled && proxyHost && proxyPort) {
      const type = String(proxy.type || 'http').trim().toLowerCase()
      const scheme = type === 'socks5' ? 'socks5' : type === 'https' ? 'https' : 'http'
      args.push(`--proxy-server=${scheme}://${proxyHost}:${proxyPort}`)
    }
  }

  // 窗口平铺
  if (windowBounds) {
    args.push(`--window-position=${windowBounds.x},${windowBounds.y}`)
    args.push(`--window-size=${windowBounds.width},${windowBounds.height}`)
  }

  // 起始页
  const startupUrl = typeof profile.startupUrl === 'string' ? profile.startupUrl.trim() : ''
  if (startupUrl) args.push(startupUrl)

  return args
}
