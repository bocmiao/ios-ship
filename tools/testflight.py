#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""TestFlight 分发收尾：确保 build 真的能被装上。

---

## 为什么需要这个

上传到 TestFlight 成功、build 状态 `VALID`，**不代表手机上能看到它**。
中间还差三步，全都在 App Store Connect 里，缺任何一步 TestFlight 里都是空的：

  1. App 要有一个**内部测试组**（betaGroup）
  2. 这个 build 要**分配进**那个组
  3. 你自己的 Apple ID 要**在组里**

踩过一次：build 4 上传成功、状态 VALID，人也打开 TestFlight 了，
里面什么都没有 —— 因为 `betaGroups` 是空的。上传只完成了一半。

## 内部组和外部组的区别（这里最容易搞错）

  - **内部组**（`isInternalGroup = true`）：成员是你的团队成员，
    **不需要 Beta App Review**，上传几分钟后就能装。最多 100 人。
  - **外部组**（`isInternalGroup = false`）：给外部邮箱发邀请，
    **第一个 build 必须过 Beta App Review**，通常等 1~3 天。

`POST /v1/betaGroups` **默认建的是外部组**。想建内部组，必须在
attributes 里显式写 `"isInternalGroup": true`。
但 `PATCH` 改不了这个字段（会报
`The attribute 'isInternalGroup' can not be included in a 'UPDATE' operation`），
建错了只能删掉重建。

## 用法

    python testflight.py status        # 只读，看现在到底缺哪一环
    python testflight.py attach        # 补齐：建组 / 挂 build / 加自己

`attach` 是幂等的，重复跑不会建重复组。

## 凭据

