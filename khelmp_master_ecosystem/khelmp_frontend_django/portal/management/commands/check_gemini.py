from django.core.management.base import BaseCommand, CommandError

from portal.ai import _generate_gemini_text_with_model


class Command(BaseCommand):
    help = 'Send a short live request to Gemini and report which configured model responds.'

    def handle(self, *args, **options):
        try:
            response, model_name = _generate_gemini_text_with_model(
                'Reply with exactly: KhelMP Gemini connection OK.'
            )
        except Exception as error:
            raise CommandError(f'Gemini check failed: {error}') from error
        self.stdout.write(self.style.SUCCESS(f'Gemini is responding with {model_name}: {response}'))