import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import services
from app.config import settings


class ProviderError(Exception):
    def __init__(self, status_code):
        super().__init__(f'provider status {status_code}')
        self.status_code = status_code


class GeminiServiceTests(unittest.TestCase):
    def test_retries_then_uses_fallback_model(self):
        client = Mock()
        client.models.generate_content.side_effect = [
            ProviderError(503),
            ProviderError(503),
            SimpleNamespace(text='  Warm up progressively.  '),
        ]
        with (
            patch('app.services.client', client),
            patch.object(settings, 'gemini_model', 'primary-model'),
            patch.object(settings, 'gemini_model_fallbacks', 'fallback-model'),
            patch('app.services.time.sleep') as sleep,
        ):
            answer = services.answer_sports_question('How should I warm up?')

        self.assertEqual(answer, 'Warm up progressively.')
        self.assertEqual(
            [call.kwargs['model'] for call in client.models.generate_content.call_args_list],
            ['primary-model', 'primary-model', 'fallback-model'],
        )
        sleep.assert_called_once_with(0.5)

    def test_bad_credentials_fail_without_retrying_models(self):
        client = Mock()
        client.models.generate_content.side_effect = ProviderError(403)
        with (
            patch('app.services.client', client),
            patch.object(settings, 'gemini_model', 'primary-model'),
            patch.object(settings, 'gemini_model_fallbacks', 'fallback-model'),
            patch('app.services.time.sleep') as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, 'API key or model permissions'):
                services.answer_sports_question('How should I warm up?')
        client.models.generate_content.assert_called_once()
        sleep.assert_not_called()

    def test_missing_api_key_fails_with_setup_guidance(self):
        with patch('app.services.client', None):
            with self.assertRaisesRegex(RuntimeError, 'not configured'):
                services.answer_sports_question('How should I warm up?')

    def test_reads_google_sdk_code_attribute(self):
        self.assertEqual(services._provider_status_code(SimpleNamespace(code=404)), 404)
        self.assertEqual(services._provider_status_code(RuntimeError('503 UNAVAILABLE')), 503)