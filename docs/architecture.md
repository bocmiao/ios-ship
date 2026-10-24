# 架构

一句话：**`ship.py` 是唯一入口，它按需调用三个专业的旁路脚本，配置由一个模块统一供给。**

```
                          ┌─────────────────┐
   你的终端 ─────────────▶│  ship.py        │  唯一入口 / 编排
                          │  doctor setup    │
                          │  secrets release │
                          │  status  init    │
                          └────┬────┬────┬───┘
                               │    │    │
              ┌────────────────┘    │    └────────────────┐
              ▼                     ▼                     ▼
      ┌───────────────┐    ┌────────────────┐    ┌───────────────┐
      │ apple-auto.py │    │ testflight.py  │    │ make-icon.py  │
      │ 证书链         │    │ 分发闭环        │    │ 图标生成       │
      └───────────────┘    └────────────────┘    └───────────────┘
              │                     │                     │
              ▼                     ▼                     ▼
        ┌──────────────────────────────────────────────────┐
        │              shipconfig.py  配置单一来源           │
        └──────────────────────────────────────────────────┘
                               │
                               ▼
        ┌──────────────────────────────────────────────────┐
        │  ship.config.json  >  project.yml 推断  >  默认值   │
        └──────────────────────────────────────────────────┘

      外部依赖：App Store Connect API（HTTPS）· GitHub CLI · git
```

---

## 为什么这样切

四种切法都能被想到，只留下最后一种。

| 切法 | 问题 |
|---|---|
| 一个大脚本包办全部 | 900+ 行，改图标逻辑要读证书代码。也测不了单块 |
| 每个命令一个脚本 | 重复的 API 鉴权、重复的配置读取、重复的颜色输出，粘了四遍 |
| 做成 Python 包发 PyPI | 用户只想 `cp -r tools/`，不想装包、不想管虚拟环境 |
| **一个入口 + 三个旁路** | 每个脚本能单独跑（`python apple-auto.py` 本身就是可用工具），入口只负责串起来 |

**零第三方依赖**是硬约束：只用标准库。原因是在 Windows 上让一个新手装 Python 已经够难了，再加一句 `pip install -r requirements.txt` 会多出一整类失败（网络、代理、pip 版本、venv 忘了激活）。`ship.cmd` 因此能直接双击跑。

唯一需要外部程序的地方：`openssl`（合成 p12）和 `gh`（GitHub 操作）。前者的替代品是 Python `cryptography`，但**那一版就是我踩坑 1 的源头**（AES-256 + SHA256 MAC），所以宁可调用 openssl 并显式传 `-certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1`。

---

## 各层职责

### ship.py —— 编排层

只做四件事：解析参数、读配置、**判断该做什么**、把活派出去。不直接处理证书字节，不直接拼 API 请求。

| 命令 | 这个命令自己做的事 | 派给谁 |
|---|---|---|
| `doctor` | **不发任何写请求**。逐组检查本地文件、API 可达性、账号下的证书/描述文件/App 记录/build 状态、GitHub secrets、最近一次 run | 读 `testflight.py` 的 API 封装，其余自己查 |
| `setup` | 决定要不要复用证书、要不要覆盖，然后调一次 | `apple-auto.py`（`--reuse-cert` 原样透传） |
| `secrets` | 读 `secrets.txt` → 逐条 `gh secret set` | 无 |
| `release` | build 号 +1 → 提交推送 → `gh workflow run` → 轮询 run 状态 → 挂测试组 | `testflight.py attach` |
| `status` | 分发状态速查 | `testflight.py` 的查询函数 |
| `init` | 生成 `ship.config.json` 样板 | `shipconfig.write_example()` |

`release` 是唯一**有副作用且不可逆**的命令，所以它做了三件防御：

1. 打一行明确的确认提示，除非 `-y`
2. build 号在**推送前**就写进 `project.yml`，不是构建时才算 —— 这样本地记录和云端永远一致
3. 上传后**必须**再查一次 `/v1/builds` 确认 `processingState`，因为 409 可能是假的（坑 11）

### apple-auto.py —— 证书链

一次跑完六步，每步都是幂等的（已存在就跳过，除非 `--force`）：

