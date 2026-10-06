import unittest
from kernel.release_notes import render, version_key


class ReleaseNotesTests(unittest.TestCase):
    def test_same_source_never_claims_new_browser_fixes(self):
        report = dict(source=dict(GITHUB_SHA='a' * 40, GITHUB_RUN_ID='123'), archiveBytes=123, archiveSha256='b' * 64)
        previous = dict(version='148.0.1.1-fb.1-pre.1', sourceCommit='a' * 40, sha256='b' * 64)
        notes = render('148.0.1.1-fb.1-pre.2', report, previous)
        self.assertIn('本次没有新增内核功能或修复', notes)
        self.assertIn('完整迁移仍待专项验证', notes)
        self.assertNotIn('首个', notes)
        changed = render('148.0.1.1-fb.1-pre.2', report, {**previous, 'sha256': 'c' * 64})
        self.assertIn('运行包摘要发生变化', changed)
        self.assertNotIn('没有新增内核功能或修复', changed)

    def test_changed_source_has_concrete_commit_and_comparison(self):
        report = dict(source=dict(GITHUB_SHA='a' * 40, GITHUB_RUN_ID='123'), archiveBytes=123, archiveSha256='b' * 64)
        notes = render('149.0.1.1-fb.1-pre.1', report, dict(version='148.0.1.1-fb.1-pre.2', sourceCommit='c' * 40),
                       [dict(sha='a' * 40, commit=dict(message='Fix canvas metrics\n\nDetails'))])
        self.assertIn('Fix canvas metrics', notes)
        self.assertIn('/compare/', notes)
        self.assertLess(version_key('148.0.1.1-fb.1-pre.9'), version_key('148.0.1.1-fb.1-pre.10'))
        self.assertLess(version_key('148.0.1.1-fb.1-pre.10'), version_key('148.0.1.1-fb.1'))
