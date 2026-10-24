#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""shipconfig.py —— 项目配置的单一来源。

---

## 为什么要有这一层

这套工具最早是为一个具体项目写的，Bundle ID、scheme、描述文件名都硬编码在
三个脚本里。换一个项目就得改十几个地方，改漏一处就报一个莫名其妙的错。

所以把它抽出来：**所有项目相关的值只在这一个地方定义**。

## 配置从哪来（优先级从高到低）

1. 命令行参数（`--bundle-id` 之类，临时覆盖用）
2. `ship.config.json`（项目根目录，显式声明）
3. 从 `project.yml` 推断（XcodeGen 工程里这些都写着，没必要重复填）
4. 内置默认值 / 报错

绝大多数情况只需要第 3 条——工程里已经有 BUNDLE ID 和 scheme 名了。

## ship.config.json 长什么样

全部字段都可选，能给就给，不能给就推断：

```json
{
  "appName": "取景助手",
  "bundleId": "com.example.myapp",
  "scheme": "MyApp",
  "xcodeDir": "MyApp",
  "teamId": "ABCDE12345",
  "profileName": "MyApp AppStore",
  "feedbackEmail": "you@example.com",
  "testflightDescription": "一句话说明这个 App 做什么",
  "testflightWhatsNew": "本次要测什么"
}
```

`xcodeDir` 是源码目录名（含 Assets.xcassets 的那个），用于找 App 图标。
`testflightDescription` / `testflightWhatsNew` 是 TestFlight 里显示的两段文字，
不填就用通用文案，不会写死某个项目的业务描述。
"""

import json
import re
from pathlib import Path

CONFIG_NAME = "ship.config.json"

DEFAULTS = {
    "appName": None,
    "bundleId": None,
    "scheme": None,
    "xcodeDir": None,
    "teamId": None,
    "profileName": None,
    "feedbackEmail": None,
    "testflightDescription": None,
    "testflightWhatsNew": None,
}

# 这些缺了就没法跑
REQUIRED = ("bundleId", "scheme")


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [警告] {path.name} 解析失败（{e}），忽略它改用推断值。")
        return {}


def _infer_from_project_yml(ios_dir):
    """从 XcodeGen 的 project.yml 里把能推断的都推断出来。

    这里用正则而不是解析 YAML，因为不引入额外依赖——
    project.yml 只是我们自己的配置文件，格式稳定。
    """
    p = Path(ios_dir) / "project.yml"
    if not p.exists():
        return {}
    txt = p.read_text(encoding="utf-8", errors="replace")
    out = {}

    # scheme：优先 schemes 段的第一个 key，其次 targets 段的第一个 key
    m = re.search(r"^schemes:\s*$\s*^  (\w+):", txt, re.M)
    if not m:
        m = re.search(r"^targets:\s*$\s*^  (\w+):", txt, re.M)
    if m:
        out["scheme"] = m.group(1)

    # xcodeDir 是**源码目录**，不是 target 名——两者经常不同名。
    # （模板里 target 叫 MyApp，但源码放在 App/ 下。）
    # 真正的来源是 targets 段里 sources 的第一条 path。
    m = re.search(r"sources:\s*\n\s*-\s*path:\s*([^\s#]+)", txt)
    if m:
        out["xcodeDir"] = m.group(1).rstrip("/")
    elif "scheme" in out:
        out["xcodeDir"] = out["scheme"]

    m = re.search(r"PRODUCT_BUNDLE_IDENTIFIER:\s*([\w.\-]+)", txt)
    if m:
        out["bundleId"] = m.group(1)

    m = re.search(r"CFBundleDisplayName:\s*(.+)", txt)
    if m:
        out["appName"] = m.group(1).strip().strip('"').strip("'")

    m = re.search(r"DEVELOPMENT_TEAM:\s*([A-Z0-9]+)", txt)
    if m:
        out["teamId"] = m.group(1)

    return out


def load(ios_dir, overrides=None):
    """读配置，返回一个 dict。

    ios_dir 是项目的 ios/ 目录（含 project.yml 的那层）。
    overrides 是命令行传进来的值，优先级最高，None 的项会被忽略。
    """
    ios_dir = Path(ios_dir)
    cfg = dict(DEFAULTS)

    # 2. ship.config.json：先找 ios/，再找项目根
    for cand in (ios_dir / CONFIG_NAME, ios_dir.parent / CONFIG_NAME):
        if cand.exists():
            cfg.update({k: v for k, v in _read_json(cand).items() if k in DEFAULTS})
            cfg["_source"] = str(cand)
            break

    # 3. 从 project.yml 补空
    inferred = _infer_from_project_yml(ios_dir)
    for k, v in inferred.items():
        if not cfg.get(k) and v:
            cfg[k] = v
            cfg.setdefault("_inferred", []).append(k)

    # 1. 命令行覆盖
    for k, v in (overrides or {}).items():
        if k in DEFAULTS and v:
            cfg[k] = v

    # scheme 推断出来了，profileName 跟着派生
    if not cfg.get("profileName") and cfg.get("scheme"):
        cfg["profileName"] = f"{cfg['scheme']} AppStore"

    # teamId 兜底：从 secrets.txt 里捞
    if not cfg.get("teamId"):
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "tf_cfg", Path(__file__).resolve().parent / "testflight.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sec = mod.read_secrets()
            if sec.get("APPLE_TEAM_ID"):
                cfg["teamId"] = sec["APPLE_TEAM_ID"]
        except Exception:
            pass

    return cfg


def validate(cfg):
    """返回缺失的必填项名字列表。"""
    return [k for k in REQUIRED if not cfg.get(k)]


def describe(cfg):
    """给 doctor 用的一小段说明：这些值是从哪来的。"""
    src = cfg.get("_source")
    inferred = cfg.get("_inferred") or []
    if src:
        return f"配置来自 {Path(src).name}"
    if inferred:
        return f"从 project.yml 推断出：{', '.join(inferred)}"
    return "全部使用命令行参数"


def write_example(path, ios_dir=None):
    """生成一份样板配置。

    已经从 project.yml 推断出来的值直接填进去（省得用户改一遍还改错），
    推断不出来的留占位，并在返回时告诉调用方哪些还要手填。
    """
    path = Path(path)
    ios_dir = Path(ios_dir) if ios_dir else path.parent

    sample = {
        "appName": "MyApp",
        "bundleId": "com.example.myapp",
        "scheme": "MyApp",
        "xcodeDir": "App",
        "teamId": "",
        "profileName": "",
        "feedbackEmail": "",
        "testflightDescription": "",
        "testflightWhatsNew": "",
    }

    inferred = _infer_from_project_yml(ios_dir)
    filled = []
    for k, v in inferred.items():
        if k in sample and v:
            sample[k] = v
            filled.append(k)

    if sample["scheme"] and not sample["profileName"]:
        sample["profileName"] = f"{sample['scheme']} AppStore"

    path.write_text(json.dumps(sample, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path, filled
