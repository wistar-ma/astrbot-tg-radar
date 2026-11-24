# TG福利雷达 tg_radar

用机主自己的 Telegram 账号会话（专用 session）实时监听其所有群/频道消息，
关键词规则 + AstrBot 大模型复核命中免费福利后，把原文和链接推送到绑定会话
（微信 /radar_bind 绑定，或自动读取 push_bridge 的 wx/tg 绑定）。

登录：插件配置填 api_id / api_hash / page_token 后，
POST /api/v1/plugins/extensions/tg_radar/enroll?t=<page_token> 启动扫码，
打开 GET /api/v1/plugins/extensions/tg_radar/qr?t=<page_token> 扫码登录。
会话保存在插件数据目录，重启自动恢复监听。
