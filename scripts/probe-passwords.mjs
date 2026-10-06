import { createServer } from 'node:http'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve, join } from 'node:path'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import assert from 'node:assert/strict'
import { launchBrowser, wait, until } from './lib/browser-session.mjs'

const [exe, outputArg] = process.argv.slice(2)
const output = resolve(outputArg)
await mkdir(output, { recursive: true })
const server = createServer((req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8')
  if (req.url === '/done') res.end('<h1>Signed in</h1>')
  else res.end(`<!doctype html><title>Password acceptance</title><h1>Local test account</h1><form action="/done" method="post"><input name="username" autocomplete="username"><input name="password" type="password" autocomplete="${req.url === '/signup' ? 'new-password' : 'current-password'}"><button type="submit">Sign in</button></form>`)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
let session, page
const results = []
const unverified = []
let failed = null
const ui = async (...args) => (await promisify(execFile)('python', ['scripts/native-ui.py', '--pid', String(session.child.pid), ...args], { windowsHide: true, timeout: 30000, maxBuffer: 1024 * 1024, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } })).stdout
const launch = async (directory, flags = []) => {
  session = await launchBrowser(resolve(exe), join(output, directory), ['--fingerprint=20260928', '--lang=en-US', '--disable-spoofing=font', '--force-renderer-accessibility', ...flags, `http://127.0.0.1:${server.address().port}/`])
  page = await session.page()
  await until(() => page.evaluate(`Boolean(document.querySelector('[name=password]'))`), 'login form')
}
const close = async () => { page?.close(); page = null; await session?.close(); session = null }
const submit = async password => {
  await page.evaluate(`document.querySelector('[name=username]').select()`)
  await page.send('Input.insertText', { text: 'test-user' })
  await page.evaluate(`document.querySelector('[name=password]').select()`)
  await page.send('Input.insertText', { text: password })
  await page.evaluate(`document.querySelector('button').click()`)
  await until(() => page.evaluate(`document.body.innerText.includes('Signed in')`), 'signed in page')
  await wait(1000)
}
try {
  const firstPassword = 'Synthetic-Test-Password-42!', nextPassword = 'Synthetic-Updated-Password-43!'
  await launch('profile')
  await submit(firstPassword)
  const controls = JSON.parse(await ui())
  assert.ok(controls.some(x => x.name === 'Save password?'))
  await writeFile(join(output, 'save-controls.json'), JSON.stringify(controls, null, 2))
  await ui('--click', 'Save')
  results.push('native-save-prompt-and-save')
  await wait(1000); await close()
  await launch('profile')
  await page.evaluate(`document.querySelector('[name=username]').focus()`)
  await until(() => page.evaluate(`document.querySelector('[name=password]').value === ${JSON.stringify(firstPassword)}`), 'restart password autofill')
  assert.equal(await page.evaluate(`document.querySelector('[name=username]').value`), 'test-user')
  results.push('restart-autofill')
  await submit(nextPassword)
  await writeFile(join(output, 'update-controls.json'), await ui())
  await ui('--click', 'Update password')
  results.push('update-saved-password')
  await wait(1000); await close()
  await launch('profile')
  await page.evaluate(`document.querySelector('[name=username]').focus()`)
  await until(() => page.evaluate(`document.querySelector('[name=password]').value === ${JSON.stringify(nextPassword)}`), 'updated password autofill')
  results.push('updated-password-restart-autofill')
  await close()
  await launch('isolated')
  await wait(1500)
  assert.equal(await page.evaluate(`document.querySelector('[name=password]').value`), '')
  results.push('profile-password-isolation')
  try {
  await page.send('Page.navigate', { url: `http://127.0.0.1:${server.address().port}/signup` })
  await until(() => page.evaluate(`document.querySelector('[name=password]')?.autocomplete === 'new-password'`), 'signup form')
  await page.send('Page.bringToFront')
  await ui('--keys', '{ESC}')
  await wait(500)
  await page.evaluate(`document.querySelector('[name=password]').focus()`)
  await ui('--keys', '+{F10}')
  await wait(500)
  await ui('--screenshot', join(output, 'generation-menu.png')).catch(error => unverified.push({ check: 'native-menu-screenshot', reason: error.message }))
  const menu = JSON.parse(await ui())
  await writeFile(join(output, 'generation-menu.json'), JSON.stringify(menu, null, 2))
  const suggest = menu.find(x => /suggest.*password/i.test(x.name))
  assert.ok(suggest, 'Native password generation menu must be available without sign-in')
  await ui('--click', suggest.name, '--control', suggest.type)
  await wait(500)
  await writeFile(join(output, 'generation-controls.json'), await ui())
  await ui('--click', 'Use password')
  assert.ok((await page.evaluate(`document.querySelector('[name=password]').value.length`)) >= 12)
  results.push('native-password-generation-without-sign-in')
  } catch (error) {
    unverified.push({ check: 'native-password-generation-without-sign-in', reason: error.message })
    await ui('--keys', '{ESC}').catch(() => {})
  }
  await page.send('Page.navigate', { url: 'chrome://password-manager/settings' })
  const findToggle = `(()=>{const walk=r=>{for(const e of r.querySelectorAll('*')){if(e.id==='passwordToggle')return e;if(e.shadowRoot){const f=walk(e.shadowRoot);if(f)return f}}};return walk(document)})()`
  await until(() => page.evaluate(`Boolean(${findToggle})`), 'password settings')
  assert.equal(await page.evaluate(`(${findToggle}).checked`), true)
  await page.evaluate(`(${findToggle}).shadowRoot.querySelector('cr-toggle').click()`)
  await until(() => page.evaluate(`(${findToggle}).checked === false`), 'save passwords disabled')
  await close()
  await launch('isolated')
  await submit(firstPassword)
  assert.ok(!JSON.parse(await ui()).some(x => x.name === 'Save password?'))
  results.push('disabled-save-setting-survives-restart-and-suppresses-prompt')
  await close()
  await launch('incognito', ['--incognito'])
  await submit(firstPassword)
  assert.ok(!JSON.parse(await ui()).some(x => x.name === 'Save password?'))
  results.push('incognito-does-not-offer-save')
  await close()
  console.log(JSON.stringify({ passed: results, unverified }))
  if (unverified.length && !process.argv.includes('--record-only')) process.exitCode = 1
} catch (error) {
  failed = error.stack
  await writeFile(join(output, 'failure.json'), JSON.stringify({ error: error.stack, passed: results }, null, 2))
  throw error
} finally {
  await writeFile(join(output, 'results.json'), JSON.stringify({ status: failed ? 'failed' : unverified.length ? 'partial' : 'passed', passed: results, unverified, error: failed }, null, 2))
  await close(); server.closeAllConnections(); server.close()
}
