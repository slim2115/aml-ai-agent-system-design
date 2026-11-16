"""CLI controls use temporary projects and never replace repository evidence."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_evaluation as cli
from tests.evaluator.fixtures import bundle, external_controls


class EvaluationCLI(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[2] / 'var' / 'tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='evaluator-cli-', dir=temporary_root)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.report = self.root / 'var' / 'evaluation' / 'report.md'
        project = patch.object(cli, 'PROJECT_ROOT', self.root)
        project.start()
        self.addCleanup(project.stop)

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(list(arguments))
        return status, stdout.getvalue() + stderr.getvalue()

    def write_bundle(self, data):
        path = self.root / 'input.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return str(path)

    def assert_no_secret(self, output, secret):
        self.assertNotIn(secret, output)
        for path in (self.report, self.report.with_suffix('.json')):
            if path.exists():
                self.assertNotIn(secret, path.read_text(encoding='utf-8'))

    def test_no_input_exits_incomplete_and_writes_report_pair(self):
        status, output = self.invoke('--no-llm')
        self.assertEqual(2, status)
        self.assertIn('Evaluation: INCOMPLETE', output)
        self.assertTrue(self.report.exists())
        self.assertEqual('INCOMPLETE', json.loads(self.report.with_suffix('.json').read_text())['verdict'])

    def test_pass_exit_code(self):
        with patch.object(cli, 'evaluate_bundle', return_value={'schema_version': '1.0', 'verdict': 'PASS'}):
            status, output = self.invoke()
        self.assertEqual(0, status)
        self.assertIn('Evaluation: PASS', output)

    def test_cli_applies_stronger_configured_threshold(self):
        data = bundle(30)
        data['external_measurements'] = external_controls(.95)
        cli.get_settings.cache_clear()
        try:
            with patch.dict(os.environ, {'FAITHFULNESS_THRESHOLD': '1.0'}):
                status, output = self.invoke('--input', self.write_bundle(data))
            self.assertEqual(1, status)
            report = json.loads(self.report.with_suffix('.json').read_text(encoding='utf-8'))
            gate = next(item for item in report['gates'] if item['name'] == 'M-02 RAG Faithfulness')
            self.assertEqual(1.0, gate['threshold'])
            self.assertEqual('FAIL', gate['status'])
        finally:
            cli.get_settings.cache_clear()

    def test_invalid_result_fails_without_copying_payload(self):
        secret = 'PRIVATE_RESULT_PAYLOAD'
        data = bundle(1)
        data['outcomes'][0]['result'] = {'secret': secret}
        status, output = self.invoke('--input', self.write_bundle(data))
        self.assertEqual(1, status)
        self.assert_no_secret(output, secret)

    def test_bad_input_exits_error_without_copying_payload(self):
        secret = 'PRIVATE_INVALID_BUNDLE'
        status, output = self.invoke('--input', self.write_bundle({'secret': secret}))
        self.assertEqual(3, status)
        self.assert_no_secret(output, secret)

    def test_json_and_read_failures_exit_error(self):
        source = self.root / 'input.json'
        for payload in ('{PRIVATE_JSON', '{"schema_version": "1.0", "schema_version": "PRIVATE_JSON"}', 'NaN'):
            with self.subTest(payload=payload):
                source.write_text(payload, encoding='utf-8')
                status, output = self.invoke('--input', str(source))
                self.assertEqual(3, status)
                self.assert_no_secret(output, 'PRIVATE_JSON')
        status, output = self.invoke('--input', str(self.root / 'PRIVATE_MISSING_FILE'))
        self.assertEqual(3, status)
        self.assert_no_secret(output, 'PRIVATE_MISSING_FILE')

    def test_evaluator_exception_is_sanitized(self):
        secret = 'PRIVATE_EXCEPTION_DETAIL'
        with patch.object(cli, 'evaluate_bundle', side_effect=RuntimeError(secret)):
            status, output = self.invoke()
        self.assertEqual(3, status)
        self.assert_no_secret(output, secret)

    def test_argument_errors_are_sanitized_and_help_succeeds(self):
        for arguments in (('--PRIVATE_ARGUMENT',), ('--output',)):
            with self.subTest(arguments=arguments):
                status, output = self.invoke(*arguments)
                self.assertEqual(3, status)
                self.assertNotIn('PRIVATE_ARGUMENT', output)
                self.assertFalse(self.report.exists())
        status, output = self.invoke('--help')
        self.assertEqual(0, status)
        self.assertIn('usage:', output)

    def test_unsafe_output_does_not_replace_default_or_historical_files(self):
        self.report.parent.mkdir(parents=True)
        self.report.write_text('existing markdown', encoding='utf-8')
        companion = self.report.with_suffix('.json')
        companion.write_text('existing JSON', encoding='utf-8')
        historic = self.root / 'docs' / 'eval-report.md'
        historic.parent.mkdir()
        historic.write_text('historical evidence', encoding='utf-8')
        for output_path in ('docs/eval-report.md', '../PRIVATE_OUTPUT.md', 'var/evaluation/report.txt',
                            'var/evaluation/.git/report.md', 'var/evaluation/PRIVATE_OUTPUT\x00.md'):
            with self.subTest(output_path=output_path):
                status, output = self.invoke('--output', output_path)
                self.assertEqual(3, status)
                self.assertNotIn('PRIVATE_OUTPUT', output)
                self.assertEqual('existing markdown', self.report.read_text())
                self.assertEqual('existing JSON', companion.read_text())
                self.assertEqual('historical evidence', historic.read_text())

    def test_report_cannot_overwrite_input(self):
        self.report.parent.mkdir(parents=True)
        for source in (self.report, self.report.with_suffix('.json')):
            with self.subTest(source=source):
                original = json.dumps(bundle(0))
                source.write_text(original, encoding='utf-8')
                status, output = self.invoke('--input', str(source))
                self.assertEqual(3, status)
                self.assertIn('OUTPUT_OVERLAPS_INPUT', output)
                self.assertEqual(original, source.read_text())

    def test_companion_symlink_outside_report_tree_is_rejected(self):
        self.report.parent.mkdir(parents=True)
        protected = self.root / '.git' / 'protected.json'
        protected.parent.mkdir()
        protected.write_text('protected content', encoding='utf-8')
        companion = self.report.with_suffix('.json')
        try:
            companion.symlink_to(protected)
        except (OSError, NotImplementedError):
            real_resolve = Path.resolve

            def redirected_resolve(path, *args, **kwargs):
                return protected if path == companion else real_resolve(path, *args, **kwargs)

            with patch.object(Path, 'resolve', autospec=True, side_effect=redirected_resolve):
                status, _ = self.invoke()
        else:
            status, _ = self.invoke()
        self.assertEqual(3, status)
        self.assertFalse(self.report.exists())
        self.assertEqual('protected content', protected.read_text())

    def test_input_hardlink_alias_is_rejected(self):
        self.report.parent.mkdir(parents=True)
        source = Path(self.write_bundle(bundle(0)))
        original = source.read_text()
        companion = self.report.with_suffix('.json')
        try:
            companion.hardlink_to(source)
        except (OSError, NotImplementedError):
            companion.write_text(original, encoding='utf-8')
            real_samefile = Path.samefile

            def aliased_samefile(path, other):
                if {path, Path(other)} == {companion, source}:
                    return True
                return real_samefile(path, other)

            with patch.object(Path, 'samefile', autospec=True, side_effect=aliased_samefile):
                status, output = self.invoke('--input', str(source))
        else:
            status, output = self.invoke('--input', str(source))
        self.assertEqual(3, status)
        self.assertIn('OUTPUT_OVERLAPS_INPUT', output)
        self.assertEqual(original, source.read_text())
        self.assertFalse(self.report.exists())

    def test_path_exception_is_sanitized(self):
        secret = 'PRIVATE_PATH_EXCEPTION'
        with patch.object(Path, 'resolve', side_effect=RuntimeError(secret)):
            status, output = self.invoke()
        self.assertEqual(3, status)
        self.assertIn('INVALID_OUTPUT_PATH', output)
        self.assertNotIn(secret, output)
        self.assertFalse(self.report.exists())

    def test_output_hardlink_cannot_replace_historical_evidence(self):
        self.report.parent.mkdir(parents=True)
        protected = self.root / 'historical.json'
        protected.write_text('historical evidence', encoding='utf-8')
        companion = self.report.with_suffix('.json')
        try:
            companion.hardlink_to(protected)
        except (OSError, NotImplementedError):
            companion.write_text('historical evidence', encoding='utf-8')
            real_stat = Path.stat

            def linked_stat(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                if path == companion:
                    fields = list(result)
                    fields[3] = 2  # st_nlink, shared by Python's os.stat_result.
                    return os.stat_result(fields)
                return result

            with patch.object(Path, 'stat', autospec=True, side_effect=linked_stat):
                status, output = self.invoke()
        else:
            status, output = self.invoke()
        self.assertEqual(3, status)
        self.assertIn('UNSAFE_OUTPUT_PATH', output)
        self.assertEqual('historical evidence', protected.read_text())
        self.assertFalse(self.report.exists())

    def test_write_failure_is_sanitized(self):
        secret = 'PRIVATE_WRITE_EXCEPTION'
        with patch.object(Path, 'write_text', side_effect=OSError(secret)):
            status, output = self.invoke()
        self.assertEqual(3, status)
        self.assertIn('REPORT_WRITE_FAILED', output)
        self.assertNotIn(secret, output)


if __name__ == '__main__':
    unittest.main()
