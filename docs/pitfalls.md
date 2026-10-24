# 18 条实测踩坑

全部在真实项目上踩过，报错文本原样复制。按排查耗时由长到短排列。

每条固定四段：**现象 → 误判 → 真相 → 怎么避**。

（编号 1–17，另加一条 15b——测试员的坑有两个，拆开写比硬塞进一条清楚。）

---

## 一、证书与签名

### 1. `MAC verification failed (wrong password?)` —— 密码是对的

**现象**

macOS runner 里导入 p12 失败：

```
security: SecKeychainItemImport: MAC verification failed (wrong password?)
```

**误判**

第一反应是 secret 里的密码写错了。于是去查密码：长度对不对、有没有多余空格、base64 编码有没有换行、GitHub secret 是不是存了带引号的版本。折腾了两轮。

然后用同一份密码在本地 openssl 里解 p12：

```bash
openssl pkcs12 -in cert.p12 -passin "pass:$P12_PASSWORD" -nokeys -noout
# 成功，无报错
```

本地能解，CI 解不了 —— 说明密码没问题，**是 p12 的加密参数和 macOS 不兼容**。

**真相**

Python `cryptography` 的 `pkcs12.serialize_key_and_certificates(..., encryption_algorithm=BestAvailableEncryption(pw))` 产出的是 **AES-256-CBC 加密 + SHA-256 MAC**。

- OpenSSL 认得，能正常解
- **macOS 的 Security.framework 只认 3DES 加密 + SHA-1 MAC**

算法不匹配时，框架会把它当成「MAC 校验失败」，然后给出那句完全误导的 `wrong password?`。

**怎么避**

导出 p12 时必须显式指定算法：

```bash
openssl pkcs12 -export \
  -inkey distribution.key \
  -in distribution.cer \
  -certfile AppleWWDRCA.cer \
  -name "Apple Distribution: Your Name (TEAMID)" \
  -passout "pass:$P12_PASSWORD" \
  -certpbe PBE-SHA1-3DES \
  -keypbe PBE-SHA1-3DES \
  -macalg sha1 \
  -out distribution.p12
```

`apple-auto.py` 的 `build_p12()` 已经这么写了。如果你用自己的脚本，把这三个参数抄过去。

`-name` 也别省：不写的话，导进 macOS keychain 后私钥没有友好名，`security find-identity` 匹配不到，后面的 `codesign` 会报找不到签名标识。

报错括号里的猜测不作为结论依据。`wrong password?` 是框架在 MAC 校验失败时统一附加的提示。

---

### 2. 个人 API Key 调不了 Provisioning 接口

**现象**

`POST /v1/certificates` 返回 403，或者干脆提示 key 无权访问该资源。

**误判**

以为是 Access 权限等级不够（选了 App Manager 想换成 Admin）。

**真相**

App Store Connect 有**两种** API 密钥，名字非常像：

| 类型 | 位置 | 能不能调证书/描述文件接口 |
|---|---|---|
| 个人密钥 Individual Key | 用户和访问 → 集成 → 密钥 | **不能** |
| 团队密钥 Team Key | 用户和访问 → 集成 → **团队密钥** | 能 |

个人密钥是绑在某个 Apple ID 上的，能读 App 列表、能传二进制，但**签发证书和描述文件属于团队级操作，必须团队密钥**。

**怎么避**

建 key 的时候认准「团队密钥」那一栏。两种 key 的 `.p8` 文件名长得一模一样（都是 `AuthKey_XXXXXXXXXX.p8`），下错了要到调 Provisioning 接口才发现。

一个快速判别：团队密钥页面上会有 **Issuer ID**（一段 UUID 形式的东西），个人密钥不需要 Issuer ID。

---

### 3. 第一次用 API 要「请求访问权限」

**现象**

key 建好了、权限也给了 Admin，调用还是被拒。

**真相**

账号（Account Holder）需要先在后台手动点一次 **Request Access**（请求访问权限），等苹果审核通过。没点过的话，API 一律不通。

**怎么避**

只在**第一次**配 API 时需要。入口在「用户和访问 → 集成 → App Store Connect API」顶部。点完通常几分钟到一天内生效，界面上会有状态。

`ship.py doctor` 会真的发一个 API 请求来验证，通了才显示 OK —— 它不会因为「文件存在」就判合格。

---

### 4. Distribution 证书团队上限是 2 张，而且不随时间恢复

**现象**

新项目申请证书报错，提示数量已达上限。

**误判**

