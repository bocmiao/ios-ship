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
| Python | 3.9+，[python.org](https://python.org/downloads/) 安装时勾选 Add to PATH |
| Python 包 | `cryptography`，`ship.py deps` 安装 |
| GitHub CLI | 执行一次 `gh auth login` |
| Apple 开发者账号 | 付费账号，$99/年。免费账号无法使用 App Store Connect API |
| GitHub 仓库 | public 仓库的 macOS runner 免费，private 仓库每月 200 分钟 |

## 第一次发版

### 1. 创建 API Key

地址是 `appstoreconnect.apple.com`，不是 `developer.apple.com`。

```
用户和访问 → 集成 → App Store Connect API → 团队密钥 → 生成 API 密钥
```

- 类型选团队密钥。个人密钥无法调用证书接口，两种密钥名称相近，选错要到申请证书时才报错。
- Access 选 App Manager 或 Admin。
- `.p8` 只提供一次下载，页面关闭后无法再次获取。
- 记下页面上的 Issuer ID。

账号首次使用 API 需先点「请求访问权限」并通过审核，否则 API 调用一律拒绝。

### 2. 复制到项目里

```bash
git clone https://github.com/bocmiao/ios-ship

cp -r ios-ship/tools       <你的iOS项目>/ios/tools
cp -r ios-ship/template/.  <你的iOS项目>/ios/
```

`template/.` 末尾的点用于一并复制 `.github` 与 `.gitignore`。写成 `template/*` 会漏掉这两项。

修改 `project.yml` 中的工程名、`bundleIdPrefix` 和 target 名：

```yaml
name: MyApp
options:
  bundleIdPrefix: com.example
targets:
  MyApp:
```

修改 `ios.yml` 与 `validate.yml` 顶部的 `env`，四个值与上面保持一致：

```yaml
env:
  PROJECT: MyApp              # project.yml 里的 name
  SCHEME: MyApp               # project.yml 里 schemes 下的 key
  APP_NAME: MyApp             # target 名，决定 ipa 文件名
  BUNDLE_ID: com.example.myapp
```

### 3. 生成配置

```bash
cd <你的iOS项目>/ios

python tools/ship.py deps
python tools/ship.py init
```

`deps` 检查依赖，缺失时提示安装。`init` 从 `project.yml` 推断配置写入 `ship.config.json`：

```
OK   已生成 ios/ship.config.json
     已从 project.yml 自动填好：scheme, xcodeDir, bundleId, appName
     其余字段可留空：teamId 从 secrets.txt 兜底，TestFlight 文案有通用默认值
```

`teamId` 留空时由 `secrets.txt` 的 `APPLE_TEAM_ID` 兜底；手填的话是开发者账号页面右上角的 10 位编号。

TestFlight 里显示的两段文字可以用 `testflightDescription`（App 简介）和 `testflightWhatsNew`（本次测什么）覆盖，不填走通用文案。

### 4. 运行体检

```bash
python tools/ship.py doctor
```

`doctor` 只读文件、只调用查询类接口，共七组检查。首次运行时证书尚未生成，输出如下：

```
[1. 本地材料]
  XX   找不到 AuthKey_*.p8
  XX   缺 distribution.key
  OK   project.yml 版本  1.0 (build 1)
  OK   App 图标
  OK   凭据没有进 Git
```

### 5. 生成证书链

把 `.p8` 放进 `ios/tools/cert/`，目录不存在则新建。然后：

```bash
python tools/ship.py setup
```

脚本依次创建：

```
生成 EC 私钥 → CSR → 向 Apple 申请证书 → 合成 p12
→ 创建 Bundle ID → 创建描述文件 → 写出 secrets.txt
```

产物在 `ios/tools/cert/`：`distribution.key`、`distribution.cer`、`distribution.p12`、`secrets.txt`。

一个团队同时最多持有 2 张 Distribution 证书，一张证书可签任意数量的 App。已有可用证书时复用，不要重复申请：

```bash
python tools/ship.py setup --reuse-cert ../old-project/ios/tools/cert
```

`--reuse-cert` 比对私钥与证书的公钥，不匹配则退出。

### 6. 创建 App 记录

苹果未开放创建 App 记录的 API，这一步在网页完成：

```
appstoreconnect.apple.com → 我的 App → 左上角 + → 新建 App
  平台：iOS
  名称：后续可改
  主要语言：简体中文
  Bundle ID：选刚创建的那个
  SKU：自定义，不公开，例如 myapp001
```

每个 App 建一次，后续版本上传到同一条记录。App Store Connect 的 App 记录与 developer 后台的 Identifier 是两个不同对象，上传认前者。

### 7. 写入 secrets

```bash
python tools/ship.py secrets
```

把 `secrets.txt` 中的 8 个值写入仓库。完成后在 `Settings → Secrets and variables → Actions` 核对，应为 8 条。

### 8. 发版

```bash
python tools/ship.py release
```

顺序为 build 号加一、提交 `project.yml` 并推送、触发云端构建、等待构建结束、等 Apple 处理完 build、加入内部测试组。

提交范围只有 `project.yml`。工作区里其他改动不会被带进这次提交，会提示留在原处。

该命令消耗一个 build 号，上传不可撤销，执行前要求确认一次。构建本身实测 3 分 45 秒，之后还要等 Apple 把 build 处理到 `VALID` 才能挂进组。

### 9. 安装

App Store 中安装 TestFlight，用开发者账号对应的 Apple ID 登录。

首次打开显示自检页：版本号、build 号、Bundle ID、设备型号、系统版本。

上传成功不等同于可安装。

## 日常发版

```bash
python tools/ship.py release
```

build 号自动加一，不需要手动修改。

只验证编译是否通过、不消耗 build 号时，push 会触发 `validate.yml` 执行免签名编译。

`ios.yml` 仅保留 `workflow_dispatch` 触发，不挂在 push 上。

## 接入第二个项目

```bash
cp -r <旧项目>/ios/tools/cert  <新项目>/ios/tools/cert
cd <新项目>/ios
python tools/ship.py init
python tools/ship.py setup --reuse-cert tools/cert --bundle-id com.example.newapp
```

`--reuse-cert` 校验通过后仅创建 Bundle ID 与描述文件。

## doctor 输出怎么看

七组检查按本地到云端排列。前面组存在阻塞项时，后面组的结果不成立。

| 组 | 检查内容 | 处理 |
|---|---|---|
| 1. 本地材料 | .p8、.key、.cer、.p12、secrets.txt、版本号、图标、凭据是否被 git 跟踪 | 运行 setup；图标用 make-icon.py |
| 2. 凭据与账号 | 生成 JWT、调用 API | 核对 Key ID、Issuer ID，确认是团队密钥 |
| 3. App 记录 | App Store Connect 中是否存在对应记录 | 手动创建，见步骤 6 |
| 4. 证书 | 有效期、剩余名额 | 名额满时用 --reuse-cert |
| 5. 描述文件 | 是否存在、是否匹配 bundleId、是否过期 | 重新运行 setup |
| 6. 构建与分发 | secrets 是否齐全、最近一次运行、是否存在 VALID 的 build、是否在内部组 | 运行 secrets 或 release |
| 7. GitHub 仓库 | 远端地址、分支、gh 登录状态 | gh auth login |

`OK` 通过，`!!` 提醒，`XX` 阻塞。存在阻塞项时退出码为 1。

## TestFlight 里看不到 App

```bash
python tools/ship.py status
```

从上传成功到设备可安装需要三个条件，`status` 指出当前不满足的一项：

1. `processingState` 为 `VALID`。刚上传的几分钟内是 `PROCESSING`。
2. build 已加入内部测试组。
3. 组内有测试员，且设备上登录的 Apple ID 是其中之一。

三项均满足仍不可见时，确认设备登录的 Apple ID 在组内，然后重启 TestFlight。

## 凭据可能泄露时

`doctor` 用 `git ls-files` 检查被跟踪的文件：

```bash
python tools/ship.py doctor
```

输出 `XX 凭据文件已经被 git 跟踪` 时：

```bash
git rm --cached ios/tools/cert/secrets.txt ios/tools/cert/distribution.p12
git commit -m "移除误提交的凭据"
```

`git rm --cached` 不改变历史。已推送到公开仓库的凭据需评估是否轮换 p12 密码，见 [docs/pitfalls.md](docs/pitfalls.md) 第 17 条。

彻底清除需重写历史并重建仓库：force push 后 GitHub 保留被覆盖的 commit，凭 hash 仍可读取。将旧仓库改名并转为私有，再用原名新建仓库推送干净历史。

## 常见问题

**必须有一台 Mac 吗**

不需要。只有编译需要 macOS，由 `macos-latest` runner 执行。证书、签名、上传在本机完成。

**能上架 App Store 吗**

可以。导出的 IPA `method` 为 `app-store-connect`，上传后在 App Store Connect 提交审核。文案、截图、隐私清单需自行准备。

**支持 Flutter、React Native、Unity 吗**

项目最终能产出 `.xcodeproj` 即可。工程需能用 XcodeGen 描述，或自行编写 `project.yml` 指向对应 target。

**App Clip、Watch App、扩展能用吗**

模板为单 target。多 target 需自行修改 `project.yml` 与 `ios.yml` 的 archive 步骤，每个 target 各配一份描述文件。

**会改动 App 代码吗**

不会。只修改签名材料、`project.yml` 中的版本号、`ship.config.json`。

**免费开发者账号能用吗**

不能。App Store Connect API 属于付费开发者计划。

**团队账号怎么配**

需要 Account Holder 创建团队密钥，并点一次「请求访问权限」。

**只装 Python 够吗**

够。`fastlane` 只在云端 runner 上运行，本机不需要 Ruby。运行时依赖 `cryptography`；`make-icon.py` 额外需要 Pillow，可选；`init`、`secrets` 只用标准库。

**一张证书能签几个 App**

不限。Distribution 证书为团队级，团队同时最多持有 2 张。过期证书仍占用名额，需手动吊销。

**证书或描述文件过期了**

构建失败，或安装后无法打开。重新运行 `setup`。证书未过期时只补发描述文件，不占用名额。

**换电脑了**

拷贝整个 `ios/tools/cert/` 目录，含私钥、证书、p12、`secrets.txt`。不要重新执行 `setup`。

**多人协作怎么做**

只保留一份 `cert/` 目录，由一人持有并通过加密压缩包分发。多人各自执行 `setup` 会各占一个证书名额。

**密码会经过第三方服务器吗**

不会。API 请求由本机直连 `api.appstoreconnect.apple.com`，鉴权用 `.p8` 本地签发的 JWT。

**为什么不让 CI 创建证书**

CI 机器销毁后私钥丢失，Apple 侧留下无法使用的证书，占用 2 个名额之一。私钥在本机生成一次，CI 只负责导入。

**CI 里私钥怎么使用**

从 secret 解码到 `$RUNNER_TEMP`，导入临时 keychain，任务结束后与 keychain 一并删除。

## 报错对照

`MAC verification failed (wrong password?)`

密码通常正确。原因是 p12 加密算法不兼容：macOS Security.framework 只接受 3DES + SHA1 MAC，`cryptography` 默认输出 AES-256 + SHA256。MAC 校验失败时框架统一提示 `wrong password?`。

本工具使用 `-certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1`。

`Couldn't find app 'com.xxx' on the account`

App 记录未创建。见步骤 6。

上传返回 409

两种相反含义：版本号不一致，或上传已成功而 altool 重试撞上 build 已存在（Apple 收到二进制后数十秒内不在 build 列表）。先查询状态：

```bash
python tools/ship.py status
```

`The bundle version must be higher than '3'`

build 号未递增，或被 push 触发的上传占用。`release` 自动递增；手动发版时修改 `project.yml` 的 `CURRENT_PROJECT_VERSION`。

`90713 Missing CFBundleIconName` / `90022 Missing 120x120`

缺图标，上传阶段才报，编译与签名均可通过。需同时具备 `Assets.xcassets/AppIcon.appiconset/`（1024×1024，无 alpha 通道）与 `project.yml` 中的 `ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon`。缺后者时 asset catalog 不编译进包。

`cannot load such file -- mutex_m`

runner 为 Ruby 3.4，`mutex_m`、`base64`、`bigdecimal` 已移出默认 gem。模板 `Gemfile` 中已声明。

`The 'locale' value is invalid`

写 `zh-Hans`，不是 `zh` 或 `zh-CN`。

`bash: command not found`（Windows）

Git for Windows 未提供 bash，PATH 中只有 `Git\cmd`。使用 `tools\ship.cmd`，可双击运行。

控制台中文乱码

cmd.exe 按 OEM 代码页解码输出。`ship.cmd` 内部执行 `chcp 65001`；直接运行 `python` 时先执行 `chcp 65001`。

`python` 打开微软商店

`WindowsApps` 下的 0 字节占位符，非真实解释器。安装 python.org 版本，或使用 `ship.cmd`（实际执行 `python -c` 判断可用性，不依赖 `where`）。

## 命令

| 命令 | 作用 | 副作用 |
|---|---|---|
| `ship.py deps` | 检查依赖，缺失时安装 | 安装 Python 包，安装前询问 |
| `ship.py doctor` | 七组检查 | 只读 |
| `ship.py init` | 生成 `ship.config.json` | 写一个配置文件 |
| `ship.py setup` | 生成整套证书链 | 调用 API 创建资源，可能占用证书名额 |
| `ship.py secrets` | 把 8 个 secrets 写入 GitHub | 修改仓库设置 |
| `ship.py release` | build 号加一、推送、构建、加入测试组 | 占用一个 build 号，不可撤销 |
| `ship.py status` | 查询分发状态 | 只读 |
| `testflight.py attach` | 单独加入 build、添加测试员、补 TestFlight 文案 | 写 App Store Connect |
| `make-icon.py` | 生成图标与 asset catalog | 写文件 |
| `selftest.py` | 89 条离线断言 | 只读 |

`setup` 参数：

```bash
--bundle-id com.example.app      # 省略时从 project.yml 推断
--reuse-cert ../old/cert         # 复用已有证书
--cert-dir tools/cert            # 指定材料目录
--force                          # 强制重新生成
--check                          # 仅验证 API Key，不创建资源
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