```
生成 EC 私钥 (P-256)
   ↓  openssl
CSR（base64 PEM）
   ↓  POST /v1/certificates  { certificateType: DISTRIBUTION, csrContent }
证书（base64 DER）
   ↓  openssl x509 -inform DER
distribution.cer
   ↓  openssl pkcs12 -export  ← 3DES + SHA1，见坑 1
distribution.p12  (friendly name = "Apple Distribution: <名字> (<TEAMID>)")
   ↓  POST /v1/bundleIds
Bundle ID
   ↓  POST /v1/profiles  { profileType: IOS_APP_STORE }
distribution.mobileprovision
   ↓  全部 base64
secrets.txt（8 个键）
```

**`--reuse-cert` 的核心是公钥比对**，不是「复制文件」：

```
旧目录 distribution.key  ──▶ 从私钥算出 SPKI
旧目录 distribution.cer  ──▶ 从证书算出 SPKI
                             两者相等？── 否 ──▶ 直接退出，不做 p12
                                  │是
                                  ▼
                          只补 Bundle ID + 描述文件
```

不比对的话，会给用户一个「生成成功」的 p12，然后在 CI 里签名失败 —— 那时离问题现场已经很远了。

### testflight.py —— 分发闭环

只负责「让 build 在手机上出现」。这是坑 13 的产物 —— 之前 `ship.py` 判定「链路通了」的标准是上传成功，而用户看的是手机。

`attach` 依次保证四件事，**每件都先查再做**：

```
1. App 有内部测试组？      没有 → POST /v1/betaGroups  { isInternalGroup: true }
                                    ↑ 显式传 true。不传就是外部组，要过审核（坑 14）
                                  有 → 是内部组吗？
                                       否 → 另建一个，不复用（外部组改不了，塞进去只会让 build 卡审核）
2. build 挂进组？          没有 → POST /v1/betaGroups/{id}/relationships/builds
3. App 级别的 TestFlight 文案？  没有 → POST /v1/betaAppLocalizations    { locale: "zh-Hans" }
4. 每个 build 的 what's new？    没有 → POST /v1/betaBuildLocalizations  { locale: "zh-Hans" }
5. 测试员在组里？          没有 → POST /v1/betaTesters  { relationships.betaGroups }
                                   ↑ 不复用同名 email 的旧记录：那是别的 App 下的（坑 15b）
```

`status` 是它的只读版本，输出直接对应「缺第几步」，而不是列一堆 API 字段让人自己判断。

### shipconfig.py —— 配置单一来源

引入它之前的问题：`bundle-id` 在四个脚本里各有一处默认值，`CamCoach` 这个名字硬编码在 12 个地方。换一个项目要全文替换，漏一处就是一次诡异失败。

优先级（高到低）：

```
1. 命令行参数           ship.py setup --bundle-id com.x.y
2. ship.config.json     显式配置，ios/ 或项目根都能放
3. project.yml 推断     用正则从 XcodeGen 描述里抠
4. DEFAULTS             全 None，validate() 会报缺
```

不引入 PyYAML：`project.yml` 是我们自己的配置文件，格式稳定，正则够了。少一个依赖，`ship.cmd` 就少一条失败路径。

| 键 | 从 project.yml 怎么推断 | 缺了会怎样 |
|---|---|---|
| `bundleId` | `PRODUCT_BUNDLE_IDENTIFIER` | **必需**，`validate()` 报错 |
| `scheme` | `schemes:` 下第一个 key，没有就取 `targets:` 下第一个 | **必需** |
| `xcodeDir` | 同 scheme（放 `Assets.xcassets` 的目录） | 找不到图标 → 上传才炸 90713 |
| `appName` | `CFBundleDisplayName` | p12 friendly name、证书名用 |
| `teamId` | `DEVELOPMENT_TEAM` | 证书匹配不到团队 |
| `profileName` | 无 | 默认 `<scheme> AppStore` |
| `repo` | 无（用 `gh repo view`） | release 推不上去 |
| `feedbackEmail` | 无 | TestFlight 反馈邮箱，有默认值 |

**`validate()` 只拦 `bundleId` 和 `scheme`**。其余缺了降级运行并给提示，不阻断 —— 因为 `setup` 和 `secrets` 不需要 `project.yml`，可能有人只想在空目录里先把证书搞定。

---

## 数据流：一次 `release`

