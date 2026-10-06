import { createServer } from 'node:http'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve, join } from 'node:path'
import { launchBrowser, wait } from './lib/browser-session.mjs'

const [executable, outputArg, ...diagnosticFlags] = process.argv.slice(2)
const output = resolve(outputArg)
await mkdir(output, { recursive: true })
const server = createServer((req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8')
  res.end('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>自产内核功能验收</title><style>body{font:24px Arial,sans-serif;padding:32px;background:#fff;color:#123}p{margin:24px 0}</style><h1>浏览器中文显示测试</h1><p>中文字体：简体中文，标点、数字 0123456789。</p><p>English text: FingerBrowser native kernel.</p><p>常用符号：￥ € $ ✓ →</p></html>')
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
let session, page
try {
  const runs = []
  for (const [index, seed] of [20260928, 20260928, 20260929, 17, 42].entries()) {
    session = await launchBrowser(resolve(executable), join(output, `profile-${index}`), [`--fingerprint=${seed}`, '--lang=zh-CN', '--disable-spoofing=font', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp', ...diagnosticFlags, `http://127.0.0.1:${server.address().port}`])
    page = await session.page(); await wait(1000)
    const sample = await page.evaluate(`(async()=>{
      const audio = new OfflineAudioContext(1,44100,44100), oscillator=audio.createOscillator(), compressor=audio.createDynamicsCompressor();
      oscillator.type='triangle'; oscillator.frequency.value=10000; oscillator.connect(compressor); compressor.connect(audio.destination); oscillator.start(0);
      const result=await audio.startRendering(), values=result.getChannelData(0); let sum=0; for(let i=4500;i<5000;i++)sum+=Math.abs(values[i]);
      const pc=new RTCPeerConnection({iceServers:[]}), candidates=[];pc.createDataChannel('local-fixture');pc.onicecandidate=e=>{if(e.candidate)candidates.push(e.candidate.candidate)};await pc.setLocalDescription(await pc.createOffer());await new Promise(r=>setTimeout(r,1500));pc.close();
      const canvas=document.createElement('canvas'); const gl=canvas.getContext('webgl');
      return { audioSum:sum,audioFrames:values.length,contextSampleRate:audio.sampleRate,renderedSampleRate:result.sampleRate,webgl:!!gl,localIceCandidates:candidates,language:navigator.language,fonts:{arial:document.fonts.check('16px Arial'),cjk:document.fonts.check('16px "Microsoft YaHei"')}};
    })()`)
    runs.push({ seed, sample })
    if (index === 0) {
      await writeFile(join(output, 'chinese-fonts.png'), Buffer.from((await page.send('Page.captureScreenshot')).data, 'base64'))
      await writeFile(join(output, 'gpu.json'), JSON.stringify(await session.browser.send('SystemInfo.getInfo'), null, 2))
      await page.send('Page.navigate', { url: 'chrome://credits' }); await wait(1000)
      await writeFile(join(output, 'third-party-credits.html'), await page.evaluate('document.documentElement.outerHTML'))
    }
    page.close(); page=null; await session.close(); session=null
  }
  const report = { status: 'diagnostic', diagnosticFlags, runs, audioStable: runs[0].sample.audioSum === runs[1].sample.audioSum && runs[0].sample.renderedSampleRate === runs[1].sample.renderedSampleRate, audioSeedDifference: new Set(runs.map(r => r.sample.renderedSampleRate)).size > 1,
    webrtcNoHostCandidatesWithoutStun: runs.every(r => r.sample.localIceCandidates.length === 0),
    limitations: ['WebRTC probe uses no public STUN/TURN; it does not certify all networks.', 'Font availability API is not glyph coverage certification; inspect the screenshot.'] }
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
} finally { page?.close(); await session?.close(); server.closeAllConnections(); server.close() }
