import { spawn, execFile } from 'node:child_process'
import { createWriteStream, createReadStream } from 'node:fs'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { randomUUID, createHash } from 'node:crypto'
import { dirname, join, resolve } from 'node:path'
import { arch, platform, release } from 'node:os'
import { parseArgs, promisify } from 'node:util'
import { buildArgs } from './lib/launch-args.mjs'
import { collectKernelProbe, runKernelProbe } from './lib/kernel-probe.mjs'
import { evaluateScenario, summarizeAcceptance } from './lib/kernel-acceptance-checks.mjs'

const { values } = parseArgs({ options: { executable: { type: 'string' }, output: { type: 'string' }, 'record-only': { type: 'boolean' }, 'without-canvas-noise': { type: 'boolean' }, help: { type: 'boolean' } } })
if (values.help || !values.executable) {
  console.log('Usage: node scripts/verify-kernel.mjs --executable <chrome.exe> [--output <report directory>] [--record-only] [--without-canvas-noise]')
  console.log('Runs visible isolated browser fixtures. Reports native API consistency, restart stability and synthetic storage isolation; does not certify site compatibility or anonymity.')
  console.log('--record-only records semantic failures without failing the command; startup, collection or cleanup errors still fail. --without-canvas-noise is a diagnostic control, not a product setting.')
  process.exit(values.help ? 0 : 1)
}
if (process.platform !== 'win32') throw new Error('This initial acceptance fixture requires Windows')

const output = resolve(values.output || `release/kernel-acceptance/${new Date().toISOString().replace(/[:.]/g, '-')}`)
await mkdir(output, { recursive: true })
const temporary = await mkdtemp(join(output, 'profiles-'))
const execFileAsync = promisify(execFile)
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const requests = new Map()
const servers = []
const report = {
  schemaVersion: 1, startedAt: new Date().toISOString(),
  host: { platform: platform(), architecture: arch(), osRelease: release(), node: process.version },
  harness: { mode: 'headed', probe: 'native page JavaScript; loopback HTTP; CDP only for version and clean shutdown', instrumentation: ['--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1'] },
  limitations: ['Three representative synthetic profiles, one Windows host and one local origin pair; not an exhaustive device matrix.', 'Remote debugging and local HTTP are test instrumentation; results do not certify an uninstrumented browser or HTTPS/TLS behavior.', 'CJK glyph coverage, audio, site compatibility, proxy routing, DNS/WebRTC leakage and profile downgrade are not certified.'],
  scenarios: []
}
if (values['without-canvas-noise']) report.harness.instrumentation.push('--disable-spoofing=font,canvas')

async function serve(request, response) {
  const url = new URL(request.url, 'http://127.0.0.1')
  const run = requests.get(url.searchParams.get('token'))
  response.setHeader('Cache-Control', 'no-store')
  response.setHeader('Accept-CH', 'Sec-CH-UA-Full-Version-List, Sec-CH-UA-Platform-Version, Sec-CH-UA-Arch, Sec-CH-UA-Bitness')
  if (url.pathname === '/headers') {
    response.setHeader('Content-Type', 'application/json')
    response.end(JSON.stringify(Object.fromEntries(Object.entries(request.headers).filter(([key]) => ['user-agent', 'accept-language'].includes(key) || key.startsWith('sec-ch-ua')))))
  } else if (!run) { response.writeHead(404).end() }
  else if (url.pathname === '/result' && request.method === 'POST' && request.headers.origin === run.config.origin) {
    let body = ''
    for await (const chunk of request) {
      body += chunk.toString()
      if (body.length > 1024 * 1024) throw new Error('Fixture report exceeds size limit')
    }
    run.resolve(JSON.parse(body))
    response.writeHead(204).end()
  } else if (url.pathname === '/worker') {
    response.setHeader('Content-Type', 'text/javascript')
    response.end(`(${collectKernelProbe.toString()})().then(postMessage).catch(error => postMessage({error:error.message}))`)
  } else if (url.pathname === '/' || url.pathname === '/frame') {
    response.setHeader('Content-Type', 'text/html; charset=utf-8')
    const config = { ...run.config, frame: url.pathname === '/frame' }
    response.end(`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>FingerBrowser 内核验收</title><body><h1>内核验收 · 临时测试环境</h1><p id="status">正在采集浏览器接口…</p><script>(${runKernelProbe.toString()})(${JSON.stringify(config)}, ${collectKernelProbe.toString()})</script></body></html>`)
  } else { response.writeHead(404).end() }
}

