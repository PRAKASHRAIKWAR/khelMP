import os
import unittest
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError

from portal import ai


class ProviderError(Exception):
    def __init__(self, status_code):
        super().__init__(f'provider status {status_code}')
        self.status_code = status_code


class DjangoGeminiTests(unittest.TestCase):
    def test_cricket_score_parser_requires_explicit_runs_and_balls(self):
        self.assertEqual(
            ai.extract_cricket_score_claim('I scored 40 runs off 10 balls', 35),
            {'runs': 40, 'balls': 10, 'is_high_score_candidate': True},
        )
        self.assertEqual(
            ai.extract_cricket_score_claim('10 balls 30 runs', 40),
            {'runs': 30, 'balls': 10, 'is_high_score_candidate': False},
        )
        self.assertIsNone(ai.extract_cricket_score_claim('Great match today'))
        self.assertIsNone(ai.extract_cricket_score_claim('700 runs off 10 balls'))

    def test_media_screening_uploads_and_deletes_temporary_provider_file(self):
        client = Mock()
        client.files.upload.return_value = SimpleNamespace(name='files/temp-media', state=SimpleNamespace(name='ACTIVE'))
        client.models.generate_content.return_value = SimpleNamespace(
            text='{"status":"likely_synthetic","confidence":87,"note":"Repeated texture artifacts are visible."}'
        )
        with (
            patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key'}),
            patch('portal.ai.Client', return_value=client),
            patch('portal.ai._gemini_model_candidates', return_value=['screen-model']),
        ):
            result = ai.analyze_uploaded_media('sample-image.png', 'image/png')

        self.assertEqual(result['status'], 'likely_synthetic')
        self.assertEqual(result['confidence'], 87)
        client.files.upload.assert_called_once()
        client.files.delete.assert_called_once_with(name='files/temp-media')

    def test_retries_transient_error_then_uses_fallback_model(self):
        client = Mock()
        client.models.generate_content.side_effect = [
            ProviderError(503),
            ProviderError(503),
            SimpleNamespace(text='  Ready for training.  '),
        ]
        with (
            patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key'}),
            patch('portal.ai._gemini_model_candidates', return_value=['primary-model', 'fallback-model']),
            patch('portal.ai.Client', return_value=client),
            patch('portal.ai.time.sleep') as sleep,
        ):
            result = ai._generate_gemini_text_with_model('test prompt')

        self.assertEqual(result, ('Ready for training.', 'fallback-model'))
        self.assertEqual(
            [call.kwargs['model'] for call in client.models.generate_content.call_args_list],
            ['primary-model', 'primary-model', 'fallback-model'],
        )
        sleep.assert_called_once_with(0.5)

    def test_bad_credentials_fail_without_retrying_models(self):
        client = Mock()
        client.models.generate_content.side_effect = ProviderError(403)
        with (
            patch.dict(os.environ, {'GEMINI_API_KEY': 'test-key'}),
            patch('portal.ai._gemini_model_candidates', return_value=['primary-model', 'fallback-model']),
            patch('portal.ai.Client', return_value=client),
            patch('portal.ai.time.sleep') as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, 'API key or model permissions'):
                ai._generate_gemini_text_with_model('test prompt')
        client.models.generate_content.assert_called_once()
        sleep.assert_not_called()

    def test_reads_google_sdk_code_attribute(self):
        self.assertEqual(ai._provider_status_code(SimpleNamespace(code=404)), 404)
        self.assertEqual(ai._provider_status_code(RuntimeError('503 UNAVAILABLE')), 503)

    def test_connection_command_reports_the_working_model(self):
        output = StringIO()
        with patch('portal.management.commands.check_gemini._generate_gemini_text_with_model', return_value=('OK', 'flash-model')):
            call_command('check_gemini', stdout=output)
        self.assertIn('flash-model', output.getvalue())

    def test_connection_command_reports_provider_failure(self):
        with patch('portal.management.commands.check_gemini._generate_gemini_text_with_model', side_effect=RuntimeError('provider unavailable')):
            with self.assertRaisesRegex(CommandError, 'provider unavailable'):
                call_command('check_gemini')

