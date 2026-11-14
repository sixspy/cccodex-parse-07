from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SlicePolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slice_total", models.IntegerField(default=0)),
                ("closure", models.CharField(choices=[("rollback_all", "整任务回滚"), ("rerun_failed", "仅重跑失败片")], default="rerun_failed", max_length=20)),
                ("locked_date", models.DateField()),
                ("job", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="policy", to="catalog.parsejob")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="ParseSlice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slice_no", models.IntegerField()),
                ("status", models.CharField(default="待领", max_length=8)),
                ("holder", models.CharField(blank=True, default="", max_length=64)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="slices", to="catalog.parsejob")),
            ],
            options={"ordering": ["slice_no"]},
        ),
        migrations.AddConstraint(
            model_name="parseslice",
            constraint=models.UniqueConstraint(fields=("job", "slice_no"), name="uniq_job_slice_no"),
        ),
    ]