```
ship.py release
  │
  ├─1─ 读 shipconfig ──▶ bundleId / scheme / xcodeDir
  │
  ├─2─ project.yml: CURRENT_PROJECT_VERSION 4 → 5     ← 写盘
  │
  ├─3─ git commit + push                              ← 不可逆，但可 revert
  │
  ├─4─ gh workflow run ios.yml                        ← GitHub 侧开始
  │        │
  │        │   ┌─ macos-latest ────────────────────────────┐
  │        │   │ 1. 装 Ruby 依赖（Gemfile 补 mutex_m 等）  │
  │        │   │ 2. 解 base64 → 导入 p12 到临时 keychain   │
  │        │   │ 3. 写描述文件                             │
  │        │   │ 4. xcodebuild archive  (XcodeGen 生成工程) │
  │        │   │ 5. xcodebuild -exportArchive              │
  │        │   │      method = app-store-connect           │
  │        │   │ 6. xcrun altool --upload-app              │
  │        │   │ 7. 删 keychain、删临时文件                 │
  │        │   └───────────────────────────────────────────┘
  │        │              ↓ 二进制进苹果
  ├─5─ 轮询 gh run view（每 10s，最多 15 分钟）        ← 这一步只代表「CI 跑完了」
  │
  ├─6─ 查 /v1/builds 找最新 build，等 processingState = VALID
  │                                                     ← 这一步才代表「苹果收下了」
  └─7─ testflight.py attach                            ← 这一步才代表「手机能装了」
```

**关键在于三个判断点被分开了。** 把它们合成一个「成功/失败」是坑 13 的成因：CI 绿了不等于 VALID，VALID 不等于能装。

`doctor` 也有类似的分层，7 组按「从本地到云端」排，靠前的组挂了就不必看后面的：

```
1. 本地材料      p8 / key / cer / p12 / secrets.txt / 版本号 / 图标
2. 凭据与账号    能不能生成 JWT、能不能调通 API
3. App 记录      ASC 里那条记录在不在（没有 API，只能手建）
4. 证书          有效期、剩余名额（上限 2）
5. 描述文件      有没有、是否匹配 bundleId、是否过期
6. 构建与分发    GitHub secrets 齐不齐、最近一次 run、有没有 VALID build、在不在内部组
7. GitHub 仓库   远端地址、分支、gh 是否已认证
```

---

## 凭据怎么流

只有三条路径，没有第四条：

```
① .p8（手动下载，唯一必须人工的一步）
      appsstoreconnect → 团队密钥 → 下载
      ↓  base64
② secrets.txt（setup 生成，gitignore）
      ↓  gh secret set ×8
③ GitHub Secrets
      ↓  注入 runner 环境
   $RUNNER_TEMP/*.p12 → security import → 用完即删
```

**私钥从不经过 CI 生成。** 这是和 `fastlane cert` 最重要的分歧：让 CI 生成私钥，私钥会出生在一台几分钟后就被销毁的机器上，而 Apple 那边留下一张永远用不了的证书 —— 团队名额只有 2 个。

`.gitignore` 挡的：`cert/`、`*.p8`、`*.p12`、`*.key`、`secrets.txt`、`*.mobileprovision`。`doctor` 第 1 组会顺带检查当前工作区有没有未忽略的可疑文件。

---

## 加一个新命令

`ship.py` 的 `main()` 里 action 是靠 `choices` 列表分发的，加命令需要动三处：

1. `ap.add_argument("action", choices=[...])` 加名字
2. 写一个 `cmd_xxx(args)`，返回 0/1
3. `main()` 末尾的分发表加一行

新命令要读配置就统一用 `args.cfg`（`main()` 已经加载好），**不要再自己 `sc.load()`** —— 那样会绕开命令行的覆盖值。

新命令要调 Apple API，用 `testflight.py` 里的 `api_token(args)` 拿 JWT，不要重写一遍 ES256 签名。

---

## 边界

**`tools/` 假设自己被放在 `<iOS项目>/ios/tools/`。** 靠 `HERE.parent` 定位 `project.yml`。放错位置时 `doctor` / `release` 会明确打印「当前位置」和「期望位置」，而不是抛 `FileNotFoundError`。

**不承诺跨平台一致。** 主力测试环境是 Windows + Git Bash（坑最多的地方，`ship.cmd` 专门处理 cmd.exe 的 GBK 问题）。macOS / Linux 用的是同一份 Python 代码，`openssl` 和 `gh` 调用没有平台分支。

**不做 App 功能。** 模板里的 `App/` 只是个能编译、能自检的最小工程 —— 它的作用是让整条链有个验证对象，不是给你当 App 骨架用的。