从 `cert/secrets.txt`（apple-auto.py 生成的那个）里读 `ASC_ISSUER_ID`
和 `ASC_KEY_ID`，p8 用 apple-auto.py 的同一套查找逻辑。
也可以用参数覆盖。
"""

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- 借 apple-auto

def load_apple_auto():
    """把 apple-auto.py 当模块加载，复用它的 JWT / HTTP / p8 查找。

    文件名里有连字符，不能直接 import，只能走 spec_from_file_location。
    """
    path = HERE / "apple-auto.py"
    if not path.exists():
        sys.exit(f"找不到 {path}，它和本脚本必须在同一目录。")
    spec = importlib.util.spec_from_file_location("apple_auto", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["apple_auto"] = mod
    spec.loader.exec_module(mod)
    return mod


def read_secrets(cert_dir=None):
    """读 secrets.txt。找不到就返回空字典，让参数兜底。

    cert_dir 优先。不给的话按几个常见位置试——注意 apple-auto.py 的
    `--out` 默认是 `tools/cert`，所以它自己生成的文件在那儿；
    但也有人把 cert 放在项目外（不把私钥材料放仓库里，更稳妥）。
    """
    cands = []
    if cert_dir:
        cands.append(Path(cert_dir) / "secrets.txt")
    cands += [
        HERE / "cert" / "secrets.txt",        # apple-auto.py 默认输出位置
        HERE.parent / "cert" / "secrets.txt",  # ios/cert
        HERE / "secrets.txt",
    ]
    for cand in cands:
        if cand.exists():
            out = {}
            for line in cand.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
            return out
    return {}


# ---------------------------------------------------------------- 读状态

def get_app(aa, token, bundle_id):
    st, r = aa.api("GET", f"/v1/apps?filter[bundleId]={bundle_id}", token)
    if st >= 300:
        sys.exit(f"查 App 失败：{aa.api_err(r)}")
    data = r.get("data", [])
    if not data:
        sys.exit(f"账号下没有 bundle id 为 {bundle_id} 的 App。")
    return data[0]


def list_builds(aa, token, app_id, limit=5):
    st, r = aa.api("GET", f"/v1/builds?filter[app]={app_id}&sort=-version&limit={limit}", token)
    if st >= 300:
        sys.exit(f"查 build 失败：{aa.api_err(r)}")
    return r.get("data", [])


def list_groups(aa, token, app_id):
    st, r = aa.api("GET", f"/v1/betaGroups?filter[app]={app_id}&limit=50", token)
    if st >= 300:
        sys.exit(f"查测试组失败：{aa.api_err(r)}")
    return r.get("data", [])


def group_detail(aa, token, group_id):
    st, r = aa.api("GET", f"/v1/betaGroups/{group_id}?include=builds,betaTesters", token)
    if st >= 300:
        return None, [], []
    builds = [x for x in r.get("included", []) if x["type"] == "builds"]
    testers = [x for x in r.get("included", []) if x["type"] == "betaTesters"]
    return r["data"], builds, testers


# ---------------------------------------------------------------- 补齐

def ensure_internal_group(aa, token, app_id):
    """返回内部组的 id。没有就建一个。

    注意：POST 建组默认是外部组，必须显式带 isInternalGroup=true。
    建错了 PATCH 改不回来，只能 DELETE 重建 —— 所以这里一步到位。
    """
    for g in list_groups(aa, token, app_id):
        if g["attributes"].get("isInternalGroup"):
            return g["id"], g["attributes"].get("name")

    body = {"data": {
        "type": "betaGroups",
        "attributes": {"name": "内部测试", "isInternalGroup": True},
        "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
    }}
    st, r = aa.api("POST", "/v1/betaGroups", token, body)
    if st >= 300:
        sys.exit(f"建内部组失败：{aa.api_err(r)}")
    g = r["data"]
    print(f"  已建内部组「{g['attributes']['name']}」id={g['id']}")
    return g["id"], g["attributes"]["name"]


def wait_for_build(aa, token, app_id, want=None, timeout=900, interval=15):
    """等 build 出现在列表里并变成 VALID，返回那条 build。

    Fastfile 里 skip_waiting_for_build_processing: true，所以 workflow 在
    Apple 收完二进制的那一刻就成功了。此时 /v1/builds 往往还查不到这个号，
    或者状态是 PROCESSING。直接去挂组会 sys.exit，把一次成功的发版报成失败。

    want 是版本号字符串；None 表示等最新的一个变成 VALID。
    """
    deadline = time.time() + timeout
    last = None
    while True:
        builds = list_builds(aa, token, app_id, limit=10)
        target = None
        for b in builds:
            if want and b["attributes"]["version"] == str(want):
                target = b
                break
        if want is None:
            target = next((b for b in builds
                           if b["attributes"]["processingState"] == "VALID"), None)

        if target:
            ps = target["attributes"]["processingState"]
            if ps == "VALID":
                return target
            if ps in ("INVALID", "FAILED"):
                sys.exit(f"build {target['attributes']['version']} 处理失败：{ps}\n"
                         f"原因多半是图标缺失或 Info.plist 有问题，"
                         f"Apple 会给账号发一封说明邮件。")
            last = ps
        if time.time() >= deadline:
            sys.exit(f"等了 {timeout // 60} 分钟，build {want or '(最新)'} 仍未到 VALID"
                     f"（当前 {last or '列表里还没有'}）。\n"
                     f"过几分钟用 `testflight.py attach --build N` 补挂即可。")
        print(f"  等待 Apple 处理（{last or '尚未出现在 build 列表'}）…")
        time.sleep(interval)


def ensure_build_attached(aa, token, group_id, app_id, want=None):
    """把 build 挂到组里。want 是版本号字符串，None 表示最新的 VALID。"""
    builds = list_builds(aa, token, app_id, limit=10)
    if not builds:
        sys.exit("这个 App 一个 build 都没有，先把上传跑通。")

    target = None
    for b in builds:
        if want and b["attributes"]["version"] == str(want):
            target = b
            break
    if target is None:
        if want:
            sys.exit(f"找不到 build {want}。现有：{[b['attributes']['version'] for b in builds]}")
        target = next((b for b in builds if b["attributes"]["processingState"] == "VALID"), None)
        if target is None:
            sys.exit("没有处于 VALID 状态的 build（可能还在处理中，等几分钟再跑）。")

    ver = target["attributes"]["version"]
    st, r = aa.api("POST", f"/v1/betaGroups/{group_id}/relationships/builds", token,
                   {"data": [{"type": "builds", "id": target["id"]}]})
    # 409 通常意味着已经在组里了，不是错
    if st >= 300 and st != 409:
        sys.exit(f"挂 build 失败：{aa.api_err(r)}")
    print(f"  build {ver} 已在组内" if st == 409 else f"  已把 build {ver} 挂进组")
    return ver


def ensure_self_in_group(aa, token, group_id):
    """把 Account Holder 加成内部测试员。

    内部测试员 = 团队成员。判定标准是「组里有没有**你自己**」，不是「组里有没有人」——
    组里已经有同事时，"有人"成立但你这台手机依然装不上。

    坑：POST /v1/betaTesters 必须同时带 betaGroups 或 builds 关系，
    只传 email 会报 "Either betaGroups or builds relationship is required"。
    """
    st, r = aa.api("GET", "/v1/users?limit=20", token)
    if st >= 300:
        print(f"  查团队成员失败（{aa.api_err(r)}），跳过加测试员")
        return
    holder = None
    for u in r.get("data", []):
        if "ACCOUNT_HOLDER" in (u["attributes"].get("roles") or []):
            holder = u
            break
    if holder is None:
        print("  团队里没有 Account Holder，跳过")
        return

    a = holder["attributes"]
    my_email = a.get("username")

    _, _, testers = group_detail(aa, token, group_id)
    emails = [t["attributes"].get("email") for t in testers if t["attributes"].get("email")]
    if my_email and my_email in emails:
        print(f"  组内已有你（{my_email}）")
        return
    if emails:
        print(f"  组内有 {len(emails)} 个测试员但没有你（{my_email}），补上")

    body = {"data": {
        "type": "betaTesters",
        "attributes": {
            "email": my_email,
            "firstName": a.get("firstName") or "Test",
            "lastName": a.get("lastName") or "User",
        },
        "relationships": {"betaGroups": {"data": [{"type": "betaGroups", "id": group_id}]}},
    }}
    st, r = aa.api("POST", "/v1/betaTesters", token, body)
    if st >= 300:
        print(f"  加测试员失败：{aa.api_err(r)}")
        return
    print(f"  已把 {my_email} 加进内部组")


def ensure_localizations(aa, token, app_id, build_id, feedback_email,
                         description=None, whats_new=None):
    """补 TestFlight 里显示的两段文字。

    没有这些，TestFlight 里 App 详情是空白的 —— 不影响安装，但看不到该测什么。
    locale 必须用 App Store 的语言代码：简体中文是 `zh-Hans`，
    写 `zh` 会报 `The 'locale' value is invalid`。

    description / whats_new 由调用方传（ship.config.json 的 testflightDescription
    与 testflightWhatsNew）。不传就用通用文案——这里绝不能写死某个项目的业务描述。
    """
    st, r = aa.api("GET", f"/v1/betaAppLocalizations?filter[app]={app_id}", token)
    if st < 300 and not r.get("data"):
        body = {"data": {
            "type": "betaAppLocalizations",
            "attributes": {
                "locale": "zh-Hans",
                "description": description or "内部测试版本，用于验证构建与安装流程。",
                "feedbackEmail": feedback_email,
            },
            "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
        }}
        st, r = aa.api("POST", "/v1/betaAppLocalizations", token, body)
        print("  已补 App 测试信息" + ("" if st < 300 else f"（失败：{aa.api_err(r)}）"))

    st, r = aa.api("GET", f"/v1/betaBuildLocalizations?filter[build]={build_id}", token)
    if st < 300 and not r.get("data"):
        body = {"data": {
            "type": "betaBuildLocalizations",
            "attributes": {
                "locale": "zh-Hans",
                "whatsNew": whats_new or "本次改动见仓库提交记录。",
            },
            "relationships": {"build": {"data": {"type": "builds", "id": build_id}}},
        }}
        st, r = aa.api("POST", "/v1/betaBuildLocalizations", token, body)
        print("  已补本次测试内容" + ("" if st < 300 else f"（失败：{aa.api_err(r)}）"))


# ---------------------------------------------------------------- 两个动作

def cmd_status(aa, token, app):
    ok = lambda b: "OK  " if b else "缺!!"
    app_id = app["id"]
    a = app["attributes"]
    print(f"App：{a['name']}  ({a['bundleId']})\n")

    builds = list_builds(aa, token, app_id)
    print(f"[{ok(bool(builds))}] build 数量 {len(builds)}")
    for b in builds[:5]:
        at = b["attributes"]
        print(f"        build {at['version']:<4} {at['processingState']:<10} 过期 {str(at.get('expirationDate'))[:10]}")

    groups = list_groups(aa, token, app_id)
    internal = [g for g in groups if g["attributes"].get("isInternalGroup")]
    external = [g for g in groups if not g["attributes"].get("isInternalGroup")]
    print(f"[{ok(bool(internal))}] 内部测试组 {len(internal)} 个" +
          ("" if internal else "  ← 没有它，TestFlight 里什么都看不到"))
    for g in internal:
        gd, gb, gt = group_detail(aa, token, g["id"])
        vers = [x["attributes"]["version"] for x in gb]
        mails = [x["attributes"].get("email") for x in gt]
        print(f"        「{g['attributes']['name']}」build={vers or '空'} 测试员={mails or '空'}")

    if external:
        print(f"[--] 外部测试组 {len(external)} 个（第一个 build 要过 Beta App Review）")
        for g in external:
            print(f"        「{g['attributes']['name']}」")

    st, r = aa.api("GET", f"/v1/betaAppLocalizations?filter[app]={app_id}", token)
    has = bool(r.get("data")) if st < 300 else False
    print(f"[{ok(has)}] TestFlight 测试信息{'：有' if has else '  ← TestFlight 里会是空白'}")

    if internal and builds:
        print("\n结论：内部组齐了，打开 TestFlight 就能看到。")


def cmd_attach(aa, token, app, args):
    app_id = app["id"]
    print("1. 内部测试组")
    gid, gname = ensure_internal_group(aa, token, app_id)

    print("2. 等 Apple 处理 build")
    target = wait_for_build(aa, token, app_id, want=args.build)
    ver = target["attributes"]["version"]

    print("3. 挂 build")
    ensure_build_attached(aa, token, gid, app_id, want=ver)

    print("4. 测试员")
    ensure_self_in_group(aa, token, gid)

    print("5. TestFlight 文案")
    # 直接用第 2 步拿到的 build id，不再查一遍列表
    ensure_localizations(aa, token, app_id, target["id"], args.feedback_email,
                         args.description, args.whats_new)

    print(f"\n完成：build {ver} 已分配给内部组「{gname}」。")
    print("iPhone 打开 TestFlight，用同一个 Apple ID 登录，几分钟内应该能看到。")


def load_cfg():
    """读 shipconfig.load()。读不到就返回空字典，让参数兜底。"""
    try:
        spec = importlib.util.spec_from_file_location("sc_for_tf", HERE / "shipconfig.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.load(HERE.parent)
    except Exception:
        return {}


def main():
    p = argparse.ArgumentParser(description="TestFlight 分发收尾")
    p.add_argument("action", choices=["status", "attach"])
    p.add_argument("--bundle-id", default="",
                   help="不填就从 project.yml / ship.config.json 推断")
    p.add_argument("--build", default=None, help="指定 build 号，默认取最新 VALID")
    p.add_argument("--p8", default=None, help="AuthKey_XXX.p8 路径，默认自动找")
    p.add_argument("--key-id", default=None)
    p.add_argument("--issuer", default=None)
    p.add_argument("--feedback-email", default=None)
    p.add_argument("--description", default="", help="TestFlight App 简介，默认取 ship.config.json")
    p.add_argument("--whats-new", default="", help="本次测试内容，默认取 ship.config.json")
    args = p.parse_args()

    aa = load_apple_auto()
    secrets = read_secrets()
    cfg = load_cfg()

    if not args.bundle_id:
        args.bundle_id = cfg.get("bundleId") or ""
    if not args.bundle_id:
        sys.exit("缺 --bundle-id；项目里也没有 project.yml / ship.config.json 可推断。")

    issuer = args.issuer or secrets.get("ASC_ISSUER_ID")
    key_id = args.key_id or secrets.get("ASC_KEY_ID")
    p8 = args.p8 or aa.find_p8()

    if not issuer:
        sys.exit("缺 Issuer ID。用 --issuer 传，或确认 cert/secrets.txt 里有 ASC_ISSUER_ID。")
    if not p8:
        sys.exit("找不到 .p8。用 --p8 指定，或放进 tools/ 目录。")
    if not key_id:
        key_id = aa.infer_key_id(p8)
    if not args.feedback_email:
        args.feedback_email = (cfg.get("feedbackEmail")
                               or secrets.get("ASC_FEEDBACK_EMAIL")
                               or "noreply@example.com")
    args.description = args.description or cfg.get("testflightDescription") or ""
    args.whats_new = args.whats_new or cfg.get("testflightWhatsNew") or ""

    token = aa.make_jwt(str(p8), key_id, issuer)
    app = get_app(aa, token, args.bundle_id)

    if args.action == "status":
        cmd_status(aa, token, app)
    else:
        cmd_attach(aa, token, app, args)


if __name__ == "__main__":
    main()
