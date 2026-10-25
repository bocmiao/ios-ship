#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ship.py —— 没有 Mac 也能把 iOS App 发到 TestFlight 的统一入口。

---

## 这东西解决什么问题

把一个 iOS App 从「代码写好」送到「手机上能装」，中间有一长串 Apple 后台操作：
申请证书、生成 p12、建 Bundle ID、建描述文件、写 CI secrets、上传构建、
建测试组、分配 build、加测试员、补 TestFlight 文案……

其中大部分只能靠点后台，而且每一步都在不同的页面、用不同的术语。
做过一遍之后全忘光，下次换项目再来一遍。

这个脚本把整条链路固化下来，一条命令跑完。

## 十个子命令

    ship.py new        从 template 建一个新项目（复制 + 改名一次做完）
    ship.py rename     一次改完工程名与 Bundle ID（11 处，手工改必漏）
    ship.py doctor     全面体检，只读，不改变任何东西
    ship.py setup      首次配置：证书 + p12 + Bundle ID + 描述文件 + secrets.txt
    ship.py secrets    把 secrets.txt 里的值写进 GitHub 仓库
    ship.py release    发版：build 号 +1 → 推送 → 触发构建 → 等 VALID → 挂进测试组
    ship.py logs       把失败流水线的日志压到能读的长度
    ship.py status     分发状态速查（比 doctor 短）
    ship.py init       生成 ship.config.json（可选，能从 project.yml 推断）
    ship.py deps       检查/安装依赖

建议顺序：`new`（或 `rename`）→ `deps` → `init` → `doctor` → `setup`（仅首次）→ `secrets`（仅首次）→ `release`。失败后跑 `logs`。

## 为什么要有 doctor

iOS 这条链最气人的地方是：**失败总是发生在最后一步**。
编译过了、签名过了、IPA 导出了，然后上传时 Apple 说版本号不对。
或者上传成功了，TestFlight 里却是空的——因为没分配测试组。

这些都是「前面全绿、最后一秒才炸」。doctor 的意义就是把最后一秒的失败
提前到第一秒，并且直接告诉你怎么修。

## 复用别人的证书

同一份 Apple 开发者账号下，**一张 Distribution 证书可以签无数个 App**（团队最多 2 张）。
所以新项目不用重新申请证书，用 `--reuse-cert` 指向旧项目的 cert 目录即可：

    ship.py setup --reuse-cert ../old-project/tools/cert

脚本会比对公钥，确认私钥和证书真的是配对的，不配对直接拒绝，不会导出一个
永远签不过的 p12。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Windows 控制台默认按 GBK(cp936) 解码管道输出，而 Python 3 往管道写 UTF-8，
# 结果中文全变「鏈湴鏉愭枡」这种乱码。这里强制 stdout/stderr 走 UTF-8，
# 配合 ship.cmd 里的 `chcp 65001` 一起用。
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

HERE = Path(__file__).resolve().parent
IOS_DIR = HERE.parent                      # ios/ 项目根目录


# ------------------------------------------------------------------ 模块加载

def _load_module(alias, filename):
    """加载同目录下的脚本。

    文件名里有连字符（apple-auto.py），不能直接 import，只能走 spec。
    """
    import importlib.util
    path = HERE / filename
    if not path.exists():
        sys.exit(f"缺少 {filename}，它必须和 ship.py 放在同一目录。")
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


aa = _load_module("apple_auto", "apple-auto.py")      # JWT / HTTP / p8 查找
tf = _load_module("testflight_tools", "testflight.py")  # 测试组 / 分发
sc = _load_module("ship_config", "shipconfig.py")     # 项目配置


# ------------------------------------------------------------------ 终端输出

class C:
    """终端配色。Windows 的 Git Bash 和现代 cmd 都支持 ANSI。"""
    G = "\033[32m"
    Y = "\033[33m"
    R = "\033[31m"
    B = "\033[36m"
    D = "\033[90m"
    X = "\033[0m"

    @classmethod
    def off(cls):
        for k in ("G", "Y", "R", "B", "D", "X"):
            setattr(cls, k, "")


if os.name == "nt" and not os.environ.get("WT_SESSION"):
    # 老 cmd 不支持 ANSI，不去污染输出
    if not os.environ.get("TERM"):
        C.off()


def h(title):
    print(f"\n{C.B}[{title}]{C.X}")


def ok(msg, detail=""):
    print(f"  {C.G}OK{C.X}   {msg}" + (f"  {C.D}{detail}{C.X}" if detail else ""))


def warn(msg, detail=""):
    print(f"  {C.Y}!!{C.X}   {msg}" + (f"  {C.D}{detail}{C.X}" if detail else ""))


def bad(msg, detail=""):
    print(f"  {C.R}XX{C.X}   {msg}" + (f"  {C.D}{detail}{C.X}" if detail else ""))


def note(msg):
    print(f"       {C.D}{msg}{C.X}")


# ------------------------------------------------------------------ 小工具

def gh(*args, check=False):
    """调 gh CLI。返回 (returncode, stdout, stderr)。

    注意 errors='replace'：gh 在 Windows 上偶尔吐出非 UTF-8 字节，
    不兜住会直接抛 UnicodeDecodeError，把真正的错误信息盖掉。
    """
    try:
        r = subprocess.run(["gh"] + list(args), capture_output=True, text=True,
                           errors="replace", cwd=str(IOS_DIR))
        if check and r.returncode != 0:
            sys.exit(f"gh 命令失败：gh {' '.join(args)}\n{r.stderr}")
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", "找不到 gh 命令（GitHub CLI 没装或不在 PATH 里）"


def git(*args):
    """调 git。返回 (returncode, stdout, stderr)。

    cwd 用 IOS_DIR 而不是仓库根：ios/ 可能只是大仓库的一个子目录，
    在这里跑 `git ls-files` 只会列出 ios/ 下的文件，正好是我们关心的范围。
    """
    try:
        r = subprocess.run(["git"] + list(args), capture_output=True, text=True,
                           errors="replace", cwd=str(IOS_DIR))
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", "找不到 git 命令"


# 哪些文件名一旦被 git 跟踪就必须立刻处理。
# 判断只看路径和扩展名，不读内容——读内容会把密码读进内存，没必要。
SENSITIVE_SUFFIX = (".p8", ".p12", ".key", ".mobileprovision", ".cer")
SENSITIVE_NAME = ("secrets.txt", "AuthKey.p8")


def is_sensitive_path(path):
    name = path.rsplit("/", 1)[-1]
    if name in SENSITIVE_NAME:
        return True
    if name.startswith("AuthKey_") and name.endswith(".p8"):
        return True
    return path.lower().endswith(SENSITIVE_SUFFIX)


def find_tracked_secrets():
    """返回被 git 跟踪的敏感文件列表；不是 git 仓库时返回 None。"""
    rc, out, _ = git("ls-files")
    if rc != 0:
        return None
    return [line for line in out.splitlines()
            if line.strip() and is_sensitive_path(line.strip())]


def read_pyproject_version():
    """从 project.yml 读出版本号。返回 (marketing, build) 或 (None, None)。"""
    p = IOS_DIR / "project.yml"
    if not p.exists():
        return None, None
    txt = p.read_text(encoding="utf-8")
    m = re.search(r'MARKETING_VERSION:\s*"?([\w.]+)"?', txt)
    b = re.search(r'CURRENT_PROJECT_VERSION:\s*"?(\d+)"?', txt)
    return (m.group(1) if m else None), (int(b.group(1)) if b else None)


