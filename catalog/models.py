from django.db import models


class ParseJob(models.Model):
    title = models.CharField(max_length=120)
    code = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=32, default="待解析")
    source_uri = models.CharField(max_length=255)
    parsed = models.IntegerField(default=0)
    landed = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class SlicePolicy(models.Model):
    """片规策略：总片数 + 片失败收口。收口写入后本日不得更换。"""

    CLOSURE_ROLLBACK_ALL = "rollback_all"
    CLOSURE_RERUN_FAILED = "rerun_failed"
    CLOSURES = [
        (CLOSURE_ROLLBACK_ALL, "整任务回滚"),
        (CLOSURE_RERUN_FAILED, "仅重跑失败片"),
    ]

    job = models.OneToOneField(ParseJob, related_name="policy", on_delete=models.CASCADE)
    slice_total = models.IntegerField(default=0)
    closure = models.CharField(max_length=20, choices=CLOSURES, default=CLOSURE_RERUN_FAILED)
    locked_date = models.DateField()

    class Meta:
        ordering = ["id"]

    @property
    def closure_name(self) -> str:
        return dict(self.CLOSURES).get(self.closure, self.closure)


class ParseSlice(models.Model):
    """切片：待领 -> 已领片 -> 已成功 / 已失败。"""

    ST_PENDING = "待领"
    ST_CLAIMED = "已领片"
    ST_OK = "已成功"
    ST_FAILED = "已失败"

    job = models.ForeignKey(ParseJob, related_name="slices", on_delete=models.CASCADE)
    slice_no = models.IntegerField()
    status = models.CharField(max_length=8, default=ST_PENDING)
    holder = models.CharField(max_length=64, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slice_no"]
        constraints = [
            models.UniqueConstraint(fields=["job", "slice_no"], name="uniq_job_slice_no"),
        ]
