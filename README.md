# ios-ship

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![自测](https://github.com/bocmiao/ios-ship/actions/workflows/selftest.yml/badge.svg)](https://github.com/bocmiao/ios-ship/actions/workflows/selftest.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/tested%20on-Windows%2011-0078d6.svg)](#)
[![No Mac](https://img.shields.io/badge/requires-Mac-never-f7df1e.svg)](#)

没有 Mac，也能把 iOS App 从零送到手机上能装。证书、签名、描述文件、CI 构建、TestFlight 分发，一条命令跑完，动手前先告诉你哪一步会失败。

**这份 README 只讲怎么用。** 为什么这么设计、和 fastlane 的区别、实测数据、验证到什么程度，在 [`docs/why.md`](docs/why.md)。

**目录**：[使用教程](#使用教程) · [Q&A](#qa) · [命令速查](#命令速查) · [文档](#文档)

---

# 使用教程

## 前置条件

| 需要 | 说明 |
|---|---|
| 一台电脑 | Windows / macOS / Linux 都行。本文命令以 Windows 为例 |
| Python 3.9+ | 只需要它跑脚本，[python.org](https://python.org/downloads/) 下载，安装时勾选 Add to PATH |
| 一个 Python 包 | `cryptography`（生成 CSR、签 API 用的 JWT）。`ship.py deps` 会帮你装 |
| [GitHub CLI](https://cli.github.com/) | 写 secrets、触发构建用。`gh auth login` 登录一次 |
| 付费 Apple 开发者账号 | $99/年。免费账号用不了 App Store Connect API |
| 一个 GitHub 仓库 | public 仓库的 macOS runner 免费；private 每月 200 分钟 |

不需要的东西：Mac、Xcode、Keychain Access、Ruby、fastlane（它只在云端跑）。

---

## 教程 1：第一次，从零到手机装上

动手操作的部分大约 20 分钟。但有两个等待不在你控制之内：如果这个账号是第一次用 API，要先等苹果审核「请求访问权限」（几分钟到一天）；每次构建要等 4 分钟左右。

### 步骤 1：建 API Key

打开 **`appstoreconnect.apple.com`**（注意不是 `developer.apple.com`，那是另一个站）。

```
用户和访问 → 集成 → App Store Connect API → 团队密钥 → 生成 API 密钥
```

三个必须注意的地方：

- **一定要选「团队密钥」**，不是「个人密钥」。个人密钥调不了证书接口。
- **Access 选 App Manager**（Admin 也行）。
- **建完立刻下载 `.p8`**，离开页面就再也下不了了。

顺手把页面上的 **Issuer ID** 抄下来（一串 UUID 形式的东西），下一步要用。

> 如果这是这个账号第一次用 API，还需要点一次 **请求访问权限**，等苹果审核通过。没过之前所有 API 调用都会被拒。

### 步骤 2：装进你的项目

```bash
git clone https://github.com/bocmiao/ios-ship

# 注意 template/. 结尾这个点 —— 用 template/* 会漏掉 .github 和 .gitignore
cp -r ios-ship/tools      <你的iOS项目>/ios/tools
cp -r ios-ship/template/. <你的iOS项目>/ios/
```

然后改掉标了 ⚠️ 的地方。就三处，都在 `project.yml` 前 12 行的注释里写着：

```yaml
name: MyApp                                    # ← 1. 你的工程名
options:
  bundleIdPrefix: com.example                  # ← 2. 你的反向域名
targets:
  MyApp:                                       # ← 3. target 名，通常和工程名一致
```

再改 `.github/workflows/ios.yml` 和 `validate.yml` 顶部的 `env:`，四个值要和上面一致：

```yaml
env:
  PROJECT: MyApp              # project.yml 里的 name:
  SCHEME: MyApp               # project.yml 里 schemes: 下的 key
  APP_NAME: MyApp             # target 名，决定 .ipa 文件名
  BUNDLE_ID: com.example.myapp
```

> 为什么分四个而不是一个？因为它们**经常同名但不是必须同名**。四者混用是这类流水线最常见的失败原因，宁可多写一行。

### 步骤 3：装依赖 + 生成配置

```bash
cd <你的iOS项目>/ios

python tools/ship.py deps     # 检查并安装 cryptography（会先问一句）
python tools/ship.py init     # 读 project.yml，生成 ship.config.json
```

`deps` 长这样：

```
  !!   cryptography 没装（必需）  证书申请与 API 鉴权
  OK   pillow（可选）  生成 App 图标
将执行：python -m pip install cryptography
现在安装吗？[Y/n]
```

`init` 会把能推断的值填进 `ship.config.json`：

```
  OK   已生成 C:\...\ios\ship.config.json
       已从 project.yml 自动填好：scheme, xcodeDir, bundleId, appName
       还空着的要手填：teamId、repo、feedbackEmail
```

打开 `ship.config.json` 补上 `teamId`（开发者账号页面右上角的 10 位 Team ID）和 `repo`（`你的名字/仓库名`）。

### 步骤 4：体检

```bash
python tools/ship.py doctor
```

**这一步别跳过。** 它只读、不改任何东西，但会把整条链扫一遍，把「最后一秒才炸的失败」提前到现在：

```
[1. 本地材料]
  XX   找不到 AuthKey_*.p8
  XX   缺 distribution.key
  OK   project.yml 版本  1.0 (build 1)
  OK   App 图标
  OK   凭据没有进 Git
```

照着 `→` 后面的提示补。现在这个阶段报错是**正常的**，因为还没做证书。

### 步骤 5：建证书链

把你的 `.p8` 放到 `ios/tools/cert/`（目录不存在就建一个），然后：

```bash
python tools/ship.py setup
```

它会依次问你 Issuer ID、Key ID，然后自动做完这些：

```
生成私钥 (P-256) → CSR → 向 Apple 申请证书 → 合成 p12
→ 建 Bundle ID → 建描述文件 → 输出 secrets.txt（8 个键）
```

跑完 `ios/tools/cert/` 里会有：`distribution.key`、`distribution.cer`、`distribution.p12`、`secrets.txt`。

> **如果你之前已经给别的项目做过证书**，别重复申请了——团队只有 2 张名额，用完只能吊销。用 `--reuse-cert` 指向旧项目的 cert 目录：
>
> ```bash
> python tools/ship.py setup --reuse-cert ../old-project/ios/tools/cert
> ```
>
> 它会比对公钥，确认私钥和证书真的配对，不配对直接拒绝。

### 步骤 6：建 App 记录（只能手动，一步）

**苹果没有开放创建 App 记录的 API**，这一步必须在网页上点：

```
appstoreconnect.apple.com → 我的 App → 左上角 + → 新建 App
  平台：iOS
  名称：随便填（后面能改）
  主要语言：简体中文
  Bundle ID：选你刚建的那个
  SKU：自己定，比如 myapp001（内部标识，不公开）
```

**只需要建一次**，以后所有版本都往这条记录上推。

> 注意 App ID 和 App 记录是**两个东西**：developer 后台的 Identifier 只是个字符串，App Store Connect 里的「App 记录」才是商店条目。上传认的是后者。

### 步骤 7：写 secrets

```bash
python tools/ship.py secrets
```

它把 `secrets.txt` 里的 8 个值逐条写进 GitHub 仓库（用 `gh`）。写完可以去 `仓库 → Settings → Secrets and variables → Actions` 核对，应该是 8 条。

### 步骤 8：发版

```bash
python tools/ship.py release
```

它做的事：build 号 +1 → 提交推送 → 触发云端构建 → 等结果 → 挂进内部测试组。

**这一步会消耗一个 build 号**，而上传不可撤销、占掉的号拿不回来。所以它会先问你一次，确认了才动。

大约 4 分钟后：

```
[构建]
  等待中... (12/90)
  完成：success
[分发]
  OK   内部测试组 Internal 已有 build 4
  OK   测试员：you@example.com
iPhone 打开 TestFlight 就能装，通常几分钟内可见。
```

### 步骤 9：装上手机

App Store 里装 **TestFlight**，用**同一个 Apple ID**（就是你 Apple 开发者账号那个）登录，App 会出现在列表里。

第一次装完，打开 App 应该看到一个自检页，显示版本号、build 号、Bundle ID、设备、系统版本。**看到这页说明整条链通了。**

---

## 教程 2：改完代码发新版本

```bash
python tools/ship.py release
```

就这一条。build 号自动加，不用管。

如果你只是想确认代码能不能编过、不想消耗 build 号：

- 提交代码后，`validate.yml` 会自动跑（免签名编译检查）
- 或者在本地跑 `python tools/ship.py doctor` 看配置有没有被动坏

> **别把 `ios.yml` 挂到 push 上。** 模板里只留了 `workflow_dispatch`，这是刻意的：改个注释就自动上传会把 build 号白烧掉。

---

## 教程 3：第二个项目

复用第一张证书，别再申请：

```bash
cp -r <旧项目>/ios/tools/cert  <新项目>/ios/tools/cert
cd <新项目>/ios
python tools/ship.py init
python tools/ship.py setup --reuse-cert tools/cert --bundle-id com.example.newapp
```

`--reuse-cert` 会校验私钥和证书配对，然后只补 Bundle ID 和描述文件。

---

## 教程 4：看懂 doctor 报告

七组检查，从本地往云端排。靠前的组挂了，后面的不用看。

| 组 | 查什么 | 挂了怎么办 |
|---|---|---|
| 1. 本地材料 | `.p8`、`.key`、`.cer`、`.p12`、`secrets.txt`、版本号、图标、**凭据有没有被 git 跟踪** | 跑 `setup`；图标跑 `make-icon.py` |
| 2. 凭据与账号 | 能不能生成 JWT、能不能调通 API | 检查 Key ID / Issuer ID / 是不是团队密钥 |
| 3. App 记录 | ASC 里那条记录在不在 | 手动建（教程 1 步骤 6） |
| 4. 证书 | 有效期、剩余名额 | 名额满了就 `--reuse-cert` |
| 5. 描述文件 | 有没有、是否匹配 bundleId、是否过期 | 重跑 `setup` |
| 6. 构建与分发 | secrets 齐不齐、最近一次 run、有没有 VALID build、在不在内部组 | 跑 `secrets` 或 `release` |
| 7. GitHub 仓库 | 远端、分支、gh 认证 | `gh auth login` |

符号含义：`OK` 通过，`!!` 提醒（能跑但迟早出事），`XX` 阻塞（不修就跑不通）。退出码 1 表示有阻塞项，适合放进 CI 当门禁。

---

## 教程 5：TestFlight 里没有 / 装不上

按这个顺序排查，九成的问题在前两步：

```bash
python tools/ship.py status
```

它会直接告诉你缺哪一步。三个「上传成功」和「能装上」之间的鸿沟：

1. **build 处理完了吗？** —— `processingState` 必须是 `VALID`。刚上传的几分钟内是 `PROCESSING`。
2. **挂进测试组了吗？** —— 光有 VALID 的 build，没分配进内部组，TestFlight 里就是空的。
3. **你在组里吗？** —— 组成员用同一个 Apple ID 登录 TestFlight 才能看到。

如果三步都满足还是看不到：确认手机上登录的 Apple ID 和你加进组的是同一个，然后杀掉 TestFlight 重开。

---

## 教程 6：怀疑凭据泄露了

先跑体检，它会直接查 `git ls-files`：

```bash
python tools/ship.py doctor
```

看到 `XX 凭据文件已经被 git 跟踪`，按提示做：

```bash
git rm --cached ios/tools/cert/secrets.txt ios/tools/cert/distribution.p12
git commit -m "移除误提交的凭据"
```

但要注意：**这不会从历史里删掉**。如果已经推到公开仓库，按 [`docs/pitfalls.md` 第 17 条](docs/pitfalls.md) 评估要不要轮换 p12 密码。

评估方法很简单：泄露的是**完整密码**还是**片段**。完整的立刻换；只泄露长度或首尾字符，边界在于剩余熵够不够。

---

# Q&A

## 能力边界

**真的一点 Mac 都不需要？**

编译必须有 Mac——但不必须是你的。GitHub 的 `macos-latest` runner 就是一台真 Mac 云机，这条链上唯一用到它的地方就是编译。证书、签名、上传全在你自己电脑上完成。

**能上架 App Store 吗，还是只能 TestFlight？**

同一条链。导出的 IPA 就是用于 App Store 的那份（`method = app-store-connect`），上传后在 App Store Connect 里点「提交审核」即可。

本工具**不做**上架文案、截图、隐私清单——那些是人的活。

**支持 Flutter / React Native / Unity 吗？**

只要最终产出 `.xcodeproj` 就支持。前提是这些框架产出的工程能用 XcodeGen 描述，或者你手写 `project.yml` 指向它们的 target。

**App Clip / Watch App / 扩展能用吗？**

模板是单 target 的。多 target 需要自己在 `project.yml` 里加，`ios.yml` 的 archive 步骤也得改（多 target 要各自配描述文件）。工具本身不拦你，但默认配置不管这些。

**会不会改我的 App 代码？**

不会。它只碰签名材料、`project.yml` 的版本号、以及 `ship.config.json`。

**免费开发者账号能用吗？**

不能。App Store Connect API 属于付费开发者计划（$99/年）。

**团队账号能用吗？**

能。但需要 **Account Holder** 去建团队密钥并点一次「请求访问权限」。

**Windows 上非要装 Python 吗？**

要。但只需要 Python，不用装 Ruby、不用装 Xcode。

Python 包只需要一个 `cryptography`（生成 CSR、签 JWT），`ship.py deps` 会帮你装。`make-icon.py` 另外要 Pillow，但那是可选的——自己有 1024×1024 的 PNG 就不用装。

`init` / `secrets` 这两个命令是纯标准库，连 `cryptography` 都不需要。

---

## 证书与名额

**团队只剩证书名额了怎么办？**

Distribution 证书一张能签**无数个 App**，它是团队级的。所以永远只需要一张，新项目用 `--reuse-cert` 复用。

名额上限 2 张且**不随过期恢复**——过期了还占着，要手动吊销才腾位置。

**证书/描述文件过期会怎样？**

构建会失败，或者装上的包打不开。重跑 `python tools/ship.py setup` 重新生成（证书没过期的话只补描述文件，不消耗名额）。

**换电脑了怎么办？**

把 `ios/tools/cert/` 整个目录拷过去就行，里面私钥、证书、p12、secrets 都齐了。**不要重新 setup**，那会浪费名额。

**能多人协作吗？**

能，但**不要每人都 setup 一遍**——那会各自消耗名额。一个人持有 `cert/` 目录，离线传给其他人（U 盘、加密压缩包），或者直接让 CI 跑构建，别人只改代码。

---

## 安全

**我的密码会经过你的服务器吗？**

不经过任何服务器。没有服务器——所有 API 调用都是你本机直连 `api.appstoreconnect.apple.com`，用的是你下载的 `.p8` 签的 JWT。

**`cert/` 目录会被提交吗？**

模板自带 `.gitignore`，已经排掉了 `cert/`、`*.p8`、`*.p12`、`secrets.txt`、`*.key`、`*.mobileprovision`。

`doctor` 会**真的去查** `git ls-files`，发现被跟踪就报阻塞项——因为 `.gitignore` 只挡未跟踪的文件，已经 `git add` 过的它管不住。

**CI 里私钥怎么用的？**

从 secret 解到 `$RUNNER_TEMP`，导入一个临时 keychain，用完连同 keychain 一起销毁。**CI 不创建证书**——私钥只在你本机生成一次。

**为什么不能「push 就自动发版」？**

因为上传不可撤销，而 build 号用掉就拿不回来。改个 README 也上传一版，几次就把号烧完了。想自动做编译检查用 `validate.yml`（免签名、不消耗 build 号）。

---

## 常见报错

**`MAC verification failed (wrong password?)` —— 但我密码是对的**

密码大概率真没错。是 p12 的加密算法不对：`macOS 的 Security.framework` 只认 3DES + SHA1 MAC，而 Python `cryptography` 默认产出 AES-256 + SHA256。报错文案里的 `wrong password?` 是框架顺手拼的猜测，别信。

工具已经处理了（`-certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1`）。如果你用自己的脚本，把这三个参数抄过去。

**`Couldn't find app 'com.xxx' on the account`**

App 记录没建。教程 1 步骤 6，手动建一条。

**上传报 409，到底成没成？**

两种完全相反的含义：

- 版本号不一致 → 真失败
- **上传其实成功了** → Apple 收下二进制后有几秒不在列表里，altool 重试时撞上「build 已存在」，把成功报成失败

**先查，别重传**：`python tools/ship.py status`。

**`The bundle version must be higher than '3'`**

build 号没加，或者被 `push` 触发自动上传烧掉了。`release` 会自动加；手动的话改 `project.yml` 的 `CURRENT_PROJECT_VERSION`。

**`90713 Missing CFBundleIconName` / `90022 Missing 120x120`**

缺图标，而且是**上传阶段**才报（编译签名全绿）。两个条件都要满足：有 `Assets.xcassets/AppIcon.appiconset/`（1024×1024 无 alpha），且 `project.yml` 里有 `ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon`。

第二条最容易漏——没有它，asset catalog 不会编进包。

**`cannot load such file -- mutex_m`**

runner 是 Ruby 3.4，fastlane 依赖的 `mutex_m`/`base64`/`bigdecimal` 已从默认 gem 移除。模板的 `Gemfile` 里已经显式声明了。

**`The 'locale' value is invalid`**

写 `zh-Hans`，不是 `zh` 也不是 `zh-CN`。

**Windows 上 `bash: command not found`**

Git for Windows 只把 `Git\cmd` 加进 PATH，`bash` 不在。用 `tools\ship.cmd`（双击也行），它不依赖 bash。

**控制台中文变乱码**

cmd.exe 按 GBK 解码 UTF-8。`ship.cmd` 里已经 `chcp 65001` 处理过；直接 `python tools/ship.py` 的话，先手动跑一次 `chcp 65001`。

**`python` 命令打开了微软商店**

那是 `WindowsApps` 下的 0 字节 Store 占位符，不是真解释器。装一次 python.org 的版本，或用 `ship.cmd`（它会真的执行 `python -c` 来判断，不看 `where` 的结果）。

---

# 命令速查

| 命令 | 做什么 | 有副作用吗 |
|---|---|---|
| `ship.py deps` | 检查依赖，缺了可以当场装 | 装包（会先问你） |
| `ship.py doctor` | 7 组体检 | **只读** |
| `ship.py init` | 生成 `ship.config.json` | 写一个配置文件 |
| `ship.py setup` | 证书链全套 | 会调 API 建资源、可能消耗证书名额 |
| `ship.py secrets` | 8 个 secrets 写进 GitHub | 会写 GitHub |
| `ship.py release` | build 号 +1 → 推送 → 构建 → 挂测试组 | **消耗一个 build 号，不可撤销** |
| `ship.py status` | 分发状态速查 | 只读 |
| `testflight.py attach` | 单独挂 build / 加测试员 / 补文案 | 会写 ASC |
| `make-icon.py` | 生成图标 + asset catalog | 写文件 |
| `selftest.py` | 78 条离线断言 | 只读 |

`setup` 的常用参数：

```bash
--bundle-id com.example.app      # 不给就从 project.yml 推断
--reuse-cert ../old/cert         # 复用已有证书（强烈建议）
--cert-dir tools/cert            # 材料放哪
--force                          # 强制重新生成
--check                          # 只验 API Key 有效，不建任何东西
```

Windows 上可以直接双击 `tools\ship.cmd`（等价于 `ship.py`）。

---

# 文档

| 文档 | 内容 |
|---|---|
| [`docs/why.md`](docs/why.md) | 和 fastlane 等方案的区别、实测数据、验证到什么程度、目录结构 |
| [`docs/pitfalls.md`](docs/pitfalls.md) | **18 条实测踩坑**，含原始报错文本、误判过程、根因 |
| [`docs/architecture.md`](docs/architecture.md) | 分层职责、数据流、一次 `release` 的完整时序 |
| [`docs/asc-api.md`](docs/asc-api.md) | App Store Connect API 速查（端点、字段、错误码、locale 合法值） |

遇到没列出的报错，先去 `pitfalls.md` 的故障对照表按现象搜一下。

---

# License

Apache-2.0。见 [LICENSE](LICENSE)。为什么不用 GPL/AGPL：[`docs/why.md`](docs/why.md#为什么是-apache-20)。