def bump_build_number():
    """把 CURRENT_PROJECT_VERSION 加一，返回新值。

    上传不可撤销，占掉的 build 号拿不回来，所以每次发版必须换号。
    这一步以前靠人记，忘一次就要多等一轮构建。
    """
    p = IOS_DIR / "project.yml"
    txt = p.read_text(encoding="utf-8")
    m = re.search(r'(CURRENT_PROJECT_VERSION:\s*"?)(\d+)("?)', txt)
    if not m:
        sys.exit("project.yml 里没找到 CURRENT_PROJECT_VERSION。")
    old = int(m.group(2))
    new = old + 1
    txt = txt[:m.start()] + f'{m.group(1)}{new}{m.group(3)}' + txt[m.end():]
    p.write_text(txt, encoding="utf-8")
    return old, new


def resolve_p8(args, secrets, cert_dir):
    """找一个能用的 .p8，返回路径或 (None, 原因)。

    四级兜底，因为这一步最容易卡住人：
      1. 命令行 --p8
      2. cert_dir 及其上一级目录里的 AuthKey_*.p8（脚本和材料常分开放）
      3. apple-auto.py 的常规搜索（cwd / tools / 下载 / 桌面）
      4. **从 secrets.txt 里的 ASC_KEY_CONTENT 反解**——它本来就是 p8 的 base64，
         存过 secrets 就一定有。这样即使原始 .p8 丢了也能继续用。
    """
    if args.p8:
        p = Path(args.p8)
        return (str(p), None) if p.exists() else (None, f"--p8 指向的文件不存在：{p}")

    if cert_dir:
        for d in (Path(cert_dir), Path(cert_dir).parent):
            if d.is_dir():
                hits = sorted(d.glob("AuthKey_*.p8"))
                if hits:
                    return str(hits[0]), None

    found = aa.find_p8()
    if found:
        return str(found), None

    # 最后一招：从 secrets 里解出来
    b64 = secrets.get("ASC_KEY_CONTENT")
    if b64:
        import base64
        import tempfile
        try:
            raw = base64.b64decode(b64)
            if b"PRIVATE KEY" in raw:
                fd, tmp = tempfile.mkstemp(prefix="AuthKey_", suffix=".p8")
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                os.chmod(tmp, 0o600)
                return tmp, None
        except Exception:
            pass

    return None, ("找不到 AuthKey_*.p8，secrets.txt 里也没有可用的 ASC_KEY_CONTENT")


def api_token(args):
    """签一个 JWT。凭据来源优先级：参数 > cert_dir 里的 secrets.txt。"""
    secrets = tf.read_secrets(args.cert_dir or None)
    cert_dir = args.cert_dir or None
    p8, why = resolve_p8(args, secrets, cert_dir)
    issuer = args.issuer or secrets.get("ASC_ISSUER_ID")
    key_id = args.key_id or secrets.get("ASC_KEY_ID")
    if not p8:
        return None, secrets, why
    if not issuer:
        return None, secrets, "缺 Issuer ID（cert/secrets.txt 里没有 ASC_ISSUER_ID，也没传 --issuer）"
    if not key_id:
        key_id = aa.infer_key_id(str(p8))
    if not key_id:
        return None, secrets, "缺 Key ID"
    return aa.make_jwt(str(p8), key_id, issuer), secrets, None


# ------------------------------------------------------------------ doctor

class Report:
    """收集体检结果，最后统一汇总。"""

    def __init__(self):
        self.blocks = 0
        self.warnings = []
        self.blockers = []

    def start(self, title):
        self.blocks += 1
        h(f"{self.blocks}. {title}")

    def ok(self, msg, detail=""):
        ok(msg, detail)

    def warn(self, msg, detail="", fix=""):
        warn(msg, detail)
        # 同一件事会在多个分组里被检查到（比如缺 p8，第 1 组和第 2 组都会发现，
        # 但给的修复提示角度不同）。按消息文本去重，保留第一次的提示。
        if not any(m == msg for m, _ in self.warnings):
            self.warnings.append((msg, fix))

    def bad(self, msg, detail="", fix=""):
        bad(msg, detail)
        if not any(m == msg for m, _ in self.blockers):
            self.blockers.append((msg, fix))

    def summary(self):
        print()
        if not self.blockers and not self.warnings:
            print(f"{C.G}全部通过，可以直接发版。{C.X}")
            return 0
        if self.blockers:
            print(f"{C.R}阻塞项 {len(self.blockers)} 个（不修就跑不通）：{C.X}")
            for i, (msg, fix) in enumerate(self.blockers, 1):
                print(f"  {i}. {msg}")
                if fix:
                    for line in fix.splitlines():
                        print(f"     {C.Y}→ {line}{C.X}")
        if self.warnings:
            print(f"{C.Y}提醒 {len(self.warnings)} 个（能跑，但迟早出事）：{C.X}")
            for i, (msg, fix) in enumerate(self.warnings, 1):
                print(f"  {i}. {msg}")
                if fix:
                    for line in fix.splitlines():
                        print(f"     {C.D}→ {line}{C.X}")
        return 1 if self.blockers else 0


