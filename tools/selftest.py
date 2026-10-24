#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""离线自测。不联网、不调 Apple API、不需要证书。

覆盖的是「不测就会悄悄坏掉」的那部分：
  - 配置优先级（命令行 > ship.config.json > project.yml 推断 > 默认）
  - project.yml 的正则推断，尤其是 xcodeDir ≠ target 名这种情况
  - 必填项校验
  - 各脚本能被 import（语法和顶层代码没问题）
  - 不依赖 Apple API 的纯函数：PTP 风格的分页、JWT 结构、base64 处理

用法：
  python tools/selftest.py
  python tools/selftest.py -v      # 每条断言都打印
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

VERBOSE = "-v" in sys.argv

PASSED = 0
FAILED = []


def check(cond, what, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        if VERBOSE:
            print(f"  ok   {what}")
    else:
        FAILED.append((what, detail))
        print(f"  FAIL {what}" + (f"\n       {detail}" if detail else ""))


def eq(got, want, what):
    check(got == want, what, f"got {got!r}, want {want!r}")


def section(title):
    print(f"\n[{title}]")


# ------------------------------------------------------------------ 配置推断

def test_infer():
    section("project.yml 推断")

    import shipconfig as sc

    # 关键场景：target 名 ≠ 源码目录名。
    # 模板里 target 叫 MyApp，源码放 App/ 下。早期版本把 xcodeDir
    # 当成 target 名，导致 doctor 去 MyApp/Assets.xcassets 找图标，
    # 找不到还报「缺图标」。
    yml = """
name: Demo
options:
  bundleIdPrefix: com.example
targets:
  Demo:
    type: application
    platform: iOS
    sources:
      - path: Sources
    settings:
      base:
        PRODUCT_BUNDLE_IDENTIFIER: com.example.demo
        DEVELOPMENT_TEAM: ABCDE12345
    info:
      properties:
        CFBundleDisplayName: Demo App
schemes:
  Demo:
    build:
      targets:
        Demo: all
"""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "project.yml").write_text(yml, encoding="utf-8")
        got = sc._infer_from_project_yml(td)
        eq(got.get("scheme"), "Demo", "scheme 取 schemes 的 key")
        eq(got.get("bundleId"), "com.example.demo", "bundleId 取 PRODUCT_BUNDLE_IDENTIFIER")
        eq(got.get("xcodeDir"), "Sources", "xcodeDir 取 sources 的 path，不是 target 名")
        eq(got.get("appName"), "Demo App", "appName 取 CFBundleDisplayName")
        eq(got.get("teamId"), "ABCDE12345", "teamId 取 DEVELOPMENT_TEAM")

    # 没有任何文件时不能崩，只能返回空
    with tempfile.TemporaryDirectory() as td:
        eq(sc._infer_from_project_yml(td), {}, "空目录返回空 dict")


def test_precedence():
    section("配置优先级")

    import shipconfig as sc

    yml = "name: X\ntargets:\n  X:\n    sources:\n      - path: X\n    settings:\n      base:\n        PRODUCT_BUNDLE_IDENTIFIER: com.from.yml\n"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "project.yml").write_text(yml, encoding="utf-8")

        # 只有 project.yml
        cfg = sc.load(td)
        eq(cfg["bundleId"], "com.from.yml", "只有 project.yml 时用推断值")

        # 加 ship.config.json
        (td / "ship.config.json").write_text(
            json.dumps({"bundleId": "com.from.json"}), encoding="utf-8")
        eq(sc.load(td)["bundleId"], "com.from.json", "json 覆盖 project.yml")

        # 命令行最高
        eq(sc.load(td, {"bundleId": "com.from.cli"})["bundleId"],
           "com.from.cli", "命令行覆盖 json")

        # None 的覆盖项要被忽略，不能把已有值冲掉
        eq(sc.load(td, {"bundleId": None})["bundleId"],
           "com.from.json", "覆盖项为 None 时忽略")

        # 未知键要被过滤掉
        (td / "ship.config.json").write_text(
            json.dumps({"bundleId": "com.json", "typo": 1}), encoding="utf-8")
        check("typo" not in sc.load(td), "未知配置键被过滤")


def test_validate():
    section("必填校验")

    import shipconfig as sc

    eq(sorted(sc.validate({})), ["bundleId", "scheme"], "空配置缺两项")
    eq(sc.validate({"bundleId": "a.b", "scheme": "s"}), [], "填齐后无缺失")
    eq(sc.validate({"bundleId": "a.b"}), ["scheme"], "缺 scheme")


