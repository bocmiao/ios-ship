# ios-ship

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![自测](https://github.com/bocmiao/ios-ship/actions/workflows/selftest.yml/badge.svg)](https://github.com/bocmiao/ios-ship/actions/workflows/selftest.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab.svg)](https://www.python.org/)

不用 Mac 完成 iOS 签名并上传 TestFlight 的脚本集合。

`ship.py` 处理证书、p12、Bundle ID、描述文件；编译由 GitHub Actions 的 `macos-latest` runner 执行；`testflight.py` 把 build 加入内部测试组。

本机依赖 Python 3.9+ 与 `cryptography`，不需要 Xcode、Keychain Access、Ruby。

## 准备工作

| 项目 | 要求 |
|---|---|
| Python | 3.9+，[python.org](https://python.org/downloads/) 下载，安装时勾选 Add to PATH。装完在终端跑 `python --version` 确认有输出 |
| git | 已装即可，`git --version` 能出版本 |
| GitHub CLI | [cli.github.com](https://cli.github.com/) 下载安装，然后 `gh auth login` 登录一次 |
| Apple 开发者账号 | 付费账号，[$99/年](https://developer.apple.com/programs/)。免费账号用不了 App Store Connect API |
| GitHub 账号 | 需要一个仓库放代码，下一步会建 |

`cryptography` 不用手动装，第 4 步的 `ship.py deps` 会检查并安装。

## 先确认你要发的是什么

这套工具发的是 iOS App，所以得有个 App。两种情况：

**已经有 Swift 代码**：把 `.swift` 文件和资源放进 `App/` 目录，`project.yml` 里的 `sources` 指向这个目录。

**还没有代码**：模板自带一个能编译的最小 SwiftUI 工程，`App.swift` 是入口，`ContentView.swift` 里有一个显示版本号的自检页。第 1 步跑完就能直接发，之后再改 `ContentView.swift` 写自己的界面。

界面用 SwiftUI 写。写不了的话，这套工具帮不上忙，它只管签名、构建、上传。

## 第一次发版

### 1. 建项目

```bash
git clone https://github.com/bocmiao/ios-ship

python ios-ship/tools/ship.py new PhotoHelper --bundle-id com.testuser.photohelper
```

第二条命令做三件事：把 `template/` 里的最小工程复制到 `PhotoHelper/ios/`，把 `tools/` 复制进去，把工程名和 Bundle ID 改到 3 个文件的 11 处。

`--bundle-id` 换成你自己的，格式是反域名，全球唯一。它会成为 App 的唯一标识，上架后不能改。`PhotoHelper` 换成你的工程名，只能用字母、数字、下划线。

产物：

```
PhotoHelper/
  ios/
    project.yml            工程描述，改版本号改这里
    App/                   源码，改 ContentView.swift
    tools/                 ship.py 等脚本
    fastlane/              只负责上传
    Gemfile                Ruby 3.4 缺的默认 gem
    .github/workflows/     两条流水线
    .gitignore             挡签名材料
```

### 2. 推到 GitHub

在 GitHub 网页建一个空仓库。**不要勾** Add a README、.gitignore、license，勾了会让本地首次 push 冲突。

然后：

```bash
cd PhotoHelper
git init
git add -A
git commit -m "init"
git branch -M main
git remote add origin https://github.com/你的GitHub用户名/PhotoHelper.git
git push -u origin main
```

把 `你的GitHub用户名` 换成实际用户名，仓库名要和网页上建的一致。

如果 push 报这句话：

```
refusing to allow an OAuth App to create or update workflow
`.github/workflows/ios.yml` without `workflow` scope
```

说明 gh 的登录凭证没有写 workflow 文件的权限。执行一次：

```bash
gh auth refresh -s workflow
```

浏览器里确认，然后重新 push。

分支必须是 `main`。`release` 里触发流水线用的是 `--ref main`，分支叫 master 会找不到。

### 3. 创建 API Key

地址是 `appstoreconnect.apple.com`，不是 `developer.apple.com`。

```
用户和访问 → 集成 → App Store Connect API → 团队密钥 → 生成 API Key
```

- 类型选团队密钥（Team Key）。个人密钥调不了证书接口，两种密钥名字相近，选错要到第 6 步才报错。
- Access 选 App Manager 或 Admin。
- `.p8` 只提供一次下载，页面关掉就再也拿不到。
- 记下页面上的 Issuer ID（一串带横杠的 UUID），第 6 步会用到。

账号第一次用 API 需要先点「请求访问权限」，等苹果审核通过。没通过之前所有 API 调用都会被拒绝。

下载的 `.p8` 放进 `PhotoHelper/ios/tools/cert/`，目录不存在就新建：

```
PhotoHelper/ios/tools/cert/AuthKey_XXXXXXXXXX.p8
```

文件名不要改。`cert/` 已经在 `.gitignore` 里，不会进 Git。

### 4. 生成配置

```bash
cd PhotoHelper/ios
python tools/ship.py deps
python tools/ship.py init
```

`deps` 检查依赖，缺 `cryptography` 时会问要不要装。`init` 从 `project.yml` 推断配置，写进 `ship.config.json`：

```
OK   已生成 ios/ship.config.json
     已从 project.yml 自动填好：scheme, xcodeDir, bundleId, appName
     其余字段可留空：teamId 从 secrets.txt 兜底，TestFlight 文案有通用默认值
```

`teamId` 留空时由 `secrets.txt` 的 `APPLE_TEAM_ID` 兜底；手填的话是开发者账号页面右上角的 10 位编号。

### 5. 运行体检

```bash
python tools/ship.py doctor
```

只读文件和查询接口，不改任何东西。七组检查从本地排到云端：

```
[1. 本地材料]
       证书材料查找位置：PhotoHelper\ios\tools\cert
  OK   API Key  AuthKey_XXXXXXXXXX.p8
  XX   证书链没生成（缺 distribution.key / distribution.cer / distribution.p12）
  !!   没有 cert/secrets.txt
  OK   project.yml 版本  1.0 (build 1)
  OK   App 图标
  OK   凭据没有进 Git

[2. 凭据与账号]
  OK   JWT 签名与鉴权
  XX   Bundle ID 未注册：com.testuser.photohelper

[3. App 记录]
       跳过：Bundle ID 还没注册，不会有 App 记录

[4. 证书]
  OK   Apple Distribution: You (TEAMID)   到期 2027-09-26

[5. 描述文件]
  !!   没找到匹配「PhotoHelper AppStore」的描述文件

[6. 构建与分发]
       跳过：这一组依赖 App 记录，上面那条建完再跑一次

[7. GitHub 仓库]
  OK   8 个 secrets 都在
```

符号含义：`OK` 通过，`!!` 提醒（能跑，但迟早出事），`XX` 阻塞（不修就跑不通）。有阻塞项时退出码是 1。

跑到这一步会看到 `证书链没生成` 和 `Bundle ID 未注册`，都是正常的，第 6 步会一起解决。

上面的输出对应「已经做完第 2、3 步」的状态。如果还没放 `.p8`，第 1 组会报 `找不到 AuthKey_*.p8`；如果还没 `git init`，会多一条 `这个目录不是 git 仓库`。这两条补上对应步骤就消失。

### 6. 生成证书链

```bash
python tools/ship.py setup
```

脚本依次做：

```
生成 EC 私钥 → CSR → 向 Apple 申请证书 → 合成 p12
→ 创建 Bundle ID → 创建描述文件 → 写出 secrets.txt
```

中间会问 Issuer ID 和 Key ID。Issuer ID 是第 3 步记下的那个，Key ID 可以从 `.p8` 文件名推出来（`AuthKey_B87349DSFW.p8` 里的 `B87349DSFW`）。

产物在 `ios/tools/cert/`：

```
distribution.key    私钥
distribution.cer    证书
distribution.p12    打包用的证书 + 私钥
secrets.txt         8 个值，第 8 步要用
```

这四样都不能丢、不能进 Git。换电脑时整个 `cert/` 拷过去，不要重新 setup。

一个团队同时最多持有 2 张 Distribution 证书，一张证书能签任意数量的 App。已经有证书的项目不要重复申请，用 `--reuse-cert` 指向旧项目的 cert 目录：

```bash
python tools/ship.py setup --reuse-cert ../old-project/ios/tools/cert
```

它比对私钥和证书的公钥，不匹配就停下，不会生成一个签不过的 p12。

### 7. 创建 App 记录

苹果没开放创建 App 记录的 API，这一步在网页做：

```
appstoreconnect.apple.com → 我的 App → 左上角 + → 新建 App
  平台：iOS
  名称：先填一个，之后能改
  主要语言：简体中文
  Bundle ID：选第 6 步创建的那个
  SKU：随便填个唯一值，例如 photohelper001，不公开
```

名称在 App Store 里是全球唯一的，重名会报 `The App Name you entered is already being used`，换一个。

每个 App 只建一次，之后所有版本都传到这条记录。浏览器后台的 Identifier 和这里是两个东西，上传认的是这里。

### 8. 写入 secrets

```bash
python tools/ship.py secrets
```

把 `secrets.txt` 里的 8 个值写进第 2 步那个仓库。8 个里有 3 个是几千字符的 base64，手动复制会漏字符，所以用命令。

写完后去仓库的 `Settings → Secrets and variables → Actions` 核对，应该有 8 条。

这一步要求当前目录在 git 仓库里、且配好了 remote。第 2 步做过就没问题。

### 9. 发版

```bash
python tools/ship.py release
```

顺序：build 号加一、提交 `project.yml` 并推送、触发云端构建、等构建结束、等 Apple 处理完、加入内部测试组。

提交范围只有 `project.yml`，工作区里其他改动不会被带进这次提交。

会问一次确认。上传不可撤销，用掉的 build 号拿不回来。构建本身约 4 分钟，之后还要等 Apple 处理 1 分钟左右。

### 10. 安装

App Store 里装 TestFlight，用开发者账号对应的 Apple ID 登录，App 会出现在列表里。

装上后打开，能看到一页自检信息：版本号、build 号、Bundle ID、设备型号、系统版本。看到这页说明整条链路通了。

上传成功不等同于能装，中间还差三步，见「TestFlight 里看不到 App」。

## 日常发版

```bash
python tools/ship.py release
```

build 号自动加一。

只想确认代码能编过、不消耗 build 号时，push 会触发 `validate.yml` 跑一次免签名编译。

`ios.yml` 只有 `workflow_dispatch` 触发，不挂在 push 上，所以改文档、改注释不会误触发上传。

## 接入第二个项目

复用已有证书，不要重新申请：

```bash
python ios-ship/tools/ship.py new NewApp --bundle-id com.testuser.newapp
cd NewApp
git init
git add -A
git commit -m "init"
git remote add origin https://github.com/你的GitHub用户名/NewApp.git
git push -u origin main

cd ios
cp -r ../../旧项目/ios/tools/cert tools/cert
python tools/ship.py init
python tools/ship.py setup --reuse-cert tools/cert
python tools/ship.py secrets
```

`--reuse-cert` 校验通过后只创建 Bundle ID 和描述文件，不碰证书。

## 已有 Xcode 工程怎么接

模板的工程由 `project.yml` 描述，CI 里跑 `xcodegen generate` 生成 `.xcodeproj`。手上已经有 `.xcodeproj` 的话，要么给它写一份 `project.yml`，要么把源码搬进模板的 `App/` 再改 `project.yml`。后者在工程结构简单时更快。

`.xcodeproj` 里能读出对应关系：

| Xcode 里的 | `project.yml` 里 |
|---|---|
| Target 名 | `targets:` 下面的键名 |
| Product Bundle Identifier | `PRODUCT_BUNDLE_IDENTIFIER` |
| Deployment Target | `options.deploymentTarget.iOS` |
| Info.plist 的各个键 | `targets.<名字>.info.properties` |
| Swift Package 依赖 | `packages:` 加 `dependencies: [{package: ...}]` |
| Signing & Capabilities | `entitlements:` 段与 `settings.base` |
| Scheme 名 | 顶层 `schemes:`，archive 步骤按这个名字找 |

4 个值决定工具能不能跑：

- `name:`：同时是工程名和默认 scheme 名。两者不同名时，`schemes:` 里显式声明。
- `sources[0].path`：工具靠它推断 `xcodeDir`，图标、Info.plist、asset catalog 都在这下面找。
- `PRODUCT_BUNDLE_IDENTIFIER`：和 App Store Connect 里的 Bundle ID 必须一致。
- `ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon`：少了它图标不会编进包，上传时才报 90713。

搬不过来的部分要自己处理：多 target（模板只配了一个）、自定义 build phase、CocoaPods（XcodeGen 支持，但要另写 `preBuildScripts` 跑 `pod install`）、Watch App 与各类扩展（每个各需一份描述文件）。

改完跑 `python tools/ship.py doctor`，它会指出 `xcodeDir` 有没有推断对、图标找没找到。推上去先让 `validate.yml` 跑一次免签名编译，编不过不会消耗 build 号。

## doctor 输出怎么看

七组检查按本地到云端排列。前面组有阻塞项时，后面组的结果不成立。

| 组 | 检查内容 | 处理 |
|---|---|---|
| 1. 本地材料 | .p8、.key、.cer、.p12、secrets.txt、版本号、图标、凭据是否被 git 跟踪 | 运行 setup；图标用 make-icon.py |
| 2. 凭据与账号 | 生成 JWT、调用 API | 核对 Key ID、Issuer ID，确认是团队密钥 |
| 3. App 记录 | App Store Connect 中是否有对应记录 | 手动创建，见步骤 7 |
| 4. 证书 | 有效期、剩余名额 | 名额满时用 --reuse-cert |
| 5. 描述文件 | 是否存在、是否匹配 bundleId、是否过期 | 重新运行 setup |
| 6. 构建与分发 | secrets 是否齐全、最近一次运行、是否有 VALID 的 build、是否在内部组 | 运行 secrets 或 release |
| 7. GitHub 仓库 | 远端地址、分支、gh 登录状态 | gh auth login |

## TestFlight 里看不到 App

```bash
python tools/ship.py status
```

从上传成功到设备能装，中间有三个条件，`status` 会指出卡在哪个：

1. `processingState` 变成 `VALID`。刚上传的几分钟内是 `PROCESSING`。
2. build 已加入内部测试组。处理完成还不够。
3. 组内有测试员，且手机上登录的 Apple ID 就是其中一个。

三条都满足仍看不到时，确认手机上登录的 Apple ID 在组里，然后杀掉 TestFlight 重新打开。

## 凭据可能泄露时

`doctor` 会用 `git ls-files` 检查被跟踪的文件：

```bash
python tools/ship.py doctor
```

看到 `XX 凭据文件已经被 git 跟踪`，按提示执行：

```bash
git rm --cached ios/tools/cert/secrets.txt ios/tools/cert/distribution.p12
git commit -m "移除误提交的凭据"
```

这只是让 git 不再跟踪，历史里还在。已经推到公开仓库的，按 [docs/pitfalls.md](docs/pitfalls.md) 第 17 条评估要不要换 p12 密码。

彻底清除的话 force push 不够。GitHub 会保留被覆盖的 commit，知道 hash 就能读。可行做法是把旧仓库改名并转私有，再用原名新建仓库推干净历史。

## 常见问题

**必须有一台 Mac 吗**

不需要。只有编译需要 macOS，跑在 GitHub 的 `macos-latest` runner 上。证书、签名、上传都在本机完成。

**能上架 App Store 吗**

可以。导出的 IPA 就是用于 App Store 的那份（`method` 为 `app-store-connect`），上传后在 App Store Connect 提交审核。文案、截图、隐私清单要自己准备。

**支持 Flutter、React Native、Unity 吗**

只要项目最终产出 `.xcodeproj` 就支持，但需要自己写 `project.yml`。Flutter、React Native、Capacitor 的 `ios/` 目录是标准 Xcode 工程，按[已有 Xcode 工程怎么接](#已有-xcode-工程怎么接)那节对照着写。

**App Clip、Watch App、扩展能用吗**

模板是单 target。多 target 要自己改 `project.yml` 和 `ios.yml` 里的 archive 步骤，每个 target 各配一份描述文件。

**会改动 App 代码吗**

不会。只改签名材料、`project.yml` 里的版本号、`ship.config.json`。

**免费开发者账号能用吗**

不能。App Store Connect API 属于付费开发者计划。

**团队账号怎么配**

需要 Account Holder 创建团队密钥，并点一次「请求访问权限」。

**只装 Python 够吗**

够。不需要 Ruby，`fastlane` 只在云端 runner 上跑。本机运行时依赖 `cryptography`；`make-icon.py` 额外需要 Pillow，可选；`init`、`secrets` 只用标准库。

**一张证书能签几个 App**

不限。Distribution 证书是团队级的，团队同时最多持 2 张。过期证书仍占名额，要手动吊销才腾位置。

**证书或描述文件过期了**

构建会失败，或装上的包打不开。重新跑 `setup`。证书本身没过期时只补一份新描述文件，不消耗名额。

**换电脑了**

把 `ios/tools/cert/` 整个目录拷过去，里面有私钥、证书、p12 和 `secrets.txt`。不要重新 setup，那会多占一个名额。

**多人协作怎么做**

只保留一份 `cert/` 目录，由一人持有并通过加密压缩包分发。多人各自跑 `setup` 会各占一个名额。

**密码会经过第三方服务器吗**

不会。没有中间服务器，API 请求由本机直连 `api.appstoreconnect.apple.com`，凭据是 `.p8` 本地签发的 JWT。

**为什么不让 CI 创建证书**

私钥会生成在一台几分钟后就销毁的机器上，Apple 那边留下一张用不了的证书，还占掉 2 个名额之一。私钥在本机生成一次，CI 只负责导入。

**CI 里私钥怎么使用**

从 secret 解码到 `$RUNNER_TEMP`，导入临时 keychain，任务结束后连同 keychain 一起删除。

## 构建失败了怎么看

```bash
python tools/ship.py logs
```

找最近一次失败的 `ios.yml`，只取失败的那个步骤，剥掉时间戳和颜色码，命中已知报错就标出来附上修法，只显示命中位置前后几行。

`gh run view <id> --log-failed` 的原始输出有一百多行，每行都带 job、步骤名、ISO 时间戳前缀，真正的报错夹在 fastlane 的 summary 表前面，直接翻很容易看漏。

指定某一次用 `--run <id>`，想看完整日志加 `--all`，只想按行数截取用 `--tail N --no-hints`。

## 报错对照

`refusing to allow an OAuth App to create or update workflow`

gh 的登录凭证缺 `workflow` 权限，推不了 `.github/workflows/`。跑 `gh auth refresh -s workflow` 授权后重推。

`MAC verification failed (wrong password?)`

密码通常是对的。原因是 p12 加密算法不兼容：macOS 的 Security.framework 只接受 3DES 加密加 SHA1 MAC，`cryptography` 默认输出 AES-256 加 SHA256。框架在 MAC 校验失败时统一提示 `wrong password?`。

本工具用的是 `-certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1`。

`Couldn't find app 'com.xxx' on the account`

App 记录没建。见步骤 7。

上传返回 409

两种相反含义。版本号不一致是真失败；也可能是上传成功了：Apple 收到二进制后有几十秒不在 build 列表里，重试时撞上「build 已存在」，把成功报成失败。先查状态：

```bash
python tools/ship.py status
```

`The bundle version must be higher than '3'`

build 号没加，或已经被 push 触发的上传用掉了。`release` 会自动加，手动发版就改 `project.yml` 里的 `CURRENT_PROJECT_VERSION`。

`90713 Missing CFBundleIconName` / `90022 Missing 120x120`

缺图标，而且在上传阶段才报，编译和签名都是通过的。要同时满足两个条件：有 `Assets.xcassets/AppIcon.appiconset/`（1024×1024，无 alpha 通道），并且 `project.yml` 里有 `ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon`。缺第二个条件时 asset catalog 不会编进包里。

`The App Name you entered is already being used`

App 名称在 App Store 全局唯一，换一个。

`cannot load such file -- mutex_m`

runner 是 Ruby 3.4，`mutex_m`、`base64`、`bigdecimal` 已经不在默认 gem 里。模板的 `Gemfile` 已声明。

`The 'locale' value is invalid`

写 `zh-Hans`，不是 `zh`，也不是 `zh-CN`。

`bash: command not found`（Windows）

Git for Windows 只把 `Git\cmd` 加进 PATH，没有 bash。用 `tools\ship.cmd`，双击也行。

控制台中文乱码

cmd.exe 按 OEM 代码页解码 UTF-8 输出。`ship.cmd` 里执行过 `chcp 65001`；直接用 `python` 跑的话，先手动执行一次 `chcp 65001`。

`python` 打开了微软商店

那是 `WindowsApps` 下的 0 字节占位符，不是真的解释器。装一次 python.org 的版本，或者用 `ship.cmd`，它会实际执行 `python -c` 来判断。

## 命令

| 命令 | 作用 | 副作用 |
|---|---|---|
| `ship.py new <目录>` | 从模板建一个新项目并改名 | 写一个新目录 |
| `ship.py deps` | 检查依赖，缺了可以当场装 | 安装 Python 包，会先问 |
| `ship.py init` | 生成 `ship.config.json` | 写一个配置文件 |
| `ship.py rename` | 改已有项目的工程名与 Bundle ID | 改 `project.yml` 与两个 workflow |
| `ship.py doctor` | 七组体检 | 只读 |
| `ship.py setup` | 生成整套证书链 | 调用 API 创建资源，可能占证书名额 |
| `ship.py secrets` | 把 8 个 secrets 写入 GitHub | 改仓库设置 |
| `ship.py release` | build 号加一、推送、构建、挂测试组 | 占一个 build 号，不可撤销 |
| `ship.py logs` | 把失败流水线的日志压到能读的长度 | 只读 |
| `ship.py status` | 查看分发状态 | 只读 |
| `testflight.py attach` | 单独挂 build、加测试员、补 TestFlight 文案 | 写 App Store Connect |
| `make-icon.py` | 生成图标与 asset catalog | 写文件 |
| `selftest.py` | 126 条离线断言 | 只读 |

`setup` 的常用参数：

```bash
--bundle-id com.example.app      # 不填则从 project.yml 推断
--reuse-cert ../old/cert         # 复用已有证书
--cert-dir tools/cert            # 指定材料目录
--force                          # 强制重新生成
--check                          # 只验证 API Key 是否有效，不创建资源
```

## 其他文档

| 文档 | 内容 |
|---|---|
| [docs/why.md](docs/why.md) | 与 fastlane 等方案的对比、实测数据、验证范围 |
| [docs/pitfalls.md](docs/pitfalls.md) | 18 条实际遇到的问题，含原始报错 |
| [docs/architecture.md](docs/architecture.md) | 脚本职责、数据流、一次 release 的时序 |
| [docs/asc-api.md](docs/asc-api.md) | App Store Connect API 速查 |

## License

Apache-2.0，见 [LICENSE](LICENSE)。
