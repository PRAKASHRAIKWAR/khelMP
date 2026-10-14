from django.db import migrations, models


def create_bootstrap_state(apps, schema_editor):
    AdminBootstrapState = apps.get_model('portal', 'AdminBootstrapState')
    AdminBootstrapState.objects.using(schema_editor.connection.alias).get_or_create(pk=1)


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0010_emailotp'),
    ]

    operations = [
        migrations.CreateModel(
            name='AdminBootstrapState',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.RunPython(create_bootstrap_state, migrations.RunPython.noop),
    ]