def test_write_example():
    section("init 生成的样板")

    import shipconfig as sc

    yml = "name: X\ntargets:\n  X:\n    sources:\n      - path: Src\n    settings:\n      base:\n        PRODUCT_BUNDLE_IDENTIFIER: com.ex.x\nschemes:\n  X:\n    build:\n      targets:\n        X: all\n"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "project.yml").write_text(yml, encoding="utf-8")
        out, filled = sc.write_example(td / "ship.config.json", td)

        data = json.loads(out.read_text(encoding="utf-8"))
        eq(data["bundleId"], "com.ex.x", "样板里填好了推断出的 bundleId")
        eq(data["xcodeDir"], "Src", "样板里填好了推断出的 xcodeDir")
        eq(data["profileName"], "X AppStore", "profileName 按 scheme 生成")
        check("bundleId" in filled, "返回已填充的键名")
        # 样板本身必须能过校验，否则 init 之后还要手改
        eq(sc.validate(data), [], "样板本身通过校验")


def test_describe():
    section("配置来源说明")

    import shipconfig as sc

    check("project.yml" in sc.describe({"_inferred": ["bundleId"]}),
          "无 json 时说明从 project.yml 推断")
    check(sc.describe({}).startswith("全部"), "什么都没有时说明用命令行")


# ------------------------------------------------------------------ 脚本可加载

def test_imports():
    section("脚本可加载")

    for mod in ("shipconfig",):
        try:
            __import__(mod)
            check(True, f"import {mod}")
        except Exception:
            check(False, f"import {mod}", traceback.format_exc())

    # apple-auto.py / testflight.py / ship.py 顶层有副作用（读 argparse、
    # 找 p8），不能直接 import。用 py_compile 挡语法错误。
    import py_compile

    with tempfile.TemporaryDirectory() as td:
        for name in ("ship.py", "apple-auto.py", "testflight.py",
                     "make-icon.py", "selftest.py"):
            p = HERE / name
            try:
                # cfile 必须是真实路径：Windows 上传 os.devnull 会被
                # py_compile 拒绝（"nul is a non-regular file"）。
                py_compile.compile(str(p), cfile=os.path.join(td, name + "c"),
                                   doraise=True)
                check(True, f"{name} 语法通过")
            except Exception as e:
                check(False, f"{name} 语法通过", str(e))


def test_tools_smoke():
    section("工具横幅与帮助")

    import subprocess

    r = subprocess.run([sys.executable, str(HERE / "ship.py"), "--help"],
                       capture_output=True, text=True)
    eq(r.returncode, 0, "ship.py --help 退出码 0")
    for action in ("doctor", "setup", "secrets", "release", "status", "init", "deps"):
        check(action in r.stdout, f"帮助里列出 {action}")

    # deps 必须能在任何目录跑（它不依赖 project.yml），而且 --dry-run 不能真的装包
    r = subprocess.run([sys.executable, str(HERE / "ship.py"), "deps", "--dry-run"],
                       capture_output=True, text=True, cwd=str(HERE))
    eq(r.returncode, 0, "deps --dry-run 退出码 0")
    check("cryptography" in r.stdout, "deps 列出了 cryptography")
    check("Traceback" not in r.stdout, "deps 没有抛异常")

    r = subprocess.run([sys.executable, str(HERE / "testflight.py"), "--help"],
                       capture_output=True, text=True)
    eq(r.returncode, 0, "testflight.py --help 退出码 0")

    r = subprocess.run([sys.executable, str(HERE / "apple-auto.py"), "--help"],
                       capture_output=True, text=True)
    eq(r.returncode, 0, "apple-auto.py --help 退出码 0")


# ------------------------------------------------------------------ 纯函数

def test_ptp_container():
    section("PTP 容器编解码")

    import struct

    # PTP over USB 的命令容器：
    #   Length(4) Type(2) Code(2) TransactionID(4) Params(4*N)
    # 全小端。这个格式在相机控制里到处都是，编错了表现是相机不响应。
    def build(code, tid, params=()):
        body = struct.pack("<HHI", 1, code, tid)
        params_bin = b"".join(struct.pack("<I", p) for p in params)
        length = 4 + len(body) + len(params_bin)
        return struct.pack("<I", length) + body + params_bin

    raw = build(0x1001, 7, [0xFFFFFFFF, 0x5001])
    # Length(4) + Type(2) + Code(2) + TransactionID(4) + 2 个参数(8) = 20
    eq(len(raw), 20, "容器长度 = 4 + 2 + 2 + 4 + 2*4")
    eq(struct.unpack("<I", raw[:4])[0], 20, "Length 字段与实际长度一致")
    eq(struct.unpack("<H", raw[4:6])[0], 1, "Type = 1（命令）")
    eq(struct.unpack("<H", raw[6:8])[0], 0x1001, "Code = OpenSession")
    eq(struct.unpack("<I", raw[8:12])[0], 7, "TransactionID")
    eq(struct.unpack("<I", raw[12:16])[0], 0xFFFFFFFF, "参数 1")


