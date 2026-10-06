// Serialized into the local fixture page and worker. Keep all dependencies inside this function.
export async function collectKernelProbe() {
  const hash = async (bytes) => [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map((byte) => byte.toString(16).padStart(2, '0')).join('')
  const result = {
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    language: navigator.language,
    languages: [...navigator.languages],
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory ?? null,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    timezoneOffsets: [new Date('2026-01-15T12:00:00Z').getTimezoneOffset(), new Date('2026-07-15T12:00:00Z').getTimezoneOffset()]
  }
  try {
    result.clientHints = navigator.userAgentData ? await navigator.userAgentData.getHighEntropyValues(['architecture', 'bitness', 'model', 'platformVersion', 'fullVersionList', 'wow64']) : null
  } catch (error) { result.clientHints = { error: error.message } }
  try {
    const canvas = new OffscreenCanvas(240, 80)
    const ctx = canvas.getContext('2d', { willReadFrequently: true })
    ctx.fillStyle = '#e6f2fb'; ctx.fillRect(0, 0, 240, 80)
    ctx.fillStyle = '#1c4260'; ctx.font = '18px Arial'; ctx.fillText('FingerBrowser 012345', 7, 25)
    ctx.fillStyle = '#b84821'; ctx.fillRect(11, 40, 137, 13)
    result.canvas = { hash: await hash(ctx.getImageData(0, 0, 240, 80).data), textWidth: ctx.measureText('FingerBrowser 012345').width }
    const metricFields = ['width', 'actualBoundingBoxLeft', 'actualBoundingBoxRight', 'actualBoundingBoxAscent', 'actualBoundingBoxDescent', 'fontBoundingBoxAscent', 'fontBoundingBoxDescent', 'emHeightAscent', 'emHeightDescent', 'hangingBaseline', 'alphabeticBaseline', 'ideographicBaseline']
    const measure = (context) => {
      // Generic font selection depends on language. Explicitly align document
      // and worker canvas contexts before comparing their native metrics.
      if ('lang' in context) context.lang = navigator.language
      const samples = []
      for (const family of ['Arial', 'Times New Roman', 'monospace']) for (const size of [12, 24]) for (const text of ['', 'FingerBrowser 012345', '中文测试', 'Ag ij !@#']) {
        context.font = `${size}px ${family}`
        const metrics = context.measureText(text)
        samples.push({ font: context.font, text, values: Object.fromEntries(metricFields.filter((field) => typeof metrics[field] === 'number').map((field) => [field, metrics[field]])) })
      }
      return samples
    }
    result.textMetrics = { offscreen: measure(ctx) }
    if (typeof document !== 'undefined') {
      const domContext = document.createElement('canvas').getContext('2d')
      domContext.font = '18px Arial'
      result.domTextWidth = domContext.measureText('FingerBrowser 012345').width
      result.textMetrics.dom = measure(domContext)
    }
  } catch (error) { result.canvas = { error: error.message } }
  try {
    const gl = new OffscreenCanvas(32, 32).getContext('webgl')
    if (!gl) result.webgl = { unavailable: true }
    else {
      const debug = gl.getExtension('WEBGL_debug_renderer_info')
      result.webgl = { vendor: gl.getParameter(gl.VENDOR), renderer: gl.getParameter(gl.RENDERER), unmaskedVendor: debug ? gl.getParameter(debug.UNMASKED_VENDOR_WEBGL) : null, unmaskedRenderer: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : null, maxTextureSize: gl.getParameter(gl.MAX_TEXTURE_SIZE) }
      gl.getExtension('WEBGL_lose_context')?.loseContext()
    }
  } catch (error) { result.webgl = { error: error.message } }
  return result
}

// Normal page JavaScript reads the browser APIs; CDP is used only for version/clean shutdown.
export async function runKernelProbe(config, collect) {
  const timeout = (promise, label) => Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(new Error(`${label} timed out`)), 15000))])
  try {
    if (config.frame) {
      parent.postMessage({ token: config.token, snapshot: await collect() }, config.origin)
      return
    }
    const frame = (origin) => timeout(new Promise((resolve, reject) => {
      const element = document.createElement('iframe')
      const receive = (event) => {
        if (event.source !== element.contentWindow || event.origin !== origin || event.data?.token !== config.token) return
        removeEventListener('message', receive)
        event.data.error ? reject(new Error(event.data.error)) : resolve(event.data.snapshot)
      }
      addEventListener('message', receive)
      element.src = `${origin}/frame?token=${config.token}`
      document.body.append(element)
    }), 'iframe')
    const worker = timeout(new Promise((resolve, reject) => {
      const instance = new Worker(`/worker?token=${config.token}`)
      instance.onmessage = ({ data }) => { instance.terminate(); data.error ? reject(new Error(data.error)) : resolve(data) }
      instance.onerror = (event) => { instance.terminate(); reject(new Error(event.message)) }
    }), 'worker')
    const [top, repeated, sameOriginFrame, crossOriginFrame, dedicatedWorker] = await Promise.all([collect(), collect(), frame(config.origin), frame(config.crossOrigin), worker])
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open('kernel-acceptance', 1)
      request.onupgradeneeded = () => request.result.createObjectStore('markers')
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(request.error)
    })
    const indexedBefore = await new Promise((resolve, reject) => {
      const request = db.transaction('markers').objectStore('markers').get('owner')
      request.onsuccess = () => resolve(request.result ?? null)
      request.onerror = () => reject(request.error)
    })
    const storageBefore = { localStorage: localStorage.getItem('owner'), cookie: document.cookie.split('; ').find((item) => item.startsWith('owner='))?.slice(6) ?? null, indexedDB: indexedBefore }
    localStorage.setItem('owner', config.marker)
    document.cookie = `owner=${config.marker}; Path=/; Max-Age=3600; SameSite=Lax`
    await new Promise((resolve, reject) => {
      const transaction = db.transaction('markers', 'readwrite')
      transaction.objectStore('markers').put(config.marker, 'owner')
      transaction.oncomplete = resolve
      transaction.onerror = () => reject(transaction.error)
    })
    db.close()
    const headers = await (await fetch('/headers')).json()
    await fetch(`/result?token=${config.token}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ top, repeated, sameOriginFrame, crossOriginFrame, dedicatedWorker, storageBefore, headers }) })
    document.getElementById('status').textContent = '采集完成，可以关闭测试窗口。'
  } catch (error) {
    if (config.frame) parent.postMessage({ token: config.token, error: error.message }, config.origin)
    else await fetch(`/result?token=${config.token}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ error: error.message }) })
  }
}
