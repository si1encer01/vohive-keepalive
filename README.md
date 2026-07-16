# VoHive Keepalive / 保号模块

[简体中文](README.md) | [English](README_EN.md)

一个非官方的 VoHive 伴生服务，用于低频、可审计地启用蜂窝数据，为长期收取短信验证码的 SIM 卡执行周期性保号。

它会在预定时间短暂开启指定设备的数据连接，**强制通过指定蜂窝网卡**访问 HTTPS 地址，记录本次实际流量和成功时间，然后恢复到接短信所需的空闲状态。成功或失败均可通过 PushDeer 通知。

> 本项目只包含独立开发的保号服务和前端集成代码，不包含 VoHive 二进制、用户数据、密码、PushDeer Key、ICCID、手机号、服务器地址或其他部署机密。本项目与 VoHive 官方无隶属或背书关系。

## 功能

- **原生风格入口**：在 VoHive 左侧菜单增加“保号”，点击后在右侧内容区显示管理页面。
- **周期执行**：按天设置执行间隔；默认 120 天，首次启动不会立即产生蜂窝流量。
- **真实蜂窝验证**：使用 Linux `SO_BINDTODEVICE` 把 HTTPS 请求锁定到指定网卡，避免服务器宽带出口造成假成功。
- **双重流量记录**：分别记录整个数据会话和验证请求的 RX、TX、总字节数。
- **安全限额**：支持连接超时、请求超时、会话时长、单次流量和响应大小上限。
- **失败重试**：失败后按独立间隔重试，不会把失败误记成保号成功。
- **空闲策略**：任务结束后可恢复为蜂窝驻网接短信、VoWiFi 或飞行模式。
- **PushDeer 通知**：成功通知包含本次流量和下次时间；失败通知包含错误和重试时间。
- **执行历史**：SQLite 保存触发来源、HTTP 状态、耗时、流量、恢复结果和错误。
- **开机自启**：提供 systemd unit；进程重启时会把未完成任务标为失败并重新应用空闲策略。
- **可回滚集成**：Nginx 网关示例保留 VoHive 原访问地址，并提供一键回滚脚本。

## 页面集成

`integration/keepalive-nav.js` 会把“保号”菜单注入 VoHive 的侧栏。用户仍然访问原来的 VoHive 地址：

```text
VoHive 左侧菜单
├── 仪表盘
├── 设备管理
├── 短信中心
└── 保号  ← 新增
             ├── 服务状态 / 下次执行 / 上次成功
             ├── 策略配置
             └── 执行历史
```

同端口集成由 `integration/nginx-vohive-gateway.conf.example` 完成：Nginx 对外监听原端口，VoHive 改为仅作为后端监听另一个端口，保号 API 通过 `/keepalive-api/` 反向代理。

当前前端集成在 **VoHive 1.5.5** 上验证。VoHive 若调整侧栏 DOM，可能需要同步修改注入脚本中的选择器。

## 为什么默认 120 天

giffgaff 当前规则要求号码至少每 6 个月发生一次有效活动；一次移动数据联网属于有效活动，但只接收短信不属于列出的保号动作。默认 120 天给网络故障、余额问题和人工处理预留约两个月余量。