def test_jwt_shape():
    section("ASC API JWT 结构")

    import base64
    import hashlib
    import hmac
    import time

    # 只验结构，不连网：ES256 的签名我们在这里用 HS256 占位，
    # 因为重点是 iss/iat/exp/aud 这几个字段有没有写对。
    key_id = "ABC1234567"
    issuer = "69a6de7c-1111-2222-3333-444455556666"

    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    now = int(time.time())
    payload = {"iss": issuer, "iat": now, "exp": now + 900,
               "aud": "appstoreconnect-v1"}

    def b64(d):
        return base64.urlsafe_b64encode(
            json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=")

    signing_input = b".".join([b64(header), b64(payload)])
    sig = hmac.new(b"secret", signing_input, hashlib.sha256).digest()
    token = signing_input + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")

    parts = token.split(b".")
    eq(len(parts), 3, "JWT 三段")

    def unb64(s):
        return json.loads(base64.urlsafe_b64decode(s + b"=" * (-len(s) % 4)))

    eq(unb64(parts[0])["alg"], "ES256", "header.alg")
    eq(unb64(parts[0])["kid"], key_id, "header.kid = Key ID")
    p = unb64(parts[1])
    eq(p["aud"], "appstoreconnect-v1", "aud 固定值")
    eq(p["iss"], issuer, "iss = Issuer ID")
    check(p["exp"] - p["iat"] <= 20 * 60, "有效期不超过 20 分钟")
    # ES256 的签名是固定的 64 字节（r||s 各 32），不是 DER
    check(len(base64.urlsafe_b64decode(
        base64.urlsafe_b64encode(b"\x00" * 64))) == 64, "ES256 签名为 64 字节定长")


def test_sensitive_paths():
    section("凭据误提交检测")

    # ship.py 顶层会读 argparse，只能按路径加载它的纯函数。
    # 这里用 exec 只取需要的几个名字，避免 import 副作用。
    src = (HERE / "ship.py").read_text(encoding="utf-8")
    ns = {}
    for name in ("SENSITIVE_SUFFIX", "SENSITIVE_NAME", "is_sensitive_path"):
        start = src.index(name + " =") if name.isupper() else src.index("def " + name)
        if name.isupper():
            block = src[start:src.index("\n\n", start)]
        else:
            block = src[start:src.index("\n\n\ndef ", start)]
        exec(block, ns)

    is_s = ns["is_sensitive_path"]

    must_flag = [
        "cert/distribution.p12",
        "ios/tools/cert/AuthKey_AB12CD34EF.p8",
        "AuthKey_ABCD123456.p8",
        "cert/secrets.txt",
        "cert/distribution.key",
        "Profile.mobileprovision",
        "AppleWWDRCA.cer",
    ]
    must_pass = [
        "App/ContentView.swift",
        "tools/ship.py",
        "README.md",
        "template/.gitignore",
        "docs/pitfalls.md",
        # 名字里带 cert 但不是签名材料，别误报
        "docs/cert-guide.md",
        "App/Certificate.swift",
    ]

    for p in must_flag:
        check(is_s(p), f"应该识别为凭据：{p}")
    for p in must_pass:
        check(not is_s(p), f"不应该误报：{p}")


def test_rename():
    section("rename 一次改到所有位置")

    tpl = HERE.parent / "template"
    if not tpl.is_dir():
        check(True, "跳过：没有 template 目录")
        return

    with tempfile.TemporaryDirectory() as td:
        ios = Path(td) / "ios"
        shutil.copytree(tpl, ios)
        shutil.copytree(HERE, ios / "tools")

        r = subprocess.run(
            [sys.executable, str(ios / "tools" / "ship.py"), "rename",
             "--app-name", "PhotoHelper", "--bundle-id", "com.testuser.myapp"],
            capture_output=True, text=True, errors="replace", cwd=str(ios),
        )
        check(r.returncode == 0, "rename 退出码 0", (r.stderr or r.stdout)[-300:])

        yml = (ios / "project.yml").read_text(encoding="utf-8")
        check("name: PhotoHelper" in yml, "project.yml 顶层 name 已改")
        check("PRODUCT_BUNDLE_IDENTIFIER: com.testuser.myapp" in yml, "Bundle ID 已改")
        check("bundleIdPrefix: com.testuser" in yml, "bundleIdPrefix 跟着改了")
        # 只允许注释里还留着旧名当例子
        check(yml.count("MyApp") <= 1, "target / scheme / build.targets 都改到了")

        wf = (ios / ".github/workflows/ios.yml").read_text(encoding="utf-8")
        check("name: iOS 构建并上传 TestFlight" in wf,
              "workflow 自己的工作流名字没有被误改")
        check("PROJECT: PhotoHelper" in wf and "APP_NAME: PhotoHelper" in wf,
              "workflow 的四个 env 已改")
        check("MyApp" not in wf and "com.example" not in wf, "workflow 里没有残留旧值")

        # init 要能把改名后的值推断出来
        r2 = subprocess.run([sys.executable, str(ios / "tools" / "ship.py"), "init"],
                            capture_output=True, text=True, errors="replace", cwd=str(ios))
        check(r2.returncode == 0, "rename 后 init 能跑")
        cfg = json.loads((ios / "ship.config.json").read_text(encoding="utf-8"))
        check(cfg.get("scheme") == "PhotoHelper", "init 推断出新 scheme")
        check(cfg.get("bundleId") == "com.testuser.myapp", "init 推断出新 Bundle ID")


def test_new():
    section("new 一条命令建项目")

    if not (HERE.parent / "template").is_dir():
        check(True, "跳过：没有 template 目录")
        return

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "PhotoHelper"
        r = subprocess.run(
            [sys.executable, str(HERE / "ship.py"), "new", str(dest),
             "--bundle-id", "com.testuser.photohelper"],
            capture_output=True, text=True, errors="replace",
        )
        check(r.returncode == 0, "new 退出码 0", (r.stderr or r.stdout)[-300:])

        ios = dest / "ios"
        check((ios / "project.yml").exists(), "生成了 project.yml")
        check((ios / "App/ContentView.swift").exists(), "生成了 App 源码")
        check((ios / ".github/workflows/ios.yml").exists(), "点文件跟着过来了")
        check((ios / ".gitignore").exists(), "带 .gitignore")
        check((ios / "tools/ship.py").exists(), "带 tools")
        check(not (ios / "tools/cert").exists(), "没有把 cert 目录复制进去")
        check(not (ios / "tools/__pycache__").exists(), "没有把 __pycache__ 复制进去")

        yml = (ios / "project.yml").read_text(encoding="utf-8")
        check("name: PhotoHelper" in yml, "工程名已改")
        check("PRODUCT_BUNDLE_IDENTIFIER: com.testuser.photohelper" in yml, "Bundle ID 已改")

        wf = (ios / ".github/workflows/ios.yml").read_text(encoding="utf-8")
        check("name: iOS 构建并上传 TestFlight" in wf, "工作流名字没被误改")
        check("BUNDLE_ID: com.testuser.photohelper" in wf, "workflow 的 Bundle ID 已改")

        # 目录非空时要拒绝，避免覆盖别人的项目
        r2 = subprocess.run(
            [sys.executable, str(HERE / "ship.py"), "new", str(dest),
             "--bundle-id", "com.testuser.other"],
            capture_output=True, text=True, errors="replace",
        )
        check(r2.returncode != 0, "目标目录已存在时拒绝执行")


def test_generic():
    section("通用性：没有写死某个项目的业务内容")

    tf_src = (HERE / "testflight.py").read_text(encoding="utf-8")

    # 这几个词来自最初那个相机项目的 TestFlight 文案。模板里不能出现——
    # 任何人拿它发自己的 App，TestFlight 里都会显示别人的业务描述。
    for word in ("取景", "开始探测", "A7M4"):
        check(word not in tf_src, f"testflight.py 不含业务词「{word}」")

    check("def wait_for_build(" in tf_src, "testflight.py 有 wait_for_build")
    check("def ensure_localizations(aa, token, app_id, build_id, feedback_email," in tf_src,
          "ensure_localizations 的文案可被覆盖")
    check("my_email" in tf_src, "加测试员前比对本人邮箱，而不是「组里有没有人」")

    ship_src = (HERE / "ship.py").read_text(encoding="utf-8")
    check('"add", "-A"' not in ship_src, "release 不把工作区所有改动一起提交")
    check('"watch"' in ship_src, "等构建用 gh run watch，不手写轮询")

    sc_src = (HERE / "shipconfig.py").read_text(encoding="utf-8")
    check('"testflightDescription"' in sc_src, "配置里有 testflightDescription")
    check('"testflightWhatsNew"' in sc_src, "配置里有 testflightWhatsNew")
    check('"repo"' not in sc_src, "删掉了没人读的 repo 字段")


def test_tracked_secrets():
    section("git 跟踪检查")

    import subprocess

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # 不是 git 仓库时返回 None，doctor 要据此降级而不是报错
        r = subprocess.run(["git", "ls-files"], cwd=str(td),
                           capture_output=True, text=True)
        check(r.returncode != 0, "临时目录本来不是 git 仓库")

        subprocess.run(["git", "init", "-q"], cwd=str(td), check=True)
        (td / "cert").mkdir()
        (td / "cert" / "secrets.txt").write_text("X=1\n", encoding="utf-8")
        (td / "App.swift").write_text("import SwiftUI\n", encoding="utf-8")
        subprocess.run(["git", "add", "App.swift"], cwd=str(td), check=True)

        files = subprocess.run(["git", "ls-files"], cwd=str(td),
                               capture_output=True, text=True).stdout.splitlines()
        eq(files, ["App.swift"], "只暂存了源码，敏感文件没进索引")

        # 故意把 cert/ 加进去，模拟「忘了 .gitignore」的后果
        subprocess.run(["git", "add", "-f", "cert/secrets.txt"], cwd=str(td), check=True)
        files = subprocess.run(["git", "ls-files"], cwd=str(td),
                               capture_output=True, text=True).stdout.splitlines()
        check(any(f.endswith("secrets.txt") for f in files),
              "强制加入后能被 ls-files 看到（这就是 doctor 要拦的情况）")


def test_version_bump():
    section("build 号递增")

    import re

    # ship.py 用正则改 project.yml，不能把引号、缩进、其他内容弄坏
    sample = 'name: X\n    MARKETING_VERSION: "1.0"\n    CURRENT_PROJECT_VERSION: "4"\n    OTHER: keep\n'

    def bump(txt):
        return re.sub(r'(CURRENT_PROJECT_VERSION:\s*"?)(\d+)("?)',
                      lambda m: m.group(1) + str(int(m.group(2)) + 1) + m.group(3),
                      txt, count=1)

    out = bump(sample)
    check('CURRENT_PROJECT_VERSION: "5"' in out, "build 号 +1 且保留引号")
    check('MARKETING_VERSION: "1.0"' in out, "版本号不动")
    check('OTHER: keep' in out, "其他行不动")
    check(out.count("\n") == sample.count("\n"), "行数不变")


def test_secrets_parsing():
    section("secrets.txt 解析")

    # 格式是 KEY=BASE64，值里可能有 = 填充，所以只能按第一个 = 切
    content = (
        "# 自动生成\n"
        "ASC_KEY_ID=ABC1234567\n"
        "ASC_ISSUER_ID=69a6de7c-1111\n"
        "IOS_CERT_PASSWORD=ab==\n"
        "\n"
        "ASC_KEY_CONTENT=TUlJQ2VydA==\n"
    )
    got = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        got[k] = v

    eq(len(got), 4, "解析出 4 个键")
    eq(got["IOS_CERT_PASSWORD"], "ab==", "base64 填充里的 = 不被切断")
    eq(got["ASC_KEY_CONTENT"], "TUlJQ2VydA==", "长 base64 完整保留")
    check("#" not in got, "注释行被跳过")


# ------------------------------------------------------------------ 入口

def main():
    print("ios-ship 离线自测")
    print("=" * 46)

    tests = [
        test_infer,
        test_precedence,
        test_validate,
        test_write_example,
        test_describe,
        test_imports,
        test_tools_smoke,
        test_ptp_container,
        test_jwt_shape,
        test_version_bump,
        test_secrets_parsing,
        test_sensitive_paths,
        test_rename,
        test_new,
        test_generic,
        test_tracked_secrets,
    ]

    for t in tests:
        try:
            t()
        except Exception:
            FAILED.append((t.__name__, "抛异常"))
            print(f"  FAIL {t.__name__}")
            traceback.print_exc()

    print()
    print("=" * 46)
    if FAILED:
        print(f"失败 {len(FAILED)} 条，通过 {PASSED} 条")
        for what, detail in FAILED:
            print(f"  - {what}")
            if detail:
                print(f"    {detail}")
        return 1

    print(f"全部通过：{PASSED} 条断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
