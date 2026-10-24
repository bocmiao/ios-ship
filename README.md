# ios-ship

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![自测](https://github.com/bocmiao/ios-ship/actions/workflows/selftest.yml/badge.svg)](https://github.com/bocmiao/ios-ship/actions/workflows/selftest.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab.svg)](https://www.python.org/)

在 Windows 上把 iOS App 发到 TestFlight 用的工具链。

证书申请、p12 合成、Bundle ID、描述文件、GitHub Actions 构建、TestFlight 分发，都由脚本完成。整个过程不需要 Mac，也不需要 Xcode 或 Keychain Access。编译那一步跑在 GitHub 的 macOS runner 上。

设计取舍、和其他方案的区别、实测数据在 [docs/why.md](docs/why.md)。

## 准备工作

| 项目 | 说明 |
|---|---|
| 系统 | Windows、macOS、Linux 均可，本文命令以 Windows 为例 |
| Python | 3.9 或更高版本，[python.org](https://python.org/downloads/) 下载，安装时勾选 Add to PATH |
| Python 包 | `cryptography`，用于生成 CSR 和签 API 的 JWT，`ship.py deps` 会装 |
| GitHub CLI | `gh auth login` 登录一次 |
| Apple 开发者账号 | 需要付费账号（$99/年），免费账号无法使用 App Store Connect API |
| GitHub 仓库 | public 仓库的 macOS runner 免费，private 仓库每月 200 分钟 |

## 第一次发版

### 1. 创建 API Key

打开 `appstoreconnect.apple.com`，不是 `developer.apple.com`。

```
用户和访问 → 集成 → App Store Connect API → 团队密钥 → 生成 API 密钥
```

创建时注意三点：

- 选团队密钥。个人密钥调不了证书接口，这两种密钥名字很像，选错了要到申请证书那一步才会报错。
- Access 选 App Manager，Admin 也可以。
- 建完马上下载 `.p8`。页面关掉之后就再也下载不了了。

同时记下页面上的 Issuer ID，下一步会用到。

如果这个账号是第一次使用 API，需要先点一次「请求访问权限」，等苹果审核通过。没通过之前 API 调用都会被拒绝。

### 2. 复制到项目里

```bash
git clone https://github.com/bocmiao/ios-ship

cp -r ios-ship/tools       <你的iOS项目>/ios/tools
cp -r ios-ship/template/.  <你的iOS项目>/ios/
```

第二条命令结尾的 `.` 不能省。写成 `template/*` 会漏掉 `.github` 和 `.gitignore` 两个点开头的路径。

然后改 `project.yml`，前三行注释里标了三处要换：工程名、`bundleIdPrefix`、target 名。

```yaml
name: MyApp
options:
  bundleIdPrefix: com.example
targets:
  MyApp:
```

再改两个 workflow 顶部的 `env`，四个值要和上面一致：

```yaml
env:
  PROJECT: MyApp              # project.yml 里的 name
  SCHEME: MyApp               # project.yml 里 schemes 下的 key
  APP_NAME: MyApp             # target 名，决定 ipa 文件名
  BUNDLE_ID: com.example.myapp
```

这四项经常同名，但不是必须同名。分开写是为了四者对不上时容易看出来。

### 3. 装依赖并生成配置

```bash
cd <你的iOS项目>/ios

python tools/ship.py deps
python tools/ship.py init
```

`deps` 检查依赖，缺 `cryptography` 时会提示并询问是否安装。`init` 读取 `project.yml`，把能推断的值填进 `ship.config.json`：

```
OK   已生成 ios/ship.config.json
     已从 project.yml 自动填好：scheme, xcodeDir, bundleId, appName
     还空着的要手填：teamId、repo、feedbackEmail
```

打开 `ship.config.json` 补上 `teamId`（开发者账号页面右上角的 10 位编号）和 `repo`。

### 4. 运行体检

```bash
python tools/ship.py doctor
```

这个命令只读文件、只发查询请求，不会改动任何东西。它把整条链路检查一遍，把后面才会暴露的问题提前报出来：

```
[1. 本地材料]
  XX   找不到 AuthKey_*.p8
  XX   缺 distribution.key
  OK   project.yml 版本  1.0 (build 1)
  OK   App 图标
  OK   凭据没有进 Git
```

这时报错是正常的，证书还没开始做。按每条后面的箭头提示补。

### 5. 生成证书链

把 `.p8` 放到 `ios/tools/cert/`，目录不存在就新建。然后：

```bash
python tools/ship.py setup
```

脚本会询问 Issuer ID 和 Key ID，然后依次完成：

```
生成 EC 私钥 → CSR → 向 Apple 申请证书 → 合成 p12
→ 创建 Bundle ID → 创建描述文件 → 写出 secrets.txt
```

结束后 `ios/tools/cert/` 目录里会有 `distribution.key`、`distribution.cer`、`distribution.p12` 和 `secrets.txt`。

如果之前已经给别的项目做过证书，不要重复申请。一个团队最多同时持有 2 张 Distribution 证书，名额用完后只能吊销旧证书，而一张证书本身可以签无数个 App。用 `--reuse-cert` 指向旧项目的 cert 目录：

```bash
python tools/ship.py setup --reuse-cert ../old-project/ios/tools/cert
```

脚本会比对新私钥和旧证书的公钥，不匹配就终止，不会生成一个签不过的 p12。

### 6. 创建 App 记录

苹果没有开放创建 App 记录的 API，这一步只能在网页上做：

```
appstoreconnect.apple.com → 我的 App → 左上角 + → 新建 App
  平台：iOS
  名称：先随便填，后面可以改
  主要语言：简体中文
  Bundle ID：选刚创建的那个
  SKU：自定义，例如 myapp001，不公开
```

只需要建一次，之后的版本都往这条记录上传。

developer 后台里的 Identifier 和这里的 App 记录是两个东西。前者只是一个 bundle id 字符串，后者才是商店条目，上传认的是后者。

### 7. 写入 secrets

```bash
python tools/ship.py secrets
```

把 `secrets.txt` 里的 8 个值逐条写进 GitHub 仓库。写完可以在 `仓库 → Settings → Secrets and variables → Actions` 核对，应该有 8 条。

### 8. 发版

```bash
python tools/ship.py release
```

依次执行：build 号加一、提交推送、触发云端构建、等待结果、把 build 加入内部测试组。

这一步会占用一个 build 号，而上传不可撤销，用掉的号拿不回来，所以脚本会先确认一次。整个过程大约 4 分钟。

### 9. 安装

在 App Store 里装 TestFlight，用开发者账号对应的 Apple ID 登录，App 会出现在列表里。

装上之后打开，应该看到一页自检信息：版本号、build 号、Bundle ID、设备型号、系统版本。能看到这页说明整条链路是通的。

注意：上传成功不等于能装上。中间还差 build 处理完成、加入测试组、测试员在组里三步，详见下面「TestFlight 里看不到 App」。

## 日常发版

```bash
python tools/ship.py release
```

build 号会自动加一，不需要手动改。

如果只想确认代码能编过，不想占用 build 号，提交代码后 `validate.yml` 会自动跑一次免签名编译检查。

`ios.yml` 只保留了 `workflow_dispatch` 触发方式，没有挂在 push 上。改文档、改注释这类提交不会触发上传。

## 接入第二个项目

复用已有的证书，不要再申请：

```bash
cp -r <旧项目>/ios/tools/cert  <新项目>/ios/tools/cert
cd <新项目>/ios
python tools/ship.py init
python tools/ship.py setup --reuse-cert tools/cert --bundle-id com.example.newapp
```

`--reuse-cert` 校验私钥与证书是否配对，通过后只创建 Bundle ID 和描述文件。

## doctor 输出怎么看

七组检查，从本地往云端排列。前面的组有问题时，后面的组不用看。

| 组 | 检查内容 | 处理方式 |
|---|---|---|
| 1. 本地材料 | .p8、.key、.cer、.p12、secrets.txt、版本号、图标、凭据是否被 git 跟踪 | 跑 setup；图标跑 make-icon.py |
| 2. 凭据与账号 | 能否生成 JWT、能否调通 API | 检查 Key ID、Issuer ID，确认用的是团队密钥 |
| 3. App 记录 | App Store Connect 里是否有对应记录 | 手动创建，见步骤 6 |
| 4. 证书 | 有效期、剩余名额 | 名额满了改用 --reuse-cert |
| 5. 描述文件 | 是否存在、是否匹配 bundleId、是否过期 | 重新跑 setup |
| 6. 构建与分发 | secrets 是否齐全、最近一次运行、是否有 VALID 的 build、是否在内部组 | 跑 secrets 或 release |
| 7. GitHub 仓库 | 远端地址、分支、gh 登录状态 | gh auth login |

符号含义：`OK` 通过，`!!` 提醒，`XX` 阻塞。有阻塞项时退出码为 1，可以接进 CI 当门禁。

## TestFlight 里看不到 App

先跑：

```bash
python tools/ship.py status
```

它会指出当前卡在哪一步。从上传成功到手机上能装，中间有三个条件：

1. build 处理完成，`processingState` 变为 `VALID`。刚上传的几分钟内是 `PROCESSING`。
2. build 已被加入内部测试组。仅仅处理完成还不够。
3. 组内有测试员，且手机上登录的 Apple ID 就是其中一个。

三步都满足仍看不到时，确认手机上的 Apple ID 与加入组的是同一个，然后杀掉 TestFlight 重新打开。

## 凭据可能泄露时

先跑体检，它会检查 `git ls-files`：

```bash
python tools/ship.py doctor
```

看到 `XX 凭据文件已经被 git 跟踪` 时，按提示执行：

```bash
git rm --cached ios/tools/cert/secrets.txt ios/tools/cert/distribution.p12
git commit -m "移除误提交的凭据"
```

这只是让 git 不再跟踪这些文件，历史记录里仍然存在。如果已经推送到公开仓库，需要评估是否更换 p12 密码，判断依据见 [docs/pitfalls.md](docs/pitfalls.md) 第 17 条。

需要彻底清除的话，force push 是不够的。GitHub 会保留被推掉的 commit，只要知道 hash 仍然可以读取。可行的做法是把旧仓库改名并转为私有，再用原名新建仓库推送干净历史。

## 常见问题

**必须有一台 Mac 吗**

不需要。整条链路上只有编译需要 macOS，而编译跑在 GitHub 的 `macos-latest` runner 上。证书、签名、上传都在本机完成。

**能上架 App Store 吗**

可以。导出的 IPA 就是用于 App Store 的那份（`method` 为 `app-store-connect`），上传后在 App Store Connect 里提交审核即可。上架文案、截图、隐私清单需要自己准备，工具不处理这部分。

**支持 Flutter、React Native、Unity 吗**

只要项目最终产出 `.xcodeproj` 就支持。前提是工程能用 XcodeGen 描述，或者自己写 `project.yml` 指向对应的 target。

**App Clip、Watch App、扩展能用吗**

模板是单 target 的。多 target 需要自己改 `project.yml`，`ios.yml` 里的 archive 步骤也要改，每个 target 各配一份描述文件。工具不限制，但默认配置不覆盖这些情况。

**会改动 App 代码吗**

不会。只改签名材料、`project.yml` 里的版本号、以及 `ship.config.json`。

**免费开发者账号能用吗**

不能。App Store Connect API 属于付费开发者计划。

**团队账号怎么配**

可以用。需要 Account Holder 去创建团队密钥，并点一次「请求访问权限」。

**只装 Python 够吗**

够。不需要 Ruby，`fastlane` 只在云端 runner 上运行。本机需要的 Python 包只有 `cryptography`，`ship.py deps` 会装。`make-icon.py` 额外需要 Pillow，可选，自己有 1024×1024 的 PNG 就不用装。`init` 和 `secrets` 只用标准库。

**一张证书能签几个 App**

不限数量。Distribution 证书是团队级的，所有项目共用一张即可。团队上限是同时持有 2 张，这个名额不随过期恢复，过期后仍然占用，需要手动吊销才腾出位置。

**证书或描述文件过期了**

构建会失败，或者装上的包打不开。重新跑 `setup`。如果证书本身还没过期，只会补一份新的描述文件，不消耗名额。

**换电脑了**

把 `ios/tools/cert/` 整个目录拷过去，里面有私钥、证书、p12 和 secrets.txt。不要重新 setup，那会多占一个名额。

**多人协作怎么做**

不要每个人都跑一次 setup，那样会各占一个名额。由一个人持有 `cert/` 目录，通过 U 盘或加密压缩包传给其他人，或者干脆只让 CI 构建，其他人只改代码。

**密码会经过第三方服务器吗**

不会。没有中间服务器，API 请求由本机直连 `api.appstoreconnect.apple.com`，凭据是下载下来的 `.p8` 签的 JWT。

**为什么不让 CI 创建证书**

私钥会生成在一台几分钟后就被销毁的机器上，Apple 那边留下一张永远用不了的证书。团队名额只有 2 个，废掉几张之后只能吊销。正确做法是私钥在本机生成一次，CI 只负责导入。

**CI 里私钥怎么使用**

从 secret 解码到 `$RUNNER_TEMP`，导入一个临时 keychain，任务结束后连同 keychain 一起删除。

## 报错对照

`MAC verification failed (wrong password?)`

密码通常是对的。这个报错来自 p12 的加密算法不兼容：macOS 的 Security.framework 只接受 3DES 加密加 SHA1 MAC，而 Python `cryptography` 默认输出 AES-256 加 SHA256。报错里的 `wrong password?` 是框架自己加的猜测。

工具已经处理了这一点（`-certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1`），自己写脚本时记得带上这三个参数。

`Couldn't find app 'com.xxx' on the account`

App 记录没建。见步骤 6。

上传返回 409

有两种相反的含义。版本号不一致是真失败；也可能是上传本身成功了：Apple 收到二进制后有几十秒不在 build 列表里，altool 重试时撞上「build 已存在」，于是把成功报成失败。先查状态再决定是否重传：

```bash
python tools/ship.py status
```

`The bundle version must be higher than '3'`

build 号没加，或者被 push 触发的自动上传用掉了。`release` 会自动加，手动发版就改 `project.yml` 里的 `CURRENT_PROJECT_VERSION`。

`90713 Missing CFBundleIconName` / `90022 Missing 120x120`

缺图标，而且是在上传阶段才报，编译和签名都是通过的。需要同时满足两个条件：有 `Assets.xcassets/AppIcon.appiconset/`（1024×1024，无 alpha 通道），并且 `project.yml` 里有 `ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon`。第二个条件容易漏，没有它 asset catalog 不会编进包里。

`cannot load such file -- mutex_m`

runner 是 Ruby 3.4，fastlane 依赖的 `mutex_m`、`base64`、`bigdecimal` 已经不在默认 gem 里。模板的 `Gemfile` 里已经声明。

`The 'locale' value is invalid`

写 `zh-Hans`，不是 `zh`，也不是 `zh-CN`。

Windows 上 `bash: command not found`

Git for Windows 只把 `Git\cmd` 加进 PATH，没有 bash。用 `tools\ship.cmd`，双击也可以。

控制台中文乱码

cmd.exe 按 GBK 解码 UTF-8 输出。`ship.cmd` 里执行过 `chcp 65001`；直接用 `python` 跑的话，先手动执行一次 `chcp 65001`。

`python` 命令打开了微软商店

那是 `WindowsApps` 下的 0 字节占位符，不是真的解释器。装一次 python.org 的版本，或者用 `ship.cmd`，它会实际执行 `python -c` 来判断，不看 `where` 的结果。

## 命令

| 命令 | 作用 | 副作用 |
|---|---|---|
| `ship.py deps` | 检查依赖，缺了可以当场安装 | 安装 Python 包，会先询问 |
| `ship.py doctor` | 七组体检 | 只读 |
| `ship.py init` | 生成 `ship.config.json` | 写一个配置文件 |
| `ship.py setup` | 生成整套证书链 | 调用 API 创建资源，可能占用证书名额 |
| `ship.py secrets` | 把 8 个 secrets 写入 GitHub | 写 GitHub 仓库设置 |
| `ship.py release` | build 号加一、推送、构建、加入测试组 | 占用一个 build 号，不可撤销 |
| `ship.py status` | 查看分发状态 | 只读 |
| `testflight.py attach` | 单独加入 build、添加测试员、补 TestFlight 文案 | 写 App Store Connect |
| `make-icon.py` | 生成图标和 asset catalog | 写文件 |
| `selftest.py` | 78 条离线断言 | 只读 |

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
| [docs/why.md](docs/why.md) | 与 fastlane 等方案的区别、实测数据、验证范围 |
| [docs/pitfalls.md](docs/pitfalls.md) | 18 条实际踩过的坑，含原始报错和排查过程 |
| [docs/architecture.md](docs/architecture.md) | 各脚本的职责、数据流、一次 release 的完整时序 |
| [docs/asc-api.md](docs/asc-api.md) | App Store Connect API 速查 |

## License

Apache-2.0，见 [LICENSE](LICENSE)。