def cmd_doctor(args):
    rep = Report()
    state = {}

    # ---------- 1. 本地材料
    rep.start("本地材料")
    secrets = tf.read_secrets(args.cert_dir or None)
    cert_dir = Path(args.cert_dir) if args.cert_dir else (HERE / "cert")
    note(f"证书材料查找位置：{cert_dir}")

    p8, p8_why = resolve_p8(args, secrets, cert_dir)
    if p8:
        rep.ok("API Key", Path(p8).name)
        state["p8"] = str(p8)
    else:
        rep.bad(p8_why or "找不到 AuthKey_*.p8",
                fix="去 appstoreconnect.apple.com → 用户和访问 → 集成 → App Store Connect API → 团队密钥\n"
                    "建完立刻下载 .p8（离开页面就再也下不了），放到 cert 目录或 --p8 指定")

    chain_files = [("distribution.key", "私钥"),
                   ("distribution.cer", "证书"),
                   ("distribution.p12", "p12")]
    missing_chain = []
    for f, label in chain_files:
        p = cert_dir / f
        if p.exists():
            rep.ok(label, f"{f}  {p.stat().st_size} 字节")
        else:
            missing_chain.append(f)
    if missing_chain:
        # 三件套是一次性生成的，拆成三条阻塞项只会让第一次体检看起来更糟。
        rep.bad(f"证书链没生成（缺 {' / '.join(missing_chain)}）",
                fix="跑 `ship.py setup` 一次生成私钥、证书、p12")

    if secrets:
        rep.ok("secrets.txt", f"{len(secrets)} 个键")
    else:
        rep.warn("没有 cert/secrets.txt", fix="跑 `ship.py setup` 生成")

    # 凭据有没有被 git 跟踪。
    # 这一条比「本地文件在不在」重要得多：文件在本地只是方便，进了 Git
    # 就是永久记录——删掉文件、加 .gitignore 都改不了已经提交的历史。
    tracked = find_tracked_secrets()
    if tracked is None:
        # 第 7 组会再遇到一次；两处用同一句 msg，靠 Report 的去重合成一条
        rep.warn("这个目录不是 git 仓库", "跳过「凭据是否误提交」检查",
                 fix="`release` 要 commit + push，得先 git init 或 clone")
    elif tracked:
        rep.bad(f"{len(tracked)} 个凭据文件已经被 git 跟踪",
                fix="\n".join(tracked[:5])
                    + ("\n..." if len(tracked) > 5 else "")
                    + "\n立刻执行：git rm --cached <上面这些路径>"
                    + "\n然后确认 .gitignore 里有 cert/ 、*.p8 、*.p12 、secrets.txt"
                    + "\n如果推过公共仓库，按 docs/pitfalls.md 第 17 条评估要不要轮换密码")
    else:
        rep.ok("凭据没有进 Git")

    mv, bv = read_pyproject_version()
    if mv and bv is not None:
        rep.ok("project.yml 版本", f"{mv} (build {bv})")
        state["marketing"], state["build"] = mv, bv
    else:
        rep.bad("project.yml 读不出版本号")

    # 图标——缺了上传时才报 90713，很容易拖到最后
    xcode_dir = args.cfg.get("xcodeDir") or args.cfg.get("scheme")
    icon = (IOS_DIR / xcode_dir / "Assets.xcassets" / "AppIcon.appiconset") if xcode_dir else None
    if icon and icon.is_dir() and any(icon.glob("*.png")):
        rep.ok("App 图标")
    elif icon and icon.is_dir():
        rep.warn("AppIcon.appiconset 目录是空的",
                 fix="放一张 1024×1024 无 alpha 的 png 进去，或跑 `python tools/make-icon.py`")
    else:
        rep.warn("没找到 App 图标",
                 fix="上传时才会报 90713 Missing CFBundleIconName / 90022 Missing 120x120\n"
                     "需要 <源码目录>/Assets.xcassets/AppIcon.appiconset/ 和 project.yml 里的\n"
                     "ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon（这一行不能少）")

    # ---------- 2. 凭据与账号
    rep.start("凭据与账号")
    # make_jwt 里一 import 就抛 ModuleNotFoundError，把「装个包」这件事
    # 变成一段谁也看不懂的堆栈。先拦下来，后面几组都要用它，直接收尾。
    if not check_dep("import cryptography"):
        rep.bad("缺 python 包 cryptography",
                fix="跑 `python tools/ship.py deps`\n"
                    "它负责证书申请与 API 鉴权（生成 CSR、签 ES256 JWT），\n"
                    "缺了它第 2 组之后的检查都跑不了")
        return rep.summary()
    token, secrets, err = api_token(args)
    if err:
        rep.bad(err, fix="确认 cert/secrets.txt 里有 ASC_KEY_ID / ASC_ISSUER_ID")
        rep.summary()
        return 1

    st, r = aa.api("GET", "/v1/bundleIds?limit=1", token)
    if st == 401:
        rep.bad("API Key 被拒（401）",
                fix="可能用了推送密钥；或 Key ID / Issuer ID 抄错；或密钥已吊销")
        rep.summary()
        return 1
    if st == 403:
        rep.bad("API Key 权限不足（403）",
                fix="建密钥时 Access 要选 App Manager 或 Admin；且必须是团队密钥，不是个人密钥")
        rep.summary()
        return 1
    if st != 200:
        rep.bad(f"鉴权请求失败 HTTP {st}", aa.api_err(r))
        rep.summary()
        return 1
    rep.ok("JWT 签名与鉴权")

    bundle_id = args.bundle_id
    st, r = aa.api("GET", f"/v1/bundleIds?filter[identifier]={bundle_id}", token)
    ids = r.get("data", []) if st < 300 else []
    bundle_ok = bool(ids)
    if bundle_ok:
        rep.ok("Bundle ID 已注册", bundle_id)
    else:
        # 不在这里 return：后面「证书 / 描述文件 / GitHub」三组不依赖它，
        # 提前返回的话用户要改一处跑一次，改三处就得跑四轮。
        rep.bad(f"Bundle ID 未注册：{bundle_id}",
                fix="跑 `ship.py setup` 会自动建；或先在后台 Identifiers 里建一个")

    # ---------- 3. App 记录
    rep.start("App 记录")
    app = None
    if not bundle_ok:
        note("跳过：Bundle ID 还没注册，不会有 App 记录")
    else:
        st, r = aa.api("GET", f"/v1/apps?filter[bundleId]={bundle_id}", token)
        apps = r.get("data", []) if st < 300 else []
        if apps:
            app = apps[0]
            app_name = app["attributes"]["name"]
            state["app"] = app
            rep.ok("App 记录存在", f"{app_name}  App ID {app['id']}")

            if re.search(r"\b(sony|canon|nikon|fujifilm|panasonic|leica|apple)\b",
                         app_name, re.I):
                rep.warn(f"App 名含第三方商标：{app_name}",
                         fix="内部 TestFlight 不受影响，但外部测试和上架会被 App Review 拒（指南 5.2.1）。\n"
                             "只需在后台改「App 信息 → 名称」，不耗 build 号、不用重新构建。")
        else:
            rep.bad("App Store Connect 里没有这个 App 的记录",
                    fix="Apple 没有开放创建 App 记录的 API，只能手动建一次：\n"
                        "appstoreconnect.apple.com → 我的 App → + → 新建 App\n"
                        "名称 / 主要语言 / Bundle ID 选上面那个 / SKU 随便填一个唯一值")

    # ---------- 4. 证书
    rep.start("证书")
    st, r = aa.api("GET", "/v1/certificates?limit=50", token)
    certs = r.get("data", []) if st < 300 else []
    dist = [c for c in certs if "DISTRIBUTION" in (c["attributes"].get("certificateType") or "")]
    if not dist:
        rep.bad("账号下没有 Distribution 证书", fix="跑 `ship.py setup` 申请一张")
    else:
        for c in dist:
            exp = (c["attributes"].get("expirationDate") or "")[:10]
            rep.ok(c["attributes"].get("displayName") or c["attributes"].get("name"),
                   f"到期 {exp}")
        if len(dist) >= 2:
            rep.warn(f"已有 {len(dist)} 张 Distribution 证书（团队上限 2 张）",
                     fix="新项目请用 --reuse-cert 复用，别再申请，否则名额用光只能吊销")

    # ---------- 5. 描述文件
    rep.start("描述文件")
    st, r = aa.api("GET", f"/v1/profiles?filter[profileType]=IOS_APP_STORE&limit=50", token)
    profs = r.get("data", []) if st < 300 else []
    mine = [p for p in profs if (p["attributes"].get("name") or "").startswith(args.profile.split()[0])]
    if mine:
        for p in mine:
            exp = (p["attributes"].get("expirationDate") or "")[:10]
            rep.ok(p["attributes"]["name"], f"到期 {exp}")
    else:
        rep.warn(f"没找到匹配「{args.profile}」的描述文件",
                 fix="跑 `ship.py setup` 生成；若 CI 里用的是别的名字，用 --profile 指定")

    # ---------- 6. 构建与分发
    rep.start("构建与分发")
    if app is None:
        note("跳过：这一组依赖 App 记录，上面那条建完再跑一次")
    else:
        st, r = aa.api("GET", f"/v1/builds?filter[app]={app['id']}&sort=-version&limit=5", token)
        builds = r.get("data", []) if st < 300 else []
        if not builds:
            rep.warn("还没有任何构建", fix="跑 `ship.py release`")
        else:
            latest = builds[0]
            ver = latest["attributes"]["version"]
            ps = latest["attributes"]["processingState"]
            rep.ok(f"最新 build {ver}", ps) if ps == "VALID" else rep.warn(f"最新 build {ver}", ps)

            if bv is not None and ver == str(bv):
                rep.ok("build 号已用掉", f"project.yml 现在是 {bv}，就是这一个")
                rep.warn(f"下次发版会撞号：build {bv} 已经上传过，且上传不可撤销",
                         fix="跑 `ship.py release`，它会自动 +1")
            elif bv is not None and int(ver) > bv:
                rep.bad(f"project.yml 的 build 号（{bv}）比线上最新的（{ver}）还小",
                        fix="上传会被拒：bundle version must be higher than previously uploaded")
            else:
                rep.ok("build 号可用", f"下一个将是 {bv + 1 if bv else '?'}")

            # 测试组是「上传成功但装不上」的元凶
            st, g = aa.api("GET", f"/v1/betaGroups?filter[app]={app['id']}&limit=20", token)
            groups = g.get("data", []) if st < 300 else []
            internal = [x for x in groups if x["attributes"].get("isInternalGroup")]
            if not internal:
                rep.bad("没有任何内部测试组 —— TestFlight 里会是空的",
                        fix="跑 `ship.py release`，或 `testflight.py attach`")
            else:
                for grp in internal:
                    gd, gb, gt = tf.group_detail(aa, token, grp["id"])
                    vers = [x["attributes"]["version"] for x in gb]
                    mails = [x["attributes"].get("email") for x in gt]
                    if vers:
                        rep.ok(f"内部组「{grp['attributes']['name']}」", f"含 build {vers}")
                    else:
                        rep.warn(f"内部组「{grp['attributes']['name']}」是空的",
                                 fix="跑 `ship.py release` 把 build 挂进去")
                    if mails:
                        rep.ok("测试员", str(mails))
                    else:
                        rep.warn("内部组里没有测试员",
                                 fix="`testflight.py attach` 会把你自己的账号加进去")

            st, bl = aa.api("GET", f"/v1/betaBuildLocalizations?filter[build]={latest['id']}", token)
            if st < 300 and bl.get("data"):
                rep.ok("TestFlight 测试文案", f"{len(bl['data'])} 条")
            else:
                rep.warn("build 没有「本次测试内容」",
                         fix="TestFlight 里会显示空白，不影响安装；`testflight.py attach` 会补")

    # ---------- 7. GitHub
    rep.start("GitHub 仓库")
    rc, out, err = gh("secret", "list")
    if rc == 127:
        rep.warn("没装 gh CLI", fix="或者跳过——不影响构建，只影响从命令行写 secrets")
    elif "not a git repository" in err.lower():
        rep.warn("这个目录不是 git 仓库",
                 fix="跳过 secrets 检查；`release` 要 commit + push，得先 git init 或 clone")
    elif rc != 0:
        rep.warn("读不到仓库 secrets", err.strip()[:120])
    else:
        have = {line.split()[0] for line in out.splitlines() if line.strip()}
        need = ["IOS_CERT_P12_BASE64", "IOS_CERT_PASSWORD", "IOS_PROFILE_BASE64",
                "IOS_PROFILE_NAME", "APPLE_TEAM_ID", "ASC_KEY_ID", "ASC_ISSUER_ID",
                "ASC_KEY_CONTENT"]
        miss = [x for x in need if x not in have]
        if miss:
            rep.bad(f"缺 {len(miss)} 个 secrets：{', '.join(miss)}",
                    fix="跑 `ship.py secrets`")
        else:
            rep.ok(f"{len(need)} 个 secrets 都在")

    rc, out, _ = gh("run", "list", "--workflow=ios.yml", "--limit", "1",
                    "--json", "databaseId,status,conclusion")
    if rc == 0 and out.strip():
        try:
            last = json.loads(out)[0]
            rep.ok("最近一次上传流水线", f"{last['status']} / {last.get('conclusion') or '-'}")
        except Exception:
            pass

    return rep.summary()


