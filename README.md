# Body Monitor - AstrBot 身体数据监测插件

**Version:** v1.3.0

基于 Health Connect Webhook 方案，接收小米手环 + 小米体脂秤 S400 数据，进行基线计算、异常检测和主动关怀，也可以选择将结构化健康事件交给 Private Companion 统一调度。

### 中国大陆设备
- [MiHealth - 小米运动健康 AstrBot 插件](https://github.com/ludan0312/astrbot_plugin_mi_health)
- [部署指南](http://ludanhome.online:19192/?p=324)

## 架构

```
小米手环/体脂秤 -> 小米运动健康 -> Health Connect -> Health Connect Webhook App -> 插件 HTTP 端点
```

## 支持数据

| 设备 | 数据类型 |
|------|---------|
| 小米手环 | 心率、步数、睡眠时长/评分、血氧、压力、HRV |
| 小米体脂秤 S400 | 体重、体脂率、BMI、肌肉量、水分率、骨量、基础代谢、内脏脂肪 |

## 安装

1. AstrBot WebUI → **插件管理** → 搜索 `Body Monitor` 安装，或上传本插件 zip
2. 在 WebUI 中配置插件参数（见下方配置项）
3. 使用 `/body_target_add here` 添加健康事件目标
4. 重启 AstrBot

## 配置项

| 配置项 | 说明 | 默认值 |
|-------|------|-------|
| `enable_private_companion_integration` | 启用 Private Companion Pull 模式联动；开启后 Body Monitor 不再直发 | `false` |
| `llm_provider_id` | 原生主动关怀使用的 LLM Provider，留空使用默认 | `""` |
| `persona_id` | 原生主动关怀使用的人格，留空使用默认 | `""` |
| `data_port` | HTTP 数据接收端口 | `7788` |
| `baseline_days` | 基线收集天数 | `7` |
| `baseline_mode` | 基线模式 (`sliding`/`fixed`) | `sliding` |
| `check_interval` | 检测间隔（秒） | `300` |
| `quiet_hours_enabled` | 是否启用静默时段 | `true` |
| `quiet_hours_start` | 静默开始时间 | `23:00` |
| `quiet_hours_end` | 静默结束时间 | `08:00` |
| `heart_rate_enabled` | 心率异常检测开关 | `true` |
| `heart_rate_threshold` | 心率异常 z-score 阈值 | `2.0` |
| `heart_rate_cooldown` | 心率告警冷却（小时） | `4` |
| `sleep_score_enabled` | 睡眠评分异常检测开关 | `true` |
| `sleep_score_threshold` | 睡眠评分异常 z-score 阈值 | `1.5` |
| `sleep_score_cooldown` | 睡眠评分告警冷却（小时） | `8` |
| `spo2_enabled` | 血氧异常检测开关 | `true` |
| `spo2_threshold` | 血氧异常 z-score 阈值 | `2.0` |
| `spo2_cooldown` | 血氧告警冷却（小时） | `4` |

默认关闭联动，此时插件保持原有行为：异常命中后由 Body Monitor 调用配置的 LLM 生成人格化关怀并直接发送。开启联动后，`llm_provider_id` 和 `persona_id` 暂不参与主动关怀，文案、审查和发送统一由 Private Companion 负责。

## 手机端配置

1. 安装 Health Connect Webhook App
2. 授予 Health Connect 权限
3. 配置 Webhook URL: `https://你的DDNS:7788/upload`
4. 同步间隔: 15 分钟
5. 在小米运动健康中开启 Health Connect 分享

## 命令

### 数据查询（走 LLM 管道，支持语音）
- `/body_status` - 查看监测状态（含体脂秤数据）
- `/body_baseline` - 查看基线统计
- `/body_body` - 查看体脂秤身体成分数据
- `/body_alerts` - 查看最近告警

> 以上命令会触发 LLM 生成自然语言回复，数据自动注入到 LLM 上下文中。如果配置了 RVC/TTS，会自动输出语音。

### 目标平台管理
- `/body_target_add here` - 将当前会话添加为健康事件目标
- `/body_target_add <UMO>` - 添加指定 UMO 为健康事件目标
- `/body_target_remove <UMO>` - 移除目标
- `/body_target_list` - 列出所有目标

### 测试
- `/body_test` - 联动关闭时直接发送测试关怀；联动开启时创建供 Private Companion 拉取的测试事件

## 主动关怀与 Private Companion 联动

插件会定时检查数据异常，触发条件：
1. **基线建立期**：前 7 天收集数据，不触发异常检测
2. **异常检测**：心率、睡眠评分、血氧等指标偏离基线 z-score 阈值时触发
3. **静默时段**：默认 23:00-08:00 不触发主动关怀或创建健康事件
4. **冷却时间**：同一指标有冷却时间，避免刷屏

### 联动关闭（默认）

Body Monitor 保留原有完整链路：调用配置的 LLM 与人格生成关怀文案、直接发送到目标会话，并把告警和生成文案写入本地数据库。`/body_test` 也保持直接发送测试消息。

### 联动开启

Body Monitor 不再调用主动文案 LLM 或直接发送，只写入结构化健康事件。事件包含发生时间、30 分钟有效期、规范指标上下文和发生时的目标快照。Private Companion 通过 `get_body_monitor_api().read_proactive_events(...)` 增量拉取，并负责目标校验、文案生成、审查和发送。

Body Monitor 的联动开关和 Private Companion 的联动开关都必须开启，事件才会进入主动候选链。Body Monitor 开关关闭时，事件接口只推进到最新游标并返回空事件，不会补发关闭期间的数据。

首次拉取只初始化到最新游标，不补发历史事件；正常运行中即使事件过期或格式不兼容，扫描游标仍会前进。

## 数据解析

插件支持 Health Connect Webhook 的多种数据格式：
- 单条记录: `{"type": "heart_rate", "value": 72}`
- 批量记录: `{"records": [...]}`
- 按类型分组: `{"heart_rate": [...], "steps": [...], "weight": [...]}`

## 体脂秤数据说明

- 体脂秤数据一天一次，不参与实时异常检测
- 联动关闭时可用于原生 LLM 关怀；联动开启时只投影为结构化事件的有限当日上下文
- 称重时同步测量的心率会参与实时异常检测

## 路由器配置

映射外部端口 `7788` 到 Unraid 的 `7788` 端口。

## 部署指南

- [Body Monitor 身体数据监测插件 – 通用部署指南](http://ludanhome.online:19192/?p=319)

## 注意事项

- 前 7 天为基线收集期，不会触发异常检测
- 静默时段（默认 23:00-08:00）不会主动关怀或创建健康事件
- 同一指标有冷却时间，避免刷屏
- 体脂秤数据需要先在小米运动健康中同步到 Health Connect
- 插件数据存储在 AstrBot 数据目录下，卸载/重装不会丢失历史数据
- 身体数据只会注入已配置目标发起的私聊健康查询；群聊、普通消息和 Private Companion 内部主动生成不会注入
