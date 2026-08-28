# lakejobai-job-radar 技术原理全解析

> 一份针对本项目的完整技术拆解：数据怎么来的、打招呼怎么发的、用什么方式"监听"、为什么用 Firefox、扫码登录后状态为什么就"同步"了、点击本地页面会话为什么 BOSS 也会变、以及完整的数据流过程。
>
> 本文基于对仓库源码的逐行阅读（`boss_firefox.py` / `boss_automation.py` / `boss_app.py` / `boss_state.py` / `boss_replier.py` / `boss_company.py` / `boss_geo.py` / `static/dashboard.html` / `lakejob_cli/`），所有结论都标注了文件与行号，可自行核对。

---

## 目录

- [1. 一句话概括](#1-一句话概括)
- [2. 整体架构与每个文件干什么](#2-整体架构与每个文件干什么)
- [3. 岗位数据是怎么提取的](#3-岗位数据是怎么提取的)
- [4. 聊天数据是怎么提取的](#4-聊天数据是怎么提取的)
- [5. 打招呼是怎么发的](#5-打招呼是怎么发的)
- [6. "监听"方案是什么](#6-监听方案是什么)
- [7. 为什么用 Firefox,其它浏览器不行吗](#7-为什么用-firefox其它浏览器不行吗)
- [8. 状态同步原理(扫码登录/本地↔BOSS)](#8-状态同步原理扫码登录本地boss)
- [9. 完整数据流(四条链路)](#9-完整数据流四条链路)
- [10. 每一项技术分别是干什么的](#10-每一项技术分别是干什么的)
- [11. 你可能没注意到的技术细节](#11-你可能没注意到的技术细节)
- [12. 风险与边界](#12-风险与边界)

---

## 1. 一句话概括

这是一个 **「用 Playwright 驱动一个真实的、持久化的 Firefox 浏览器去操作 BOSS 直聘网页」** 的项目。

关键认知是：**本地控制台不是 BOSS 的另一个客户端,而是这套自动化浏览器的"遥控器 + 数据镜像"。** 所有动作(搜索、投递、打招呼、发消息、点会话)最终都是在那**同一个真实 Firefox 浏览器**里执行的;本地 SQLite 只是浏览器从 BOSS 页面上"读出来"的数据的镜像缓存。这就是为什么"扫个码登录就同步了""我点本地会话,BOSS 也会变"——因为根本没有两套状态,只有一个浏览器。

---

## 2. 整体架构与每个文件干什么

```
浏览器/前端                     FastAPI 后端                     BOSS 直聘
┌──────────────┐  HTTP/WS    ┌─────────────────┐  Playwright  ┌───────────────┐
│ dashboard.html│◄───────────►│   boss_app.py    │◄────────────►│  zhipin.com    │
│  (Web 控制台) │  fetch/WS   │  ├ 路由+WS+监控   │   Firefox     │               │
└──────────────┘             │  ├ boss_automation│   持久化Profile              │
└──── lakejob CLI ──► HTTP ──►│  │  ├ boss_firefox│◄─ 真实登录态 ─┘               │
    (AI Agent 调用)           │  ├ boss_replier  │──────────►│ AI API (DeepSeek等)
                             │  ├ boss_state ──► SQLite      └───────────────┘
                             │  ├ boss_company / boss_geo
                             └─────────────────┘
```

| 文件 | 行数 | 职责 | 关键内容 |
|---|---|---|---|
| `boss_firefox.py` | 1302 | **浏览器基座 + 采集** | `BossScraper` 类：拉起持久化 Firefox、扫码登录、反检测注入、搜索列表/详情页 DOM 提取、HR 活跃度解析、薪资反混淆 |
| `boss_automation.py` | 2006 | **交互自动化** | `BossAutomation` 继承上面的类：投递、打招呼、发消息、换微信/电话/简历、翻页扫描、聊天监控循环(读消息/AI回复) |
| `boss_app.py` | 2649 | **FastAPI 后端** | REST 路由、WebSocket 广播、后台 `chat_monitor_loop` 监控循环、Playwright 单线程执行器、搜索/同步/发消息等业务编排 |
| `boss_state.py` | 1064 | **SQLite 数据层** | 表结构、岗位/会话/消息/设置/公司缓存/每日统计的读写、公司去重、自动迁移 |
| `boss_replier.py` | 337 | **AI 文本生成** | 招呼语生成(模板/智能)、AI 自动回复 + HR 兴趣度评估、微信号编码绕过过滤 |
| `boss_company.py` | 94 | 公司画像聚合(**半成品**) | 公司页数据拼装、top HR 选取、候选公司排序。⚠ 它 import 的 `pick_top_hr` / `list_jobs_by_company` / `list_companies_by_position_count` 等函数**在仓库任何地方都没有定义**,当前只有测试引用它,运行中的 app 并不加载它(详见 §11.6) |
| `boss_geo.py` | 370 | 城市/区县/商圈映射 | 拉取 BOSS 城市/商圈 API 并缓存,中文区名→BOSS code(两级解析,`multiBusinessDistrict` 可收任意层级 code)。注意:**规模/融资阶段的 code 映射不在这里**,而在 `boss_firefox.py:693-694` 与 `boss_app.py:995-998` |
| `static/dashboard.html` | 3018 | 单文件 SPA 前端 | 深色控制台,HTTP+WS 双通道,无构建无 CDN |
| `lakejob_cli/` | — | CLI(AI Agent 入口) | 18 条命令,stdout 只输出 JSON 信封,通过 HTTP 调同一后端 |
| `interview/` | — | 面试问答子模块 | 独立面试题 Agent(基于 `llm_client.py` 复用 AI 配置) |
| `tests/` | — | 单测 | `test_smart_send.py`(纯函数+实例方法)、`test_boss_state.py`(数据层) |

> **注意类继承关系**：`BossAutomation(boss_automation)` **继承自** `BossScraper(boss_firefox)`。所以浏览器启动、登录、搜索采集的能力都来自 `boss_firefox.py`,交互能力在子类 `boss_automation.py` 中扩展。

---

## 3. 岗位数据是怎么提取的

### 3.1 搜索列表页:构造 BOSS URL → 让浏览器"真的去搜" → 读 DOM

不走 BOSS 的开放 API,而是**用浏览器访问真实的搜索结果页,再从页面 DOM 里抠数据**。这是整套方案的核心思路:**全程复用登录态、cookie、Referer,因此没有鉴权问题、签名问题。**

- **URL 构造**(`boss_firefox.py:708-752`)：`https://www.zhipin.com/web/geek/job?` + `query`(关键词)/`city`(城市 code)/`jobType`/`salary`/`degree`/`experience`/`scale`/`stage`/`multiBusinessDistrict`(区县,重复参数)。
  - 所有筛选参数都是 BOSS 前端实际使用的 **4 位 code**(如 `jobType=1901`=全职、`salary=405`=10-20K、`degree=203`=本科),这是从多个 GitHub 爬虫项目 + 实测验证得到的(`boss_firefox.py:689-730` 有完整注释)。
- **等待加载**(`_wait_for_jobs_loaded`, `boss_firefox.py:645-668`)：轮询 `a[href*="/job_detail/"]` 卡片数,连续 3 次相同才认为加载完。比固定 sleep 快且稳。
- **滚动加载全部**(`_scroll_all`, `boss_firefox.py:954-979`)：BOSS 列表是**虚拟滚动 + 懒加载**,`scrollHeight` 在加载完前可能不变,所以改用**卡片计数**判断"滚到底了没",滚回顶部确保 DOM 完整。
- **提取**(`_extract_job_cards`, `boss_firefox.py:840-952`)：在页面内 `page.evaluate` 执行 JS,遍历所有 `a[href*="/job_detail/"]`,用 `closest()` 找到整张卡片,再按 `.job-name`/`.salary`/`.company-name` 等选择器 + 文本特征兜底提取:标题、薪资、公司、城市、经验、学历、公司链接(`/gongsi/<id>.html` → `companyId`)、**HR 活跃度文案**。
- **文本兜底**(`boss_firefox.py:762-809`)：如果 DOM 选择器一个都没匹配到,回退到按 `body.innerText` 分行、用薪资正则 `\d+[-~]\d+K` 定位每张卡的锚点,再向上/向下扫描邻近行还原字段。双轨机制提高健壮性。

### 3.2 薪资反混淆:BOSS 的"加密"其实是 Unicode 移位

这是很关键且容易被忽略的点。BOSS 前端会把薪资数字渲染成 `U+E030~U+E039`(私有使用区)的字符,直接抓到的文本是"乱码"。项目用 `decode_salary()`(`boss_firefox.py:334-335`)解密:

```python
"".join(str(ord(c) - 0xE030) if 0xE030 <= ord(c) <= 0xE039 else c for c in text)
# U+E030+0 = '0', U+E030+1 = '1', ... 即字符码减去 0xE030 就是数字本身
```

这不是真加密,而是 BOSS 用字体/字符码做"反爬混淆",让普通抓取看不到数字;这里一行代码就还原了。

### 3.3 详情页:逐个访问 → 提取 JD + HR 信息

`fetch_detail()`(`boss_firefox.py:995-1103`)：
- `page.goto(url)` 访问岗位详情页;
- 从 body 文本里找 **HR 真实姓名/头衔**(通过 `HR`/`招聘者`/`人事`/`HRBP` 等标记 + 其上一行短文本)、HR 活跃度文案;
- 从"职位描述/岗位职责"开始截取,到"公司介绍/工商信息"为止的文本作为 JD;
- 多套选择器 + 文本正则兜底。

### 3.4 HR 活跃度:中文文案 → 天数

BOSS 显示的是"刚刚活跃/今日活跃/3日内活跃/本周活跃/30日内活跃/半年内活跃"这类文案。`parse_hr_active()`(`boss_firefox.py:406-442`)用正则把它解析成数字天数,连中文数字(`三`、`十二`)都能解析(`_parse_cn_int`, `boss_firefox.py:389-403`)。这个天数用于 **"跳过长期不活跃的 HR"** 过滤,避免浪费每日投递上限。

### 3.5 入库前的清洗漏斗(`boss_app.py:1477-1558`)

搜索返回后,后端按顺序做多层过滤再入库:
1. **福利关键词筛选** `_filter_by_welfare`(AND 逻辑);
2. **公司去重** `has_company_been_applied`(已发过 → 跳过);
3. **HR 不活跃过滤**(`hr_active_days > 阈值` → 跳过);
4. **标题黑名单 + 乱码兜底**:`_is_garbled_text()`(`boss_app.py:917-933`)用正则 `\?{3,}|[�]{2,}|[-]{2,}` 识别 BOSS 详情页混入的乱码(含薪资混淆字符),命中的岗位直接丢弃;
5. 命中关键词黑名单的岗位以 `status='filtered'` 入库,便于用户在投递记录页看到"为什么被过滤"。

> 搜索是"先有真实搜索结果,再做本地业务过滤",而不是把过滤条件硬塞给 BOSS。

---

## 4. 聊天数据是怎么提取的

### 4.1 会话列表:`poll_conversation_list()`(`boss_automation.py:746-859`)

- **方式一(DOM)**：找到会话列表项(`li[role="listitem"]` 等),从 `.name-text` 精确提取 **HR 名字**,用精确 class 或文本特征提取岗位名(`.position-name`),检测 `.red-dot` 未读红点;
- **方式二(正则兜底)**：`body.innerText` 用 `(\d{1,2}:\d{2})\s+(名字)\s+(\[状态\])\s+(消息内容)` 的正则切出每条会话。

### 4.2 消息内容:`read_visible_messages()`(`boss_automation.py:861-986`)

这是聊天数据提取最精巧的函数,难点是**别把左侧会话列表当聊天内容、区分我/HR、还原时间**：
- **视口过滤**:只处理 `getBoundingClientRect()` 中心在视口右侧 35% 之外的元素(`r.left + r.width/2 >= vw*0.35`),从而**只读右侧聊天窗,避开左侧会话列表**;
- **发送方判定**:按 CSS class 是否含 `item-myself/myself/self` 或元素位于视口 52% 右侧 → `me`,否则 `hr`;
- **状态剥除**:消息里的 `已读/未读/送达/发送失败/已发送` 前后缀被 `clean()` 剥掉,只留正文;
- **时间还原**:扫描时间分隔条(`[class*="time-divider"]` 等),把"14:30""昨天 14:30""06-12 14:30"解析成 ISO 时间,按 DOM 垂直位置 `top` 匹配到它下方最近的每条消息。BOSS 不给你时间字段,这是靠布局推断的。

### 4.3 会话头部信息:`read_chat_header_info()`(`boss_automation.py:1017-1051`)

读 `.position-name`(岗位名)、`.salary`、`.city`,把聊天窗口头部当前岗位的"岗位名 · 薪资 · 城市"写回本地会话。

### 4.4 在线状态:`read_chat_online_status()`(`boss_automation.py:988-1015`)

读 `img.chat-online-stats` 的 alt/src/父元素文本判断在线/离线/忙碌。

### 4.5 securityId 获取(交换微信/电话的关键)

`_get_chat_security_id()`(`boss_automation.py:1253-1316`)有 3 层来源:页面 HTML 正则搜 `securityId` → window 全局对象扫描 → **用 BOSS 内部 API** `https://www.zhipin.com/wapi/zprelation/friend/geekFilterByLabel` 拉好友列表按 HR 名匹配。拿到 securityId 后,`send_wechat`/`send_phone` 直接调 BOSS 的 `/wapi/zpchat/exchange/test` 接口发起官方交换。

> **重要**:这些 BOSS 内部 API 是**在浏览器页面里用 `page.evaluate` 执行 `fetch()` 调用的**(`boss_automation.py:1293-1299, 1324-1336`),自动带上真实 cookie + `x-requested-with: XMLHttpRequest` + Referer。这就是 README 里说的"风控绕开"手法:绕过了从 Python 直接发 HTTP 请求时缺 cookie/签名的问题。

### 4.6 微信提取

监控循环里,如果 HR 消息包含微信号(`wxid_`、`微信:`、`加我` 等正则,`boss_automation.py:1816-1826`),自动提取并 `update_conversation_wechat()` 存库,前端"微信交换记录"页可见。

---

## 5. 打招呼是怎么发的

### 5.1 两种模式(`boss_replier.py`)

| 模式 | 触发 | 实现 |
|---|---|---|
| **模板模式**(默认) | `greeting_mode=template` | 把设置里的 `greeting_template` 做 `{job_title}`/`{company}` 字符串替换(`generate_greeting`, `boss_replier.py:310-337`)。默认模板甚至自带自我介绍:"…正在和你聊天的这个AI工具是我自己开发的——就当是我的技术名片了"(`boss_state.py:212`) |
| **智能模式** | `greeting_mode=smart` | 调 LLM 读 JD + 简历摘要,按 `smart_greeting_prompt`(可自定义)生成 ≤100 字个性化招呼语,突出与岗位的匹配点 + "效果付费"式话术;失败自动回退模板(`generate_smart_greeting`, `boss_replier.py:225-307`) |

### 5.2 批量投递时只调一次 AI

`apply_batch()`(`boss_automation.py:522-574`)智能模式下**只对第一条岗位调 LLM**,后面所有岗位复用同一句招呼语——避免每条都等 2-8 秒 LLM 响应。

### 5.3 发送动作 = 模拟真人打字

`apply_to_job()`(`boss_automation.py:307-504`)投递流程:
1. 日限检查(`get_today_application_count` vs `daily_apply_limit`,超限返 429);
2. 公司去重、HR 活跃度过滤、标题关键词黑名单(都在投递前,不消耗日限);
3. `page.goto(job_url)` 打开详情页(用 `domcontentloaded` 而非 `load`,因为详情页外链广告/统计脚本多,等 load 会卡 10-30s,`boss_automation.py:384-386`);
4. 安全检查 `check_page_safety()`(验证码/滑块/账号异常/频控);
5. 点「立即沟通」(`SELECTORS["apply_button"]`,多选择器兜底),**兼容 BOSS 2025+ 的中央浮窗聊天弹窗**(`boss_automation.py:53-54` 注释);
6. `send_message()`(`boss_automation.py:1195-1251`):点击输入框 → `Ctrl+A`+`Backspace` 清空 → `keyboard.type(text, delay=20~40ms)` **逐字键入**(模拟真人,确保 BOSS 检测到真实输入事件)→ `Enter` 发送 → 用 body 文本校验是否发出(`check = text[:8]`)。

### 5.4 发完写库

投递成功后:更新 `applications.status='applied'`、写 `greeting_text/greeting_sent_at`、有 HR 名字则 `get_or_create_conversation` 建会话、`increment_daily_stat("applications_sent")`。

### 5.5 自动回复(后续接管聊天)

监控循环发现未回复的 HR 消息后,`generate_reply()`(`boss_replier.py:119-202`)用 SYSTEM_PROMPT(要求 AI 坦诚自己是求职者开发的 AI 助手、**绝不代承诺面试**、引导加微信)+ 最近 5 条对话上下文 + JD + 简历摘要,生成 `{"reply": "...", "interest": "high/medium/low"}` JSON,同时评估 HR 兴趣度。发回复前会按 HR 消息关键词自动执行:
- HR 要"简历" → `send_resume()` 点 BOSS 官方「发简历」按钮;
- HR 要"微信" → `send_wechat()` 走 BOSS 官方「换微信」通道;
- HR 要"电话" → `send_phone()`。

**顺序很讲究**:先执行真实发送动作,再让 AI 说出"已通过BOSS把简历发给您了"——保证 AI 说的话在物理上已经发生(`boss_automation.py:1920` 注释)。

---

## 6. "监听"方案是什么

> 先给结论:**没有任何网络层拦截(没有 `page.on('response')`、没有 `page.route()` 抓包)。** 全项目 grep 确认过。所谓"监听"= **DOM 轮询 + 后台异步循环 + WebSocket 推送**,三层拼出来的。

### 6.1 后台循环 `chat_monitor_loop()`(`boss_app.py:2494-2613`)

浏览器一启动(`/api/system/start`)、或重新登录、或服务启动时,就创建一个 asyncio 后台任务。循环体:
1. 随机睡 `min_reply_delay_sec~max_reply_delay_sec`(默认 15-20s,**加上随机抖动**);
2. `monitor_paused` 则跳过本轮;
3. `automation.heartbeat()` 轻量检查登录态(不导航、不触发反爬),**连续 2 次失败 → 广播 `session_expired` 并退出**;
4. 每轮 `keep_alive()` 保活:已登录时**只在聊天页轻量滚动/移动鼠标**,避免频繁 reload 被检测(`boss_automation.py:268-289`);
5. `auto_reply_enabled` 为 true 才执行 `run_chat_monitor_cycle()`。

### 6.2 单个监控周期 `run_chat_monitor_cycle()`(`boss_automation.py:1569-2006`)

```
1. 只在不在聊天页时才导航到 /web/geek/chat(避免每轮刷新触发登录检查)
2. 点「未读」Tab → 只显示有未读的会话
3. poll_conversation_list() 扫会话列表
4. 与数据库已知会话按 HR 名匹配;新会话则建库
5. 每轮最多处理前 3 个未读会话:
   open_conversation_by_name() 打开会话
   read_visible_messages() 读消息 → replace_conversation_messages() 全量覆盖本地
   更新在线状态 / 岗位头信息 / 提取微信号
6. 从消息尾部往回找"未回复的 HR 消息"(跳过 BOSS 系统通知)
7. 若未回复且自动回复开启:
   generate_reply() → 先发简历/微信/电话 → send_message() 发 AI 回复 → 写库
8. 清空输入框残留 → 重新点「未读」Tab 刷新侧栏
```

### 6.3 WebSocket 把变化"推"到前端

`broadcast_ws()`(`boss_app.py:1098`)把监控结果推给所有 `/ws` 连接。前端 `handleWS()`(`dashboard.html:2060-2091`)收到 `new_messages`/`auto_reply_sent`/`wechat_exchanged`/`session_expired`/`safety_warning`/`search_complete`/`apply_complete` 等事件后,自动重新拉会话列表和消息,实现"BOSS 有动静 → 控制台立刻变"。

### 6.4 安全问题兜底

每轮 `check_page_safety()`(`boss_automation.py:227-248`)检查验证码(`验证/滑块/拼图/captcha`)、账号异常(`账号异常/违规/冻结`)、频控(`操作太频繁/稍后再试`),任一命中即广播 `safety_warning` 并停止自动操作。

> **小结**:监听是"定时去看页面 DOM,发现新东西就处理并推送",而不是"被动接收 BOSS 的推送/拦截网络"。这也解释了为什么它在 15-20 秒粒度上是"准实时"的。

---

## 7. 为什么用 Firefox,其它浏览器不行吗

### 7.1 事实层面

代码里明确写死用的是 Playwright 的 Firefox:

```python
self._ctx = self._pw.firefox.launch_persistent_context(str(PROFILE_DIR), **kw)
# boss_firefox.py:468  +  README「Playwright + Firefox 持久化 Profile」
```

安装依赖时也是 `playwright install firefox`。

### 7.2 为什么可以"只有 Firefox 也行",以及为什么特意选它

**"其它浏览器可以吗?"——技术上完全可以。** Playwright 同时支持 `chromium`、`firefox`、`webkit`,把 `self._pw.firefox` 换成 `self._pw.chromium` 就能跑 Chromium 版,代码其余部分(选择器、DOM 读取、`page.evaluate`)完全浏览器无关。所以不是"只有 Firefox 可以",而是**作者在工程上选择了 Firefox**。结合代码与常见实践,理由可归纳为:

1. **持久化 Profile 是刚需,而 Firefox 的持久化上下文最"干净"**(`launch_persistent_context` 用 `firefox_user_data/` 目录,登录 cookie 直接落在这个目录,重启即恢复)。
2. **反检测注入对 Firefox 更隐蔽**:项目用 `ANTI_DETECT`(`boss_firefox.py:114-193`)隐藏 `navigator.webdriver`、伪造语言/硬件/时区/Canvas/WebGL/权限等指纹。业内普遍认为 Chrome 系的自动化特征(如 `navigator.webdriver`、浏览器二进制特征)被反爬/验证码系统识别得更狠,Firefox 指纹相对"普通",更接近真人用户。
3. **自带上游构建、安装简单**:`playwright install firefox` 一条命令装好,不依赖系统已装的 Chrome/Edge,也避免"用户装的是哪个 Chrome 版本"的兼容问题。
4. **UA 与桌面一致**:代码里把 UA 伪装成 `Firefox/125.0`(`boss_firefox.py:466`),配合上述指纹,让 BOSS 看到的是一个普通桌面 Firefox。
5. 它也是这个项目灵感来源 `boss-agent-cli` 的既有选型,继承而来。

> 诚实说明:README 和代码并没有一段"为什么非 Firefox 不可"的正式文档,以上是**基于代码特征 + 业内常见工程实践**的合理推断。换成 Chromium 需要同步调整的是:浏览器类型、UA/指纹伪装脚本,以及可能更高的验证码触发率。

---

## 8. 状态同步原理(扫码登录/本地↔BOSS)

### 8.1 "为什么扫个码,状态就同步了?"

因为**根本没有两套登录状态,只有一套**——你扫的码,就是让**那个 Playwright 控制的真实 Firefox** 完成 BOSS 登录:

```
扫码流程:
1. 前端点「重新扫码登录」→ POST /api/system/relogin (boss_app.py:1247)
2. 后端关旧浏览器 → 新建 BossAutomation → start() → login()
3. login() (boss_firefox.py:601-641):
   page.goto(".../web/user/?ka=header-login") 打开登录页
   等 600s,每 1s 轮询 URL: 一旦跳回已登录页面(/web/geek 等)且 _login_prompt_visible()==False → 登录成功
   state = self._ctx.storage_state()  → 把 cookie/localStorage 写到 .boss_profile/firefox_state.json
4. 前端无需轮询,后端扫码完成后广播 relogin_ok,前端 sessionStatus 变"登录态正常"
```

**关键**:浏览器是**持久化上下文**(profile 目录 `firefox_user_data/`),cookie 本身就存在那个目录里。下次 `start()`(`boss_firefox.py:459-487`)时:
- `launch_persistent_context(profile_dir)` 自动恢复全部 cookie(登录态);
- 若 `firefox_state.json` 存在,再 `add_cookies` 兜底恢复一次。

于是:**你只要扫过一次码,BOSS 的登录 cookie 就永久存在这个浏览器的 profile 里,之后每次启动浏览器都是"已登录"状态。** 本地控制台的 `status`/`heartbeat` 只是读这个浏览器当前页面的登录态,自然就"同步"了。

### 8.2 "我点本地会话,BOSS 也会变"——因为本地页面在遥控同一个浏览器

本地 Web 控制台对 BOSS 的每次"点击",都会让**同一个 Firefox 真的去点**:

| 前端操作 | 调用的 API | 后端驱动浏览器做什么 |
|---|---|---|
| 点某个会话 | `POST /api/conversations/{id}/sync`(`boss_app.py:2072`) | `open_conversation_by_name(hr_name)` **在真实浏览器里定位并点击该会话**(`boss_automation.py:1053-1193`),然后 `read_visible_messages()` 读消息 |
| 点开不读消息 | `POST /api/conversations/{id}/open`(`boss_app.py:2201`) | 同上,只打开会话 |
| 发消息 | `POST /api/conversations/{id}/send`(`boss_app.py:2169`) | 先打开会话,再 `send_message()` 在浏览器里**逐字键入 + Enter 发送**,**浏览器发送成功后才写本地库**(`boss_automation.py:2187` 注释"浏览器发送失败,本地不会写入") |
| 搜索 | `POST /api/jobs/search` | 浏览器导航到搜索页 → 滚动 → 读 DOM |
| 投递 | `POST /api/jobs/apply` | 浏览器打开详情页 → 点「立即沟通」→ 发招呼语 |
| 导航聊天页 | `POST /api/system/navigate-chat` | `automation.navigate_to_chat()` 浏览器跳聊天页 |

所以当你点本地会话时,**同一个 Firefox 窗口的 BOSS 页面真的切到了那个会话**,右侧聊天窗真的打开了——你肉眼看 BOSS 当然"也在变化"。这跟"另开一个客户端再同步"完全是两回事。

### 8.3 反向:BOSS 变了怎么同步到本地?

由 6 节的**监控循环**负责:BOSS 有未读消息 → 循环发现 → 打开会话 → 读消息 → **全量覆盖本地缓存**(`replace_conversation_messages`, `boss_state.py:884-920`)→ 广播 WS → 前端刷新。注意 `replace_conversation_messages` 是**先 DELETE 该会话本地所有消息再按 BOSS DOM 当前状态重插**,所以本地永远是 BOSS 的"快照副本",不存在增量对账的复杂度。它还聪明地保留了 `ai_generated=1` 的标记(本地 AI 发的消息删掉后重插仍保持 AI 角标,`boss_state.py:887-902`)。

### 8.4 线程/并发模型(同步之所以能串起来的前提)

FastAPI 是 asyncio,Playwright 是同步 API 且**要求所有浏览器操作都在同一线程**。所以:
- `_playwright_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pw")`(`boss_app.py:135`);
- `_run_pw(fn)`(`boss_app.py:138-150`)把同步操作塞进这个单线程池,执行前还 `asyncio.set_event_loop(None)` 清掉 asyncio 状态(否则 Playwright 检测到 event loop 会拒绝运行);
- 所有需要碰浏览器的端点都包在 `_run_pw(...)` 里,并用 `asyncio.Lock`(`browser_sync_lock`)互斥,避免多个请求同时操作浏览器打架(锁被占时直接返回本地缓存,`boss_app.py:2090-2097`)。

---

## 9. 完整数据流(四条链路)

### 链路 A:搜索岗位

```
前端 doSearch() ──POST /api/jobs/search──► 后端 search_jobs()
  └ 城市名→code,前端code直传(BOSS原生参数)
  └ _run_pw(automation.search): 浏览器构造URL→导航→等卡片→滚动→_extract_job_cards(DOM)+文本兜底
  └ 福利AND筛选 → 公司去重 → HR不活跃过滤 → 标题黑名单/乱码过滤
  └ 入库SQLite: 有则 update_application_from_job, 无则 add_application
  └ broadcast_ws({type:"search_complete"})
  └ 返回 {jobs_found, saved, skipped_*}
前端 WS收到search_complete → toast + loadJobs() 渲染卡片
```

### 链路 B:投递 + 打招呼

```
前端"一键投递/翻5页" ──► /api/jobs/apply-batch 或 /api/jobs/scan-and-apply
  └ apply_batch(): 智能模式先generate_greeting(仅一次LLM), 条间随机30-90s延迟
  └ 每条 apply_to_job():
      日限检查 → 公司去重 → HR活跃过滤 → 关键词黑名单
      → 浏览器打开详情页 → check_page_safety
      → 点「立即沟通」 → 等聊天输入框(兼容中央弹窗)
      → send_message(greeting) 逐字键入+Enter → 验证
      → 写库(applied/greeting_text) → 有HR名建会话 → increment_daily_stat
  └ broadcast_ws(apply_complete/batch_complete)
前端 WS → 更新卡片状态为 applied + 刷新漏斗
```

### 链路 C:点击本地会话 → 读消息(同步)

```
前端 selectConversation(id)
  └ 显示"正在对齐颗粒度..."遮罩 → syncConversation(id) → POST /api/conversations/{id}/sync
后端 sync_conversation_messages():
  browser_sync_lock 加锁
  → open_conversation_by_name(hr_name)   ★真实浏览器打开该会话★
  → read_visible_messages() / read_chat_online_status() / read_chat_header_info()
  → replace_conversation_messages() 全量覆盖本地消息
  → 更新 online_status/job_title/salary/city → update_conversation_last_message
  → 返回消息
前端 renderMessages() 渲染(右侧BOSS浏览器此刻也正开着这个会话)
```

### 链路 D:后台监控 + 自动回复(BOSS → 本地 → AI → BOSS)

```
chat_monitor_loop() 每15-20s(+抖动)：
  heartbeat 检查登录 → keep_alive 轻量保活
  run_chat_monitor_cycle():
    导航/切「未读」→ poll_conversation_list()
    → 对前3个未读会话: 打开→read_visible_messages→replace_conversation_messages(落库)
    → 找未回复HR消息 → generate_reply()(LLM)
    → 按关键词先发简历/微信/电话(BOSS官方通道)
    → send_message(AI回复) → add_message(me, ai_generated=True) 落库
    → increment_daily_stat("auto_replies_sent")
  → broadcast_ws(new_messages/auto_reply_sent/wechat_exchanged)
前端 WS → 聊天页 loadConversations + loadMessages 刷新
```

---

## 10. 每一项技术分别是干什么的

| 技术 | 在本项目的作用 |
|---|---|
| **Playwright(sync API)** | 唯一浏览器驱动。负责导航、点击、逐字输入、DOM 读取、`page.evaluate` 内联 JS、`storage_state` 存取、`launch_persistent_context` 持久化登录态 |
| **Firefox 持久化上下文** | 登录态随 profile 目录永久保存,重启即恢复;也是"扫码一次永久登录"的根基 |
| **ANTI_DETECT 注入脚本** | 隐藏 `navigator.webdriver`,伪造语言/硬件并发/时区(Asia/Shanghai)/Canvas 噪声/WebGL 厂商串/通知权限/network connection,让 BOSS 把自动化浏览器当成真人桌面 |
| **`page.evaluate(fetch)` 浏览器内调用** | 对 BOSS 内部 API(`/wapi/zprelation/friend/...`、`/wapi/zpchat/exchange/test`)在页面上下文中发请求,自动带 cookie/Referer/X-Requested-With,绕开直连缺鉴权/签名的问题(README 所称"风控绕开") |
| **FastAPI + uvicorn** | 后端 Web 服务,~40 个 REST 端点 + 1 个 `/ws` WebSocket |
| **WebSocket** | 后端→前端的实时事件推送(`new_messages`/`search_complete`/`session_expired` 等),实现"BOSS 有动静页面就变" |
| **`ThreadPoolExecutor(max_workers=1)` + `_run_pw`** | 让同步 Playwright 与 asyncio FastAPI 共存:所有浏览器操作串行执行在唯一线程,执行前清空 event loop 状态 |
| **SQLite(WAL 模式)** | 本地数据镜像:岗位/会话/消息/设置/每日统计/候选池/公司缓存。WAL 支持多线程读 |
| **单文件 SPA dashboard.html(Vanilla JS)** | 无构建无 CDN 的深色控制台,HTTP 拉取 + WS 推送双通道渲染 |
| **Click + httpx(lakejob CLI)** | 18 条命令,stdout 只输出 JSON 信封,供 AI Agent 以 subprocess 方式调用;CLI 只是后端 HTTP 客户端,不直接碰浏览器 |
| **OpenAI 兼容 Chat Completions API(DeepSeek/OpenRouter/小米 MiMo/自定义)** | 招呼语生成、自动回复+兴趣度、JD 分析、简历优化、沟通建议 |
| **`interview/llm_client.py`** | AI 配置懒加载(每次从 SQLite 读 key/base_url/model),被 boss_replier 与 interview 子模块复用 |
| **`boss_geo.py` + BOSS 城市/商圈 API** | 中文城市/区名 → BOSS code 映射,前端下拉数据源(带 6 小时内存缓存 + 静态兜底) |
| **pre-commit / GitHub Actions(ci.yml)** | 代码规范(riff)与 CI 检查 |
| **`tests/`(unittest, 临时 DB)** | 纯函数 + 实例方法单测,不依赖真实浏览器:HR 活跃解析、公司去重、招呼语生成、冷却逻辑 |

---

## 11. 你可能没注意到的技术细节

这里收集了大量"藏在代码里但一眼看不到"的设计,按主题归类:

### 11.1 反爬/风控对抗
- **薪资 Unicode 反混淆** `decode_salary`(见 3.2)。不只用于显示,还用于判断"薪资是否命中范围"(`salary_ok`, `boss_firefox.py:338-351`)和识别乱码岗位。
- **登录页 vs 详情页的智能判断** `_login_prompt_visible()`(`boss_firefox.py:506-589`)：不只查"登录"字样,而是先查 URL 是否在登录路径,再查页面是否出现"职位描述/立即沟通/已沟通/聊天"等**已登录特征**(优先级更高,避免把详情页误判成登录页),再查强登录提示,最后用 JS 检查登录框是否**真实可见**(宽高>0、opacity、`aria-hidden`)。`is_logged_in_page()` 对 `about:blank` 返回 True(未知不当作过期)。
- **保持低姿态的保活**:`keep_alive()`(`boss_automation.py:268-289`)已登录时**不刷新页面**,只在聊天页滚动/移动鼠标,避免频繁 reload 触发检测。
- **翻页双重策略**:优先点 BOSS 的「下一页」按钮(`a[ka="page-next"]`),disabled 则判断到底;失败兜底直接改 URL 的 `page` 参数(`boss_automation.py:580-624`)。
- **输入事件仿真**:发消息不直接 `fill()`,而是 `keyboard.type` **逐字**敲入(delay 20-40ms)+ Enter,让 BOSS 检测到真实键盘事件;`_human_type` 还先点击再敲(`boss_automation.py:194-203`)。
- **微信号编码绕过内容过滤**:`_encode_wechat`(`boss_replier.py:74-81`)把微信号里的 `-` 替换成全角 `一`,规避 BOSS 对微信消息的过滤(SYSTEM_PROMPT 也明确禁止 AI 在文字里直接出现"微信/VX"等词)。

### 11.2 数据一致性与去重
- **公司名模糊去重**:`_normalize_company_name`(`boss_state.py:260-274`)去掉"有限公司/集团/股份/(中国)"等中英文后缀,再 `has_company_been_applied` 三层匹配:company_id 精确 → 全名精确 → 归一化名模糊。且只把 `applied/replied/interview` 算"已发",`pending/filtered` 不算,避免误杀。
- **AI 结果 24h DB 缓存**:`/api/jobs/optimize-resume` 与 `chat-suggestion` 把结果 JSON 存进 `applications.optimize_result/chat_suggestion_result` + 时间戳,24h 内命中直接返回(带 `_cached` 标记),省 token(`boss_app.py:1823-1837, 1919-1933`)。
- **公司信息 24h 缓存**:`companies` 表,`UNIQUE(name COLLATE NOCASE, company_id)`,UPSERT 刷新 fetched_at,过期自动清理(`boss_state.py:359-453`)。
- **乱码清洗**:`_GARBLE_RE` 识别 `??`、U+FFFD、U+E030-E039 混入的坏文本;`clean_open_positions`(`boss_state.py:476-500`)过滤公司页"在招岗位"里的薪资文案和"更多/加载更多"等 UI 噪音。
- **会话垃圾清理**:startup 时删除"HR/你好/消息/未知HR"等垃圾会话名,并按 `hr_name` 合并重复会话(`boss_app.py:102-129`)。

### 11.3 浏览器/并发工程
- **`_run_pw` 清 event loop 的 hack**:`asyncio.set_event_loop(None)` 是让 Playwright sync API 在 asyncio 环境里能跑的关键(否则报 "Playwright sync API is not allowed in event loop")。
- **`browser_sync_lock`** 全局互斥,防止 `sync` 和监控循环同时抢浏览器。
- **CORS 全开** `allow_origins=["*"]`(`boss_app.py:78-84`)——本工具只在 127.0.0.1 本地跑,方便开发。
- **print 打时间戳**:模块加载时 monkeypatch 了 `builtins.print`,所有后端日志自动带 `[HH:MM:SS]` 前缀(`boss_app.py:22-27`),纯调试便利。
- **Windows 编码修复**:`boss_firefox.py:34` 把 stdout 包成 utf-8 TextIOWrapper,避免中文日志在 Windows 控制台乱码。
- **前端 static 响应加 `no-cache`**:`/` 返回 HTML 时设置 `Cache-Control: no-cache, no-store`(`boss_app.py:1115-1124`),保证改前端代码刷新即生效。

### 11.4 业务上的聪明点
- **智能招呼语批量只生成一次**(见 5.2)。
- **自动回复的触发动作编排**:先发简历/微信/电话,后发 AI 文字,顺序固定;回复前检查 `resume_sent`/`hr_wechat`/`phone_shared` 是否已发过,不重复发;回复里要"已发送"的措辞只在动作真实发生后出现。
- **未读计数更新**:`update_conversation_last_message`(`boss_state.py:755-783`)先比对 last_message_text/from 是否真变了,没变就不刷时间戳,避免监控循环打开旧会话虚增"收到回复";回复成功后用 `-999` 清零未读。
- **每轮只处理前 3 个未读会话** + 处理完重新点「未读」Tab,因为 BOSS 会把已读的会话移出列表(`boss_automation.py:1617-1622, 1993-2002`)。
- **BOSS 系统通知过滤**:"你与该职位竞争者PK情况/竞争力分析/BOSS安全提示/系统消息/今日推荐/该Boss已查看了你的简历"这类不算 HR 回复,不触发自动回复、不更新 last_message_from(`boss_automation.py:1839-1852`)。
- **消息时间靠布局推断**(见 4.2),没有平台时间字段也能还原时间线。
- **`_filter_by_welfare` AND 逻辑**:多福利关键词必须全部命中,过滤严格。

### 11.5 CLI 的设计
- **JSON 信封契约**:所有命令 `{ok, command, data, pagination, error}`,stdout 纯 JSON、stderr 日志,exit 0/1 区分成败——专为 AI Agent 设计(`lakejob schema` 能输出工具描述)。
- CLI 通过 HTTP 调 FastAPI,`LAKEJOB_API` 环境变量可改地址,连接失败伪装成 503。

### 11.6 其它文件里的彩蛋
- `boss_geo.py` 直接用 BOSS 公开 API(`/wapi/zpCommon/data/city.json`)拉城市,带 6 小时内存缓存,并内置一份**静态城市码兜底表**,API 不可用也能工作。
- `tests/test_smart_send.py` 通过 `setup_module` 把 `boss_state.DB_PATH` 指向临时文件,不污染真实库;且注释说明可直接 `python -m unittest` 跑(规避 Windows 上 pytest teardown 异常)。
- `interview/` 子模块复用同一份 AI 配置,`llm_client.llm_chat_deepseek` 每次调用都从 SQLite 懒加载 key,改设置立即生效。
- `CHANGELOG/CHANGES.md` 记录了"BOSS 2025+ 改中央浮窗聊天弹窗""虚拟滚动导致 scrollHeight 失效""页面改版选择器失效"等真实踩坑,是理解项目演进的好材料。
- **测试先行、实现未合入的半成品**——看代码时注意区分"测试里设计了什么"与"生产代码里真跑了什么":
  - `boss_company.py` 是典型例子:它 `from boss_automation import pick_top_hr`、`from boss_state import list_jobs_by_company, list_companies_by_position_count`,但这几个函数**在整个仓库只有引用、没有 `def` 定义**;`tests/test_smart_send.py` 为它们写了整套测试(`TestCompanyBuilder` 等,通过 `BossAutomation.__dict__[...]` 绕过 import 直接取方法)。当前 `boss_company` 只在测试里被 import,**运行中的 `boss_app.py` 不加载它**,所以线上不报错、但功能也不存在。
  - 公司的真实抓取走 `boss_app._scrape_company_page()`(`boss_app.py:2358-2426`),抓的是 行业/规模/融资/成立/在招岗位/简介,**并没有抓"法人/老板"字段**。`boss_app.py:1936-1939` 那句"对方很可能是公司老板/法人本人"只是**塞给 LLM 的提示词**,不是真实法人数据——README 对"法人识别"的宣称目前是超前于实现的。
  - 风控退避同理:`tests/test_smart_send.py` 的 `TestCooldownBackoff` 为 `_trigger_cooldown`/`_cooldown_remaining`/`_respect_cooldown`/`in_cooldown`(含 banned 冷却 ≥1000s、rate_limit 指数退避封顶 30 分钟)和 `inspect_page_safety` 写了测试,但这些方法在 `boss_automation.py` 里**都没有实现**——生产代码只有 `check_page_safety()`(L227-248)的关键词检测→停止操作 + 各处随机延迟 + 硬上限(`MAX_APPLY_PER_DAY=50`/`MAX_AUTO_REPLY_PER_DAY=200`, `boss_automation.py:153-154`)。README 宣称的"风控自动冷却退避"目前只落地了前半段。

---

## 12. 风险与边界

- **合规**:README 明确"仅用于个人账号求职辅助,每日投递上限默认 15、风控冷却退避、触发风控立即停止手动操作"。这是个人求职辅助工具,不是批量采集/商业用途。
- **脆弱性**:一切依赖 BOSS 的 DOM 结构。BOSS 改版时,大量 CSS 选择器会失效——项目用"多选择器列表 + 文本正则兜底 + 可配置 `selector_overrides`(settings 表) + `/api/debug/*` 选择器诊断端点"来应对。
- **成本**:自动回复/AI 功能依赖外部 LLM API Key,未配置时自动回复不工作(监控循环会提示)。
- **"文档领先于实现"**:README/CHANGELOG 宣传的部分能力(风控指数退避/长冷却、`boss_company.py` 公司画像、法人识别)当前**只有测试、没有合入实现**,详见 §11.6。读文档时要区分"宣称的能力"与"代码里真正跑起来的能力",前者只体现在 `tests/test_smart_send.py` 与 README 里。
- **局限**:聊天是准实时轮询(15-20s 粒度),不是毫秒级;`_login_prompt_visible` 是启发式判断,极端页面可能误判;浏览器崩溃由前端 `browser_crashed` 提示一键重启。

---

*本文基于仓库 commit `a281112` 之后的当前工作区源码撰写。行号是写作当时的真实行号,随版本演进可能漂移。*