# ------------------------------------------------------------------ setup

def cmd_setup(args):
    """转调 apple-auto.py。它已经把整条证书链做完了，这里只负责把参数对齐。"""
    # --cert-dir 要真的生效：doctor 用它找材料，setup 却写死 tools/cert 的话，
    # 用户指定了目录会两边对不上（材料写进 A、体检去 B 找）。
    out_dir = Path(args.cert_dir) if args.cert_dir else (HERE / "cert")
    argv = [
        sys.executable, str(HERE / "apple-auto.py"),
        "--bundle-id", args.bundle_id,
        "--profile", args.profile,
        "--out", str(out_dir),
    ]
    if args.p8:
        argv += ["--p8", args.p8]
    if args.key_id:
        argv += ["--key-id", args.key_id]
    if args.issuer:
        argv += ["--issuer-id", args.issuer]
    if args.reuse_cert:
        argv += ["--reuse-cert", args.reuse_cert]
    if args.force:
        argv += ["--force"]
    print(f"{C.D}→ {' '.join(argv[1:])}{C.X}")
    return subprocess.call(argv, cwd=str(IOS_DIR))


# ------------------------------------------------------------------ secrets

def cmd_secrets(args):
    """把 cert/secrets.txt 写进 GitHub 仓库 secrets。

    用 `gh secret set` 一次性写，免去在网页上一个个复制粘贴——
    八个值里有三个是几千字符的 base64，手动贴极容易漏字符。
    """
    secrets = tf.read_secrets(args.cert_dir or None)
    if not secrets:
        bad("没有 cert/secrets.txt，先跑 `ship.py setup`")
        return 1

    rc, out, _ = gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
    if rc != 0:
        bad("当前目录不是 GitHub 仓库，或 gh 没登录")
        note("先 `gh auth login`，并确认 remote 指向正确仓库")
        return 1
    repo = out.strip()
    print(f"\n目标仓库：{repo}\n")

    # secrets.txt 里除注释外全是 KEY=VALUE，直接整份灌进去
    failed = []
    for key, val in secrets.items():
        if args.dry_run:
            print(f"  {C.D}(dry-run){C.X} {key}  {len(val)} 字符")
            continue
        rc, _, err = gh("secret", "set", key, "--body", val)
        if rc == 0:
            ok(key, f"{len(val)} 字符")
        else:
            bad(key, err.strip()[:100])
            failed.append(key)

    if args.dry_run:
        print(f"\n{C.D}dry-run，没有真的写入。{C.X}")
        return 0
    if failed:
        bad(f"{len(failed)} 个写入失败：{', '.join(failed)}")
        return 1
    print(f"\n{C.G}全部写入完成。{C.X} 去 Actions 页面手动跑一次 `ios.yml` 验证。")
    return 0


# ------------------------------------------------------------------ rename

# 名字在三个文件里散成 11 处：project.yml 里 7 处、两个 workflow 里 4 处。
# 手工改必漏，而漏了不会立刻报错——要到 CI 的 archive 阶段才说找不到 scheme。
#
# 两个文件用不同的规则：workflow 顶部的 `name:` 是工作流名字，
# 不是工程名，不能跟着改。
_PROJECT_YML_RULES = [
    (r"^(name:\s*)\S+", "name"),                          # 顶层，.xcodeproj 的名字
    (r"(bundleIdPrefix:\s*)\S+", "prefix"),
    (r"(PRODUCT_BUNDLE_IDENTIFIER:\s*)\S+", "bundle"),
    (r"(CFBundleDisplayName:\s*)\S+", "name"),
]
_WORKFLOW_RULES = [
    (r"(\bPROJECT:\s*)\S+", "name"),
    (r"(\bSCHEME:\s*)\S+", "name"),
    (r"(\bAPP_NAME:\s*)\S+", "name"),
    (r"(\bBUNDLE_ID:\s*)\S+", "bundle"),
]


