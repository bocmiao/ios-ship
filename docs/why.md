# 为什么做这个，以及一些取舍

README 只讲怎么用。这里放背景、和现有方案的对比、实测数据、验证边界——看的人少，但决定要不要用的时候需要。

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
| `mobile-stores-cicd` | 65 个命令覆盖 ASC + Play 全流程 | 侧重元数据与 sideload，不含免 Mac 证书链 |
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

---

## 为什么不让 CI 创建证书

这是最想强调的一条。

让 CI 跑 `fastlane cert` 看似优雅，但**私钥会创建在一台几分钟后就被销毁的机器上**。Apple 那边留下一张永远用不了的证书，而每个团队的 Distribution 证书名额只有 **2 个**。

有人在一天内废掉了 3 张，第二张证书申请被拒的时候才发现。

正确做法：**私钥在自己机器上生成一次，CI 只负责导入。**

`setup` 就是这么做的。

---

## 一条命令背后的分层

`ship.py` 只做编排，真正的活分给三个脚本，每个都能单独跑：

```
ship.py  ──┬─▶ apple-auto.py   证书链
           ├─▶ testflight.py   分发闭环
           └─▶ make-icon.py    图标
                    │
              shipconfig.py    配置单一来源
```

分层理由、数据流、一次 `release` 的完整时序见 [`architecture.md`](architecture.md)。

---

## 目录结构

```
tools/
  ship.py               统一入口：doctor / setup / secrets / release / status / init
  apple-auto.py         证书链：CSR → 申请证书 → p12 → Bundle ID → 描述文件 → secrets.txt
  testflight.py         分发：内部测试组 + 挂 build + 加测试员 + 补 TestFlight 文案
  shipconfig.py         项目配置的单一来源（命令行 > ship.config.json > project.yml 推断）
  make-icon.py          生成 1024×1024 无 alpha 的 App 图标（需要 Pillow）
  selftest.py           离线自测：78 条断言，不联网不调 API
  ship.cmd              Windows 双击入口（纯 ASCII，自己找 Python）

template/
  project.yml           XcodeGen 工程描述（含必改项标注）
  .gitignore            挡签名材料，跟着模板一起复制过去
  App/                  最小 SwiftUI 工程 + 自检首页 + 图标
  .github/workflows/    ios.yml（签名构建上传）+ validate.yml（免签名编译校验）
  fastlane/Fastfile     只负责上传
  Gemfile               Ruby 3.4 缺的默认 gem 在这里补

docs/
  pitfalls.md           18 条实测踩坑
  asc-api.md            App Store Connect API 速查（端点/字段/错误码）
  architecture.md       各层职责与数据流
  why.md                本文件
```

教程只有一份，在 [README](../README.md) 里。不另开 quickstart，省得改了一处忘了另一处。

---

## 一些实测数据

- 一次完整构建：**3 分 45 秒**（macOS runner，含签名、导出、上传）
- 离线自测：**78 条断言**，3 秒内跑完
- TestFlight build 有效期：**90 天**
- 团队 Distribution 证书上限：**2 张**（不随时间恢复）
- 公开仓库的 macOS runner：**免费**；私有仓库 200 分钟/月，超出 $0.062/分钟
- 内部测试员上限：**100 人**（外部 10000 人）

---

## 验证到什么程度

写清楚哪些是真跑过的，哪些没有——省得你踩到一个我没踩过的坑还以为是我的锅。

**跑通过**（在一个真实的相机辅助 App 项目上，Windows 11 + `macos-latest`）：

- `doctor` 七组检查，包括团队已有 2 张证书时的名额提示
- `setup` 完整证书链：EC 私钥 → CSR → `POST /v1/certificates` → p12 → Bundle ID → `POST /v1/profiles`
- 生成 p12 在 macOS runner 上被 `security import` 接受（3DES + SHA1）
- `secrets` 写 8 个键进 GitHub
- 云端 Archive → 导出 IPA → 上传 TestFlight，`processingState` 到 `VALID`
- `testflight.py attach` 建内部组、挂 build、加测试员、补 `zh-Hans` 文案，手机上装上了
- 图标生成后过了苹果的 90713 / 90022 检查

**没跑通过**：

- `ship.py release` 的**全自动**版本。它要消耗一个 build 号，而上传不可撤销。实际发版时是分步走的：手动 `gh workflow run` → 等 VALID → `testflight.py attach`。`release` 把这三步按顺序封装了（代码路径都有单测覆盖，但没做过一次真实的全链路）。
- `--reuse-cert` 的**跨项目**复用。验证过公钥比对逻辑（私钥和证书不配对时确实会被拒），但只有一个项目需要签，没真的拿 A 项目的证书去签 B 项目。
- Linux 下的 `ship.cmd`（它只对 Windows 有意义）、以及脚本在 macOS 本机上的表现。

**几个数字是抄文档的，不是我测的**：TestFlight build 有效期 90 天、内部测试员上限 100 人、私有仓库 200 分钟/月。

证书上限 2 张也是官方口径——但确实在自己团队上查到**正好占了 2 张**（到 2027-09 和 2027-10），所以决定第二个项目不再申请、改为复用。`doctor` 里那条提示就是这么来的。没有真的去撞一次「第三张被拒」，因为不想把名额浪费在验证这件事上。

---

## 开发

```bash
python tools/selftest.py        # 78 条断言，几秒跑完
python tools/selftest.py -v     # 每条都打印
```

覆盖配置优先级、`project.yml` 的正则推断、必填校验、样板生成、凭据误提交检测，以及 PTP 容器和 ASC JWT 的结构。不联网、不调 API、不需要证书。

CI 除了在 Python 3.9 / 3.12 下各跑一遍，还有一步是**按 README 的步骤在空目录里真的装一遍**：确认 `cp -r template/.` 连点文件一起过去了、`init` 能推断出正确配置、`doctor` 在未配置状态下给的是可读指引而不是 Traceback。

写这一步的原因是：改 `shipconfig.py` 的推断逻辑时，把 `xcodeDir` 当成 target 名了。模板里 target 叫 `MyApp`、源码放 `App/` 下，于是 `doctor` 跑去 `MyApp/Assets.xcassets` 找图标，找不到就报「缺图标」——而图标明明在。这种错跑一次 `doctor` 就能看见，但得有人真的去跑。

---

## 适用边界

**能做**：SwiftUI / UIKit / Objective-C 原生工程，或任何最终产出 `.xcodeproj` 的项目（Flutter、React Native、Capacitor 加了 ios 目录之后同理）。

**不做**：不做 App 功能开发，不做上架文案和素材，不碰你的账号密码（全程只用 API Key，不需要 Apple ID 密码，也不会有二次验证弹窗）。

**注意**：签名材料（`.p8`、`.p12`、`distribution.key`、`secrets.txt`）永远不进 Git。模板自带 `.gitignore`，`doctor` 会真的去查 `git ls-files`。

---

## 为什么是 Apache-2.0

这类工程工具需要能被任何人无顾虑地用在自己的 CI 里。AGPL 对本地命令行工具提供不了额外保护，却会挡住一部分企业用户；GPL 与 App Store 的分发条款也有摩擦。
