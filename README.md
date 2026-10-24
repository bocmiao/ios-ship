# ios-ship

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![自测](https://github.com/bocmiao/ios-ship/actions/workflows/selftest.yml/badge.svg)](https://github.com/bocmiao/ios-ship/actions/workflows/selftest.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/tested%20on-Windows%2011-0078d6.svg)](#)
[![No Mac](https://img.shields.io/badge/requires-Mac-never-f7df1e.svg)](#)

**没有 Mac，也能把 iOS App 从零送到手机上能装。** 证书、签名、描述文件、CI 构建、TestFlight 分发，一条命令跑完，并且在动手之前先告诉你哪一步会失败。

> 在 Windows 上一路实测走通：申请证书 → 合成 p12 → 建 Bundle ID → 建描述文件 → 写 CI secrets → 云端 Archive 导出 IPA → 上传 TestFlight → 分配测试组 → iPhone 装上。全程没有碰过 Mac，也没有打开过 Keychain Access。

---

## 它解决什么

一个 Windows 开发者想做 iOS，遇到的第一句话通常是「你需要一台 Mac」。实际上真正必须 Mac 的只有一件事：**编译**。而编译现在可以交给 GitHub Actions 的 macOS runner。

真正卡人的是编译**前后**那些事：

- `Apple Distribution` 证书怎么申请？要从 CSR 开始，而生成 CSR 又要 Keychain Access。
- `.p12` 怎么导出？还是 Keychain Access。
- 描述文件呢？网页上点，但哪个 Bundle ID 对哪个证书，容易配错。
- 8 个 CI secrets，其中三个是几千字符的 base64，手动复制极容易漏字符。
- 上传成功了，TestFlight 里却是空的——因为**上传成功不等于能装上**。
- 下次发版报 `bundle version must be higher`——build 号忘了加，而上一次上传已经占掉了。

这些全靠点网页和试错，做过一遍就忘，换个项目再来一遍。

**这个工具把这些事全接过来。**

---

## 和现有方案的区别

iOS CI/CD 的轮子很多，但大部分有个共同前提：**你手里已经有 `.p12` 和 `.mobileprovision`**。

| 方案 | 它做什么 | 它没做什么 |
|---|---|---|
| `fastlane match` | 把签名材料加密存进 Git 仓库，CI 里自动装 | 首次创建证书/描述文件仍要 Mac 或交互式操作 |
| `apple-actions/import-codesign-certs` | CI 里导入 p12 | 假设 p12 已经存在 |
| [ios-builder (MobAI)](https://github.com/MobAI-App/ios-builder) | 从 Windows 触发云端构建 | 签名材料要自己准备；构建流程绑定 MobAI |
| [mobile-stores-cicd](https://github.com/) | 65 个命令覆盖 ASC + Play 全流程 | 侧重元数据与 sideload，不含免 Mac 证书链 |
| 各类教程文章 | 教你怎么点 | 点完就过时，不解决问题 |

**ios-ship 的定位：不教你怎么做，直接做完。** 而且多做两件事——**体检**和**分发闭环**。

`doctor` 会在跑之前把整条链扫一遍，把「最后一秒才炸的失败」提前到第一秒：

```
[4. 证书]
  OK   Apple Distribution: Your Name (TEAMID)   到期 2027-09-26
  OK   Apple Distribution: Your Name (TEAMID)   到期 2027-10-04
  !!   已有 2 张 Distribution 证书（团队上限 2 张）
       → 新项目请用 --reuse-cert 复用，别再申请，否则名额用光只能吊销
```

这一条我是在自己项目上跑出来的——**团队只有 2 张 Distribution 证书的名额**，但我之前完全不知道，差点在下一个项目上又申请一张。

---

## 快速开始

需要：Windows/macOS/Linux 任意一台、Python 3.9+、[GitHub CLI](https://cli.github.com/)、付费 Apple 开发者账号（$99/年）。

**其余零依赖**——`ship.py` / `apple-auto.py` / `testflight.py` 只用标准库，不需要 `pip install`，也不需要虚拟环境。唯一例外是 `make-icon.py`（要 Pillow 画图），但图标可以自己准备一张 PNG 顶替。

### 1. 拿到 API Key

去 **`appstoreconnect.apple.com`**（注意不是 `developer.apple.com`）：

```
用户和访问 → 集成 → App Store Connect API → 团队密钥 → 生成 API 密钥
Access 选 App Manager，建完立刻下载 .p8（离开页面就再也下不了）
```

顺手把页面上的 **Issuer ID** 抄下来。

> 两个坑：① 必须选**团队密钥**，个人密钥调不了证书接口；② 第一次用 API 要先点「请求访问权限」，等审核通过。

### 2. 装进你的项目

```bash
git clone https://github.com/YOUR/ios-ship

# 注意 template/. 结尾这个点 —— 用 template/* 会漏掉 .github 目录
cp -r ios-ship/tools    <你的iOS项目>/ios/tools
cp -r ios-ship/template/. <你的iOS项目>/ios/
```

然后在 `project.yml` 和两个 workflow 里改掉标了 ⚠️ 的值（`com.example.myapp`、`MyApp`、`App`）。

跑一次 `python tools/ship.py init` 会自动把 `project.yml` 里能推断的值填进 `ship.config.json`，省得四处改。

### 3. 体检 → 配置 → 发版

```bash
cd <你的iOS项目>/ios

python tools/ship.py doctor            # 先看缺什么
python tools/ship.py setup             # 首次：证书链全套（只问一次 Issuer ID）
python tools/ship.py secrets           # 把 8 个 secrets 写进 GitHub
python tools/ship.py release           # 发版，约 4 分钟
```

Windows 上可以直接双击 `tools\ship.cmd`。

---

## 六个命令

| 命令 | 做什么 | 会不会改动东西 |
|---|---|---|
| `doctor` | 7 组检查：本地材料 / 凭据 / App 记录 / 证书 / 描述文件 / 构建分发 / GitHub | **只读** |
| `setup` | 生成私钥和 CSR → Apple 签成证书 → 合成 p12 → 建 Bundle ID → 建描述文件 → 输出 secrets.txt | 会调 API 建资源 |
| `secrets` | 把 secrets.txt 的 8 个值写进 GitHub | 会写 GitHub |
| `release` | build 号 +1 → 推送 → 触发构建 → 等待 → 挂进内部测试组 | **会消耗一个 build 号** |
| `status` | 分发状态速查（能装？不能装？缺哪一步？） | 只读 |
| `init` | 从 `project.yml` 推断出 `ship.config.json` 样板 | 只写一个配置文件 |

`setup` 支持 `--reuse-cert <dir>`：**复用别的项目已有的证书**。同一张 Distribution 证书能签无数个 App，没必要每个项目都申请一张（名额只有 2 个）。

```bash
python tools/ship.py setup --bundle-id com.example.newapp --reuse-cert ../old-project/tools/cert
```

它会比对公钥，确认私钥和证书真的是配对的；不配对直接拒绝，不会给你一个永远签不过的 p12。

---

## 这条链上真实存在的坑

完整 17 条在 [`docs/pitfalls.md`](docs/pitfalls.md)，这里挑几个最费时间的：

**p12 的算法必须指定。** Python `cryptography` 默认产出 AES-256 + SHA256 MAC 的 p12，openssl 能正常读，但 **macOS 的 Security.framework 只认 3DES + SHA1**，`security import` 会报 `MAC verification failed (wrong password?)` ——**提示说密码错，其实密码是对的**。这个坑我查了两轮才发现。

**上传的 409 有两种完全相反的含义。** 一种是真的失败（版本号不一致），另一种是**上传其实成功了**——Apple 收下二进制后有几秒不在 builds 列表里，altool 自动重试时撞上「build 已存在」，把成功报成失败。所以看到 409 要**先查 builds 列表**，别直接重传。

**TestFlight 里空白，多半是没分配测试组。** 而且 `POST /v1/betaGroups` **默认建的是外部组**，外部组的第一个 build 必须过 Beta App Review（1~3 天）。建内部组必须显式写 `"isInternalGroup": true`，而且这个字段 `PATCH` 改不了，建错了只能删了重建。

**别把上传挂在 `push` 上。** 我有一次用个人项目试，改个注释、提交一版编不过的代码，都自动跑完签名上传，白烧了 3 个 build 号。上传不可撤销，占掉的号拿不回来。

---

## 为什么不让 CI 创建证书

这是我最想强调的一条。

让 CI 跑 `fastlane cert` 看似优雅，但**私钥会创建在一台几分钟后就被销毁的机器上**。Apple 那边留下一张永远用不了的证书，而每个团队的 Distribution 证书名额只有 **2 个**。

有人在一天内废掉了 3 张，第二张证书申请被拒的时候才发现。

正确做法：**私钥在自己机器上生成一次，CI 只负责导入。**

`setup` 就是这么做的。

---

## 目录

```
tools/
  ship.py               统一入口：doctor / setup / secrets / release / status / init
  apple-auto.py         证书链：CSR → 申请证书 → p12 → Bundle ID → 描述文件 → secrets.txt
  testflight.py         分发：内部测试组 + 挂 build + 加测试员 + 补 TestFlight 文案
  shipconfig.py         项目配置的单一来源（命令行 > ship.config.json > project.yml 推断）
  make-icon.py          生成 1024×1024 无 alpha 的 App 图标（需要 Pillow）
  selftest.py           离线自测：57 条断言，不联网不调 API
  ship.cmd              Windows 双击入口（纯 ASCII，自己找 Python）

template/
  project.yml           XcodeGen 工程描述（含必改项标注）
  App/                  最小 SwiftUI 工程 + 自检首页 + 图标
  .github/workflows/    ios.yml（签名构建上传）+ validate.yml（免签名编译校验）
  fastlane/Fastfile     只负责上传
  Gemfile               Ruby 3.4 缺的默认 gem 在这里补

docs/
  pitfalls.md           17 条实测踩坑
  asc-api.md            App Store Connect API 速查（端点/字段/错误码）
  architecture.md       各层职责与数据流
```

---

## 自测

```bash
python tools/selftest.py        # 57 条断言，几秒跑完
python tools/selftest.py -v     # 每条都打印
```

覆盖配置优先级、`project.yml` 的正则推断、必填校验、样板生成，以及 PTP 容器和 ASC JWT 的结构。不联网、不调 API、不需要证书。

CI 除了在 Python 3.9 / 3.12 下各跑一遍，还有一步是**按 README 的步骤在空目录里真的装一遍**：确认 `cp -r template/.` 连点文件一起过去了、`init` 能推断出正确配置、`doctor` 在未配置状态下给的是可读指引而不是 Traceback。

写这一步的原因是：我改 `shipconfig.py` 的推断逻辑时，把 `xcodeDir` 当成 target 名了。模板里 target 叫 `MyApp`、源码放 `App/` 下，于是 `doctor` 跑去 `MyApp/Assets.xcassets` 找图标，找不到就报「缺图标」——而图标明明在。这种错跑一次 `doctor` 就能看见，但得有人真的去跑。

---

## 一些实测数据

- 一次完整构建：**3 分 45 秒**（macOS runner，含签名、导出、上传）
- TestFlight build 有效期：**90 天**
- 团队 Distribution 证书上限：**2 张**（不随时间恢复）
- 公开仓库的 macOS runner：**免费**；私有仓库 200 分钟/月，超出 $0.062/分钟
- 内部测试员上限：**100 人**（外部 10000 人）

---

## 适用边界

**能做**：SwiftUI / UIKit / Objective-C 原生工程，或任何最终产出 `.xcodeproj` 的项目（Flutter、React Native、Capacitor 加了 ios 目录之后同理）。

**不做**：不做 App 功能开发，不做上架文案和素材，不碰你的账号密码（全程只用 API Key，不需要 Apple ID 密码，也不会有二次验证弹窗）。

**注意**：签名材料（`.p8`、`.p12`、`distribution.key`、`secrets.txt`）永远不进 Git，`.gitignore` 已经排好了。`doctor` 会检查你项目里有没有误提交。

---

## License

Apache-2.0。见 [LICENSE](LICENSE)。

> 选 Apache-2.0 而不是 GPL/AGPL：这类工程工具需要能被任何人无顾虑地用在自己的 CI 里。AGPL 对本地命令行工具提供不了额外保护，却会挡住一部分企业用户。