def _rewrite_names(ios_dir, old, new, new_bundle, dry_run=False):
    """改 project.yml 与两个 workflow 里的名字。

    返回 [(相对路径, [(行号, 旧行, 新行), ...]), ...]。
    """
    ios_dir = Path(ios_dir)
    prefix = ".".join(new_bundle.split(".")[:-1]) or new_bundle
    result = []

    for rel in ("project.yml", ".github/workflows/ios.yml", ".github/workflows/validate.yml"):
        path = ios_dir / rel
        if not path.exists():
            continue
        rules = _PROJECT_YML_RULES if path.name == "project.yml" else _WORKFLOW_RULES
        lines = path.read_text(encoding="utf-8").splitlines()
        hits, out = [], []
        for i, ln in enumerate(lines, 1):
            if ln.lstrip().startswith("#"):
                out.append(ln)
                continue
            before = ln
            for pattern, kind in rules:
                if not re.search(pattern, ln):
                    continue
                val = {"name": new, "bundle": new_bundle, "prefix": prefix}[kind]
                ln = re.sub(pattern, lambda mm: mm.group(1) + val, ln, count=1)
            # 剩下的旧名：targets 段、schemes 段、build.targets 里的键
            if old and old != new and old in ln:
                ln = ln.replace(old, new)
            if ln != before:
                hits.append((i, before.strip(), ln.strip()))
            out.append(ln)

        if not hits:
            continue
        result.append((rel, hits))
        if not dry_run:
            path.write_text("\n".join(out) + "\n", encoding="utf-8")

    return result


def cmd_new(args):
    """从仓库里的 template 建一个新项目：复制 + 改名，一条命令。

    存在的理由：README 原来让用户敲 `cp -r template/. <你的iOS项目>/ios`，
    而 Windows 的 cmd 里没有 cp，`<...>` 还会被当成重定向符。
    `new` 只用 Python 标准库，三个平台都能跑。
    """
    tpl = HERE.parent / "template"
    if not tpl.is_dir():
        bad("找不到 template 目录", str(tpl))
        note("`new` 要在 clone 下来的仓库里跑：")
        note("  git clone https://github.com/bocmiao/ios-ship")
        note("  python ios-ship/tools/ship.py new PhotoHelper --bundle-id com.you.photohelper")
        return 1

    if not args.target:
        bad("要给一个目标目录名", "例如：ship.py new PhotoHelper --bundle-id com.you.photohelper")
        return 1

    dest = Path(args.target).expanduser()
    if not dest.is_absolute():
        dest = (Path.cwd() / dest).resolve()
    ios = dest / "ios"

    if ios.exists() and any(ios.iterdir()) and not args.force:
        bad(f"{ios} 已经存在且不是空的", "换个目录名，或加 --force 覆盖同名文件")
        return 1

    name = (args.app_name or dest.name).strip()
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        bad("工程名只能用字母、数字、下划线", f"收到：{name}")
        note("可以显式指定：--app-name PhotoHelper")
        return 1

    bundle = (args.bundle_id or "").strip()
    if not bundle:
        bad("要给 --bundle-id", "格式是反域名，例如 com.yourname.photohelper")
        note("它会成为 App 的唯一标识，上架后不能改")
        return 1
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9.\-]+$", bundle) or "." not in bundle:
        bad("Bundle ID 格式不对", f"收到：{bundle}")
        return 1

    ios.mkdir(parents=True, exist_ok=True)
    # 不复制 cert（可能藏着别人的凭据）、不复制缓存和上次的配置
    ignore = shutil.ignore_patterns("__pycache__", "cert", "ship.config.json", "*.pyc")
    shutil.copytree(HERE, ios / "tools", ignore=ignore, dirs_exist_ok=True)
    shutil.copytree(tpl, ios, dirs_exist_ok=True)

    m = re.search(r"^name:\s*(\S+)", (ios / "project.yml").read_text(encoding="utf-8"), re.M)
    old = m.group(1) if m else ""
    result = _rewrite_names(ios, old, name, bundle)
    changed = sum(len(h) for _, h in result)

    print(f"\n  已创建 {dest}")
    print(f"  工程名    {old}  →  {name}")
    print(f"  Bundle ID →  {bundle}")
    print(f"  改写了 {changed} 处（project.yml 与两个 workflow）")
    print()
    print("  里面是一个能编译的最小 SwiftUI 工程：")
    print(f"    {ios / 'App' / 'ContentView.swift'}   改这个文件写你的界面")
    print()
    print("  下一步：")
    print(f"    cd {ios}")
    print("    python tools/ship.py deps")
    print("    python tools/ship.py init")
    print("    python tools/ship.py doctor")
    return 0


def cmd_rename(args):
    """把工程名与 Bundle ID 改到所有出现的位置。

    只改配置，不动 App 源码和目录名——源码目录叫 App 还是 Source 都不影响。
    """
    yml = IOS_DIR / "project.yml"
    if not yml.exists():
        bad("当前目录没有 project.yml", str(IOS_DIR))
        return 1

    txt = yml.read_text(encoding="utf-8")
    m = re.search(r"^name:\s*(\S+)", txt, re.M)
    old = m.group(1) if m else ""
    mb = re.search(r"PRODUCT_BUNDLE_IDENTIFIER:\s*(\S+)", txt)
    old_bundle = mb.group(1) if mb else ""

    new = (args.app_name or "").strip()
    if not new:
        bad("要给 --app-name", f"当前工程名：{old or '(读不到)'}")
        return 1
    if not re.match(r"^[A-Za-z0-9_]+$", new):
        bad("工程名只能用字母、数字、下划线", f"收到：{new}")
        note("它会变成 target 名和 scheme 名，不能有空格、连字符、中文")
        return 1

    new_bundle = (args.bundle_id or old_bundle).strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9.\-]+$", new_bundle):
        bad("Bundle ID 格式不对", f"收到：{new_bundle}")
        return 1

    print(f"\n  工程名    {old or '(空)'}  →  {new}")
    print(f"  Bundle ID {old_bundle or '(空)'}  →  {new_bundle}")
    print()

    result = _rewrite_names(IOS_DIR, old, new, new_bundle, dry_run=args.dry_run)
    changed = sum(len(h) for _, h in result)
    for rel, hits in result:
        print(f"  {rel}  {len(hits)} 处")
        for i, before, after in hits:
            print(f"    {i:3d}  {before}")
            print(f"         {after}")

    if not changed:
        print(f"  没有可改的行。工程名可能已经是 {new}。")
        return 0
    if args.dry_run:
        print(f"\n{C.D}dry-run，{changed} 处未写入。去掉 --dry-run 执行。{C.X}")
        return 0

    print(f"\n{C.G}改了 {changed} 处。{C.X}")
    if old and old != new and (IOS_DIR / old).is_dir():
        print(f"  源码目录 {old}/ 还在：可以改名并同步 project.yml 的 sources，也可以不改")
    print("  下一步：python tools/ship.py init（重建配置）→ doctor")
    return 0


# ------------------------------------------------------------------ release

