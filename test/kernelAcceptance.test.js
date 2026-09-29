import assert from 'node:assert/strict'
import { test } from 'node:test'
import { evaluateScenario, summarizeAcceptance } from '../scripts/lib/kernel-acceptance-checks.mjs'

function scenario() {
  const snapshot = { userAgent: 'Chrome/148.0.0.0', platform: 'Win32', language: 'en-US', languages: ['en-US', 'en'], hardwareConcurrency: 8, deviceMemory: 8, timezone: 'America/New_York', timezoneOffsets: [300, 240], clientHints: { architecture: 'x86', bitness: '64', platform: 'Windows', platformVersion: '19.0.0', mobile: false, brands: [{ brand: 'Chromium', version: '148' }], fullVersionList: [{ brand: 'Chromium', version: '148.0.1.1' }] }, canvas: { hash: 'a'.repeat(64), textWidth: 182 }, webgl: { vendor: 'test', renderer: 'test' } }
  snapshot.domTextWidth = 182
  return { id: 'fixture', fingerprint: { platform: 'windows', platformVersion: '19.0.0', language: 'en-US', acceptLanguages: 'en-US,en', hardwareConcurrency: 8, timezone: 'America/New_York' }, runs: ['first', 'restart', 'same-seed-isolated'].map((kind) => ({ kind, observed: { ...Object.fromEntries(['top', 'repeated', 'sameOriginFrame', 'crossOriginFrame', 'dedicatedWorker'].map((context) => [context, structuredClone(snapshot)])), storageBefore: Object.fromEntries(['cookie', 'localStorage', 'indexedDB'].map((field) => [field, kind === 'restart' ? 'fixture' : null])), headers: { 'user-agent': snapshot.userAgent, 'accept-language': 'en-US,en;q=0.9', 'sec-ch-ua-platform': '"Windows"', 'sec-ch-ua-platform-version': '"19.0.0"', 'sec-ch-ua-arch': '"x86"', 'sec-ch-ua-bitness': '"64"', 'sec-ch-ua-mobile': '?0', 'sec-ch-ua': '"Chromium";v="148"', 'sec-ch-ua-full-version-list': '"Chromium";v="148.0.1.1"' } } })) }
}
const failures = (input) => evaluateScenario(input).filter((check) => check.status === 'failed')

test('acceptance checks cover native contexts, headers, persistent and isolated storage', () => {
  const input = scenario()
  assert.deepEqual(failures(input), [])
  const checks = evaluateScenario(input)
  assert.equal(summarizeAcceptance([{ checks }]).status, 'passed')
  input.runs[2].observed.storageBefore.cookie = 'fixture'
  input.runs[1].observed.storageBefore.indexedDB = null
  input.runs[0].observed.headers['sec-ch-ua-platform-version'] = '"10.0.0"'
  const failed = failures(input)
  assert.ok(failed.some((check) => check.id === 'same-seed-isolated/storage'))
  assert.ok(failed.some((check) => check.id === 'restart/storage'))
  assert.ok(failed.some((check) => check.id === 'first/http-client-hints'))
})

test('measureText noise and worker differences are failures even when pixel hashes match', () => {
  const input = scenario()
  input.runs[0].observed.top.canvas.textWidth = -0.0005
  const failed = failures(input)
  assert.ok(failed.some((check) => check.id === 'first/text-metrics-sanity'))
  const worker = failed.find((check) => check.id === 'first/dedicatedWorker')
  assert.equal(worker.differences[0].field, 'canvas')
  input.runs[1].observed.crossOriginFrame.language = 'fr-FR'
  assert.ok(failures(input).some((check) => check.id === 'restart/crossOriginFrame/stability'))
})

test('unavailable WebGL is unverified instead of a successful equality of missing values', () => {
  const input = scenario()
  for (const run of input.runs) for (const context of ['top', 'repeated', 'sameOriginFrame', 'crossOriginFrame', 'dedicatedWorker']) run.observed[context].webgl = { unavailable: true }
  const checks = evaluateScenario(input)
  assert.deepEqual(checks.filter((check) => check.status === 'failed'), [])
  assert.equal(checks.filter((check) => check.status === 'unverified').length, 3)
  assert.equal(summarizeAcceptance([{ checks }]).status, 'partial')
})

test('missing runs, context data and incomplete captures cannot pass acceptance', () => {
  const input = scenario()
  input.runs.pop()
  delete input.runs[0].observed.dedicatedWorker
  delete input.runs[0].observed.top.canvas.hash
  const failed = failures(input)
  assert.ok(failed.some((check) => check.id === 'same-seed-isolated'))
  assert.ok(failed.find((check) => check.id === 'first/probe-completeness').differences.some((item) => item.field === 'top.canvas'))
  assert.equal(summarizeAcceptance([]).status, 'failed')
})
