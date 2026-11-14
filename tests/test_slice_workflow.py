import datetime

import pytest

from catalog.models import ParseJob, ParseSlice, SlicePolicy

pytestmark = pytest.mark.django_db


def _login(client):
    r = client.post("/login", {"username": "admin", "password": "123456"})
    assert r.status_code == 302


def _job():
    return ParseJob.objects.create(title="大文件解析", code="big_file", source_uri="file://drops/big.bin")


def _policy(client, job, total=3, closure="rerun_failed"):
    return client.post(f"/jobs/{job.id}/policy/", {"slice_total": total, "closure": closure}, follow=True)


def _claim(client, job, no, holder=""):
    data = {"slice_no": str(no)}
    if holder:
        data["holder"] = holder
    return client.post(f"/jobs/{job.id}/claim/", data, follow=True)


def test_policy_write_cuts_slices(client):
    _login(client)
    job = _job()
    r = _policy(client, job, total=4, closure="rollback_all")
    assert r.status_code == 200
    assert ParseSlice.objects.filter(job=job).count() == 4
    job.refresh_from_db()
    assert job.status == "切片解析中"
    assert job.policy.closure == "rollback_all"
    body = r.content.decode("utf-8")
    assert "片规策略已写入" in body and "本日不得更换" in body


def test_claim_race_only_one_wins_and_holder_shown(client):
    _login(client)
    job = _job()
    _policy(client, job, total=2)
    r = _claim(client, job, 1, holder="阿黎")
    assert "领片成功" in r.content.decode("utf-8")
    # 第二人同时抢同一片：只成一枚
    r = _claim(client, job, 1, holder="小周")
    body = r.content.decode("utf-8")
    assert "抢片失败" in body
    s = ParseSlice.objects.get(job=job, slice_no=1)
    assert s.holder == "阿黎" and s.status == "已领片"
    # 读盘详情可读持有人姓名
    r = client.get(f"/jobs/{job.id}/")
    assert "阿黎" in r.content.decode("utf-8")


def test_claim_empty_slice_no_fails(client):
    _login(client)
    job = _job()
    _policy(client, job, total=2)
    r = client.post(f"/jobs/{job.id}/claim/", {"slice_no": ""}, follow=True)
    assert "片号空着" in r.content.decode("utf-8")
    assert ParseSlice.objects.filter(job=job, status="已领片").count() == 0


def test_assemble_forbidden_until_all_slices_ok(client):
    _login(client)
    job = _job()
    _policy(client, job, total=2)
    _claim(client, job, 1)
    client.post(f"/jobs/{job.id}/slices/1/result/", {"outcome": "ok"}, follow=True)
    # 2 号片还待领：未齐片不能拼
    r = client.post(f"/jobs/{job.id}/assemble/", follow=True)
    assert "禁止拼片落档" in r.content.decode("utf-8")
    job.refresh_from_db()
    assert job.status != "已入库"


def test_assemble_after_all_ok(client):
    _login(client)
    job = _job()
    _policy(client, job, total=2)
    for no in (1, 2):
        _claim(client, job, no)
        client.post(f"/jobs/{job.id}/slices/{no}/result/", {"outcome": "ok"}, follow=True)
    job.refresh_from_db()
    assert job.status == "已解析待入库"
    r = client.post(f"/jobs/{job.id}/assemble/", follow=True)
    assert "拼片落档完成" in r.content.decode("utf-8")
    job.refresh_from_db()
    assert job.status == "已入库" and job.landed == 2


def test_closure_locked_same_day(client):
    _login(client)
    job = _job()
    _policy(client, job, total=2, closure="rerun_failed")
    r = _policy(client, job, total=2, closure="rollback_all")
    assert "片失败收口本日不得更换" in r.content.decode("utf-8")
    job.refresh_from_db()
    assert job.policy.closure == "rerun_failed"


def test_closure_changeable_next_day(client):
    _login(client)
    job = _job()
    _policy(client, job, total=2, closure="rerun_failed")
    SlicePolicy.objects.filter(job=job).update(locked_date=datetime.date(2000, 1, 1))
    r = _policy(client, job, total=2, closure="rollback_all")
    assert "已更换" in r.content.decode("utf-8")
    job.refresh_from_db()
    assert job.policy.closure == "rollback_all"


def _setup_one_failed(client, closure):
    job = _job()
    _policy(client, job, total=3, closure=closure)
    for no in (1, 2, 3):
        _claim(client, job, no)
    client.post(f"/jobs/{job.id}/slices/1/result/", {"outcome": "ok"}, follow=True)
    client.post(f"/jobs/{job.id}/slices/2/result/", {"outcome": "ok"}, follow=True)
    client.post(f"/jobs/{job.id}/slices/3/result/", {"outcome": "fail"}, follow=True)
    return job


def test_recycle_rerun_failed_keeps_ok_slices(client):
    _login(client)
    job = _setup_one_failed(client, "rerun_failed")
    r = client.post(f"/jobs/{job.id}/recycle/", {"closure": "rerun_failed"}, follow=True)
    assert "仅重跑失败片" in r.content.decode("utf-8")
    states = dict(ParseSlice.objects.filter(job=job).values_list("slice_no", "status"))
    assert states == {1: "已成功", 2: "已成功", 3: "待领"}


def test_recycle_rollback_all_resets_everything(client):
    _login(client)
    job = _setup_one_failed(client, "rollback_all")
    r = client.post(f"/jobs/{job.id}/recycle/", {"closure": "rollback_all"}, follow=True)
    assert "整任务回滚" in r.content.decode("utf-8")
    states = set(ParseSlice.objects.filter(job=job).values_list("status", flat=True))
    assert states == {"待领"}
    job.refresh_from_db()
    assert job.status == "切片解析中" and job.parsed == 0


def test_recycle_closure_drift_rejected(client):
    _login(client)
    job = _setup_one_failed(client, "rerun_failed")
    # 点回收时收口漂了：提交值与片规策略不一致
    r = client.post(f"/jobs/{job.id}/recycle/", {"closure": "rollback_all"}, follow=True)
    assert "片失败收口偏了" in r.content.decode("utf-8")
    # 漂移被拒，失败片原样保留
    assert ParseSlice.objects.filter(job=job, status="已失败").count() == 1


def test_board_lists_states_and_closure_side_by_side(client):
    _login(client)
    job = _setup_one_failed(client, "rerun_failed")
    r = client.get("/slices/")
    body = r.content.decode("utf-8")
    for head in ("已领片", "已成功", "已失败", "收口名"):
        assert head in body
    assert "仅重跑失败片" in body
    # 各态分开计数：已成功 2、已失败 1
    assert "2" in body and "1" in body


def test_detail_shows_progress_and_closure(client):
    _login(client)
    job = _setup_one_failed(client, "rollback_all")
    r = client.get(f"/jobs/{job.id}/")
    body = r.content.decode("utf-8")
    assert "切片进度" in body and "整任务回滚" in body
    assert "已成功 2" in body and "已失败 1" in body
