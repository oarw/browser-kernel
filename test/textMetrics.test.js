import { test } from 'node:test'
import assert from 'node:assert/strict'
import { compareMetrics } from '../scripts/compare-text-metrics.mjs'

function fixture() {
  const make = noise => ({ kernel: { executableSha256: 'a'.repeat(64) }, harness: { instrumentation: noise ? [] : ['--disable-spoofing=font,canvas'] }, summary: { failed: 0 }, scenarios: [0, 1, 2].map(i => ({
    id: String(i), fingerprint: { seed: i + 1 }, checks: [], runs: ['first', 'restart', 'same-seed-isolated'].map(kind => ({ kind, observed: Object.fromEntries(['top', 'repeated', 'sameOriginFrame', 'crossOriginFrame', 'dedicatedWorker'].map(context => {
      const samples = Array.from({ length: 24 }, (_, n) => ({ font: '12px Arial', text: n % 4 ? 'Sample' : '', values: { width: n % 4 ? 100 * (noise ? 1 + (i + 1) / 1000000 : 1) : 0 } }))
      return [context, { textMetrics: { offscreen: structuredClone(samples), ...(context === 'dedicatedWorker' ? {} : { dom: structuredClone(samples) }) } }]
    })) }))
  })) })
  const candidate = make(true), control = make(false), official = make(false)
  official.scenarios[0].checks.push({ id: 'first/text-metrics-sanity', status: 'failed' })
  return [official, candidate, control]
}
test('native metrics gate accepts matching controls and retained seed noise', () => {
  assert.equal(compareMetrics(...fixture()).status, 'passed')
})
test('native metrics gate rejects missing samples, mismatched binaries and zero-noise candidates', () => {
  for (const damage of [
    ([, candidate]) => { candidate.scenarios[0].runs[1].observed.dedicatedWorker.textMetrics.offscreen.pop() },
    ([,, control]) => { control.kernel.executableSha256 = 'b'.repeat(64) },
    ([, candidate, control]) => { candidate.scenarios = structuredClone(control.scenarios) },
    ([, candidate]) => { candidate.cleanupWarning = 'forced shutdown' }
  ]) {
    const reports = fixture(); damage(reports)
    assert.equal(compareMetrics(...reports).status, 'failed')
  }
})
