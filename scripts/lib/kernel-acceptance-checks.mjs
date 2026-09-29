import { isDeepStrictEqual } from 'node:util'

const fields = ['userAgent', 'platform', 'language', 'languages', 'hardwareConcurrency', 'deviceMemory', 'timezone', 'timezoneOffsets', 'clientHints', 'canvas']
export function compareSnapshots(left, right) {
  return fields.filter((field) => !isDeepStrictEqual(left?.[field], right?.[field])).map((field) => ({ field, expected: left?.[field] ?? null, actual: right?.[field] ?? null }))
}

export function evaluateScenario(scenario) {
  const checks = []
  const add = (id, differences) => checks.push({ id, status: differences.length ? 'failed' : 'passed', differences })
  const expectedPlatform = { windows: 'Win32', macos: 'MacIntel', linux: 'Linux x86_64' }[scenario.fingerprint.platform]
  for (const run of scenario.runs) {
    const data = run.observed || {}
    const missing = []
    for (const context of ['top', 'repeated', 'sameOriginFrame', 'crossOriginFrame', 'dedicatedWorker']) {
      for (const field of fields) {
        const value = data?.[context]?.[field]
        const incompleteCanvas = field === 'canvas' && (!/^[a-f0-9]{64}$/.test(value?.hash || '') || !Number.isFinite(value?.textWidth))
        const incompleteHints = field === 'clientHints' && (!Array.isArray(value?.brands) || !value.brands.length || !Array.isArray(value?.fullVersionList) || !value.fullVersionList.length || typeof value?.platformVersion !== 'string')
        if (value == null || value?.error || value?.unavailable || incompleteCanvas || incompleteHints) missing.push({ field: `${context}.${field}`, expected: 'available', actual: value ?? null })
      }
    }
    add(`${run.kind}/probe-completeness`, missing)
    const contexts = ['top', 'repeated', 'sameOriginFrame', 'crossOriginFrame', 'dedicatedWorker']
    const webglMissing = contexts.filter((context) => !data[context]?.webgl || data[context].webgl.unavailable || data[context].webgl.error)
    if (webglMissing.length) checks.push({ id: `${run.kind}/webgl`, status: 'unverified', differences: webglMissing.map((context) => ({ field: `${context}.webgl`, expected: 'available for comparison', actual: data[context]?.webgl ?? null })) })
    else add(`${run.kind}/webgl`, contexts.filter((context) => !isDeepStrictEqual(data.top.webgl, data[context].webgl)).map((context) => ({ field: `${context}.webgl`, expected: data.top.webgl, actual: data[context].webgl })))
    const textWidths = contexts.flatMap((context) => [
      { field: `${context}.canvas.textWidth`, actual: data[context]?.canvas?.textWidth },
      ...(context === 'dedicatedWorker' ? [] : [{ field: `${context}.domTextWidth`, actual: data[context]?.domTextWidth }])
    ])
    add(`${run.kind}/text-metrics-sanity`, textWidths.filter(({ actual }) => !Number.isFinite(actual) || actual < 18).map(({ field, actual }) => ({ field, expected: 'at least 18px for the fixed ASCII fixture at 18px font size', actual: actual ?? null })))
    for (const context of ['repeated', 'sameOriginFrame', 'crossOriginFrame', 'dedicatedWorker']) add(`${run.kind}/${context}`, compareSnapshots(data.top, data[context]))
    const fp = scenario.fingerprint
    const configured = { platform: expectedPlatform, hardwareConcurrency: fp.hardwareConcurrency, language: fp.language, languages: fp.acceptLanguages.split(','), timezone: fp.timezone }
    add(`${run.kind}/configured-fields`, Object.entries(configured).filter(([field, value]) => !isDeepStrictEqual(data.top?.[field], value)).map(([field, value]) => ({ field, expected: value, actual: data.top?.[field] ?? null })))
    add(`${run.kind}/platform-version`, data.top?.clientHints?.platformVersion === fp.platformVersion ? [] : [{ field: 'clientHints.platformVersion', expected: fp.platformVersion, actual: data.top?.clientHints?.platformVersion ?? null }])
    const expectedStorage = run.kind === 'restart' ? scenario.id : null
    add(`${run.kind}/storage`, ['localStorage', 'cookie', 'indexedDB'].filter((field) => data.storageBefore?.[field] !== expectedStorage).map((field) => ({ field, expected: expectedStorage, actual: data.storageBefore?.[field] ?? null })))
    add(`${run.kind}/http-user-agent`, data.headers?.['user-agent'] === data.top?.userAgent ? [] : [{ field: 'user-agent', expected: data.top?.userAgent, actual: data.headers?.['user-agent'] }])
    const headerIdentity = { 'sec-ch-ua-platform': 'platform', 'sec-ch-ua-platform-version': 'platformVersion', 'sec-ch-ua-arch': 'architecture', 'sec-ch-ua-bitness': 'bitness' }
    const headerDifferences = []
    for (const [header, field] of Object.entries(headerIdentity)) {
      let actual = null
      try { actual = JSON.parse(data.headers?.[header]) } catch { /* Missing or malformed headers fail comparison. */ }
      const expected = data.top?.clientHints?.[field]
      if (expected === undefined || actual !== expected) headerDifferences.push({ field: header, expected: expected ?? null, actual })
    }
    const expectedMobile = data.top?.clientHints?.mobile ? '?1' : '?0'
    if (data.headers?.['sec-ch-ua-mobile'] !== expectedMobile) headerDifferences.push({ field: 'sec-ch-ua-mobile', expected: expectedMobile, actual: data.headers?.['sec-ch-ua-mobile'] ?? null })
    for (const [header, field] of [['sec-ch-ua', 'brands'], ['sec-ch-ua-full-version-list', 'fullVersionList']]) {
      const value = data.headers?.[header] || ''
      const actual = [...value.matchAll(/"([^"\\]*)";v="([^"\\]*)"/g)].map((match) => ({ brand: match[1], version: match[2] }))
      if (!actual.length || !isDeepStrictEqual(actual, data.top?.clientHints?.[field])) headerDifferences.push({ field: header, expected: data.top?.clientHints?.[field] ?? null, actual })
    }
    const accepted = (data.headers?.['accept-language'] || '').split(',').map((item) => item.trim().split(';')[0])
    if (!isDeepStrictEqual(accepted, data.top?.languages)) headerDifferences.push({ field: 'accept-language', expected: data.top?.languages ?? null, actual: accepted })
    add(`${run.kind}/http-client-hints`, headerDifferences)
  }
  const first = scenario.runs.find((run) => run.kind === 'first')
  for (const kind of ['restart', 'same-seed-isolated']) {
    const next = scenario.runs.find((run) => run.kind === kind)
    if (!first || !next) add(kind, [{ field: 'run', expected: 'completed', actual: 'missing' }])
    else for (const context of ['top', 'sameOriginFrame', 'crossOriginFrame', 'dedicatedWorker']) add(`${kind}/${context}/stability`, compareSnapshots(first.observed[context], next.observed[context]))
  }
  return checks
}

export function summarizeAcceptance(scenarios) {
  const checks = scenarios.flatMap((scenario) => scenario.checks)
  const passed = checks.filter((check) => check.status === 'passed').length
  const failed = checks.filter((check) => check.status === 'failed').length
  const unverified = checks.filter((check) => check.status === 'unverified').length
  return { status: !checks.length || failed || passed + unverified !== checks.length ? 'failed' : unverified ? 'partial' : 'passed', passed, failed, unverified }
}
