"""
Путь вложения расширен до 255 символов.

С прежними 100 каталог `attachments/<имя объекта>_<uuid>/` занимал почти весь
предел, и файл с длинным именем ронял сохранение SuspiciousFileOperation:
хранилище срезало имя по остатку и съедало его целиком. Расширение поля —
только половина правки, вторая живёт в `get_attachment_upload_path`, которая
теперь сама укладывает имя в предел.
"""

import data.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('data', '0002_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='attachment',
            name='path',
            field=models.FileField(max_length=255, upload_to=data.models.get_attachment_upload_path, verbose_name='Файл'),
        ),
    ]
