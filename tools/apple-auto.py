#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""用 App Store Connect API Key 自动搞定 iOS 发布证书、App ID、描述文件。

你手动只需做一件事：建 API Key。
（Apple 刻意不允许用 API 建 API Key，这一步没有绕过的办法。）

拿到 .p8 + Key ID + Issuer ID 之后，剩下全部由这个脚本完成：

  1. 本地生成 RSA 2048 私钥 + CSR
  2. 上传 CSR 让 Apple 签成发布证书，下载 .cer
  3. 合成 .p12（密码自动生成，随 secret 一起给你）
  4. 建 Bundle ID（已存在就复用）
  5. 建 App Store 描述文件，下载 .mobileprovision
  6. 把 8 个 GitHub Secrets 打印出来并写进 cert/secrets.txt

用法：
  python apple-auto.py --p8 AuthKey_ABC12345.p8 --key-id ABC12345 --issuer-id 69a6de7c-xxxx-xxxx-xxxx-xxxxxxxxxxxx

可选参数：
  --bundle-id   不给就从 ship.config.json / project.yml 推断
  --profile     描述文件名（不给就用 <app-name> AppStore）
  --out         默认 ./cert
  --check       只验证 API Key 是否有效，不创建任何东西
  --force       强制重新签证书（默认会复用 cert/ 里已有的）

不需要 openssl，不需要 Mac，不需要钥匙串访问。
"""

import argparse
import base64
import json
import os
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 项目名。只影响显示文案和证书/p12 的友好名——Apple 签发时不看这些，
# 真正决定归属的是 Team ID。main() 一开始会按 --app-name 或工程文件重新赋值。
APP_NAME = "iOS App"

# Windows 控制台默认 GBK，中文会炸。强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API = "https://api.appstoreconnect.apple.com"
JWT_TTL = 900  # Apple 上限 20 分钟，留足余量取 15 分钟


# ---------------------------------------------------------------- 小工具

def die(msg, hint=None):
    print()
    print("  [X] " + msg)
    if hint:
        print()
        for line in hint.split("\n"):
            print("      " + line)
    print()
    sys.exit(1)


def ok(msg):
    print("  [OK] " + msg)


def info(msg):
    print("       " + msg)


def step(n, total, msg):
    print()
    print("  [" + str(n) + "/" + str(total) + "] " + msg)


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def require_crypto():
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        die(
            "缺少 cryptography 这个库。",
            "装一下就好（只需一次）：\n"
            "  " + sys.executable + " -m pip install cryptography\n\n"
            "如果提示 pip 不存在，改用：\n"
            "  " + sys.executable + " -m ensurepip --upgrade",
        )


# ---------------------------------------------------------------- JWT

def make_jwt(p8_path, key_id, issuer_id):
    """用 ES256 手签 JWT。不依赖 pyjwt，只依赖 cryptography。"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    try:
        with open(p8_path, "rb") as f:
            pem = f.read()
    except FileNotFoundError:
        die(
            "找不到私钥文件：" + p8_path,
            "确认文件名是 AuthKey_XXXXXXXX.p8 这种，且和你放脚本的目录对上。\n"
            "注意这个文件 Apple 只让下载一次，丢了只能吊销重建。",
        )

    try:
        priv = serialization.load_pem_private_key(pem, password=None)
    except Exception as e:
        die("这个 .p8 文件读不了：" + str(e), "确认下载完整，且是 App Store Connect API 的密钥（不是推送密钥）。")

    if not isinstance(priv, ec.EllipticCurvePrivateKey):
        die("这个 .p8 不是 EC 私钥，Apple 的 API Key 应该是 ES256。")

    now = int(time.time())
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + JWT_TTL,
        "aud": "appstoreconnect-v1",
    }

    h = b64url(json.dumps(header, separators=(",", ":")).encode())
    p = b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = (h + "." + p).encode("ascii")

    der = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")

    return h + "." + p + "." + b64url(sig)


# ---------------------------------------------------------------- HTTP

