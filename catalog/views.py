from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from catalog.models import ParseJob, ParseSlice, SlicePolicy

CLOSURE_NAMES = dict(SlicePolicy.CLOSURES)


def _authed(request) -> bool:
    return request.session.get("user") == settings.ADMIN_USER


def _tallies(job) -> dict:
    """切片进度各态分开计数。"""
    rows = job.slices.values("status").annotate(n=Count("id"))
    counts = {row["status"]: row["n"] for row in rows}
    return {
        "pending": counts.get(ParseSlice.ST_PENDING, 0),
        "claimed": counts.get(ParseSlice.ST_CLAIMED, 0),
        "ok": counts.get(ParseSlice.ST_OK, 0),
        "failed": counts.get(ParseSlice.ST_FAILED, 0),
        "total": sum(counts.values()),
    }


def _refresh_job(job: ParseJob) -> None:
    """按切片进度回写任务状态与已解析数（进度写在读盘详情）。"""
    if job.status == "已入库":
        return
    t = _tallies(job)
    if not t["total"]:
        return
    job.parsed = t["ok"]
    job.status = "已解析待入库" if t["ok"] == t["total"] else "切片解析中"
    job.save(update_fields=["parsed", "status"])


def root(request):
    return HttpResponseRedirect("/jobs/" if _authed(request) else "/login")


@require_http_methods(["GET", "POST"])
def login_page(request):
    err = ""
    if request.method == "POST":
        user = request.POST.get("username", "").strip()
        pwd = request.POST.get("password", "")
        if user == settings.ADMIN_USER and pwd == settings.ADMIN_PASSWORD:
            request.session["user"] = user
            return HttpResponseRedirect("/jobs/")
        err = "账号或密码错误"
    return render(request, "login.html", {"error": err, "lab": settings.LAB_NAME})


def logout_view(request):
    request.session.flush()
    return HttpResponseRedirect("/login")


def job_list(request):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    items = list(ParseJob.objects.all())
    return render(request, "jobs_list.html", {"items": items, "lab": settings.LAB_NAME, "user": request.session["user"]})


def slice_board(request):
    """领片列表：并排写出已领片、已成功、已失败、收口名。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    jobs = ParseJob.objects.annotate(
        n_pending=Count("slices", filter=Q(slices__status=ParseSlice.ST_PENDING)),
        n_claimed=Count("slices", filter=Q(slices__status=ParseSlice.ST_CLAIMED)),
        n_ok=Count("slices", filter=Q(slices__status=ParseSlice.ST_OK)),
        n_failed=Count("slices", filter=Q(slices__status=ParseSlice.ST_FAILED)),
        n_total=Count("slices"),
    ).select_related("policy")
    rows = []
    for job in jobs:
        policy = getattr(job, "policy", None)
        rows.append({
            "job": job,
            "pending": job.n_pending,
            "claimed": job.n_claimed,
            "ok": job.n_ok,
            "failed": job.n_failed,
            "total": job.n_total,
            "closure_name": policy.closure_name if policy else "未写入",
        })
    return render(request, "slices_board.html", {
        "rows": rows, "lab": settings.LAB_NAME, "user": request.session["user"],
    })


def job_detail(request, job_id: int):
    """读盘详情：进度、持有人姓名、片规策略、逐片操作。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    policy = getattr(job, "policy", None)
    slices = list(job.slices.all())
    return render(request, "job_detail.html", {
        "job": job,
        "policy": policy,
        "slices": slices,
        "tally": _tallies(job),
        "closures": SlicePolicy.CLOSURES,
        "closure_names": CLOSURE_NAMES,
        "lab": settings.LAB_NAME,
        "user": request.session["user"],
    })


