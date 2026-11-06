"""账单骨架判据。

这一层最容易造出的假象是「余额看起来很足」——因为它一直在减 0。
所以每条判据都对着"能不能伪造充裕"打：

  · 算不出金额的消耗必须让余额变成 UNKNOWN，而不是静默不减
  · 余额必须从流水**算**出来，不能靠一个可能漂了的缓存字段
  · 扣不动时必须报错，不能静默放行
  · 坏行必须计数，不能被悄悄跳过
  · `top_up` 必须自报"没接支付"——否则会被误当成入账入口
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_fail = 0


def ok(msg: str) -> None:
    global _fail
    print(f"✔ {msg}")


def bad(msg: str) -> None:
    global _fail
    _fail += 1
    print(f"✘ {msg}")


def eq(got, want, label: str) -> None:
    if got == want:
        ok(f"{label}（{got}）")
    else:
        bad(f"{label} —— 期望 {want!r}，实际 {got!r}")


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="rkbill_"))


def test_new_account_gets_quota():
    """新账户必须有 free 套餐额度 —— 否则新用户什么都做不了。"""
    from rongke.billing import Billing, PLANS
    d = _tmp()
    try:
        b = Billing(d)
        a = b.account()
        if a.id.startswith("acct_"):
            ok(f"账户自动建好（{a.id[:14]}…）")
        else:
            bad(f"账户 id 形状不对：{a.id!r}")
        eq(a.plan, "free", "默认套餐")
        bal = b.balance()
        want = float(PLANS["free"]["quota_usd"] or 0)
        eq(bal["granted"], want, "建户即授予 free 额度")
        eq(bal["spent"], 0.0, "初始消耗为 0")
        eq(bal["remaining"], want, "初始余额等于额度")
        if bal["exact"]:
            ok("余额是精确值（不是 UNKNOWN）")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_charge_deducts():
    from rongke.billing import Billing
    d = _tmp()
    try:
        b = Billing(d)
        b.charge(0.5, run_id="r1", model="m")
        bal = b.balance()
        eq(bal["spent"], 0.5, "消耗记上了")
        eq(bal["remaining"], 1.5, "余额扣掉了（free 额度 2.0）")
        e, _ = b.ledger()
        charges = [x for x in e if x.kind == "charge"]
        eq(len(charges), 1, "流水里有一条 charge")
        eq(charges[0].run_id, "r1", "流水记住了 run_id")
        eq(charges[0].model, "m", "流水记住了 model")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_unknown_cost_poisons_balance():
    """算不出金额的消耗必须让余额变 UNKNOWN —— 这是本层最重要的一条。

    真缺陷形态：把 UNKNOWN 当 0 加，余额永不减少，用户看到"还有很多额度"
    而实际早超了。那是假安慰。
    """
    from rongke.billing import Billing
    from rongke.pricing import UNKNOWN
    d = _tmp()
    try:
        b = Billing(d)
        b.charge(0.5)
        bal1 = b.balance()
        if bal1["exact"]:
            ok("已知金额时余额精确")

        b.charge(UNKNOWN, run_id="r2")
        bal2 = b.balance()
        if bal2["remaining"] == UNKNOWN:
            ok("出现 UNKNOWN 消耗后，余额变 UNKNOWN（不假装还充足）")
        else:
            bad(f"UNKNOWN 消耗后余额仍是 {bal2['remaining']!r} —— 伪造了充裕")
        eq(bal2["unknown_charges"], 1, "未知消耗被计数")
        if not bal2["exact"]:
            ok("exact=False 标明余额是上限而非真值")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_insufficient_raises():
    """扣不动必须报错，不能静默放行。"""
    from rongke.billing import Billing, InsufficientBalance
    d = _tmp()
    try:
        b = Billing(d)
        try:
            b.charge(99.0)
        except InsufficientBalance as e:
            ok("余额不足时抛 InsufficientBalance")
            if "额度不足" in str(e) and "$" in str(e):
                ok("错误消息说了差多少")
            else:
                bad(f"错误消息不够具体：{e}")
            eq(e.needed, 99.0, "异常带上了 needed")
            if e.balance is not None:
                ok("异常带上了 balance")
        else:
            bad("余额不足竟然没报错 —— 会静默超支")

        # enforce=False 时放行（记账不阻断，用于事后对账）
        b.charge(99.0, enforce=False)
        ok("enforce=False 时允许记录超额消耗")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_balance_computed_not_cached():
    """余额必须从流水算出来 —— 手改 account.json 不该影响余额。"""
    from rongke.billing import Billing
    d = _tmp()
    try:
        b = Billing(d)
        b.charge(0.3)
        before = b.balance()["remaining"]

        # 往 account.json 里塞一个假的余额字段（模拟"缓存字段"）
        acc = json.loads((d / "account.json").read_text(encoding="utf-8"))
        acc["balance"] = 999.0
        (d / "account.json").write_text(
            json.dumps(acc, ensure_ascii=False), encoding="utf-8")

        after = Billing(d).balance()["remaining"]
        eq(after, before, "塞进 account.json 的假 balance 不影响真实余额")
        if after != 999.0:
            ok("余额不读缓存字段")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_corrupt_line_counted():
    """坏行必须计数，不能悄悄跳过。"""
    from rongke.billing import Billing
    d = _tmp()
    try:
        b = Billing(d)
        b.charge(0.1)
        # 追加两行坏 JSON
        with (d / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as f:
            f.write("{这不是 json\n")
            f.write("也不对\n")
        entries, bad = b.ledger()
        eq(bad, 2, "两行坏 JSON 被计数")
        if len(entries) >= 2:
            ok(f"好的流水仍然读得出来（{len(entries)} 条）")
        bal = b.balance()
        eq(bal["bad_lines"], 2, "bad_lines 出现在 balance 里（人能看见账本坏了）")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_plan_change_grants_new_quota():
    from rongke.billing import Billing, PLANS
    d = _tmp()
    try:
        b = Billing(d)
        b.change_plan("pro")
        a = b.account()
        eq(a.plan, "pro", "套餐换成 pro")
        bal = b.balance()
        want = float(PLANS["free"]["quota_usd"]) + float(PLANS["pro"]["quota_usd"])
        eq(bal["granted"], want, "新套餐额度叠加授予")
        e, _ = b.ledger()
        grants = [x for x in e if x.kind == "grant"]
        if any("未实际收费" in x.note for x in grants):
            ok("grant 流水自报「未实际收费」")
        else:
            bad("grant 流水没说明未实际收费 —— 会被误当成真入账")

        try:
            b.change_plan("nosuchplan")
        except ValueError as ex:
            ok(f"不存在的套餐被拒：{str(ex)[:30]}…")
        else:
            bad("不存在的套餐竟然被接受")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_topup_is_placeholder():
    """`top_up` 必须自报是占位，不能让人以为是真入账。"""
    from rongke.billing import Billing
    d = _tmp()
    try:
        b = Billing(d)
        # ★ 先显式建户再量 before：`balance()` 本身不触发建户，
        #   而 `top_up()` 会。不先建户的话，before 量的是"还没授予额度"
        #   的 0，after 却是"建户 2.0 + 充值 10.0"，差值是 12 不是 10 ——
        #   看起来像 bug，实际是判据取错了基准点。
        b.account()
        # 用相对增量断言，不写死绝对值 —— 上一个用例可能改过套餐
        before = b.balance()["remaining"]
        e = b.top_up(10.0)
        after = b.balance()["remaining"]
        eq(after, before + 10.0, "充值额度加上了（相对增量 +10）")
        if "未接支付" in e.note or "未实际收款" in e.note:
            ok("充值流水自报「未接支付网关」")
        else:
            bad(f"充值流水没说明没接支付：{e.note!r}")

        try:
            b.top_up(-1)
        except ValueError:
            ok("负数充值被拒")
        else:
            bad("负数充值竟然被接受")

        try:
            b.top_up("abc")
        except ValueError:
            ok("非数字充值被拒")
        else:
            bad("非数字充值竟然被接受")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_report_declares_payment_not_connected():
    """报告必须明说没接支付 —— 这是本模块最重要的诚实点。"""
    from rongke.billing import Billing
    d = _tmp()
    try:
        r = Billing(d).report()
        if r.get("payment_connected") is False:
            ok("report 里 payment_connected = False")
        else:
            bad(f"report 没声明支付未接：{r.get('payment_connected')!r}")
        assum = " ".join(r.get("assumptions") or [])
        if "不接支付" in assum or "未接支付" in assum:
            ok("assumptions 里写明了不接支付")
        else:
            bad("assumptions 里没提支付")
        if "UNKNOWN" in assum:
            ok("assumptions 里写明了 UNKNOWN 的语义")
        else:
            bad("assumptions 里没提 UNKNOWN")
        # price 是占位要有标记
        if r["plan"].get("note"):
            ok("套餐带说明（note 非空）")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_plan_diff_marks_current_and_upgrades():
    from rongke.billing import plan_diff
    rows = plan_diff("pro")
    eq(len(rows), 4, "四个套餐")
    cur = [r for r in rows if r["current"]]
    eq(len(cur), 1, "只有一个标记为当前")
    eq(cur[0]["id"], "pro", "当前是 pro")
    ups = [r["id"] for r in rows if r["is_upgrade"]]
    eq(ups, ["lab", "enterprise"], "升级方向是 lab/enterprise")
    # free 不该是升级
    if "free" not in ups:
        ok("free 不算升级")
    # 占位定价要有 None（面议）的情况被保留
    ent = [r for r in rows if r["id"] == "enterprise"][0]
    if ent["price_usd"] is None:
        ok("机构版价格是 None（面议），没被编一个数")
    else:
        bad(f"机构版价格被编成了 {ent['price_usd']!r}")


def test_ledger_is_append_only():
    """流水文件只追加：已有内容的字节前缀不能被改动。"""
    from rongke.billing import Billing
    d = _tmp()
    try:
        b = Billing(d)
        b.charge(0.1)
        raw1 = (d / "ledger.jsonl").read_bytes()
        b.charge(0.2)
        raw2 = (d / "ledger.jsonl").read_bytes()
        if raw2.startswith(raw1):
            ok("流水只追加（旧内容一个字节都没动）")
        else:
            bad("流水被改写了 —— 账本不可重放")
        if len(raw2) > len(raw1):
            ok("流水变长了")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_concurrent_appends_dont_corrupt():
    """多线程同时扣费不该写出坏行（每个任务结束都扣一次）。"""
    from rongke.billing import Billing
    import threading
    d = _tmp()
    try:
        b = Billing(d)
        errs = []

        def work(i):
            try:
                Billing(d).charge(0.01, run_id=f"r{i}", enforce=False)
            except Exception as e:                            # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=work, args=(i,)) for i in range(20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        if errs:
            bad(f"并发扣费报错：{errs[0]}")
        entries, badlines = Billing(d).ledger()
        eq(badlines, 0, "并发追加没有产生坏行")
        charges = [e for e in entries if e.kind == "charge"]
        eq(len(charges), 20, "20 笔消耗都记上了")
        bal = Billing(d).balance()
        eq(bal["spent"], 0.2, "总消耗正确（20 × 0.01）")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_broken_account_file_does_not_silently_reset():
    """账户文件坏了必须报错，不能静默重建 —— 那会让旧流水全成孤儿。"""
    from rongke.billing import Billing
    d = _tmp()
    try:
        b = Billing(d)
        b.account()
        (d / "account.json").write_text("{坏了", encoding="utf-8")
        try:
            Billing(d).account()
        except RuntimeError as e:
            ok("账户文件坏了时报错")
            if "流水" in str(e) or "孤儿" in str(e):
                ok("错误解释了为什么不能静默重建")
        else:
            bad("账户文件坏了竟然静默重建 —— 旧流水会找不到归属")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_password_never_stored_plaintext():
    """密码**绝不能**以明文或可逆形式落盘 —— 这是本模块唯一的加密要求。"""
    from rongke.billing import Billing
    d = _tmp()
    try:
        b = Billing(d)
        b.set_password("hunter2-非常秘密")
        raw = (d / "account.json").read_text(encoding="utf-8")
        if "hunter2" in raw:
            bad("明文密码写进了 account.json —— 这条绝不能允许")
        else:
            ok("account.json 里没有明文密码")
        if "非常秘密" in raw:
            bad("明文密码（中文部分）写进了 account.json")
        else:
            ok("account.json 里没有明文密码（中文部分）")
        acc = json.loads(raw)
        if acc.get("salt") and acc.get("pwd_hash") and acc.get("pwd_iter"):
            ok("存了 salt / pwd_hash / pwd_iter 三件")
        else:
            bad(f"哈希字段不全：{list(acc.keys())}")
        if len(acc.get("salt", "")) >= 32:
            ok(f"盐够长（{len(acc['salt'])} hex 字符）")
        if int(acc.get("pwd_iter", 0)) >= 100_000:
            ok(f"迭代数够高（{acc['pwd_iter']}）")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_password_verify():
    from rongke.billing import Billing
    d = _tmp()
    try:
        b = Billing(d)
        # 没设密码时恒为 True（单机免登录语义）
        if b.verify_password("随便什么"):
            ok("没设密码时任何输入都通过（本地免登录）")
        else:
            bad("没设密码竟然校验失败 —— 首次使用会锁死自己")
        if not b.login_state()["login_required"]:
            ok("没设密码时 login_required = False")

        b.set_password("correct horse")
        if b.verify_password("correct horse"):
            ok("正确密码通过")
        else:
            bad("正确密码被拒 —— 哈希或比较写错了")
        if not b.verify_password("wrong"):
            ok("错误密码被拒")
        else:
            bad("错误密码竟然通过")
        if not b.verify_password(""):
            ok("空密码被拒（已设密码时）")
        else:
            bad("已设密码时空密码竟然通过 —— 等于没上锁")
        st = b.login_state()
        if st["login_required"] and st["password_set"]:
            ok("设了密码后 login_required = True")

        # 取消密码
        b.set_password("")
        if b.verify_password("anything"):
            ok("取消密码后回到免登录")
        else:
            bad("取消密码后仍然要求密码 —— 会锁死自己")
        raw = json.loads((d / "account.json").read_text(encoding="utf-8"))
        if "salt" not in raw and "pwd_hash" not in raw:
            ok("取消密码后字段被清掉了（不留空壳）")
        else:
            bad("取消密码后仍留着 salt/pwd_hash 字段")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_password_salt_is_random():
    """同一个密码在两台机器上必须产生不同摘要（盐是随机的）。"""
    from rongke.billing import Billing
    d1, d2 = _tmp(), _tmp()
    try:
        b1, b2 = Billing(d1), Billing(d2)
        b1.set_password("same")
        b2.set_password("same")
        h1 = json.loads((d1 / "account.json").read_text(encoding="utf-8"))
        h2 = json.loads((d2 / "account.json").read_text(encoding="utf-8"))
        if h1["salt"] != h2["salt"]:
            ok("两次设同一密码用了不同的盐")
        else:
            bad("盐一样 —— 用了固定盐")
        if h1["pwd_hash"] != h2["pwd_hash"]:
            ok("同密码产生不同摘要（盐生效）")
        else:
            bad("同密码产生相同摘要 —— 盐没起作用")
        if b1.verify_password("same") and b2.verify_password("same"):
            ok("各自都能验过自己的密码")
    finally:
        shutil.rmtree(d1, ignore_errors=True)
        shutil.rmtree(d2, ignore_errors=True)


def test_password_survives_reload():
    """密码要能跨进程读回来 —— 否则重启就登录不了。"""
    from rongke.billing import Billing
    d = _tmp()
    try:
        Billing(d).set_password("pass-123")
        b2 = Billing(d)
        if b2.verify_password("pass-123"):
            ok("新实例能验过老密码")
        else:
            bad("新实例验不过老密码 —— from_dict 漏了字段")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cleanup_renames_instead_of_deletes():
    """账本模块**不许删文件** —— 清理一律改名。

    仓库纪律「内核包里零删除调用」不分"自己的临时文件"与"用户数据"：
    一旦给临时文件开口子，下一个删错的就会是用户数据。这里的判据是
    **模块源码里没有删除调用**，而不是"跑起来没删东西" —— 后者要构造
    失败路径才看得见，前者每次都能看。
    """
    import re as _re
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parent / "rongke" / "billing.py").read_text(
        encoding="utf-8")
    # 去掉注释再扫：注释里"引述"删除语法是文档，不是删除点
    body = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    hits = _re.findall(r"\b(?:os\.remove|os\.unlink|os\.rmdir|shutil\.rmtree)"
                       r"\s*\(|\.unlink\s*\(|\.rmtree\s*\(", body)
    if not hits:
        ok("billing.py 里零删除调用（清理靠改名）")
    else:
        bad(f"billing.py 里有删除调用：{len(hits)} 处")
    # 正控：这把尺子能响
    if _re.search(r"\.unlink\s*\(", "x = p.unlink()"):
        ok("判据能响（正控）")
    else:
        bad("判据响不了 —— 它在测空气")


def test_charge_billing_target_is_injectable():
    """`Rongke._charge_billing` 的落点必须能注入。

    为什么必须有这个口子：测试要记账**但绝不许碰用户真台账**。
    实测踩过：`resume_test` 的夹具转调真 `_charge_billing` 之后，
    每跑一次回归就往用户台账里记一笔假消耗 —— 那种污染事后看着
    像用户自己花的，根本查不出来。
    """
    import tempfile as _tf
    from types import SimpleNamespace as _S
    from pathlib import Path as _P
    from rongke.loop import Rongke, RunResult
    from rongke.llm import Usage

    d = _P(_tf.mkdtemp(prefix="rkbill_inj_"))
    try:
        rk = Rongke.__new__(Rongke)       # 不重构造（会连 MCP / 装插件）
        rk.cfg = _S(provider=_S(name="t", model="m"),
                    billing=_S(enabled=True))
        rk.billing_root = d / "billing"
        res = RunResult(success=True, reason="inj", steps=1,
                        usage=Usage(input_tokens=10, output_tokens=5))
        res.cost = 0.5
        res.run_id = "inj-1"
        out = rk._charge_billing(res)
        if (d / "billing" / "ledger.jsonl").exists():
            ok("台账落在注入的目录里")
        else:
            bad("台账没落在注入的目录里 —— 它会写进用户真台账")
        if out.get("charged") == 0.5:
            ok("自述里的金额就是这一轮成本")
        else:
            bad(f"自述金额不对：{out.get('charged')!r}")
        # 关掉时如实说 skipped，不假装记了
        rk.cfg.billing = _S(enabled=False)
        if rk._charge_billing(res).get("skipped") is True:
            ok("账单关掉 ⇒ 自述 skipped")
        else:
            bad("账单关掉时没报 skipped")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_report_never_leaks_credentials():
    """`report()` 是给界面/命令行看的，**不许**带出凭据材料。

    实测事故：`report()` 原来直接把 `account.to_dict()` 塞回去，而那个方法
    是落盘用的 —— 于是 `/api/billing` 这个**只读 GET 端点**把
    `salt` / `pwd_hash` / `pwd_iter` 一起送给了浏览器。

    "PBKDF2 很难逆推"不是理由：派生出摘要的原料没有理由离开磁盘。
    判据用**白名单**：`report()["account"]` 的键必须都在允许集里，
    将来 `Account` 加了新字段也默认不会跟着泄露。
    """
    from rongke.billing import Billing
    d = _tmp()
    try:
        b = Billing(d)
        b.set_password("secret-xyz")     # 确保 salt/hash 真的存在
        rep = b.report()
        acct = rep.get("account") or {}
        forbidden = {"salt", "pwd_hash", "pwd_iter"}
        leaked = forbidden & set(acct)
        if not leaked:
            ok("report() 的 account 里没有 salt/pwd_hash/pwd_iter")
        else:
            bad(f"report() 泄露了凭据材料：{sorted(leaked)}")
        allowed = {"id", "email", "plan", "created", "plan_since",
                   "password_set", "ledger", "account_file"}
        extra = set(acct) - allowed
        if not extra:
            ok("account 的键都在白名单内")
        else:
            bad(f"account 多出未列入白名单的字段：{sorted(extra)}"
                f"（确认无害后加进允许集）")
        # 但"有没有设密码"这件事要能看到 —— 界面据此提示登录
        if acct.get("password_set") is True:
            ok("password_set 仍然可见（界面要知道要不要登录）")
        else:
            bad(f"password_set 不对：{acct.get('password_set')!r}")
        # 台账位置由后端给出，前端不自己拼
        if acct.get("ledger") and acct["ledger"].endswith("ledger.jsonl"):
            ok("account.ledger 给出了真实台账路径")
        else:
            bad(f"account.ledger 缺失或不像路径：{acct.get('ledger')!r}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_locks_live_outside_the_data_dir():
    """锁文件不许落在**用户的数据目录**里。

    背景：本仓纪律「内核包里零删除调用」，所以锁释放只能改名
    （`os.replace` 成 `.lock.done`）。改名会留下残留 —— 每个锁一个、
    几字节、不累积。第一版就留在 `billing/` 里，于是用户的账单文件夹
    长这样：`account.json  ledger.jsonl  .account.lock.done  .ledger.lock.done`。
    那不该是用户看到的东西。

    现在锁实体搬到系统临时目录（按绝对路径哈希命名，所以不同数据根
    不会共用同一把锁），数据目录只剩真数据。

    判据打在**用完之后目录里有什么**上，不打在"代码把 lock 放哪了"上 ——
    后者是读代码，前者是事实。
    """
    import tempfile as _tf
    from pathlib import Path as _P
    from rongke.billing import Billing
    d = _P(_tf.mkdtemp())
    try:
        b = Billing(d / "billing")
        b.account()                                  # 建户（走一次锁）
        b.charge(model="m", cost=0.5, note="锁残留判据")
        b.set_password("pw")
        b.verify_password("pw")
        b.change_plan("pro")
        b.balance()                                  # 读流水（再走一次锁）
        files = sorted(p.name for p in (d / "billing").iterdir())
        junk = [f for f in files if ".lock" in f or f.endswith((".done", ".abandoned"))]
        if not junk:
            ok(f"数据目录里没有锁残留（只剩 {', '.join(files)}）")
        else:
            bad(f"数据目录里留了锁垃圾：{junk}")

        # 锁确实在跑 —— 不是"因为压根没加锁所以没残留"
        from rongke.billing import _lock_path
        lp = _lock_path(d / "billing" / "account.json")
        if "rongke-locks" in str(lp):
            ok(f"锁实体在系统临时目录里（{lp.name}）")
        else:
            bad(f"锁还留在原地：{lp}")
        # 正控：这把锁真的会拦第二个持有者
        import rongke.billing as _bl
        with _bl._file_lock(d / "billing" / "account.json") as got1:
            with _bl._file_lock(d / "billing" / "account.json", timeout=0.2) as got2:
                if got1 and not got2:
                    ok("正控：锁真的互斥（第二个持有者拿不到）")
                else:
                    bad(f"锁没起作用：got1={got1} got2={got2}")
        # 不同数据根不许共用同一把锁
        other = _lock_path(_P(_tf.mkdtemp()) / "billing" / "account.json")
        if other != lp:
            ok("不同数据根拿到不同的锁（不会互相卡死）")
        else:
            bad("不同数据根共用了同一把锁 —— 会互相卡死")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_topup_is_idempotent_with_a_key():
    """支付回调必须幂等：同一个交易号只许入账一次。

    背景：我在产品说明里写了"用交易号当 run_id 去重"，写完才发现
    `top_up()` **根本不接受** `run_id`，也没有任何去重 —— 那是一句
    没兑现的文档承诺。补上实现之后才敢留那句话。

    支付方会重投回调（网络抖动、超时重试、人工补发）。没有幂等键时，
    同一笔钱会被记成两笔 grant，等于凭空多给额度。

    ★ 不带 `run_id` 时**照记两条**是有意的：没有幂等键就无法判断
      "这两笔是不是同一笔"，替调用方猜"大概重复了所以跳过"会**吞掉
      真实的第二笔充值**。宁可多记，让人能从台账看出来。
    """
    import tempfile as _tf
    from pathlib import Path as _P
    from rongke.billing import Billing
    d = _P(_tf.mkdtemp())
    try:
        b = Billing(d / "billing")
        b.account()
        base = b.balance()["granted"]
        e1 = b.top_up(10, run_id="TXN-001", note="支付回调")
        got1 = b.balance()["granted"]
        if abs(got1 - (base + 10)) < 1e-9:
            ok("带交易号充值一次 ⇒ 额度加一次")
        else:
            bad(f"充值后额度不对：{got1}（期望 {base + 10}）")
        if e1.run_id == "TXN-001":
            ok("交易号被写进流水（事后能对账）")
        else:
            bad(f"交易号没落进流水：{e1.run_id!r}")

        # 重投：必须只算一次
        e2 = b.top_up(10, run_id="TXN-001", note="重投")
        got2 = b.balance()["granted"]
        if abs(got2 - got1) < 1e-9:
            ok("★ 同一交易号重投 ⇒ 额度不再增加（幂等）")
        else:
            bad(f"★ 重投又给了一次钱：{got1} → {got2}")
        if e2.ts == e1.ts:
            ok("★ 重投返回的是台账里那条旧记录（不是新写一条）")
        else:
            bad("重投新写了一条流水 —— 额度虽对但台账重复了")

        # 不同交易号必须各记各的
        b.top_up(5, run_id="TXN-002")
        if abs(b.balance()["granted"] - (got1 + 5)) < 1e-9:
            ok("不同交易号各自入账（没有互相吞掉）")
        else:
            bad("不同交易号被误当成重复")

        # 不带键 ⇒ 两条都记（不替调用方猜）
        before = b.balance()["granted"]
        b.top_up(7)
        b.top_up(7)
        after = b.balance()["granted"]
        if abs(after - (before + 14)) < 1e-9:
            ok("不带幂等键 ⇒ 两条都记（无键就不能猜，免得吞掉真充值）")
        else:
            bad(f"不带键时被静默去重了：{before} → {after}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    print("=== [1] 新账户自带额度 ===")
    test_new_account_gets_quota()
    print("\n=== [2] 扣费与余额 ===")
    test_charge_deducts()
    print("\n=== [3] UNKNOWN 消耗不许伪装成 0 ===")
    test_unknown_cost_poisons_balance()
    print("\n=== [4] 余额不足必须报错 ===")
    test_insufficient_raises()
    print("\n=== [5] 余额从流水算，不读缓存 ===")
    test_balance_computed_not_cached()
    print("\n=== [6] 坏行必须计数 ===")
    test_corrupt_line_counted()
    print("\n=== [7] 换套餐 ===")
    test_plan_change_grants_new_quota()
    print("\n=== [8] 充值是占位 ===")
    test_topup_is_placeholder()
    print("\n=== [9] 报告声明支付未接 ===")
    test_report_declares_payment_not_connected()
    print("\n=== [10] 套餐对照 ===")
    test_plan_diff_marks_current_and_upgrades()
    print("\n=== [11] 流水只追加 ===")
    test_ledger_is_append_only()
    print("\n=== [12] 并发追加不坏账 ===")
    test_concurrent_appends_dont_corrupt()
    print("\n=== [13] 账户坏了不静默重置 ===")
    test_broken_account_file_does_not_silently_reset()
    print("\n=== [14] 密码绝不明文落盘 ===")
    test_password_never_stored_plaintext()
    print("\n=== [15] 密码校验 ===")
    test_password_verify()
    print("\n=== [16] 盐是随机的 ===")
    test_password_salt_is_random()
    print("\n=== [17] 密码跨进程有效 ===")
    test_password_survives_reload()
    print("\n=== [18] 清理靠改名，不删除 ===")
    test_cleanup_renames_instead_of_deletes()
    print("\n=== [19] 记账落点可注入（测试不许碰真台账）===")
    test_charge_billing_target_is_injectable()
    print("\n=== [20] report() 不许泄露凭据材料 ===")
    test_report_never_leaks_credentials()
    print("\n=== [21] 锁不在用户数据目录里留垃圾 ===")
    test_locks_live_outside_the_data_dir()
    print("\n=== [22] 充值幂等（支付回调会重投）===")
    test_topup_is_idempotent_with_a_key()
    print()
    if _fail:
        print(f"✘ 失败 {_fail} 项")
        sys.exit(1)
    print("✔ 全部通过")