def cmd_release(args):
    """发版全流程：+build 号 → 推送 → 触发构建 → 等 → 挂进测试组。

    为什么把「挂测试组」也放进来：上传成功不等于手机能装。
    以前这一步是手工补的，忘了就是白等一轮。
    """
    token, secrets, err = api_token(args)
    if err:
        bad(err)
        return 1
    app = tf.get_app(aa, token, args.bundle_id)

    if not args.yes:
        mv, bv = read_pyproject_version()
        print(f"\n即将：\n  · build 号 {bv} → {bv + 1 if bv else '?'}")
        print(f"  · 推送 main，触发 ios.yml")
        print(f"  · 等构建完成后，把新 build 挂进内部测试组")
        print(f"\n{C.Y}上传不可撤销，占掉的 build 号拿不回来。{C.X}")
        if input("继续？[y/N] ").strip().lower() not in ("y", "yes"):
            print("已取消。")
            return 0

    # 1. 版本号
    old, new = bump_build_number()
    ok(f"build 号 {old} → {new}")

    # 2. 提交推送
    # 只提交 project.yml。发版时工作区里常有改了一半的代码，
    # `git add -A` 会把它一起塞进 release 提交推上去，事后很难拆开。
    st_yml = subprocess.run(["git", "status", "--porcelain", "project.yml"],
                            capture_output=True, text=True, errors="replace", cwd=str(IOS_DIR))
    if st_yml.stdout.strip():
        subprocess.run(["git", "add", "project.yml"], cwd=str(IOS_DIR))
        subprocess.run(["git", "commit", "-m", f"release: build {new}"], cwd=str(IOS_DIR))
        ok("已提交 project.yml")
    else:
        note("project.yml 没有改动，直接推送当前 HEAD")

    st_all = subprocess.run(["git", "status", "--porcelain"],
                            capture_output=True, text=True, errors="replace", cwd=str(IOS_DIR))
    others = [x for x in st_all.stdout.splitlines()
              if x.strip() and "project.yml" not in x]
    if others:
        warn(f"工作区还有 {len(others)} 个文件没提交",
             "本次只提交 project.yml，这些留在工作区，不影响构建")

    r = subprocess.run(["git", "push", "origin", "HEAD"], capture_output=True,
                       text=True, errors="replace", cwd=str(IOS_DIR))
    if r.returncode != 0:
        bad("推送失败", r.stderr.strip()[:200])
        return 1
    ok("已推送")

    # 3. 触发（ios.yml 只手动触发，push 不会自动上传）
    rc, out, err = gh("workflow", "run", "ios.yml", "--ref", "main")
    if rc != 0:
        bad("触发流水线失败", err.strip()[:200])
        return 1
    ok("流水线已触发")

    # 等 run 出现在列表里（刚触发时还没编号，通常 3–10 秒）
    run_id = None
    for _ in range(20):
        time.sleep(3)
        rc, out, _ = gh("run", "list", "--workflow=ios.yml", "--limit", "1",
                        "--json", "databaseId,status")
        if rc == 0 and out.strip():
            try:
                run_id = json.loads(out)[0]["databaseId"]
                break
            except Exception:
                pass
    if not run_id:
        warn("没拿到 run id，去网页看进度", "Actions → iOS 构建并上传 TestFlight")
        return 1

    # 4. 等结果：交给 gh 自己盯，--exit-status 把结论变成退出码
    note(f"run {run_id}，约 4 分钟")
    print()
    wr = subprocess.run(["gh", "run", "watch", str(run_id), "--exit-status", "--interval", "15"],
                        cwd=str(IOS_DIR))
    if wr.returncode != 0:
        bad("构建失败或被取消")
        note(f"看日志：gh run view {run_id} --log-failed")
        return 1
    ok("构建并上传成功")

    # 5. 挂进内部测试组
    # upload 用的是 skip_waiting_for_build_processing，workflow 成功的那一刻
    # Apple 那边往往还在处理。先等到 VALID 再挂，否则会把成功报成失败。
    print()
    h("分发")
    gid, gname = tf.ensure_internal_group(aa, token, app["id"])
    target = tf.wait_for_build(aa, token, app["id"], want=new)
    ver = target["attributes"]["version"]
    tf.ensure_build_attached(aa, token, gid, app["id"], want=ver)
    tf.ensure_self_in_group(aa, token, gid)
    tf.ensure_localizations(aa, token, app["id"], target["id"], args.feedback_email,
                            args.cfg.get("testflightDescription"),
                            args.cfg.get("testflightWhatsNew"))

    print(f"\n{C.G}完成。{C.X} build {ver} 已进入内部组「{gname}」。")
    print("iPhone 打开 TestFlight 就能装，通常几分钟内可见。")
    return 0


# ------------------------------------------------------------------ 依赖

# 真依赖清单。写在这里而不是散在文档里，因为文档会过期、这个不会。
#   cryptography —— CSR 生成、ES256 签 JWT。setup / doctor / status / release 要用。
#   pillow       —— 只有 make-icon.py 画图要用，可选。
DEPS = [
    ("cryptography", "import cryptography", True,
     "证书申请与 API 鉴权（CSR / ES256 JWT）"),
    ("pillow", "from PIL import Image", False,
     "生成 App 图标（可选，你有自己的 PNG 就不用装）"),
]


def check_dep(import_stmt):
    try:
        exec(import_stmt, {})
        return True
    except Exception:
        return False


def cmd_deps(args):
    """检查并安装依赖。装的时候用当前这个 Python 的 -m pip，保证装进同一个环境。"""
    print()
    missing = []
    for name, stmt, required, why in DEPS:
        has = check_dep(stmt)
        tag = "必需" if required else "可选"
        if has:
            ok(f"{name}（{tag}）", why)
        else:
            warn(f"{name} 没装（{tag}）", why)
            missing.append((name, required))

    print()
    if not missing:
        print(f"{C.G}依赖齐了。{C.X}")
        return 0

    to_install = [n for n, req in missing if req or args.install_optional]
    if not to_install:
        print(f"{C.Y}只是缺可选的包，不影响发版。{C.X}")
        print(f"  要装的话：{sys.executable} -m pip install pillow")
        return 0

    pkgs = " ".join(to_install)
    cmd = [sys.executable, "-m", "pip", "install"] + to_install
    print(f"{C.D}将执行：{' '.join(cmd)}{C.X}")

    if args.dry_run:
        print(f"{C.D}dry-run，没有真的装。{C.X}")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print(f"\n{C.Y}非交互环境，没有自动安装。请手动执行上面的命令。{C.X}")
            return 1
        ans = input("现在安装吗？[Y/n] ").strip().lower()
        if ans not in ("", "y", "yes"):
            print(f"\n{C.Y}没装。需要的时候跑 `ship.py deps --yes`。{C.X}")
            return 1

    rc = subprocess.call(cmd)
    if rc != 0:
        bad("安装失败")
        note("常见原因：没联网、公司代理、或 pip 版本太旧")
        note(f"可以先试 {sys.executable} -m ensurepip --upgrade")
        return 1

    # 装完立刻复验，别让「pip 说成功但其实装到别的解释器去了」蒙混过关
    print()
    still = [name for name, stmt, _req, _why in DEPS
             if name in to_install and not check_dep(stmt)]
    if still:
        bad(f"装完了还是找不到：{', '.join(still)}")
        note(f"确认装的时候用的是这个解释器：{sys.executable}")
        return 1
    print(f"{C.G}装好了，现在可以跑 `ship.py setup`。{C.X}")
    return 0