def api(method, path, token, body=None):
    """返回 (status, dict)。不抛异常，交给调用方判断。"""
    url = path if path.startswith("http") else API + path
    data = None
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw.decode("utf-8", "replace")}
        return e.code, parsed
    except urllib.error.URLError as e:
        die(
            "连不上 Apple 的 API：" + str(e.reason),
            "检查网络/代理。公司网络如果拦了 api.appstoreconnect.apple.com 会让这一步失败。",
        )


def api_err(payload):
    """把 Apple 的错误 JSON 摊平成一句话。"""
    errs = payload.get("errors") or []
    if not errs:
        return json.dumps(payload, ensure_ascii=False)[:400]
    parts = []
    for e in errs:
        t = e.get("title") or ""
        d = e.get("detail") or ""
        parts.append((t + " / " + d).strip(" /"))
    return "; ".join(parts)


# ---------------------------------------------------------------- 证书

def gen_key_and_csr(app_name="iOS App"):
    """生成 RSA 私钥和 CSR。

    app_name 只影响 CSR 里的 Common Name，而 Apple 签发时不看这个字段
    （真正决定归属的是你的账号和 Team ID），所以填什么都不影响能不能签。
    留着是为了在后台的证书列表里认得出是哪个项目的。
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.COMMON_NAME, f"{app_name} Distribution"),
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, app_name),
                    x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
                ]
            )
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    return key, key_pem, csr_pem


def build_p12(key, cer_der, password, warn=True, friendly_name=None):
    """合成 .p12。

    格式有讲究，别再改回去：
      cryptography 默认给 AES-256 加密 + SHA256 MAC（PKCS#12 v1.1）。
      openssl 能读，但 macOS 的 Security.framework 只认老的 3DES + SHA1 MAC，
      `security import` 会报 "MAC verification failed during PKCS12 import
      (wrong password?)" —— 提示语说密码错，其实密码是对的，是算法不认。
      这个坑在 CI 里查了两轮才发现，所以这里优先走 openssl 精确指定算法。
    """
    import shutil
    import subprocess
    import tempfile

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_der_x509_certificate(cer_der)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    tmpdir = tempfile.mkdtemp(prefix="ship-p12-")
    try:
        kp = os.path.join(tmpdir, "k.pem")
        cp = os.path.join(tmpdir, "c.pem")
        op = os.path.join(tmpdir, "o.p12")
        with open(kp, "wb") as f:
            f.write(key_pem)
        with open(cp, "wb") as f:
            f.write(cert_pem)

        base = [
            "openssl", "pkcs12", "-export",
            "-inkey", kp, "-in", cp, "-out", op,
            "-passout", "pass:" + password,
            "-certpbe", "PBE-SHA1-3DES",
            "-keypbe", "PBE-SHA1-3DES",
            "-name", friendly_name or (APP_NAME + " Distribution"),
        ]

        # 先试完整版（OpenSSL 1.1.1+ 才有 -macalg）
        for cmd in (base + ["-macalg", "sha1"], base):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True)
            except FileNotFoundError:
                break
            if r.returncode == 0 and os.path.exists(op):
                with open(op, "rb") as f:
                    return f.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 没有 openssl，退回 cryptography 的默认格式。
    # 文件本身没问题，但 macOS 的 security import 可能导不进去——先说清楚。
    if warn:
        info("提示：本机没有可用的 openssl，.p12 用默认算法生成")
        info("      如果要拿去 macOS 的 CI 签名，建议装 openssl 后重跑")

    from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12

    return pkcs12.serialize_key_and_certificates(
        name=(friendly_name or (APP_NAME + " Distribution")).encode(),
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=BestAvailableEncryption(password.encode()),
    )


def rand_password(n=24):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def infer_app_name():
    """猜个项目名，纯为了显示和命名好看。

    顺序：ship.config.json 的 appName → project.yml 的 CFBundleDisplayName
    → project.yml 的第一个 target 名 → 兜底。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)

    for d in (here, parent):
        cfg = os.path.join(d, "ship.config.json")
        if os.path.exists(cfg):
            try:
                with open(cfg, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("appName"):
                    return data["appName"]
                if data.get("scheme"):
                    return data["scheme"]
            except Exception:
                pass

    proj = os.path.join(parent, "project.yml")
    if os.path.exists(proj):
        try:
            txt = open(proj, encoding="utf-8", errors="replace").read()
            m = re.search(r"CFBundleDisplayName:\s*(.+)", txt)
            if m:
                return m.group(1).strip().strip('"').strip("'")
            m = re.search(r"^targets:\s*$\s*^  (\w+):", txt, re.M)
            if m:
                return m.group(1)
        except Exception:
            pass

    return "iOS App"


def infer_bundle_id():
    """猜 Bundle ID。

    顺序：ship.config.json 的 bundleId → project.yml 的
    PRODUCT_BUNDLE_IDENTIFIER → 空字符串（交给调用方去问）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)

    for d in (here, parent):
        cfg = os.path.join(d, "ship.config.json")
        if os.path.exists(cfg):
            try:
                with open(cfg, encoding="utf-8") as f:
                    val = (json.load(f) or {}).get("bundleId")
                if val:
                    return val
            except Exception:
                pass

    proj = os.path.join(parent, "project.yml")
    if os.path.exists(proj):
        try:
            txt = open(proj, encoding="utf-8", errors="replace").read()
            m = re.search(r"PRODUCT_BUNDLE_IDENTIFIER:\s*([\w.\-]+)", txt)
            if m:
                return m.group(1)
        except Exception:
            pass

    return ""


def detect_repo():
    """猜当前仓库的 owner/name，纯为了打印一个能点的链接。

    先试 gh（最准，能处理 remote 不叫 origin 的情况），
    再退回解析 git remote 的 URL。都不行返回空字符串。
    """
    import subprocess

    try:
        p = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner",
                            "-q", ".nameWithOwner"],
                           capture_output=True, text=True, timeout=15)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except Exception:
        pass

    try:
        p = subprocess.run(["git", "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=15)
        if p.returncode == 0:
            url = p.stdout.strip()
            m = re.search(r"github\.com[:/]+(.+?)(?:\.git)?$", url)
            if m:
                return m.group(1)
    except Exception:
        pass

    return ""


def find_p8():
    """在几个最可能的位置找 AuthKey_*.p8，省得让人手打路径。"""
    import glob

    here = os.path.dirname(os.path.abspath(__file__))
    patterns = [
        os.path.join(os.getcwd(), "AuthKey_*.p8"),
        os.path.join(here, "AuthKey_*.p8"),
        os.path.join(os.path.expanduser("~"), "Downloads", "AuthKey_*.p8"),
        os.path.join(os.path.expanduser("~"), "Desktop", "AuthKey_*.p8"),
    ]
    hits = []
    for p in patterns:
        hits.extend(glob.glob(p))
    # 去掉重复，保留最新的
    hits = list(dict.fromkeys(hits))
    hits.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    return hits


def infer_key_id(p8_path):
    """Apple 的文件名就是 AuthKey_<KeyID>.p8，Key ID 不用手抄。"""
    import re

    m = re.search(r"AuthKey_([A-Z0-9]{10})\.p8$", os.path.basename(p8_path))
    return m.group(1) if m else ""


def ask(prompt, default="", validator=None):
    """交互输入。stdin 不是终端时（比如 CI）直接返回默认值，不卡住。"""
    if not sys.stdin.isatty():
        return default
    while True:
        suffix = (" [" + default + "]") if default else ""
        try:
            val = input("  " + prompt + suffix + ": ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if not val:
            val = default
        if not val:
            print("    这个不能空，再填一次")
            continue
        if validator and not validator(val):
            print("    格式看起来不对，再填一次")
            continue
        return val


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--p8", default="", help="AuthKey_XXXX.p8 的路径（不填就自动找）")
    ap.add_argument("--key-id", default="", help="Key ID（不填就从文件名推）")
    ap.add_argument("--issuer-id", default="", help="Issuer ID（UUID 形式）")
    ap.add_argument("--app-name", default="", help="项目显示名，不填就从工程文件推断")
    ap.add_argument("--bundle-id", default="", help="不给就从 ship.config.json / project.yml 推断")
    ap.add_argument("--profile", default="")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "cert"))
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--reuse-cert",
        default="",
        metavar="DIR",
        help="复用已有的一套私钥+证书（目录里要有 distribution.key 和 distribution.cer），不重新签发",
    )
    args = ap.parse_args()

    global APP_NAME
    APP_NAME = args.app_name or infer_app_name()
    if not args.profile:
        args.profile = APP_NAME + " AppStore"

    # bundle id 不给就推断；推断不出来在这里就问，别等到第 5 步才炸
    if not args.bundle_id:
        args.bundle_id = infer_bundle_id()
        if args.bundle_id:
            print("  Bundle ID 推断为: " + args.bundle_id)
    if not args.bundle_id and not args.check:
        print()
        print("  不知道该建哪个 Bundle ID（工程目录里没有 ship.config.json / project.yml）。")
        print("  格式是反域名，比如 com.yourname.myapp —— 全球唯一，被占了就换一个。")
        args.bundle_id = ask("Bundle ID")
    if args.bundle_id and not re.match(r"^[A-Za-z0-9][A-Za-z0-9.\-]+$", args.bundle_id):
        die("Bundle ID 格式不对: " + args.bundle_id,
            "只能有字母、数字、点、短横线，且以字母或数字开头。")

    require_crypto()

    print()
    print("  " + APP_NAME + " 证书自动化")
    print("  " + "-" * 52)

    # ---- 0. 补全三个必填项
    if not args.p8:
        hits = find_p8()
        if len(hits) == 1:
            args.p8 = hits[0]
            print("  自动找到密钥文件: " + os.path.basename(args.p8))
        elif len(hits) > 1:
            print()
            print("  找到多个 AuthKey_*.p8，用哪个？")
            for i, h in enumerate(hits[:5]):
                print("    " + str(i + 1) + ") " + h)
            pick = ask("输入序号", "1")
            try:
                args.p8 = hits[max(0, min(len(hits), int(pick)) - 1)]
            except ValueError:
                args.p8 = hits[0]
            print("  用: " + args.p8)
        else:
            print()
            print("  没找到 AuthKey_*.p8。")
            print("  这个文件要先去 App Store Connect 生成，建完能下载一次。")
            print("  详见 docs 里的《证书指南》。")
            print()
            args.p8 = ask("把 .p8 拖进来或输入完整路径")

    if not args.p8 or not os.path.exists(args.p8):
        die(
            "没拿到 API Key 文件（AuthKey_XXXXXXXXXX.p8）。",
            "生成入口（注意是 appstoreconnect 那个站，不是 developer 站）：\n"
            "  appstoreconnect.apple.com\n"
            "  -> 用户和访问 -> 集成 -> App Store Connect API -> 团队密钥 -> 生成 API 密钥\n"
            "  Access 选 App Manager；建完立刻下载 .p8，离开页面就再也下不了了。\n\n"
            "把这个文件放到当前目录，再跑一次。",
        )

    if not args.key_id:
        args.key_id = infer_key_id(args.p8)
        if args.key_id:
            print("  Key ID 从文件名推出: " + args.key_id)
        else:
            args.key_id = ask("Key ID（10 位大写字母数字）")

    if not args.issuer_id:
        print()
        print("  还差 Issuer ID。这个是一长串 UUID，在同一个页面上方，")
        print("  形如 69a6de7c-1234-5678-9abc-def012345678")
        print()
        args.issuer_id = ask(
            "Issuer ID",
            validator=lambda v: len(v) >= 20 and "-" in v,
        )

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    if args.check:
        total = 1
    else:
        total = 6

    # ---- 1. 鉴权
    step(1, total, "验证 API Key")
    token = make_jwt(args.p8, args.key_id, args.issuer_id)
    info("JWT 已签（有效期 15 分钟）")

    status, payload = api("GET", "/v1/bundleIds?limit=1", token)
    if status == 401:
        die(
            "API Key 被拒（401）",
            "四个可能，按可能性排序：\n"
            "  1. 这个 .p8 是「推送密钥」不是「API 密钥」\n"
            "     两种文件都叫 AuthKey_XXXXXXXXXX.p8，扩展名和内容格式一模一样，\n"
            "     光看文件分不出来。推送密钥在 developer.apple.com/account -> Keys 生成，\n"
            "     勾的是 Apple Push Notifications；API 密钥在 appstoreconnect.apple.com\n"
            "     -> 用户和访问 -> 集成 -> App Store Connect API 生成。\n"
            "  2. Key ID 或 Issuer ID 抄错了（Issuer ID 是 UUID，带横杠，别只抄前半段）\n"
            "  3. 这个 .p8 属于另一个开发者账号，Issuer ID 却是这个账号的——两者必须同属一个 Team\n"
            "  4. 密钥已被吊销（后台能看到状态变成 Revoked）",
        )
    if status == 403:
        die(
            "API Key 有效，但没有权限（403）",
            "建密钥时 Access 要选 App Manager 或 Admin。\n"
            "Developer 角色读不了证书，也建不了描述文件。\n\n"
            "另外确认你用的是「团队密钥」（Team Key），不是「个人密钥」（Individual Key）。\n"
            "Apple 文档写明个人密钥不能用 Provisioning endpoints——建不了证书和描述文件。\n"
            "两者名字很像，选错要走到后面才报错。",
        )
    if status != 200:
        die("鉴权请求失败（HTTP " + str(status) + "）", api_err(payload))

    ok("API Key 有效")

    # 顺手从返回里挖 Team ID（seedId 就是 Team ID）
    team_id = ""
    for item in (payload.get("data") or []):
        sid = (item.get("attributes") or {}).get("seedId")
        if sid:
            team_id = sid
            break
    if team_id:
        info("Team ID: " + team_id)
    else:
        info("Team ID: 暂未取到（不影响后续步骤）")

    if args.check:
        print()
        print("  " + "=" * 54)
        print("  这个密钥能用")
        print("  " + "=" * 54)
        print()
        print("  它属于这个开发者账号，Team ID：")
        print("      " + (team_id if team_id else "（取不到，不影响使用）"))
        print()
        print("  核对一下这是不是你要打包的那个账号。")
        print("  如果你本来就是从别的项目留下的密钥，只要属于同一个账号就正好——")
        print("  团队密钥对整个 Team 下所有 App 通用，不必重新建。")
        print()
        print("  确认没问题就去掉 --check 再跑一次，会开始签证书。")
        print()
        return

    # ---- 2. 本地生成私钥和 CSR
    step(2, total, "本地生成私钥和 CSR")
    key_path = os.path.join(out, "distribution.key")
    csr_path = os.path.join(out, "distribution.certSigningRequest")
    cer_path = os.path.join(out, "distribution.cer")
    p12_path = os.path.join(out, "distribution.p12")

    cert_id = None
    p12_password = ""
    reuse = (not args.force) and os.path.exists(key_path) and os.path.exists(p12_path)

    # --reuse-cert：搬一套现成的私钥+证书进来，跳过签发。
    # 理由：发布证书是 Team 级的，一张能签这个 Team 下所有 App；
    # 而 Apple 只给 2 张名额，能复用就不该重签。
    if args.reuse_cert and not reuse:
        from cryptography import x509 as _x509
        from cryptography.hazmat.primitives import serialization as _ser

        src_dir = os.path.abspath(args.reuse_cert)
        src_key = os.path.join(src_dir, "distribution.key")
        src_cer = os.path.join(src_dir, "distribution.cer")
        for p_ in (src_key, src_cer):
            if not os.path.exists(p_):
                die(
                    "复用目录里缺文件：" + p_,
                    "这个目录要同时有 distribution.key 和 distribution.cer。\n"
                    "叫别的名字也行，改过来即可——必须是这一对。",
                )

        with open(src_key, "rb") as f:
            _key_pem = f.read()
        try:
            _key = _ser.load_pem_private_key(_key_pem, password=None)
        except Exception as e:
            die("读不了私钥：" + str(e), "私钥可能加了密码，先解开再放进来。")

        with open(src_cer, "rb") as f:
            _cer_raw = f.read()
        try:
            _cert = _x509.load_der_x509_certificate(_cer_raw)
        except Exception:
            try:
                _cert = _x509.load_pem_x509_certificate(_cer_raw)
            except Exception as e:
                die("读不了证书：" + str(e))

        def _spki(pub):
            return pub.public_bytes(_ser.Encoding.DER, _ser.PublicFormat.SubjectPublicKeyInfo)

        if _spki(_cert.public_key()) != _spki(_key.public_key()):
            die(
                "这套私钥和证书不是一对，配不上。",
                "用不配对的材料签出来的包，装机会报签名无效。\n"
                "去掉 --reuse-cert，让脚本重新签一张。",
            )

        import datetime as _dt

        cer_der = _cert.public_bytes(_ser.Encoding.DER)
        with open(key_path, "wb") as f:
            f.write(_key_pem)
        with open(cer_path, "wb") as f:
            f.write(cer_der)

        ok("复用已有私钥和证书（没重新签发，不占证书名额）")
        try:
            _cn = _cert.subject.get_attributes_for_oid(_x509.oid.NameOID.COMMON_NAME)[0].value
            info("证书：" + _cn)
        except Exception:
            pass
        try:
            _ou = _cert.subject.get_attributes_for_oid(_x509.oid.NameOID.ORGANIZATIONAL_UNIT_NAME)[0].value
            info("Team ID：" + _ou)
        except Exception:
            pass
        _end = getattr(_cert, "not_valid_after_utc", None)
        if _end is not None:
            _days = (_end - _dt.datetime.now(_dt.timezone.utc)).days
            info("到期：" + _end.date().isoformat() + "（还剩 " + str(_days) + " 天）")
            if _days < 30:
                info("快过期了，建议改成 --force 重签一张")

        p12_password = rand_password()
        with open(p12_path, "wb") as f:
            f.write(build_p12(_key, cer_der, p12_password))
        ok(".p12 已用新密码重新合成")
        reuse = True

    if reuse:
        if not p12_password:
            ok("cert/ 里已有证书和私钥，跳过重新签发")
            info("（想强制重签就加 --force）")
            # 复用模式下密码无从得知，让用户从上次的 secrets.txt 里读
            sec_path = os.path.join(out, "secrets.txt")
            if os.path.exists(sec_path):
                with open(sec_path, encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("IOS_CERT_PASSWORD="):
                            p12_password = line.split("=", 1)[1].strip()
        # 描述文件必须绑定「本地私钥对应的那张证书」的 id，
        # 随便挑一张会让打包时签名对不上。所以上一轮跑完时把 id 存了下来。
        cid_path = os.path.join(out, "cert-id.txt")
        if os.path.exists(cid_path):
            with open(cid_path, encoding="utf-8") as f:
                cert_id = f.read().strip() or None
            if cert_id:
                info("复用上次的证书 ID: " + cert_id)
    else:
        key, key_pem, csr_pem = gen_key_and_csr(APP_NAME)
        with open(key_path, "wb") as f:
            f.write(key_pem)
        with open(csr_path, "wb") as f:
            f.write(csr_pem)
        ok("私钥 " + os.path.basename(key_path) + " 已生成（2048 位 RSA）")
        ok("CSR " + os.path.basename(csr_path) + " 已生成")

        # ---- 3. 让 Apple 签证书
        step(3, total, "上传 CSR，让 Apple 签成发布证书")
        csr_b64 = base64.b64encode(csr_pem).decode("ascii")
        body = {
            "data": {
                "type": "certificates",
                "attributes": {
                    "certificateType": "DISTRIBUTION",
                    "csrContent": csr_b64,
                },
            }
        }
        status, payload = api("POST", "/v1/certificates", token, body)

        if status == 409:
            st2, p2 = api("GET", "/v1/certificates?filter[certificateType]=DISTRIBUTION&limit=200", token)
            names = []
            for it in (p2.get("data") or []):
                names.append((it.get("attributes") or {}).get("name") or it.get("id"))
            die(
                "证书数量到上限了（Apple 一般只给 2 张发布证书）",
                "已存在的证书：\n  " + ("\n  ".join(names) if names else "（读不到列表）") + "\n\n"
                "去后台吊销一张不用的：\n"
                "  developer.apple.com/account -> Certificates, Identifiers & Profiles\n"
                "  -> Certificates -> 勾选旧的 Distribution 证书 -> Revoke\n\n"
                "注意：吊销会让正在用那张证书的 App 失效，但已经上架的 App 不受影响。",
            )
        if status not in (200, 201):
            die("签发证书失败（HTTP " + str(status) + "）", api_err(payload))

        cert = payload.get("data") or {}
        cert_id = cert.get("id")
        content = (cert.get("attributes") or {}).get("certificateContent")
        if not content:
            die("Apple 返回里没有证书内容，可能是 API 版本变动。", json.dumps(payload)[:600])
        cer_der = base64.b64decode(content)
        with open(cer_path, "wb") as f:
            f.write(cer_der)
        ok("证书已签发并保存 " + os.path.basename(cer_path))
        if cert_id:
            with open(os.path.join(out, "cert-id.txt"), "w", encoding="utf-8") as f:
                f.write(cert_id)

        # ---- 4. 合成 p12
        step(4, total, "合成 .p12（CI 导入用）")
        p12_password = rand_password()
        p12_bytes = build_p12(key, cer_der, p12_password)
        with open(p12_path, "wb") as f:
            f.write(p12_bytes)
        ok(".p12 已生成（密码自动生成，见最后输出）")

    # ---- 5. Bundle ID
    step(5, total, "准备 Bundle ID: " + args.bundle_id)
    status, payload = api(
        "GET", "/v1/bundleIds?filter[identifier]=" + args.bundle_id, token
    )
    bundle_id = None
    if status == 200:
        for it in (payload.get("data") or []):
            if (it.get("attributes") or {}).get("identifier") == args.bundle_id:
                bundle_id = it.get("id")
                if not team_id:
                    team_id = (it.get("attributes") or {}).get("seedId") or ""
                break

    if bundle_id:
        ok("已存在，直接复用")
    else:
        body = {
            "data": {
                "type": "bundleIds",
                "attributes": {
                    "identifier": args.bundle_id,
                    "name": APP_NAME,
                    "platform": "UNIVERSAL",
                },
            }
        }
        status, payload = api("POST", "/v1/bundleIds", token, body)
        if status not in (200, 201):
            # 老接口只认 IOS，回退一次
            body["data"]["attributes"]["platform"] = "IOS"
            status, payload = api("POST", "/v1/bundleIds", token, body)
        if status not in (200, 201):
            die("建 Bundle ID 失败（HTTP " + str(status) + "）", api_err(payload))
        bundle_id = (payload.get("data") or {}).get("id")
        if not team_id:
            team_id = ((payload.get("data") or {}).get("attributes") or {}).get("seedId") or ""
        ok("已创建")

    # ---- 6. 描述文件
    step(6, total, "准备描述文件: " + args.profile)

    # 证书 id 复用模式下去查一下当前 Distribution 证书里最新的那张
    if not cert_id:
        # 没有存档的 id，就用本地 .cer 的 DER 内容和 Apple 上的证书逐一比对，
        # 找出「和本地私钥配对」的那一张。随便挑一张会导致签名对不上。
        local_der = None
        if os.path.exists(cer_path):
            with open(cer_path, "rb") as f:
                local_der = f.read()
        status, payload = api(
            "GET", "/v1/certificates?filter[certificateType]=DISTRIBUTION&limit=200", token
        )
        if status == 200:
            for it in (payload.get("data") or []):
                c = (it.get("attributes") or {}).get("certificateContent")
                if c and local_der and base64.b64decode(c) == local_der:
                    cert_id = it.get("id")
                    info("按证书内容匹配到本地私钥对应的那张")
                    # 存下来，下次就不用再扫一遍列表了
                    try:
                        with open(os.path.join(out, "cert-id.txt"), "w", encoding="utf-8") as f:
                            f.write(cert_id)
                    except Exception:
                        pass
                    break
    if not cert_id:
        die(
            "找不到和本地私钥配对的发布证书。",
            "cert/ 里的私钥和 Apple 上的证书对不上，说明私钥被删过或证书被吊销过。\n"
            "加 --force 重跑一次，会重新签一张（可能会占用证书名额，必要时先去后台吊销旧的）。",
        )

    # 同名 profile 先删掉，保证幂等
    status, payload = api("GET", "/v1/profiles?filter[name]=" + urllib.parse.quote(args.profile) + "&limit=200", token)
    if status == 200:
        for it in (payload.get("data") or []):
            if (it.get("attributes") or {}).get("name") == args.profile:
                api("DELETE", "/v1/profiles/" + it.get("id"), token)
                info("清掉同名的旧描述文件")

    body = {
        "data": {
            "type": "profiles",
            "attributes": {
                "name": args.profile,
                "profileType": "IOS_APP_STORE",
            },
            "relationships": {
                "bundleId": {"data": {"type": "bundleIds", "id": bundle_id}},
                "certificates": {"data": [{"type": "certificates", "id": cert_id}]},
            },
        }
    }
    status, payload = api("POST", "/v1/profiles", token, body)
    if status not in (200, 201):
        die("建描述文件失败（HTTP " + str(status) + "）", api_err(payload))

    prof = payload.get("data") or {}
    content = (prof.get("attributes") or {}).get("profileContent")
    if not content:
        die("Apple 返回里没有描述文件内容。", json.dumps(payload)[:600])
    prof_path = os.path.join(out, "distribution.mobileprovision")
    with open(prof_path, "wb") as f:
        f.write(base64.b64decode(content))
    ok("描述文件已保存 " + os.path.basename(prof_path))

    # ---- 汇总
    if not team_id:
        team_id = "<去 developer.apple.com/account 右上角看 Team ID>"

    def b64_of(path):
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")

    # 名字必须和 .github/workflows/ios.yml 里 secrets.XXX 完全一致，
    # 差一个字 CI 就会读成空值，然后在签名那步报一句看不懂的错。
    short = [
        ("APPLE_TEAM_ID", team_id),
        ("ASC_KEY_ID", args.key_id),
        ("ASC_ISSUER_ID", args.issuer_id),
        ("IOS_CERT_PASSWORD", p12_password),
        ("IOS_PROFILE_NAME", args.profile),
    ]
    long_vals = [
        ("ASC_KEY_CONTENT", b64_of(os.path.abspath(args.p8)), "AuthKey .p8 的 base64"),
        ("IOS_CERT_P12_BASE64", b64_of(p12_path), "distribution.p12 的 base64"),
        ("IOS_PROFILE_BASE64", b64_of(prof_path), "distribution.mobileprovision 的 base64"),
    ]

    sec_path = os.path.join(out, "secrets.txt")
    with open(sec_path, "w", encoding="utf-8") as f:
        f.write("# GitHub 仓库 -> Settings -> Secrets and variables -> Actions -> New repository secret\n")
        f.write("# 一条一个，名字照抄等号左边，值照抄等号右边（含 base64 长串，已转好，直接贴）\n")
        f.write("# 这个文件含私钥材料，.gitignore 已排除，别提交、别外传\n\n")
        for k, v in short:
            f.write(k + "=" + v + "\n")
        for k, v, _ in long_vals:
            f.write(k + "=" + v + "\n")

    print()
    print("  " + "=" * 54)
    print("  全部完成")
    print("  " + "=" * 54)
    print()
    print("  产物都在：" + out)
    print("    distribution.key          私钥（别外传）")
    print("    distribution.cer          Apple 签发的证书")
    print("    distribution.p12          CI 导入用的打包证书")
    print("    distribution.mobileprovision  描述文件")
    print("    secrets.txt               8 个 Secret 的成品，可直接粘贴")
    print()
    print("  短值（可以直接抄）：")
    print("  " + "-" * 54)
    for k, v in short:
        print("  " + k + "=" + v)
    print("  " + "-" * 54)
    print()
    print("  长值（base64，太长了不在这儿刷屏）：")
    for k, v, desc in long_vals:
        print("    " + k + "  <- " + desc + "，" + str(len(v)) + " 字符")
    print()
    print("  这 8 条已经全部写进下面的文件，打开复制粘贴即可：")
    print("    " + sec_path)
    print()
    repo = detect_repo()
    if repo:
        print("  仓库地址：https://github.com/" + repo + "/settings/secrets/actions")
    else:
        print("  仓库地址：https://github.com/<你的账号>/<你的仓库>/settings/secrets/actions")
    print()


if __name__ == "__main__":
    try:
        import urllib.parse  # noqa: F401
    except Exception:
        pass
    main()