以为像描述文件一样有有效期回收 —— 等旧的过期就好了。

**真相**

一个团队同时最多持有 **2 张 Apple Distribution 证书**。这个名额**不随过期释放**（过期后仍然计入，要手动吊销才腾位置）。

而一张 Distribution 证书可以给**无数个 App** 签名 —— 它是团队级的，不是 App 级的。

只申请一张，之后所有项目复用。

**怎么避**

```bash
python tools/ship.py setup --bundle-id com.example.newapp \
  --reuse-cert ../old-project/tools/cert
```

它会读旧目录的 `distribution.key` 和 `distribution.cer`，比对公钥（SPKI）确认两者真的配对，不配就直接拒绝 —— 不会给你一个「看起来能用、签名时才炸」的 p12。

`ship.py doctor` 也会提前把这个名额状态打出来：

```
[4. 证书]
  OK   Apple Distribution: Your Name (TEAMID)   到期 2027-09-26
  OK   Apple Distribution: Your Name (TEAMID)   到期 2027-10-04
  !!   已有 2 张 Distribution 证书（团队上限 2 张）
       → 新项目请用 --reuse-cert 复用，别再申请
```

这条检查是加在差点犯第二次错之后。

---

### 5. Xcode 26 的导出方法改名了

**现象**

`xcodebuild -exportArchive` 报 ExportOptions 里的 `method` 值无效。

**真相**

Xcode 26 之前叫 `app-store`，**26 开始叫 `app-store-connect`**。写旧值直接失败。

```xml
<key>method</key>
<string>app-store-connect</string>
```

**怎么避**

模板里的 ExportOptions 已经是新值。如果从老教程抄配置，这里要改。

---

## 二、Bundle ID 与 App 记录

### 6. App ID ≠ App 记录，而创建 App 记录没有 API

**现象**

证书有了、描述文件有了、上传报：

```
Couldn't find app 'com.example.myapp' on the account
```

**误判**

以为要在 developer 后台再建一个 Identifier。但那里明明已经有了。

**真相**

这是**两个不同的东西**：

| | 在哪 | 是什么 | 有 API 吗 |
|---|---|---|---|
| App ID / Identifier | developer.apple.com → Identifiers | 一个 bundle id 字符串，绑定 capability | 有 |
| App 记录 | appstoreconnect.apple.com → 我的 App | 一个商店条目，含名称、SKU、语言、上架信息 | **没有** |

上传二进制认的是**后者**。而苹果**没有开放创建 App 记录的 API**，只能手动建一次。

**怎么避**

手工建一次，30 秒：

```
appstoreconnect.apple.com → 我的 App → 左上角 + → 新建 App
  平台：iOS
  名称：随便填（后面能改）
  主要语言：简体中文
  Bundle ID：下拉选你刚建的那个
  SKU：自己定，比如 myapp001（内部标识，不公开）
  用户访问权限：完全访问
```

**只需要建一次**，之后所有版本都往这条记录上推。

`ship.py doctor` 会检查这条记录存不存在，缺了会直接告诉你「这一步必须手动，没有 API」。

---

### 7. Bundle ID 全球唯一

**现象**

```json
{ "errors": [{ "status": "409",
  "code": "ENTITY_ERROR.ATTRIBUTE.INVALID.DUPLICATE",
  "title": "The provided entity includes an attribute with a value that has already been used" }] }
```

**真相**

`com.myapp.app` 这种「看起来通用」的 ID 早被人占了。全球唯一，抢占先到先得。

**怎么避**

用反域名格式带上自己的标识：`com.<你的标识>.<产品名>`。比如 `com.yourname.myapp` 而不是 `com.myapp.app`。

被占了就换，不用申诉 —— 换个前缀的事。

---

## 三、构建

### 8. 图标：编译全绿、签名全绿、**上传才炸**

**现象**

本地和 CI 的编译、签名、导出全都成功，上传阶段报：

```
ERROR ITMS-90713: Missing CFBundleIconName in Info.plist
ERROR ITMS-90022: Missing required icon file. The bundle does not contain
an app icon for iPhone / iPod Touch of exactly '120x120' pixels
```

**误判**

以为要手写 Info.plist 的 `CFBundleIconName` 键。

**真相**

两个条件都要满足，缺一不可：

1. 有完整的 asset catalog：`Assets.xcassets/AppIcon.appiconset/`（含 `Contents.json` + 一张 **1024×1024 无 alpha 通道**的 PNG）
2. `project.yml` 里显式写 `ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon`