@require_POST
def job_policy(request, job_id: int):
    """写入/更换片规策略。片失败收口写进策略后本日不得更换。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    back = f"/jobs/{job.id}/"

    closure = request.POST.get("closure", "").strip()
    if closure not in CLOSURE_NAMES:
        messages.error(request, "片失败收口只能二选一：整任务回滚 或 仅重跑失败片")
        return HttpResponseRedirect(back)
    raw_total = request.POST.get("slice_total", "").strip()
    if not raw_total.isdigit() or not (1 <= int(raw_total) <= 999):
        messages.error(request, "总片数无效，切片失败")
        return HttpResponseRedirect(back)
    slice_total = int(raw_total)
    today = timezone.localdate()
    policy = getattr(job, "policy", None)

    if policy is None:
        with transaction.atomic():
            policy = SlicePolicy.objects.create(
                job=job, slice_total=slice_total, closure=closure, locked_date=today,
            )
            ParseSlice.objects.bulk_create([
                ParseSlice(job=job, slice_no=no) for no in range(1, slice_total + 1)
            ])
        _refresh_job(job)
        messages.success(request, f"片规策略已写入并切片：共 {slice_total} 片，片失败收口「{CLOSURE_NAMES[closure]}」，本日不得更换")
        return HttpResponseRedirect(back)

    if slice_total != policy.slice_total:
        messages.error(request, "已按片规策略切片，总片数不得更换")
        return HttpResponseRedirect(back)
    if closure != policy.closure:
        if policy.locked_date == today:
            messages.error(request, "片失败收口本日不得更换")
            return HttpResponseRedirect(back)
        policy.closure = closure
        policy.locked_date = today
        policy.save(update_fields=["closure", "locked_date"])
        messages.success(request, f"片失败收口已更换为「{CLOSURE_NAMES[closure]}」，本日不得更换")
        return HttpResponseRedirect(back)
    messages.info(request, "片规策略未变")
    return HttpResponseRedirect(back)


@require_POST
def slice_claim(request, job_id: int):
    """抢领切片：后台加锁（原子条件更新），同一片两人同时抢只成一枚。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    back = f"/jobs/{job.id}/"
    if getattr(job, "policy", None) is None:
        messages.error(request, "请先写入片规策略再抢领切片")
        return HttpResponseRedirect(back)

    raw = request.POST.get("slice_no", "").strip()
    if not raw:
        messages.error(request, "片号空着，领片失败")
        return HttpResponseRedirect(back)
    if not raw.isdigit():
        messages.error(request, "片号无效，领片失败")
        return HttpResponseRedirect(back)
    slice_no = int(raw)
    holder = request.POST.get("holder", "").strip() or request.session["user"]

    # 单条原子 UPDATE 即抢片锁：只有仍处「待领」的片能被领走，并发抢领只成一枚。
    won = ParseSlice.objects.filter(
        job=job, slice_no=slice_no, status=ParseSlice.ST_PENDING,
    ).update(status=ParseSlice.ST_CLAIMED, holder=holder)
    if won == 1:
        _refresh_job(job)
        messages.success(request, f"领片成功：{holder} 领得 {slice_no} 号片")
    else:
        messages.error(request, f"抢片失败：{slice_no} 号片已被他人领走或不可领")
    return HttpResponseRedirect(back)


@require_POST
def slice_result(request, job_id: int, slice_no: int):
    """已领片回报解析结果：成功或失败。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    back = f"/jobs/{job.id}/"
    outcome = request.POST.get("outcome", "")
    target = {"ok": ParseSlice.ST_OK, "fail": ParseSlice.ST_FAILED}.get(outcome)
    if target is None:
        messages.error(request, "解析结果无效")
        return HttpResponseRedirect(back)
    moved = ParseSlice.objects.filter(
        job=job, slice_no=slice_no, status=ParseSlice.ST_CLAIMED,
    ).update(status=target)
    if moved == 1:
        _refresh_job(job)
        messages.success(request, f"{slice_no} 号片已记为{target}")
    else:
        messages.error(request, f"{slice_no} 号片不在已领片状态，无法回报结果")
    return HttpResponseRedirect(back)


@require_POST
def job_recycle(request, job_id: int):
    """回收失败片：按片失败收口执行。点回收时收口漂了则提示「片失败收口偏了」。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    back = f"/jobs/{job.id}/"
    policy = getattr(job, "policy", None)
    if policy is None:
        messages.error(request, "请先写入片规策略再回收")
        return HttpResponseRedirect(back)

    posted = request.POST.get("closure", "").strip()
    if posted != policy.closure:
        messages.error(request, "片失败收口偏了：页面收口与片规策略不一致，请刷新读盘详情后再点回收")
        return HttpResponseRedirect(back)

    failed = job.slices.filter(status=ParseSlice.ST_FAILED)
    if not failed.exists():
        messages.info(request, "没有失败片，无需回收")
        return HttpResponseRedirect(back)

    with transaction.atomic():
        locked = ParseJob.objects.select_for_update().get(pk=job.pk)
        if policy.closure == SlicePolicy.CLOSURE_ROLLBACK_ALL:
            # 整任务回滚：失败片拖垮整任务，已析片一并回滚，全部重新待领。
            locked.slices.exclude(status=ParseSlice.ST_PENDING).update(
                status=ParseSlice.ST_PENDING, holder="",
            )
            messages.success(request, "已按「整任务回滚」回收：失败片拖垮整任务，已析片并回滚，全部重新待领")
        else:
            # 仅重跑失败片：保住成功片，只把失败片退回待领。
            locked.slices.filter(status=ParseSlice.ST_FAILED).update(
                status=ParseSlice.ST_PENDING, holder="",
            )
            messages.success(request, "已按「仅重跑失败片」回收：保住成功片，仅失败片重新待领")
    _refresh_job(job)
    return HttpResponseRedirect(back)


@require_POST
def job_assemble(request, job_id: int):
    """拼片落档：后台加锁，片没领齐（未全部成功）禁止落档。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    back = f"/jobs/{job_id}/"
    with transaction.atomic():
        job = ParseJob.objects.select_for_update().get(pk=job_id)
        slices = job.slices.all()
        if not slices.exists():
            messages.error(request, "请先写入片规策略切片，再拼片落档")
            return HttpResponseRedirect(back)
        if slices.exclude(status=ParseSlice.ST_OK).exists():
            messages.error(request, "片没领齐，禁止拼片落档")
            return HttpResponseRedirect(back)
        total = slices.count()
        job.parsed = total
        job.landed = total
        job.status = "已入库"
        job.save(update_fields=["parsed", "landed", "status"])
    messages.success(request, f"拼片落档完成：{total} 片齐整，整档已入库")
    return HttpResponseRedirect(back)