# ------------------------------------------------------------------ logs

# 上传能失败的报错就那么几种，但它们在日志里淹没在几百行编译输出中间。
# 命中这里就把含义和修法直接标出来，剩下的让用户自己翻。
LOG_HINTS = [
    ("CFBundleIconName",
     "asset catalog 没编进包：project.yml 要有 ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon"),
    ("90713", "同上，最终原因是 Info.plist 里没有 CFBundleIconName"),
    ("90022", "缺 120×120 图标，或者图标带 alpha 通道（png 必须不透明）"),
    ("MAC verification failed",
     "p12 的加密算法 macOS 不认，得用 3DES + SHA1（docs/pitfalls.md 第 9 条）"),
    ("bundle version must be higher",
     "build 号撞了：CURRENT_PROJECT_VERSION 要大于线上已有的最新号"),
    ("No profiles for", "描述文件没匹配上这个 Bundle ID，重跑 `ship.py setup`"),
    ("doesn't include signing certificate", "描述文件里缺这张证书，得重新生成 profile"),
    ("Couldn't find app", "ASC 里没有这个 Bundle ID 对应的 App 记录，先手工建一个"),
    ("INVALID_STATE",
     "CFBundleShortVersionString 与 ASC 里已有的版本对不上（看 MARKETING_VERSION）"),
    ("Authentication failed", "API Key 被拒：确认是团队密钥，且 Key ID / Issuer ID 没抄错"),
]


def _strip_ansi(s):
    """去掉 ANSI 颜色码。

    altool 的输出自带颜色，gh 原样透传。要处理两种形态：
    真实的 ESC 字节，以及被某些工具写成字面 `^[[31m` 的转义序列。
    """
    s = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", s)
    s = re.sub(r"\^\[\[[0-9;?]*[ -/]*[@-~]", "", s)
    return s


def _strip_ts(rest):
    """gh 日志每行是 `<ISO 时间戳> <内容>`，时间戳固定以 Z + 空格结束。"""
    m = re.match(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z\s(.*)$", rest)
    return m.group(1) if m else rest


def _parse_run_log(text):
    """切成 [(job, step, 内容)]。

    gh 的行格式是 `job \\t step \\t 时间戳 内容`。以前直接把 --log-failed
    的原始输出丢给人看，一屏里 200 行都是 build / 上传到 TestFlight 这样的前缀。
    只有颜色码没有内容的行（altool 每段的收尾）直接丢掉。
    """
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) == 3:
            job, step, body = parts[0].strip(), parts[1].strip(), parts[2]
        elif len(parts) == 2:
            job, step, body = parts[0].strip(), "", parts[1]
        else:
            job, step, body = "", "", line
        body = _strip_ansi(_strip_ts(body)).rstrip()
        if not body.strip():
            continue
        rows.append((job, step, body))
    return rows


def cmd_logs(args):
    """把一次失败的日志从几百行里挑出有用的部分。

    release 失败时只打一行 `gh run view <id> --log-failed` 让用户自己去跑，
    跑出来的原始输出在 cmd 里根本读不了：每行都带 job / step / 时间戳前缀，
    真正的报错夹在中间。这条命令做的事就是把它压到能读的密度。
    """
    want_run = (args.run_id or "").strip()
    runs = []

    if not want_run:
        rc, out, err = gh("run", "list", "--workflow=ios.yml", "--limit", "10",
                          "--json", "databaseId,conclusion,status,createdAt")
        if rc == 127:
            bad("没装 gh CLI", "从 https://cli.github.com 装一个，然后 gh auth login")
            return 1
        if rc != 0:
            bad("读不到流水线列表", (err or out).strip()[:200])
            return 1
        try:
            runs = json.loads(out or "[]")
        except Exception:
            runs = []
        if not runs:
            note("还没有 ios.yml 的运行记录。第一次发版跑 `ship.py release`。")
            return 0
        failed = [r for r in runs if r.get("conclusion") == "failure"]
        target = failed[0] if failed else runs[0]
        run_id = str(target["databaseId"])
        conclusion = target.get("conclusion") or target.get("status") or "?"
    else:
        run_id = want_run
        conclusion = "?"

    h(f"流水线 {run_id}（{conclusion}）")

    # 最近一次成功时别装作用力翻日志，直接说清楚
    if conclusion == "success" and not args.all:
        ok("这次是成功的，没有失败日志")
        note("想看完整输出加 --all；指定某一次用 --run <id>")
        return 0

    if args.all:
        rc, out, err = gh("run", "view", run_id, "--log")
    else:
        rc, out, err = gh("run", "view", run_id, "--log-failed")
    if rc != 0:
        bad(f"取不到日志（gh 返回 {rc}）", (err or out).strip()[:200])
        note("运行还没结束的话，用 `gh run watch <id>` 盯着，结束后再跑这条")
        return 1

    rows = _parse_run_log(out)
    if not rows:
        note("这次运行没有输出。")
        return 0

    # 命中已知报错，并且记下它周围的行走范围。
    # 只截末尾 N 行是不够的：fastlane 失败后会打一个 summary 表，
    # 把真正的 altool 报错顶到几十行之前，正好被截掉。
    hits = []
    hit_span = []
    seen_hint = set()
    seen_idx = set()
    for idx, (_job, step, text) in enumerate(rows if not args.no_hints else []):
        low = text.lower()
        for sig, hint in LOG_HINTS:
            if sig.lower() in low:
                if hint not in seen_hint:
                    seen_hint.add(hint)
                    hits.append((sig, hint, step))
                for k in range(max(0, idx - 6), min(len(rows), idx + 4)):
                    if k not in seen_idx:
                        seen_idx.add(k)
                        hit_span.append(k)
                break

    if hits:
        print()
        for sig, hint, step in hits:
            print(f"  {C.Y}>>{C.X} 命中 {C.B}{sig}{C.X}：{hint}")
            if step:
                print(f"     {C.D}在步骤「{step}」{C.X}")

    if hit_span:
        hit_span.sort()
        if len(hit_span) > 80:
            hit_span = hit_span[:80]
        show_idx = hit_span
        shown = [rows[k] for k in hit_span]
    else:
        total = len(rows)
        tail_n = total if args.tail <= 0 else min(args.tail, total)
        show_idx = list(range(total - len(rows[-tail_n:]), total)) if tail_n else []
        shown = rows[-tail_n:]

    # 一次失败通常全落在同一个步骤里。每行都顶着「上传到 TestFlight」
    # 除了占宽度没有信息量，改成标题里说一次。
    steps = {step for _j, step, _t in shown if step}
    single_step = steps.pop() if len(steps) == 1 else ""

    print()
    if hit_span:
        h(f"命中位置附近 {len(shown)} 行" + (f"（「{single_step}」）" if single_step else ""))
    else:
        h(f"最后 {len(shown)} 行" + (f"（全部来自「{single_step}」）" if single_step else ""))

    prev = None
    for i, (_job, step, text) in enumerate(shown):
        if prev is not None and show_idx[i] != prev + 1:
            print(f"  {C.D}...{C.X}")
        prev = show_idx[i]
        prefix = ""
        if step and not single_step and not text.startswith(step):
            prefix = f"{C.D}{step[:14]:<14}{C.X} "
        print(f"  {prefix}{text}")

    print()
    if hit_span:
        note(f"共 {len(rows)} 行，只显示命中的那几处。看全末尾 60 行：ship.py logs --tail 60 --no-hints")
    else:
        note(f"共 {len(rows)} 行，只显示末尾 {len(shown)} 行。看全部：ship.py logs --tail 0")
    return 0