缺第 2 条时 asset catalog 不编译进包，`CFBundleIconName` 随之缺失，报错指向的是 `Info.plist` 少字段。

**怎么避**

```bash
python tools/make-icon.py logo.png --out App/Assets.xcassets/AppIcon.appiconset
```

它会：去掉 alpha 通道（有这个通道也会被 90022 拒）、缩放到 1024、生成 `Contents.json`（single-size 格式，Xcode 26 支持）。

然后确认 `project.yml` 里那一行在。

`ios.yml` 里加了一道前置检查，缺图标直接失败在编译前，不用等到上传。

---

### 9. 版本号必须和 App Store Connect 一致

**现象**

```
409 ENTITY_ERROR.RELATIONSHIP.INVALID.INVALID_STATE
```

**误判**

以为是 build 号冲突，去改 build 号 —— 改了还是报同样的错。

**真相**

`MARKETING_VERSION`（就是 `CFBundleShortVersionString`）必须和 ASC 里那条 App 记录的**当前版本号字符串完全一致**。包里写 `0.1.0`、ASC 里是 `1.0`，就对不上。

注意这跟 build 号是两件事：

- **版本号**（`1.0`）：会展示给用户，必须和 ASC 一致
- **build 号**（`4`）：内部递增，每次上传必须比上次大

**怎么避**

`project.yml` 里 `MARKETING_VERSION` 直接写成你 ASC 上的值。`ship.py release` 会自动把 `CURRENT_PROJECT_VERSION` 加一。

---

### 10. build 号用掉就拿不回来，上传不可撤销

**现象**

```
ERROR ITMS-90186: Invalid Pre-Release Train. The train version '1.0' is closed
ERROR ITMS-90189: Redundant Binary Upload. You've already uploaded a build with
build number '3'
```

或者：

```
The provided entity attribute value is invalid: 'buildNumber' must be higher than '3'
```

**真相**

上传**不可撤销**。一个 build 号被任何二进制占用过，就永久属于那个二进制。删掉 build 也好、版本过期也好，号都回不来。

所以「随便传一版试试」是有成本的 —— 试错几次就把号烧掉了。

**误判 → 更大代价**

一开始 `ios.yml` 是挂在 `push` 上的。结果：

- 改个 README 注释 → 自动构建 → 自动上传 → 烧掉一个号
- 提交了一版编不过的代码 → 编译失败，还好没烧
- 提交了一版能编过的中间状态 → 又烧一个

一天里无意义地推了 3 个 build。

**怎么避**

1. `ios.yml` 的 triggers **只留 `workflow_dispatch`**，不要挂 `push`
2. 日常「代码能不能编过」交给另一条**免签名**流水线（模板里的 `validate.yml`），它只编译，不签名、不导出、不上传
3. 真的想发版，用 `python tools/ship.py release`

---

### 11. 409 也可能是上传成功

**现象**

```
409 ... "There already exists a build with this version and build number"
```

**误判**

看到 409 就以为失败，重新跑一遍上传。结果越跑越乱。

**真相**

Apple 收下二进制后，到它能出现在 `GET /v1/builds` 列表里，中间有几秒延迟。`altool` 内置了重试逻辑，第二次尝试时撞上「build 已存在」，把这个**本来就成功**的上传报成了失败。

所以同一个 409 有两种完全相反的含义：

- 版本号不一致 → 真失败
- 上传成功后的重试碰撞 → **假失败**

**怎么避**

看到 409 上传错误，**先查，别重传**：

```bash
python tools/ship.py status
```

或者直接看：

```bash
curl -s -H "Authorization: Bearer $JWT" \
  "https://api.appstoreconnect.apple.com/v1/builds?filter[app]=$APP_ID&limit=3"
```

`processingState` 是 `VALID` 就是成功了。

---

### 12. Ruby 3.4 把 fastlane 要的 gem 从默认里删了

**现象**

runner 上 `bundle exec fastlane` 直接崩：

```
cannot load such file -- mutex_m (LoadError)
```

或者 `base64`、`bigdecimal` 找不到。

**真相**

macOS runner 现在是 Ruby 3.4。这几个库在 3.4 里**从默认 gem 降级为 bundled gem / 移出标准库**，必须显式声明依赖。

**怎么避**

`Gemfile` 里写全：

```ruby
source "https://rubygems.org"

gem "fastlane"
gem "mutex_m"
gem "base64"
gem "bigdecimal"
```

模板里已经带了。

---

## 四、TestFlight 分发（最容易整块漏掉）

这一组是「所有步骤都显示成功，但手机上什么都没有」的根源。

