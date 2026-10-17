# TK剪辑器授权管理

基于 [2574533879/key-manager](https://github.com/2574533879/key-manager) 的 MIT fork，用于 TK剪辑器的 Python 客户端卡密验证。管理后台位于 https://tk.shaohua.fun ，客户端使用独立的签名验证接口。

## 已完成的改造

- 自动建立「TK剪辑器」，产品编号 `TK_EDITOR`；一个应用管理卡密、到期时间和设备名额。
- 自定义卡从首次激活计时；续期保留卡号，已过期卡从续期时重新计算，禁用状态保持。
- SQLite WAL、写事务和设备唯一索引避免并发激活超额。
- Ed25519 签名响应，绑定随机数、卡密摘要、产品、机器码及软件名称，短期授权默认 300 秒。
- 统一 Python SDK，真正的联网复验心跳和单调时钟授权期限；旧版无签名接口默认关闭。
- 使用安全随机数生成卡密，加入输入校验、两级限流、生产配置检查。
- 管理密码使用 Argon2，兼容原 bcrypt；区分 access/refresh token，修改密码会撤销旧令牌。
- 后台增加续期和 Python 接入页面，修正在线筛选分页与分页数量。
- 更新并锁定依赖，增加自动化测试、systemd 服务、Nginx HTTPS 配置及每日备份模板。

## Python 接入

见 [SDK_GUIDE.md](SDK_GUIDE.md)。后台「Python 软件接入」提供本服务器公钥及示例。当前唯一应用为 TK剪辑器，`app_code` 为 `TK_EDITOR`。旧接入编号仍可兼容已有客户端。

## 开发与测试

Python 3.11 / 3.12。安装 `requirements.txt` 后运行：

```sh
pip install -r requirements.txt pytest 'httpx>=0.28,<1' requests
python -m pytest -q
python build_css.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

开发模式缺省管理员沿用上游设置；公网部署必须设置 `ENVIRONMENT=production`、随机管理员密码及 JWT 密钥。`.env.example` 只作为配置格式参考。生产服务使用单个 Uvicorn worker；当前进程内限流不会跨 worker 共享。

## 服务器运维

部署模板在 [deploy/](deploy/)。当前服务器按独立服务部署：

- 程序 `/opt/shaohua-license/current`，虚拟环境 `/opt/shaohua-license/venv`。
- 配置 `/etc/shaohua-license.env`（root 可读），数据与签名私钥 `/var/lib/shaohua-license/`。
- 服务 `shaohua-license.service`，仅监听 `127.0.0.1:8810`，由域名 Nginx 转发。
- `GET /healthz` 提供健康检查；正式 API 为 `POST /api/client/verify`。
- `shaohua-license-cert-renew.timer` 每日两次检查证书续期；续期后自动检查并重载现有 Nginx。
- 每日北京时间 02:35 左右备份数据库、配置与签名私钥到 `/var/backups/shaohua-license/`，保留 14 天。备份仍在同一服务器，建议另行复制至你控制的存储。

常用命令：

```sh
systemctl status shaohua-license
journalctl -u shaohua-license -n 100 --no-pager
systemctl restart shaohua-license
systemctl start shaohua-license-backup
systemctl list-timers shaohua-license-backup.timer
```

升级前手动备份并保存当前 release。更新程序后重启服务、检查 `/healthz`。恢复时停止服务，将数据库和签名私钥一起恢复，保证 `shaohua-license` 用户可读写，然后重新启动。签名私钥变化后，旧软件内置的公钥将无法验证新响应，必须妥善保留原私钥。

## 授权边界

同一机器重复运行客户端共用设备名额；离线不自动释放设备。服务端拒绝会在下一次成功联网复验时生效；断网最多保留当前短期租期。客户端本地代码和机器码均可被修改，签名协议不能保证软件无法破解。

保留上游 MIT [LICENSE](LICENSE)。
