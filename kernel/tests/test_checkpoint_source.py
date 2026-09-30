from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checkpoint_source as source


class CheckpointSourceTests(unittest.TestCase):
    def setUp(self):
        self.current = {'GITHUB_REPOSITORY': 'owner/kernel', 'GITHUB_SHA': 'b' * 40,
                        'GITHUB_RUN_ID': '200', 'GITHUB_RUN_ATTEMPT': '1'}
        self.expected = {'runId': 100, 'attempt': 2, 'commit': 'a' * 40,
                         'artifactId': 300, 'manifestSha256': 'c' * 64}
        self.repo = {'id': 10, 'full_name': 'owner/kernel', 'visibility': 'public', 'default_branch': 'main'}
        self.run = {'id': 100, 'run_attempt': 2, 'head_sha': 'a' * 40, 'head_branch': 'main',
                    'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'success',
                    'path': '.github/workflows/build-kernel.yml', 'repository': {'id': 10},
                    'head_repository': {'id': 10}, 'run_started_at': '2026-09-29T10:00:00Z',
                    'updated_at': '2026-09-29T11:00:00Z'}
        self.artifact = {'id': 300, 'name': 'checkpoint-100-2-1', 'expired': False,
                         'size_in_bytes': 1000, 'digest': 'sha256:' + 'd' * 64,
                         'created_at': '2026-09-29T10:50:00Z', 'expires_at': '2026-10-01T10:50:00Z',
                         'workflow_run': {'id': 100, 'repository_id': 10, 'head_repository_id': 10,
                                          'head_sha': 'a' * 40, 'head_branch': 'main'}}
        self.comparison = {'status': 'ahead', 'merge_base_commit': {'sha': 'a' * 40}}
        self.checked = datetime(2026, 9, 30, tzinfo=timezone.utc)

    def verify(self, **overrides):
        values = dict(repository=self.repo, run=self.run, artifact=self.artifact,
                      comparison=self.comparison, expected=self.expected,
                      current=self.current, checked_at=self.checked)
        return source.verify_metadata(**(values | overrides))

    def test_verified_receipt_keeps_both_identities_and_pinned_digest(self):
        report = self.verify()
        self.assertEqual(report['targetContext'], self.current)
        self.assertEqual(report['sourceContext']['GITHUB_RUN_ID'], '100')
        self.assertEqual(report['sourceContext']['GITHUB_RUN_ATTEMPT'], '2')
        self.assertEqual(report['artifactId'], 300)
        self.assertEqual(source.receipt_context(report, self.current, 'c' * 64), report['sourceContext'])
        for changed in ({'GITHUB_RUN_ID': '201'}, {'GITHUB_RUN_ATTEMPT': '2'},
                        {'GITHUB_SHA': 'e' * 40}, {'GITHUB_REPOSITORY': 'other/kernel'}):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                source.receipt_context(report, self.current | changed, 'c' * 64)
        with self.assertRaises(RuntimeError):
            source.receipt_context(report, self.current, 'e' * 64)

    def test_wrong_source_attempt_fork_event_branch_and_failed_runs_rejected(self):
        for changed in ({'id': 101}, {'run_attempt': 1}, {'head_sha': 'e' * 40},
                        {'event': 'pull_request'}, {'head_branch': 'feature'}, {'status': 'in_progress'},
                        {'conclusion': 'failure'}, {'head_repository': {'id': 11}},
                        {'repository': {'id': 11}}, {'path': '.github/workflows/checkpoint-pilot.yml'}):
            with self.subTest(changed=changed), self.assertRaisesRegex(RuntimeError, 'trusted build'):
                self.verify(run=self.run | changed)

    def test_artifact_substitution_expiry_size_and_attempt_window_rejected(self):
        changes = [{'id': 301}, {'name': 'checkpoint-100-1-1'}, {'expired': True},
                   {'digest': ''}, {'size_in_bytes': 0}, {'size_in_bytes': 21 * 1024**3},
                   {'expires_at': '2026-09-30T00:00:00Z'},
                   {'created_at': '2026-09-29T09:59:59Z'}, {'created_at': '2026-09-29T11:00:01Z'}]
        for field, value in [('id', 101), ('repository_id', 11), ('head_repository_id', 11),
                             ('head_sha', 'e' * 40), ('head_branch', 'feature')]:
            changes.append({'workflow_run': self.artifact['workflow_run'] | {field: value}})
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                self.verify(artifact=self.artifact | changed)

    def test_private_repository_unrelated_commit_and_self_resume_rejected(self):
        for changed in ({'visibility': 'private'}, {'full_name': 'other/kernel'}, {'default_branch': 'dev'}):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                self.verify(repository=self.repo | changed)
        for changed in ({'status': 'behind'}, {'status': 'diverged'}, {'merge_base_commit': {'sha': 'e' * 40}}):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                self.verify(comparison=self.comparison | changed)
        with self.assertRaises(RuntimeError):
            self.verify(current=self.current | {'GITHUB_RUN_ID': '100'})

    def test_api_queries_are_scoped_to_pinned_source_and_current_commit(self):
        with patch.dict('os.environ', {'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch'}), \
                patch.object(source, 'github_api', side_effect=[self.repo, self.run, self.artifact, self.comparison]) as api, \
                patch.object(source, 'datetime') as clock:
            clock.now.return_value = self.checked
            clock.fromisoformat.side_effect = datetime.fromisoformat
            source.verify_source(self.expected, self.current)
        self.assertEqual([call.args[0] for call in api.call_args_list], [
            'repos/owner/kernel', 'repos/owner/kernel/actions/runs/100',
            'repos/owner/kernel/actions/artifacts/300', 'repos/owner/kernel/compare/' + 'a' * 40 + '...' + 'b' * 40])
        for environment in ({'GITHUB_REF': 'refs/pull/1/merge', 'GITHUB_EVENT_NAME': 'pull_request'},
                            {'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'push'}):
            with patch.dict('os.environ', environment), patch.object(source, 'github_api') as api, self.assertRaises(RuntimeError):
                source.verify_source(self.expected, self.current)
            api.assert_not_called()

    def test_ids_and_digests_reject_partial_or_shell_like_input(self):
        for value in ('', '-1', '0', '1/../2', '123; echo x', ' 123'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                source.positive_id(value)
        for value in ('abc123', 'A' * 40, 'g' * 40):
            with self.subTest(value=value), self.assertRaises(ValueError):
                source.checksum(value, 40)


if __name__ == '__main__':
    unittest.main()