### 13. 上传成功 ≠ 手机上能装上

**现象**

CI 全绿，`/v1/builds` 里 `processingState: VALID`。打开 TestFlight App —— **空白，什么都没有**。

**误判**

以为是 Apple 处理慢，等了一天。第二天还是空白。

**真相**

`VALID` 只代表「苹果把二进制处理完了」。要让手机能装，还差**三步，缺任何一步都是空白**：

1. 这个 App 有**内部测试组**（Internal Beta Group）
2. 那个 build **被分配**进这个组
3. **你的 Apple ID 在这个组里**

这三步分布在两个不同的 API 资源里，而且都不会自动发生。

**怎么避**

```bash
python tools/ship.py status    # 会直接告诉你缺第几步
```

或者跑一次完整分发：

```bash
python tools/testflight.py attach --build 4
```

它会把建组、挂 build、加测试员、补本地化文案全做掉，已经存在的不重复建。

只验证「上传成功」不构成链路可用。曾出现 build 已达 `VALID` 但未加入任何测试组的情况，判定为成功，设备端 TestFlight 为空。验证终点是可安装，不是中间状态。

---

### 14. `POST /v1/betaGroups` 默认建的是**外部**组

**现象**

组建好了，build 也挂上了，TestFlight 里还是空白，或者提示「等待审核」。

**真相**

不传 `isInternalGroup` 时，API 建的默认是**外部组**。外部组的第一个 build **必须通过 Beta App Review**（1–3 天人工审核）才能用。

内部组没有这个要求，立刻可用，上限 100 人。

而且这个字段**建完改不了**：

```json
{ "errors": [{ "status": "409",
  "code": "ENTITY_ERROR.ATTRIBUTE.INVALID",
  "detail": "isInternalGroup can not be included in a 'UPDATE' operation" }] }
```

**怎么避**

建组时显式传：

```json
{
  "data": {
    "type": "betaGroups",
    "attributes": {
      "name": "Internal",
      "isInternalGroup": true
    },
    "relationships": {
      "app": { "data": { "type": "apps", "id": "<APP_ID>" } }
    }
  }
}
```

建错了只能删组重建 —— 不能改。

`testflight.py` 的 `ensure_internal_group()` 特意**不复用非内部组**：名字撞上了也会另建一个内部组，而不是把 build 塞进那个外部组里假装成功。

---

### 15. `POST /v1/betaTesters` 必须带关系

**现象**

```json
{ "errors": [{ "status": "409",
  "code": "ENTITY_ERROR.RELATIONSHIP.REQUIRED",
  "detail": "Either 'betaGroups' or 'builds' relationship is required" }] }
```

**真相**

新建测试员时，必须同时说明「这个人属于哪些组」或「这个人能测哪些 build」，因为一个测试员可以是多个 App 的测试员，光有 email 不知道该挂哪。

**怎么避**

一次带上内联关系：

```json
{
  "data": {
    "type": "betaTesters",
    "attributes": { "email": "you@example.com", "firstName": "San", "lastName": "Zhang" },
    "relationships": {
      "betaGroups": { "data": [{ "type": "betaGroups", "id": "<GROUP_ID>" }] }
    }
  }
}
```

---

### 15b. 别的 App 下的测试员不能直接复用

**现象**

想把已存在的测试员加进新 App 的组：

```json
{ "errors": [{ "status": "409",
  "detail": "Tester(s) cannot be assigned" }] }
```

**真相**

`GET /v1/betaTesters` 是**团队级**的，会把你所有 App 的测试员都列出来。但这些测试员记录是**按 App 归属**的，别的 App 下的记录不能直接挂到新 App 上。

**怎么避**

别去复用，直接 `POST /v1/betaTesters` 新建一条（同一个 email 在不同 App 下是不同的记录，这不冲突）。`testflight.py` 就是这么做的。

---

### 16. locale 必须写 `zh-Hans`

**现象**

```json
{ "errors": [{ "status": "409",
  "code": "ENTITY_ERROR.ATTRIBUTE.INVALID",
  "detail": "The 'locale' value is invalid" }] }
```

**误判**

试了 `zh-CN`（BCP-47 习惯写法）→ 还是错。试了 `zh_CN` → 还是错。

**真相**

ASC API 的 locale 用的是 App Store 的语言代码体系，不是 BCP-47：

- 简体中文 = **`zh-Hans`**
- 繁体中文 = `zh-Hant`
- 英文（美国）= `en-US`

