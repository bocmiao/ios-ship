# 设计取舍与验证范围

## 解决的问题

Windows 上做 iOS，只有编译环节需要 macOS，可以交给 GitHub Actions 的 `macos-latest` runner。编译前后的事项没有平台限制，但都在 Apple 的网页后台手工完成：

- Apple Distribution 证书：从 CSR 开始，而生成 CSR 依赖 Keychain Access。
- `.p12` 导出：依赖 Keychain Access。
- 描述文件：网页创建，需手工对应 Bundle ID 与证书。
- 8 个 CI secrets，其中三个是数千字符的 base64，手工复制易出错。
- 上传成功但 TestFlight 为空：缺少内部测试组或测试员。
- 下次发版报 `bundle version must be higher`：build 号未递增，上一次上传已占用。

换项目时上述流程需重做一遍。

## 和现有方案的区别

iOS CI/CD 工具的共同前提是 `.p12` 与 `.mobileprovision` 已经存在：

| 方案 | 做什么 | 不做什么 |
|---|---|---|
| `fastlane match` | 签名材料加密存入 Git 仓库，CI 内自动装载 | 首次创建证书与描述文件仍需 Mac 或交互操作 |
| `apple-actions/import-codesign-certs` | CI 内导入 p12 | 假定 p12 已存在 |
| [ios-builder (MobAI)](https://github.com/MobAI-App/ios-builder) | 从 Windows 触发云端构建 | 签名材料需自行准备；流程绑定 MobAI |
| `mobile-stores-cicd` | 65 个命令覆盖 ASC 与 Play 流程 | 侧重元数据与 sideload，不含免 Mac 证书链 |

ios-ship 从 CSR 开始生成签名材料，并提供工具体检与 TestFlight 分发：

```
[4. 证书]
  OK   Apple Distribution: Your Name (TEAMID)   到期 2027-09-26
  OK   Apple Distribution: Your Name (TEAMID)   到期 2027-10-04
  !!   已有 2 张 Distribution 证书（团队上限 2 张）
       → 新项目请用 --reuse-cert 复用，别再申请，否则名额用光只能吊销
```

## 为什么不让 CI 创建证书

`fastlane cert` 在 CI 内运行，私钥生成在临时机器上，任务结束后随机器销毁。Apple 侧留下无法使用的证书，而团队 Distribution 证书上限为 2 张。

私钥在本机生成一次，CI 只负责导入。`setup` 按此实现。

## 脚本分层

`ship.py` 只做编排，具体工作由三个脚本完成，每个可单独运行：

```
ship.py  ──┬─▶ apple-auto.py   证书链
           ├─▶ testflight.py   分发闭环
           └─▶ make-icon.py    图标
                    │
              shipconfig.py    配置单一来源
```

分层理由、数据流、一次 `release` 的时序在 [`architecture.md`](architecture.md)。

## 目录结构

```
tools/
  ship.py               统一入口：doctor / setup / secrets / release / status / init
  apple-auto.py         证书链：CSR → 申请证书 → p12 → Bundle ID → 描述文件 → secrets.txt
  testflight.py         分发：内部测试组 + 挂 build + 加测试员 + 补 TestFlight 文案
  shipconfig.py         项目配置的单一来源（命令行 > ship.config.json > project.yml 推断）
  make-icon.py          生成 1024×1024 无 alpha 的 App 图标（需要 Pillow）
  selftest.py           离线自测：78 条断言，不联网不调 API
  ship.cmd              Windows 双击入口（纯 ASCII，自行探测 Python）

template/
  project.yml           XcodeGen 工程描述（含必改项标注）
  .gitignore            挡签名材料，随模板一起复制
  App/                  最小 SwiftUI 工程 + 自检首页 + 图标
  .github/workflows/    ios.yml（签名构建上传）+ validate.yml（免签名编译校验）
  fastlane/Fastfile     只负责上传
  Gemfile               Ruby 3.4 缺失的默认 gem

docs/
  pitfalls.md           18 条实测问题
  asc-api.md            App Store Connect API 速查（端点/字段/错误码）
  architecture.md       各层职责与数据流
  why.md                设计取舍与验证范围
```

## 实测数据

- 一次完整构建：3 分 45 秒（macOS runner，含签名、导出、上传）
- 离线自测：78 条断言，3 秒内跑完
- TestFlight build 有效期：90 天
- 团队 Distribution 证书上限：2 张，不随时间恢复
- macOS runner：public 仓库免费；private 仓库 200 分钟/月，超出 $0.062/分钟
- 内部测试员上限：100 人，外部 10000 人

## 验证范围

**已跑通**（Windows 11 客户端 + `macos-latest` runner）：

- `doctor` 七组检查，包括团队已有 2 张证书时的名额提示
- `setup` 完整证书链：EC 私钥 → CSR → `POST /v1/certificates` → p12 → Bundle ID → `POST /v1/profiles`
- 生成的 p12 被 macOS runner 的 `security import` 接受（3DES + SHA1）
- `secrets` 写入 8 个键进 GitHub
- 云端 Archive → 导出 IPA → 上传 TestFlight，`processingState` 到达 `VALID`
- `testflight.py attach` 创建内部组、挂 build、加测试员、补 `zh-Hans` 文案，设备安装成功
- 图标通过 90713 / 90022 检查

**未跑通**：

- `ship.py release` 全自动版本。该命令消耗一个 build 号且上传不可撤销，实际发版由手动 `gh workflow run`、等待 `VALID`、`testflight.py attach` 三步完成。`release` 按顺序封装了这三步，代码路径有单元测试覆盖，完整链路未执行过。
- `--reuse-cert` 的跨项目复用。公钥比对逻辑已验证（私钥与证书不配对时被拒绝），但只有单一项目需要签名，未用 A 项目的证书签过 B 项目。
- Linux 与 macOS 本机上的脚本行为。`ship.cmd` 仅适用于 Windows。

**数据来源为官方文档而非实测**：TestFlight build 有效期 90 天、内部测试员上限 100 人、private 仓库 200 分钟/月。

证书上限 2 张为官方说明，另在团队账号上查到已占满 2 张（到期 2027-09 与 2027-10），据此决定第二个项目复用而不新申请。`doctor` 的名额提示由此而来。第三张证书被拒的实际响应未验证。

## 开发

```bash
python tools/selftest.py        # 78 条断言，几秒跑完
python tools/selftest.py -v     # 每条都打印
```

覆盖配置优先级、`project.yml` 的正则推断、必填校验、样板生成、凭据误提交检测，以及 PTP 容器和 ASC JWT 结构。不联网、不调 API、不需要证书。

CI 在 Python 3.9 与 3.12 下各跑一次自测，另按 README 步骤在空目录安装一遍：确认 `cp -r template/.` 复制到点文件、`init` 推断出正确配置、`doctor` 在未配置状态下给可读指引而非 Traceback。

该步骤起因于 `shipconfig.py` 曾把 `xcodeDir` 推断为 target 名。模板的 target 为 `MyApp`、源码在 `App/`，`doctor` 因此在 `MyApp/Assets.xcassets` 下查找图标并报缺失。

## 适用边界

**支持**：SwiftUI / UIKit / Objective-C 工程，以及任何最终产出 `.xcodeproj` 的项目（Flutter、React Native、Capacitor 含 ios 目录）。

**不支持**：App 功能开发、上架文案与素材、Apple ID 密码。全程只使用 API Key，无密码输入与二次验证。

签名材料（`.p8`、`.p12`、`distribution.key`、`secrets.txt`）不进 Git。模板自带 `.gitignore`，`doctor` 通过 `git ls-files` 检查。

## 为什么是 Apache-2.0

工程工具需要能被无顾虑地用于他人 CI。AGPL 对本地命令行工具无额外保护作用，且会阻碍部分企业用户；GPL 与 App Store 分发条款存在冲突。
