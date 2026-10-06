import { spawn, execFile } from 'node:child_process'
import { readFile, mkdir, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { promisify } from 'node:util'

export const wait = ms => new Promise(resolve => setTimeout(resolve, ms))
export async function until(check, label, timeout = 20000) {
  const end = Date.now() + timeout
  while (Date.now() < end) { const value = await check(); if (value) return value; await wait(100) }
  throw new Error(`Timed out: ${label}`)
}
export async function connect(url) {
  const socket = new WebSocket(url)
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { socket.close(); reject(new Error('CDP connect timeout')) }, 10000)
    socket.addEventListener('open', () => { clearTimeout(timer); resolve() }, { once: true })
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('CDP connect failed')) }, { once: true })
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
    send(method, params = {}) {
      return new Promise((resolve, reject) => {
        const id = ++sequence
        const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)) }, 20000)
        pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }))
      })
    },
    async evaluate(expression) {
      const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true })
      if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails))
      return result.result.value
    }
  }
}
export async function launchBrowser(executable, directory, args = []) {
  await mkdir(directory, { recursive: true })
  await rm(join(directory, 'DevToolsActivePort'), { force: true })
  const child = spawn(executable, [`--user-data-dir=${directory}`, '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1', ...args], { stdio: 'ignore', windowsHide: false })
  let failure
  child.once('error', error => { failure = error })
  let port, browser
  try {
    port = await until(async () => {
      if (failure) throw failure
      if (child.exitCode !== null) throw new Error(`Browser exited (${child.exitCode})`)
      try { return (await readFile(join(directory, 'DevToolsActivePort'), 'utf8')).split('\n')[0].trim() } catch (error) { if (!['ENOENT', 'EPERM', 'EBUSY', 'EACCES'].includes(error.code)) throw error }
    }, 'debug port')
    const metadata = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json()
    browser = await connect(metadata.webSocketDebuggerUrl)
    return {
      child, browser, metadata,
      async page(match = () => true) {
        const target = await until(async () => (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(x => x.type === 'page' && match(x)), 'page target')
        return connect(target.webSocketDebuggerUrl)
      },
      async close() {
        await browser.send('Browser.close').catch(() => {})
        try { await until(() => child.exitCode !== null, 'clean browser exit', 10000) }
        finally { browser.close(); if (child.exitCode === null) await promisify(execFile)('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true }) }
      }
    }
  } catch (error) {
    browser?.close()
    if (child.pid && child.exitCode === null) await promisify(execFile)('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true }).catch(() => {})
    throw error
  }
}