完整合法值见 [`asc-api.md`](asc-api.md)。

**怎么避**

`betaAppLocalizations`（App 级文案）和 `betaBuildLocalizations`（每个 build 的 what's new）都建一份 `zh-Hans`，TestFlight 里才不会显示一堆英文占位。

`testflight.py` 会自动补这两个。

---

## 五、凭据

### 17. GitHub 的 secret 遮蔽只替换**完整值**，不覆盖切片

**现象**

工作流里写：

```yaml
echo "密码长度: ${#CERT_PASSWORD}"
echo "首字符: ${CERT_PASSWORD:0:1}"
```

日志里出现的是字面的 `24` 和 `a` —— **没有被遮蔽**。仓库是 public，任何人对 Actions 日志有读权限。

这条是真发生在我自己项目上的，被一个代码审查工具扫出来。

**真相**

GitHub 的 secret 遮蔽机制是字符串替换：扫描日志，把出现的**完整 secret 值**替换成 `***`。

`${#VAR}` 产出的派生值（长度）、`${VAR:0:1}` 产出的切片，**都不会被替换** —— 它们不是那个完整值。

后果评估：密码是 24 位 62 字符表 CSPRNG，泄露了「长度 + 首字符 + 末字符」后剩余熵约 131 bit，暴力破解不可行。所以**不需要轮换密码**。但这不是可以依赖的运气 —— 换成 6 位密码就是灾难。

**怎么避**

规矩三条：

1. **诊断只输出布尔结果**：`能不能打开`、`解密成功/失败`，不输出任何片段
2. 需要区分失败原因时，用**非敏感量**：文件字节数、返回码、错误类型
3. `echo` 密码变量之前先想一句：这行会不会进日志？

```yaml
# 对：只说字节数，不说内容
echo "p12 base64 长度: ${#CERT_P12_BASE64}"   # base64 本身也是 secret，长度无关紧要
echo "解码后字节数: $(wc -c < "$RUNNER_TEMP/cert.p12" | tr -d ' ')"
```

注意区分：**base64 的长度**泄不泄露信息？对一眼能算出长度的编码（p12 结构固定），长度几乎没有信息量。**密码的长度**有 —— 它直接缩小搜索空间。所以前者可打，后者不可。

---

## 附：故障对照表

| 现象 | 多半是 | 条目 |
|---|---|---|
| `MAC verification failed (wrong password?)` | p12 是 AES-256/SHA256，要改 3DES+SHA1 | 1 |
| API 403 | 用了个人密钥 / Access 不够 | 2 |
| API 全部被拒（第一次） | 没点 Request Access | 3 |
| 证书建不了、提示名额满 | 团队上限 2 张，改 `--reuse-cert` | 4 |
| ExportOptions `method` 无效 | Xcode 26 要 `app-store-connect` | 5 |
| `Couldn't find app 'com.xxx'` | App 记录没建（只能手建） | 6 |
| 建 Bundle ID 报 409 DUPLICATE | 全球唯一，换个前缀 | 7 |
| `90713` / `90022` | 缺图标的两个条件之一 | 8 |
| `409 INVALID_STATE`（上传） | 版本号与 ASC 不一致 | 9 |
| `must be higher than N` | build 号没加 / 被 push 自动烧掉 | 10 |
| 上传报 409 但不确定真假 | 先查 `/v1/builds`，别重传 | 11 |
| `cannot load such file -- mutex_m` | Ruby 3.4 要显式声明 gem | 12 |
| TestFlight 里空白 | 缺内部组 / 缺挂 build / 缺测试员 | 13 |
| `isInternalGroup ... UPDATE` | 建成了外部组，删了重建 | 14 |
| `Either 'betaGroups' or 'builds' relationship` | 建测试员要带关系 | 15 |
| `Tester(s) cannot be assigned` | 那是别的 App 下的测试员，新建一条 | 15b |
| `The 'locale' value is invalid` | 要写 `zh-Hans` | 16 |
| Actions 日志出现凭据片段 | 用了 `${#VAR}` 这类切片 | 17 |

---

## 共性

18 条中有 5 条（10、11、13、14、17）属于判断标准问题，与 API 调用方式无关：

- 把「命令没报错」当成「事情做成了」（13）
- 把「报错了」当成「失败了」（11）
- 把「执行了这一步」当成「这一步生效了」（14）
- 把「存在遮蔽机制」当成「所有敏感值都被遮蔽」（17）

工具能自动化的是第一种。其余三种需验证到可安装、可运行、日志无敏感输出。
