# TK剪辑器 Python 接入

服务器：`https://tk.shaohua.fun`，产品编号：`TK_EDITOR`。

后台「Python 软件接入」页面提供当前服务器的公钥和可复制示例。把交付包里的 `tk_license.py` 放进 TK剪辑器项目，使用接入包预设的产品编号、公钥和机器码生成规则。

安装：`pip install -r sdk/requirements.txt`。

```python
from tk_license import LicenseClient, LicenseError

client = LicenseClient(
    server_url="https://tk.shaohua.fun",
    app_code="TK_EDITOR",
    public_key="从后台 Python 软件接入页面复制公钥",
    client_name="TK剪辑器",
)

try:
    info = client.verify(input("请输入卡密：").strip())
    client.start_heartbeat(on_error=lambda error: print(str(error)))
    print("到期时间：", info["expire_time"] or "永久")
    while 有待处理任务:
        client.require_valid()
        执行业务任务()
except LicenseError as error:
    print("授权失败：", error)
finally:
    client.close()
```

`verify()` 每次发起新的随机请求并校验服务端 Ed25519 签名、请求随机数、卡密摘要、产品编号、机器码、软件名称及响应时间。公钥应内置在发布软件中，不能在每次验证时无条件相信服务器返回的新公钥。

后台心跳每 60 秒执行一次真正的 `verify()`。服务端明确拒绝会立即使本地授权失效；网络中断时已获得的授权最多持续当前租期（默认 300 秒，且不超过卡密实际到期时间）。`require_valid()` 是业务操作前的必需检查，不会自动退出进程。长任务须在可中断的循环中再次检查。`on_error` 在后台线程运行；Qt/Tk 等 GUI 请通过信号/消息队列更新界面。

授权绑定设备数量，而非同时运行的软件进程数量。同一电脑重复运行 TK剪辑器只占一个名额。设备离线不会自动解绑；管理员在卡密详情里解绑。Windows 主要使用 MachineGuid，Linux 使用 machine-id，macOS 使用平台 UUID；重装系统可能需要解绑。设备码由客户端提交，无法阻止有意伪造客户端的攻击者。

自定义天数从第一次成功激活开始计时，升级客户端不会重置到期时间。禁用卡密会阻止 TK剪辑器授权验证；后台续期不会自动解除禁用状态。

不向客户端分发签名私钥、后台密码或 JWT 密钥。完全运行在本地的 Python 代码仍可被修改；高价值的服务端接口也需要独立验证授权。

问题反馈使用 `client.submit_feedback(app_version, description, logs_path=None, contact='', submission_id=None)`，详情见 [反馈接入说明](反馈接入说明.md)。验证响应新增的 `feedback_token` 由 SDK 自动使用；它只能提交反馈，不能登录后台。重试时复用原提交编号及 ZIP。