# ------------------------------------------------------------------ 入口

def main():
    ap = argparse.ArgumentParser(
        description="没有 Mac 也能把 iOS App 发到 TestFlight",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  ship.py rename --app-name PhotoHelper --bundle-id com.you.photohelper
  ship.py doctor
  ship.py setup --bundle-id com.example.myapp
  ship.py setup --reuse-cert ../old/cert
  ship.py secrets
  ship.py release
""",
    )
    ap.add_argument("action",
                    choices=["new", "doctor", "setup", "secrets", "release", "status",
                             "init", "deps", "rename", "logs"],
                    help="要做什么")
    ap.add_argument("target", nargs="?", default="",
                    help="new 的目标目录名，例如 PhotoHelper")
    ap.add_argument("--app-name", default="", help="工程名（rename / new 用，字母数字下划线）")
    ap.add_argument("--bundle-id", default="", help="不填就从 project.yml / ship.config.json 推断")
    ap.add_argument("--profile", default="", help="描述文件名，默认 <scheme> AppStore")
    ap.add_argument("--p8", default="")
    ap.add_argument("--key-id", default="")
    ap.add_argument("--issuer", default="")
    ap.add_argument("--cert-dir", default="", metavar="DIR",
                    help="证书材料目录（含 secrets.txt / distribution.*），默认 tools/cert")
    ap.add_argument("--reuse-cert", default="", metavar="DIR",
                    help="复用已有证书目录（含 distribution.key / distribution.cer）")
    ap.add_argument("--force", action="store_true", help="setup 时强制重新生成")
    ap.add_argument("--yes", "-y", action="store_true", help="release 时跳过确认")
    ap.add_argument("--dry-run", action="store_true", help="secrets / deps 时只打印不执行")
    ap.add_argument("--install-optional", action="store_true",
                    help="deps 时连可选的 Pillow 一起装")
    ap.add_argument("--feedback-email", default="",
                    help="TestFlight 反馈邮箱，默认取 ship.config.json 或 noreply@example.com")
    ap.add_argument("--run", dest="run_id", default="", metavar="ID",
                    help="logs 时指定 run id，默认看最近一次失败")
    ap.add_argument("--tail", type=int, default=60, metavar="N",
                    help="logs 显示末尾多少行，0 表示全部（默认 60）")
    ap.add_argument("--all", action="store_true",
                    help="logs 时取完整日志，而不只是失败步骤")
    ap.add_argument("--no-hints", dest="no_hints", action="store_true",
                    help="logs 时不按已知报错挑行，直接输末尾 N 行")
    args = ap.parse_args()

    # ---------- new：从 template 建一个新项目，只在仓库里跑
    if args.action == "new":
        return cmd_new(args)

    # ---------- deps：查/装依赖，不碰项目配置
    if args.action == "deps":
        return cmd_deps(args)

    # ---------- logs：只读流水线日志，不碰本地配置
    if args.action == "logs":
        return cmd_logs(args)

    # ---------- init：只生成样板配置，不动任何东西
    if args.action == "init":
        target = IOS_DIR / "ship.config.json"
        if target.exists() and not args.force:
            warn(f"{target} 已存在", "要覆盖加 --force")
            return 1
        _, filled = sc.write_example(target, IOS_DIR)
        ok(f"已生成 {target}")
        if filled:
            note(f"已从 project.yml 自动填好：{', '.join(filled)}")
        note("其余字段可留空：teamId 从 secrets.txt 兜底，TestFlight 文案有通用默认值")
        note("改完跑 `ship.py doctor` 看还缺什么")
        return 0

    # doctor / release 要读 project.yml（版本号、图标）和 git 仓库。
    # 如果这个脚本被直接放在别处跑（比如刚从 skill 里复制出来），
    # 报「找不到 project.yml」比抛一堆 FileNotFoundError 好懂得多。
    # status 只查 API，不依赖本地工程，所以不拦。
    if args.action in ("doctor", "release", "rename"):
        if not (IOS_DIR / "project.yml").exists():
            print(f"\n{C.R}这个脚本得放在 iOS 项目的 tools/ 目录下。{C.X}\n")
            print(f"  当前位置：{HERE}")
            print(f"  期望有：  {IOS_DIR / 'project.yml'}")
            print()
            print("  正确用法是把 tools/ 整个复制进项目：")
            print("    <你的iOS项目>/")
            print("      project.yml")
            print("      .github/workflows/ios.yml")
            print("      tools/          ← 这些脚本放这儿")
            print()
            print(f"  只是想配置证书的话，`setup` 和 `secrets` 不需要 project.yml，可以直接跑。")
            return 1

    # ---------- 统一加载配置：命令行 > ship.config.json > project.yml 推断
    cfg = sc.load(IOS_DIR, {"bundleId": args.bundle_id, "profileName": args.profile})

    # 每个命令真正需要什么，就只校验什么。
    #   doctor / release / status 要认工程：bundleId + scheme 都得有
    #   setup 只要能建 Bundle ID 就行，scheme 只是用来生成描述文件名，
    #         没有 project.yml 时用 --profile 或者让 apple-auto 自己推
    #   secrets 只读 secrets.txt，什么配置都不需要
    # 之前一律按 doctor 的标准拦，导致空目录里 `setup` 报「缺 scheme」，
    # 和「setup 不需要 project.yml」的说法自相矛盾。
    if args.action in ("doctor", "release", "status"):
        missing = sc.validate(cfg)
    elif args.action == "setup":
        missing = [] if cfg.get("bundleId") else ["bundleId"]
    else:
        missing = []

    if missing:
        print(f"\n{C.R}缺关键配置：{', '.join(missing)}{C.X}\n")
        print("  两种补法：")
        print("    1. 跑 `ship.py init` 生成 ship.config.json，把值填上")
        print("    2. 用参数临时给：--bundle-id com.example.myapp")
        print()
        print(f"  当前的推断结果：{sc.describe(cfg)}")
        return 1

    # 回填，后面所有函数直接用
    args.bundle_id = cfg.get("bundleId") or ""
    args.profile = cfg.get("profileName") or ""
    args.cfg = cfg
    args.feedback_email = (args.feedback_email or cfg.get("feedbackEmail")
                           or "noreply@example.com")

    # setup / release / status 都要签 JWT（生成 CSR、调 API）。
    # 缺包时 make_jwt 抛的是 ModuleNotFoundError 加一长串堆栈，
    # 用户看到的第一行是 "No module named 'cryptography'"，看不出接下来该干什么。
    if args.action in ("setup", "release", "status") and not check_dep("import cryptography"):
        print(f"\n{C.R}缺 python 包 cryptography{C.X}\n")
        print("  跑 `python tools/ship.py deps` 装上。")
        print("  它负责生成 CSR 和签 ES256 JWT，这三条命令都要用。")
        return 1

    if args.action == "doctor":
        return cmd_doctor(args)
    if args.action == "rename":
        return cmd_rename(args)
    if args.action == "setup":
        return cmd_setup(args)
    if args.action == "secrets":
        return cmd_secrets(args)
    if args.action == "release":
        return cmd_release(args)
    if args.action == "status":
        token, _, err = api_token(args)
        if err:
            bad(err)
            return 1
        tf.cmd_status(aa, token, tf.get_app(aa, token, args.bundle_id))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