- [giffgaff：号码因不活跃被停用](https://help.giffgaff.com/en/articles/242797-understanding-why-your-number-has-been-deactivated)
- [giffgaff 服务条款](https://www.giffgaff.com/terms)

不同运营商规则可能不同，请按实际条款修改周期。

## 运行要求

- Linux 与 root 权限（`SO_BINDTODEVICE` 和网卡控制需要）
- Python 3.10 或更高版本，仅使用标准库
- 已正常运行并完成设备接入的 VoHive
- VoHive API 可从本机访问
- 蜂窝接口可在 `/sys/class/net/<interface>` 找到
- 可选：Nginx，用于原生侧栏和同端口集成
- 可选：PushDeer，用于执行结果推送

## 安装

以下示例使用 `/opt/vohive-keepalive`。请先阅读并按自己的 VoHive 端口、设备 ID 和蜂窝网卡修改配置。

```bash
sudo install -d -m 700 /opt/vohive-keepalive /etc/vohive-keepalive /var/lib/vohive-keepalive
sudo install -m 700 vohive_keepalive.py /opt/vohive-keepalive/
sudo install -m 600 config.example.json /etc/vohive-keepalive/config.json
sudo install -m 600 service.env.example /etc/vohive-keepalive/service.env
sudo install -m 644 vohive-keepalive.service /etc/systemd/system/

sudoedit /etc/vohive-keepalive/config.json
sudoedit /etc/vohive-keepalive/service.env

sudo systemctl daemon-reload
sudo systemctl enable --now vohive-keepalive.service
sudo systemctl status vohive-keepalive.service
```

至少需要填写：

- `device_id`：VoHive 中的设备 ID
- `interface`：蜂窝数据网卡，例如 `wwan0`
- `VOHIVE_BASE_URL`、`VOHIVE_USER`、`VOHIVE_PASSWORD`
- `BASIC_PASSWORD`：保号管理 API 的独立强密码
- 若使用通知，填写 `PUSHDEER_KEY`

服务默认只监听 `127.0.0.1:7582`。不要把未加 TLS 的 Basic Auth 接口直接暴露到公网。

## 原生侧栏集成

1. 备份 VoHive 配置。
2. 把 VoHive 的监听端口从对外端口改为后端端口，例如从 `7575` 改为 `17575`。
3. 安装注入脚本：

   ```bash
   sudo install -d -m 755 /opt/vohive-ui-gateway
   sudo install -m 644 integration/keepalive-nav.js /opt/vohive-ui-gateway/
   ```

4. 复制 Nginx 示例，生成保号 API 的 Basic 凭据，并替换 `__KEEPALIVE_BASIC_AUTH__`：

   ```bash
   printf '%s' 'YOUR_API_USER:YOUR_STRONG_PASSWORD' | base64
   sudo install -m 600 integration/nginx-vohive-gateway.conf.example \
     /etc/nginx/conf.d/vohive-gateway.conf
   sudoedit /etc/nginx/conf.d/vohive-gateway.conf
   ```

5. 检查并启动：

   ```bash
   sudo nginx -t
   sudo systemctl restart vohive.service vohive-keepalive.service nginx.service
   curl -fsS http://127.0.0.1:7575/keepalive-api/status
   ```

6. 确认原 VoHive 页面和 WebSocket 均正常，再保留新的网关配置。

示例端口只是默认参考。如果你的安装目录或配置格式不同，请同步修改 Nginx 和 `integration/rollback-gateway.sh`。回滚脚本假定部署前已保存：

```text
/opt/vohive/config/config.yaml.before-keepalive-gateway
```

## 环境变量

参考 [`service.env.example`](service.env.example)。真实环境文件应设为 `0600`，不得提交到 Git。

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `VOHIVE_BASE_URL` | VoHive API 根地址 | `http://127.0.0.1:7575/api` |
| `VOHIVE_USER` | VoHive 用户名 | `admin` |
| `VOHIVE_PASSWORD` | VoHive 密码 | 空 |
| `BASIC_USER` | 保号 API 用户名 | `admin` |
| `BASIC_PASSWORD` | 保号 API 密码 | 空 |
| `LISTEN_HOST` | 保号服务监听地址 | `127.0.0.1` |
| `LISTEN_PORT` | 保号服务端口 | `7582` |
| `CONFIG_PATH` | 配置文件 | `/etc/vohive-keepalive/config.json` |
| `DATABASE_PATH` | SQLite 数据库 | `/var/lib/vohive-keepalive/keepalive.db` |
| `PUSHDEER_KEY` | PushDeer PushKey | 空，不推送 |
| `PUSHDEER_ENDPOINT` | PushDeer API | 官方接口 |

## HTTP API

除 `/health` 外均使用 HTTP Basic Auth。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/` | 独立中文管理页 |
| `GET` | `/api/status` | 当前状态、上次成功、下次执行 |
| `GET` / `PUT` | `/api/config` | 读取或更新配置 |
| `GET` | `/api/history?limit=50` | 执行历史 |
| `POST` | `/api/run` | 立即执行，正文必须为 `{"confirm": true}` |

“立即执行”会真实开启蜂窝数据并可能产生漫游资费。

## 测试

```bash
python3 -m unittest -v test_keepalive.py
```

普通用户运行时，要求 root 的网卡绑定测试会跳过；在 Linux root 环境中会使用 loopback 完成真实 `SO_BINDTODEVICE` 测试，不会使用蜂窝数据。

## 安全提示

- 不要提交 `service.env`、真实配置、SQLite 数据库、日志或 Nginx 中生成后的 Authorization 值。
- 验证网址只接受 HTTPS 且禁止 URL 内嵌账号密码。
- `max_session_bytes` 是保护上限，不代表运营商最终计费字节数。
- 部署后先检查设备 ID、接口名和空闲策略；不要在资费昂贵的 SIM 上直接点击“立即保号”。
- 公开访问时应额外配置 TLS、访问控制和防火墙；推荐仅在内网使用。

## 许可证

[MIT](LICENSE)