async function startServer() {
  const server = createServer((request, response) => { void serve(request, response).catch((error) => { response.writeHead(500).end(); requests.get(new URL(request.url, 'http://127.0.0.1').searchParams.get('token'))?.resolve({ error: error.message }) }) })
  servers.push(server)
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
  return `http://127.0.0.1:${server.address().port}`
}

async function connectBrowser(directory, hasExited) {
  const deadline = Date.now() + 20000
  while (Date.now() < deadline) {
    if (hasExited()) throw new Error('Browser exited before exposing its debugging endpoint')
    let contents
    try { contents = await readFile(join(directory, 'DevToolsActivePort'), 'utf8') } catch (error) {
      // Chromium can briefly hold the file exclusively while publishing it on Windows.
      if (!['ENOENT', 'EBUSY', 'EPERM', 'EACCES'].includes(error.code)) throw error
    }
    if (contents) {
      const [port, path] = contents.trim().split(/\r?\n/)
      if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535 || !/^\/devtools\/browser\/[a-zA-Z0-9-]+$/.test(path)) { await wait(50); continue }
      const socket = new WebSocket(`ws://127.0.0.1:${port}${path}`)
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => { socket.close(); reject(new Error('CDP connection timed out')) }, 5000)
        socket.addEventListener('open', () => { clearTimeout(timer); resolve() }, { once: true })
        socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('CDP connection failed')) }, { once: true })
      })
      let sequence = 0
      const pending = new Map()
      socket.addEventListener('message', ({ data }) => {
        const message = JSON.parse(String(data)), item = pending.get(message.id)
        if (!item) return
        pending.delete(message.id); clearTimeout(item.timer)
        message.error ? item.reject(new Error(message.error.message)) : item.resolve(message.result)
      })
      socket.addEventListener('close', () => { for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error('CDP closed')) }; pending.clear() })
      return {
        close: () => socket.close(),
        send(method) {
          return new Promise((resolve, reject) => {
            const id = ++sequence
            const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timed out: ${method}`)) }, 5000)
            pending.set(id, { resolve, reject, timer })
            socket.send(JSON.stringify({ id, method }))
          })
        }
      }
    }
    await wait(50)
  }
  throw new Error('Browser startup timed out')
}

async function captureRun(executable, scenario, kind, directory, origin, crossOrigin) {
  await mkdir(directory, { recursive: true })
  await rm(join(directory, 'DevToolsActivePort'), { force: true })
  const token = randomUUID()
  let deliver
  const completed = new Promise((resolve) => { deliver = resolve })
  requests.set(token, { config: { token, origin, crossOrigin, marker: scenario.id }, resolve: deliver })
  const args = [...buildArgs({ fingerprint: scenario.fingerprint, startupUrl: `${origin}/?token=${token}`, proxy: { enabled: false } }, directory, '', { width: 1100, height: 760, x: 40, y: 40 }), ...report.harness.instrumentation]
  const log = createWriteStream(join(output, `${scenario.id}-${kind}.log`))
  const started = Date.now()
  const child = spawn(executable, args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: false })
  child.stdout.pipe(log, { end: false }); child.stderr.pipe(log, { end: false })
  let exited = false, spawnError
  const stopped = new Promise((resolve) => {
    child.once('error', (error) => { spawnError = error; exited = true; resolve() })
    child.once('exit', () => { exited = true; resolve() })
  })
  let cdp, timer, closeTimer
  try {
    cdp = await connectBrowser(directory, () => exited)
    const version = await cdp.send('Browser.getVersion')
    const observed = await Promise.race([
      completed,
      stopped.then(() => { throw spawnError || new Error('Browser exited before the fixture completed') }),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Native page probe timed out')), 30000) })
    ])
    if (observed.error) throw new Error(observed.error)
    return { kind, elapsedMs: Date.now() - started, browser: version, observed }
  } finally {
    clearTimeout(timer)
    requests.delete(token)
    await cdp?.send('Browser.close').catch(() => {})
    if (!exited) await Promise.race([stopped, new Promise((resolve) => { closeTimer = setTimeout(resolve, 5000) })])
    clearTimeout(closeTimer)
    if (!exited && child.pid) {
      await execFileAsync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, timeout: 10000 })
      await stopped
      report.cleanupWarning = 'At least one browser required forced termination; restart persistence cannot be certified.'
    }
    cdp?.close()
    log.end()
  }
}

try {
  const executable = resolve(values.executable)
  const executableHash = createHash('sha256')
  for await (const chunk of createReadStream(executable)) executableHash.update(chunk)
  report.kernel = { executableSha256: executableHash.digest('hex'), source: 'Explicit executable; archive provenance must be checked separately against the build or source record.' }
  const origin = await startServer(), crossOrigin = await startServer()
  const profiles = [
    { id: 'windows-zh', fingerprint: { seed: 20260928, platform: 'windows', platformVersion: '19.0.0', hardwareConcurrency: 8, brand: 'Chrome', language: 'zh-CN', acceptLanguages: 'zh-CN,zh', timezone: 'Asia/Shanghai' } },
    { id: 'macos-en', fingerprint: { seed: 20260929, platform: 'macos', platformVersion: '15.7.1', hardwareConcurrency: 8, brand: 'Chrome', language: 'en-US', acceptLanguages: 'en-US,en', timezone: 'America/New_York' } },
    { id: 'linux-en', fingerprint: { seed: 20260930, platform: 'linux', platformVersion: '', hardwareConcurrency: 8, brand: 'Chrome', language: 'en-US', acceptLanguages: 'en-US,en', timezone: 'Europe/London' } }
  ]
  for (const profile of profiles) {
    const scenario = { ...profile, runs: [], checks: [] }
    report.scenarios.push(scenario)
    for (const kind of ['first', 'restart', 'same-seed-isolated']) {
      const directory = join(temporary, `${profile.id}-${kind === 'same-seed-isolated' ? 'clone' : 'original'}`)
      console.log(`Collecting ${profile.id}/${kind}`)
      scenario.runs.push(await captureRun(executable, scenario, kind, directory, origin, crossOrigin))
      await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2))
    }
    scenario.checks = evaluateScenario(scenario)
  }
  report.summary = summarizeAcceptance(report.scenarios)
  if (report.cleanupWarning) report.summary.status = 'error'
} catch (error) {
  report.error = error.stack || error.message
  report.summary = { status: 'error' }
} finally {
  for (const server of servers) { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)) }
  try {
    if (dirname(temporary) !== output || !temporary.startsWith(join(output, 'profiles-'))) throw new Error('Unexpected fixture cleanup path')
    await rm(temporary, { recursive: true, force: true, maxRetries: 10, retryDelay: 300 })
  } catch (error) { report.cleanupError = error.message; report.summary = { ...report.summary, status: 'error' } }
  report.finishedAt = new Date().toISOString()
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2))
}
console.log(JSON.stringify({ report: join(output, 'report.json'), ...report.summary, error: report.error || null }, null, 2))
process.exitCode = report.summary.status === 'passed' || (values['record-only'] && report.summary.status !== 'error') ? 0 : 1
