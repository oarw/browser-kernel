"""Release-body generation; project documentation stays in the manager repository."""
import argparse
import json
import re
import subprocess
from pathlib import Path

REPO = 'oarw/browser-kernel'
ROOT = f'https://github.com/{REPO}'
PATTERN = r'(\d+)\.(\d+)\.(\d+)\.(\d+)-fb\.(\d+)(?:-pre\.(\d+))?'


def version_key(version):
    match = re.fullmatch(PATTERN, version)
    if not match:
        raise ValueError('Invalid kernel release version')
    return tuple(int(value) if value is not None else 10**12 for value in match.groups())


def api(path):
    return json.loads(subprocess.check_output(['gh', 'api', f'repos/{REPO}/{path}'], encoding='utf8'))


def asset(tag, name):
    return json.loads(subprocess.check_output(['gh', 'release', 'download', tag, '--repo', REPO,
                                             '--pattern', name, '--output', '-'], encoding='utf8'))


def render(version, report, previous=None, commits=()):
    version_key(version)
    source = report['source']['GITHUB_SHA']
    lines = [f'# FingerBrowser 内核 {version}', '', '## 本次更新', '']
    if previous and previous['sourceCommit'] == source:
        lines += [f'- 与上一版 `{previous["version"]}` 使用相同的 Chromium 源码提交；本次没有新增内核功能或修复。',
                  '- 重新核验运行包并发布独立候选版本，供软件按版本下载安装和切换。']
    elif previous:
        lines += [f'- 相比 `{previous["version"]}` 更新了内核源码，具体提交如下：']
        lines += [f'  - {item["commit"]["message"].splitlines()[0]}（[{item["sha"][:8]}]({ROOT}/commit/{item["sha"]})）' for item in commits]
        lines += [f'- [完整源码对比]({ROOT}/compare/{previous["sourceCommit"]}...{source})。提交列表不等同于功能验收。']
    else:
        lines += ['- 首个可由 FingerBrowser 版本目录下载安装的自产内核候选，提供包摘要、可执行文件摘要及源码来源。']
    if report.get('executableSha256') == 'fd05ed90618bb93c2d848b75255942726890751866122a0aa074397faf12f4ac':
        lines += ['', '## 本版包含的功能', '',
                  '- 修复 Canvas TextMetrics 文本测量和页面、iframe、Worker 的结果一致性。',
                  '- 提供本机密码保存、更新和重启自动填写能力；无痕环境不提示保存。',
                  '- 支持管理器的指纹种子、环境隔离及独立浏览器数据目录。',
                  '- 此功能列表描述当前二进制；若上方说明源码未变，不代表这些功能是本次新增。']
    patches = report.get('inputs', {}).get('overlaySha256', {})
    if patches:
        lines += ['', '## 构建补丁', ''] + [f'- `{name}`' for name in sorted(patches)]
    lines += ['', '## 版本与校验', '',
              f'- Chromium 基础版本：`{version.split("-")[0]}`；Windows x64 实验组件构建。',
              f'- 运行包：`browser-kernel-windows-x64.zip`，{report["archiveBytes"]:,} 字节。',
              f'- 运行包 SHA-256：`{report["archiveSha256"]}`。',
              f'- [源码提交]({ROOT}/tree/{source}) · [完整编译记录]({ROOT}/actions/runs/{report["source"]["GITHUB_RUN_ID"]})。',
              '- Assets 同时提供 kernel.json、package-report.json 和 SHA256SUMS.txt；运行包内包含许可证及构建来源。',
              '', '## 已知限制与升级', '',
              '- 这是预发行候选，编译和发布成功不代表完整浏览器验收或发行审查通过。',
              '- 真实显卡 WebGL、强密码生成菜单和旧用户环境完整迁移仍待专项验证。',
              '- 管理器刷新版本列表后可并行安装，不会自动更换默认内核；环境可单独固定版本。',
              '- 切换前关闭所有环境并完整备份应用数据目录。配置备份不含 Cookie、保存密码或网页存储。',
              '- 内核指纹修复可能改变网站看到的结果，部分网站可能要求重新登录或验证。', '']
    return '\n'.join(lines)


def generate(version, report):
    releases = api('releases?per_page=100')
    candidates = [item for item in releases if not item['draft'] and re.fullmatch(PATTERN, item['tag_name'])
                  and version_key(item['tag_name']) < version_key(version)
                  and any(value['name'] == 'kernel.json' for value in item['assets'])]
    previous, commits = None, []
    if candidates:
        prior = max(candidates, key=lambda item: version_key(item['tag_name']))
        previous = asset(prior['tag_name'], 'kernel.json')
        if previous['sourceCommit'] != report['source']['GITHUB_SHA']:
            commits = api(f'compare/{previous["sourceCommit"]}...{report["source"]["GITHUB_SHA"]}')['commits']
    return render(version, report, previous, commits)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    version_key(args.tag)
    args.output.write_text(generate(args.tag, asset(args.tag, 'package-report.json')), encoding='utf8')
