# App Store Connect API 速查

`https://api.appstoreconnect.apple.com`

## 鉴权

ES256 JWT，有效期最长 20 分钟（这里用 15 分钟）。

```
header:  {"alg":"ES256","kid":"<Key ID>","typ":"JWT"}
payload: {"iss":"<Issuer ID>","iat":<now>,"exp":<now+900>,"aud":"appstoreconnect-v1"}
```

签名是 ES256，但 **JWT 要求的是 64 字节 raw 签名（r||s）**，而 `cryptography` 默认产出 DER 编码。必须转换：
- DER 去掉前两个字节的 SEQUENCE 头，再把两段 INTEGER 各补零到 32 字节

请求头：`Authorization: Bearer <jwt>`，写操作加 `Content-Type: application/json`。

无需 `pyjwt`，标准库 + `cryptography` 足够。参考 `tools/apple-auto.py` 的 `make_jwt()`。

## 常用端点

### 账号与 App

| 用途 | 请求 |
|---|---|
| 验证密钥能通 | `GET /v1/bundleIds?limit=1` |
| Bundle ID 是否已注册 | `GET /v1/bundleIds?filter[identifier]=com.x.y` |
| 查 App 记录 | `GET /v1/apps?filter[bundleId]=com.x.y` |
| 建 Bundle ID | `POST /v1/bundleIds`（`identifier`, `name`, `platform: IOS`） |
| **建 App 记录** | **没有 API，只能手动建** |

### 证书

| 用途 | 请求 |
|---|---|
| 列证书 | `GET /v1/certificates?limit=50` |
| 申请 Distribution | `POST /v1/certificates` |

```json
{"data":{"type":"certificates",
  "attributes":{"certificateType":"DISTRIBUTION","csrContent":"<base64 PEM>"}}}
```
返回的 `certificateContent` 是 **base64 的 DER**，解码后是 `.cer`。

### 描述文件

| 用途 | 请求 |
|---|---|
| 列 | `GET /v1/profiles?filter[profileType]=IOS_APP_STORE&limit=50` |
| 建 | `POST /v1/profiles` |

```json
{"data":{"type":"profiles",
  "attributes":{"name":"MyApp AppStore","profileType":"IOS_APP_STORE"},
  "relationships":{
    "bundleId":{"data":{"type":"bundleIds","id":"<id>"}},
    "certificates":{"data":[{"type":"certificates","id":"<id>"}]}}}}
```
`profileContent` 是 base64 的 `.mobileprovision`。

### 构建

| 用途 | 请求 |
|---|---|
| 最新几个 build | `GET /v1/builds?filter[app]={appId}&sort=-version&limit=10` |
| 单个 build | `GET /v1/builds/{id}?include=betaAppReviewSubmission` |

关键属性：`version`（build 号）、`processingState`（`PROCESSING` / `VALID` / `INVALID`）、`expirationDate`（90 天）、`expired`。

### TestFlight 分发

| 用途 | 请求 |
|---|---|
| 列测试组 | `GET /v1/betaGroups?filter[app]={appId}&limit=50` |
| **建内部组** | `POST /v1/betaGroups` |
| 组内含 build 与测试员 | `GET /v1/betaGroups/{id}?include=builds,betaTesters` |
| 挂 build 进组 | `POST /v1/betaGroups/{id}/relationships/builds` |
| 建测试员（同时入组） | `POST /v1/betaTesters` |
| 组内测试员 | `GET /v1/betaGroups/{id}/betaTesters` |
| App 级测试信息 | `GET/POST /v1/betaAppLocalizations?filter[app]={appId}` |
| build 的「本次测试内容」 | `GET/POST /v1/betaBuildLocalizations?filter[build]={buildId}` |

**建内部组**（`isInternalGroup` 必须显式给，否则建出来是外部组）：
```json
{"data":{"type":"betaGroups",
  "attributes":{"name":"内部测试","isInternalGroup":true},
  "relationships":{"app":{"data":{"type":"apps","id":"<appId>"}}}}}
```

**建测试员**（必须同时带关系）：
```json
{"data":{"type":"betaTesters",
  "attributes":{"email":"a@b.com","firstName":"A","lastName":"B"},
  "relationships":{"betaGroups":{"data":[{"type":"betaGroups","id":"<gid>"}]}}}}
```

**本地化**（`locale` 用 App Store 语言代码）：
```json
{"data":{"type":"betaAppLocalizations",
  "attributes":{"locale":"zh-Hans","description":"...","feedbackEmail":"a@b.com"},
  "relationships":{"app":{"data":{"type":"apps","id":"<appId>"}}}}}
```

## 只读字段（写不进去，会报错）

| 字段 | 报错 |
|---|---|
| `betaGroups.isInternalGroup` 在 PATCH 时 | `The attribute 'isInternalGroup' can not be included in a 'UPDATE' operation` |
| `builds.processingState` | 由 Apple 控制 |

## 语言代码（locale 枚举节选）

`ar-SA` `ca` `cs` `da` `de-DE` `el` `en-AU` `en-CA` `en-GB` `en-US` `es-ES` `es-MX`
`fi` `fr-CA` `fr-FR` `he` `hi` `hr` `hu` `id` `it` `ja` `ko` `ms` `nl-NL` `no` `pl`
`pt-BR` `pt-PT` `ro` `ru` `sk` `sv` `th` `tr` `uk` `vi` **`zh-Hans`** **`zh-Hant`**

注意不是 `zh`、也不是 `zh-CN`。

## 错误码对照

| HTTP | 含义 | 常见原因 |
|---|---|---|
| 401 | 未授权 | 用了推送密钥；Key ID / Issuer ID 错；密钥已吊销 |
| 403 | 权限不足 | 个人密钥；Access 不是 App Manager/Admin；打 Provisioning 接口 |
| 409 | 冲突 | 见下方细分 |
| 422 | 实体校验失败 | 字段值不合法（如 locale） |

409 常见 `code`：

- `ENTITY_ERROR.ATTRIBUTE.INVALID.DUPLICATE` — build 号重复
- `ENTITY_ERROR.RELATIONSHIP.INVALID.INVALID_STATE` — 版本号不匹配，**或上传其实成功了**
- `ENTITY_ERROR.ATTRIBUTE.INVALID` — 字段值非法（locale 等）

## 响应格式

成功：`{"data": {...}}` 或 `{"data": [...]}`，带 `include` 时多一个 `included` 数组。
失败：`{"errors":[{"status","code","title","detail"}]}`，`detail` 才是人话。

## 参考实现

- `tools/apple-auto.py` — JWT、CSR、证书、p12、Bundle ID、描述文件
- `tools/testflight.py` — 测试组、build 分配、测试员、本地化
- `tools/ship.py` — 编排与体检
