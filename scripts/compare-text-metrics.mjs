import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import assert from 'node:assert/strict'
import { isDeepStrictEqual } from 'node:util'
import { pathToFileURL } from 'node:url'

export function compareMetrics(official, candidate, control) {
  const checks = []
  const check = (id, passed, detail) => checks.push({ id, status: passed ? 'passed' : 'failed', ...(passed ? {} : { detail }) })
  const contexts = ['top', 'repeated', 'sameOriginFrame', 'crossOriginFrame', 'dedicatedWorker']
  check('same-candidate-binary', /^[a-f0-9]{64}$/.test(candidate.kernel?.executableSha256) && candidate.kernel.executableSha256 === control.kernel?.executableSha256)
  check('control-disables-canvas-only', control.harness?.instrumentation?.includes('--disable-spoofing=font,canvas') && !candidate.harness?.instrumentation?.includes('--disable-spoofing=font,canvas'))
  for (const [label, report] of Object.entries({ official, candidate, control })) {
    check(`${label}/collection`, !report.error && !report.cleanupError && !report.cleanupWarning && report.scenarios?.length === 3)
  }
  check('candidate-core-regressions', candidate.summary?.failed === 0, candidate.summary)
  check('control-core-regressions', control.summary?.failed === 0, control.summary)
  check('official-reproduces-defect', official.scenarios?.some(s => s.checks?.some(c => c.id.endsWith('/text-metrics-sanity') && c.status === 'failed')))
  const seedWidths = []
  for (const scenario of candidate.scenarios || []) {
    const reference = control.scenarios?.find(s => s.id === scenario.id)
    check(`${scenario.id}/control-profile`, isDeepStrictEqual(scenario.fingerprint, reference?.fingerprint))
    check(`${scenario.id}/complete-runs`, isDeepStrictEqual(scenario.runs.map(r => r.kind), ['first', 'restart', 'same-seed-isolated']))
    const first = scenario.runs[0]?.observed?.top?.textMetrics?.offscreen
    for (const run of scenario.runs) {
      const baseline = reference?.runs.find(r => r.kind === run.kind)
      for (const context of contexts) {
        for (const mode of context === 'dedicatedWorker' ? ['offscreen'] : ['offscreen', 'dom']) {
          const id = `${scenario.id}/${run.kind}/${context}/${mode}`
          const actual = run.observed?.[context]?.textMetrics?.[mode]
          const plain = baseline?.observed?.[context]?.textMetrics?.[mode]
          check(`${id}/samples`, actual?.length === 24 && plain?.length === 24)
          if (actual?.length !== 24 || plain?.length !== 24) continue
          check(`${id}/stability`, isDeepStrictEqual(actual, first))
          let noisyWidths = 0
          const errors = []
          for (let i = 0; i < actual.length; i++) {
            const a = actual[i], b = plain[i]
            if (a.font !== b.font || a.text !== b.text || !isDeepStrictEqual(Object.keys(a.values), Object.keys(b.values))) errors.push({ i, reason: 'sample identity' })
            for (const [field, value] of Object.entries(a.values)) {
              const normal = b.values[field]
              const valid = Number.isFinite(value) && Number.isFinite(normal) && (a.text === '' ? value === normal : Math.abs(value - normal) <= Math.max(1e-10, Math.abs(normal) * 0.000006))
              if (!valid) errors.push({ i, field, value, normal })
            }
            if (a.text && !(a.values.width > 0 && b.values.width > 0)) errors.push({ i, reason: 'non-positive text width' })
            if (a.text && a.values.width !== b.values.width) noisyWidths++
          }
          check(`${id}/native-metrics`, errors.length === 0, errors)
          check(`${id}/seed-noise-retained`, noisyWidths > 0)
        }
      }
    }
    seedWidths.push(first?.filter(x => x.text).map(x => x.values.width))
  }
  check('different-seeds-differ', seedWidths.length === 3 && seedWidths.every(Boolean) && new Set(seedWidths.map(x => JSON.stringify(x))).size === 3)
  const failed = checks.filter(c => c.status === 'failed').length
  return { status: failed ? 'failed' : 'passed', passed: checks.length - failed, failed,
    scope: 'TextMetrics only; WebGL and other capability gaps remain in the original reports.', checks }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const [officialPath, candidatePath, controlPath, output] = process.argv.slice(2)
  assert.ok(output, 'Usage: node scripts/compare-text-metrics.mjs <official report> <candidate report> <control report> <output>')
  const reports = await Promise.all([officialPath, candidatePath, controlPath].map(async p => JSON.parse(await readFile(p, 'utf8'))))
  const result = compareMetrics(...reports)
  await mkdir(dirname(resolve(output)), { recursive: true })
  await writeFile(output, JSON.stringify(result, null, 2))
  console.log(JSON.stringify({ status: result.status, passed: result.passed, failed: result.failed, output }))
  process.exitCode = result.failed ? 1 : 0
}
