# -*- coding: utf-8 -*-
"""tg_radar: 用用户自己的 Telegram 账号会话实时监听所有群/频道，
命中免费福利后经 AstrBot 推送到绑定的会话（微信/企业微信等）。"""
import asyncio
import hashlib
import io
import json
import os
import re
import time

try:
    from astrbot.api.star import Context, Star, register
    from astrbot.api.event import filter, AstrMessageEvent
    from astrbot.api.message_components import Plain
    from astrbot.core.message.message_event_result import MessageChain
    from astrbot.api.web import request as web_request, json_response, error_response
    try:
        from astrbot.api.star import StarTools
    except Exception:
        StarTools = None
except Exception:
    from astrbot.api.star import Context, Star, register  # type: ignore
    from astrbot.api.event import filter, AstrMessageEvent  # type: ignore
    from astrbot.api.message_components import Plain  # type: ignore
    from astrbot.core.message.message_event_result import MessageChain  # type: ignore
    from astrbot.api.web import request as web_request, json_response, error_response  # type: ignore
    StarTools = None

STRONG = ["免费领取", "限时免费", "免费送", "免费领", "抽奖", "赠送", "白嫖", "0元",
          "免费获得", "免费开通", "免费试用", "免费会员", "免费游戏", "喜加一",
          "giveaway", "free game", "free trial", "airdrop"]
WEAK = ["免费", "福利", "优惠", "折扣", "促销", "活动", "领取", "秒杀", "特价", "free"]
EXCLUDE = ["求购", "收购", "出售", "转让", "出一个", "卖号", "招聘", "广告"]
# 黄赌毒硬过滤（用户 2026-11-25 定版：黄赌毒直接忽略）：命中任一词直接忽略——
# 不调 AI 评分、不尝试领取、不推送，只记一条 score=0 的 dropped 记录供审查计数。
BLOCKED = ["博彩", "赌博", "赌场", "网赌", "赌球", "娱乐城", "彩金", "体验金",
           "棋牌", "百家乐", "老虎机", "六合彩", "时时彩", "下注", "盘口",
           "首充", "洗码", "银河国际",
           "约炮", "外围女", "外围模特", "探花", "裸聊", "情色", "黄片",
           "卖淫", "嫖娼", "楼凤",
           "冰毒", "大麻", "麻古", "摇头丸", "海洛因", "可卡因", "毒品", "吸毒"]


@register("tg_radar", "wistar", "TG福利雷达：实时监听TG群消息，命中免费福利推送到微信", "1.2.2")
class TgRadar(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        if not self.config:
            try:
                p = os.path.join("data", "config", "tg_radar_config.json")
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8-sig") as f:
                        self.config = json.load(f)
            except Exception:
                self.config = {}
        self.data_dir = self._data_dir()
        os.makedirs(self.data_dir, exist_ok=True)
        self.session_path = os.path.join(self.data_dir, "tg_listen")
        self.state_path = os.path.join(self.data_dir, "state.json")
        self.state = self._load_state()
        self.client = None
        self._listener_task = None
        self._enroll_task = None
        self._qr_png_b64 = ""
        self._qr_url = ""
        self._enroll_status = "idle"
        try:
            self.context.register_web_api("/tg_radar/qr", self.api_qr_page, ["GET"], "tg radar qr page")
            self.context.register_web_api("/tg_radar/qrpage", self.api_qr_html, ["GET"], "tg radar qr html page")
            self.context.register_web_api("/tg_radar/enroll", self.api_enroll, ["POST", "GET"], "tg radar enroll")
            self.context.register_web_api("/tg_radar/status", self.api_status, ["GET"], "tg radar status")
        except Exception as e:
            print(f"[tg_radar] register_web_api failed: {e}", flush=True)
        if os.path.exists(self.session_path + ".session"):
            self._start_listener()

    # ---------- helpers ----------
    def _data_dir(self):
        if StarTools is not None:
            try:
                return str(StarTools.get_data_dir())
            except Exception:
                pass
        return os.path.join("data", "plugin_data", "tg_radar")

    def _cfg(self, key, default=""):
        v = self.config.get(key) if isinstance(self.config, dict) else None
        return default if v is None else v

    def _load_state(self):
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {"alert_origin": "", "seen": [], "user": "", "hits": 0}

    def _save_state(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
        except Exception as e:
            print(f"[tg_radar] save state failed: {e}", flush=True)

    def _api_creds(self):
        try:
            api_id = int(str(self._cfg("api_id", "0")).strip() or 0)
        except Exception:
            api_id = 0
        return api_id, str(self._cfg("api_hash", "")).strip()

    def _rules_score(self, text):
        t = text.lower()
        score = 0
        hits = []
        for w in STRONG:
            if w.lower() in t:
                score += 2
                hits.append(w)
        for w in WEAK:
            if w.lower() in t:
                score += 1
                hits.append(w)
        for w in EXCLUDE:
            if w in text:
                score -= 3
                hits.append("排除:" + w)
        if "http://" in t or "https://" in t or "t.me/" in t:
            score += 1
            hits.append("带链接")
        if re.search(r"(截止|限时|过期|结束|deadline|ends)", t):
            score += 1
            hits.append("带时限")
        return score, hits

    def _blocked_word(self, text):
        for w in BLOCKED:
            if w in text:
                return w
        return ""

    def _desc(self, reason, text):
        """推送里给用户看的一句话说明（参考老福利程序的 💡 一行式）：
        AI 给了真实说明就用它；AI 未返回/失败时用原文摘录，让用户一眼看懂是什么。"""
        bad = ("AI 未返回", "解析失败", "评分失败", "无可用模型", "老雷达")
        if reason and not any(b in reason for b in bad):
            return reason
        return re.sub(r"\s+", " ", text).strip()[:90]

    def _freeradar_score(self, text):
        """freebie-radar pushes carry their own 0-100 score in a fixed format:
        header '🎁 真福利精选 · [真福利直推] 高分 85' and per-item '... · 85 分'.
        Returns the best score found, or None when the format does not match."""
        if "真福利精选" not in text:
            return None
        scores = []
        for m in re.finditer(r"高分\s*(\d{1,3})", text):
            scores.append(int(m.group(1)))
        for m in re.finditer(r"·\s*(\d{1,3})\s*分", text):
            scores.append(int(m.group(1)))
        if not scores:
            return None
        return max(0, min(100, max(scores)))

    async def _ai_score(self, text):
        """Score 0-100: is this a real, claimable free benefit for a mainland-China user?
        Returns (score:int|None, reason:str)."""
        try:
            llm = getattr(self.context, "llm_generate", None)
            if llm is None:
                return None, "无可用模型"
            prompt = (
                "给下面这条 Telegram 群消息打分（0-100），判断它是不是一个真实、可领取、且中国大陆用户能用的免费福利。\n"
                "高分（70+）：明确的免费游戏/免费服务/免费 VPS/免费额度/抽奖/赠送，有清楚的领取入口。\n"
                "低分（30 以下）：只是提问、讨论、晒图、付费广告、需要美国运营商/美国地址/美国支付的福利、地区锁定无法参与的活动。\n"
                "只回答 JSON：{\"score\": 数字, \"reason\": \"一句话说明是什么福利\"}\n\n消息：\n" + text[:1200]
            )
            resp = await llm(prompt=prompt)
            content = getattr(resp, "completion_text", None) or str(resp)
            m = re.search(r"\{.*\}", content, re.S)
            if m:
                data = json.loads(m.group(0))
                return int(data.get("score", 0)), str(data.get("reason", ""))[:120]
            return None, "解析失败"
        except Exception as e:
            print(f"[tg_radar] ai score failed: {e}", flush=True)
            return None, f"评分失败：{e}"

    def _alert_targets(self):
        targets = []
        origin = self.state.get("alert_origin", "")
        if origin:
            targets.append(origin)
        try:
            pb = os.path.join(os.path.dirname(self.data_dir), "push_bridge", "bindings.json")
            if os.path.exists(pb):
                with open(pb, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for name in ("wx", "wechat", "tg"):
                    item = data.get(name)
                    if item and item.get("umo") and item["umo"] not in targets:
                        targets.append(item["umo"])
        except Exception:
            pass
        return targets

    async def _alert(self, text):
        targets = self._alert_targets()
        if not targets:
            print("[tg_radar] hit but no alert target bound yet", flush=True)
            return False
        ok = False
        for umo in targets:
            try:
                await self.context.send_message(umo, MessageChain().message(text))
                ok = True
            except Exception as e:
                print(f"[tg_radar] send to {umo} failed: {e}", flush=True)
        return ok


    def _err(self, msg, status=400):
        try:
            return error_response(msg, status=status)
        except TypeError:
            return error_response(msg)


    async def _try_claim(self, event):
        """If the hit message carries a participation/claim callback button,
        click it with the listener account and report what happened."""
        CLAIM_WORDS = ["参与", "抽奖", "领取", "报名", "参加", "加入", "抽", "enter", "join", "claim"]
        try:
            msg = event.message
            rows = getattr(msg, "buttons", None) or []
            for r, row in enumerate(rows):
                for c, btn in enumerate(row):
                    if getattr(btn, "url", None):
                        continue  # link buttons are for the owner to open
                    text = (getattr(btn, "text", "") or "").strip()
                    if not text:
                        continue
                    low = text.lower()
                    if any(w in low for w in CLAIM_WORDS):
                        res = await msg.click(r, c)
                        answer = ""
                        try:
                            answer = (getattr(res, "message", "") or "") if res is not None else ""
                        except Exception:
                            answer = ""
                        return f"已自动点击「{text}」" + (f"：{answer}" if answer else "（无即时回执）")
            return "仅通知（无可自动点的参与按钮，需本人处理）"
        except Exception as e:
            return f"自动领取失败：{e}"

    # ---------- listener ----------
    def _start_listener(self):
        if self._listener_task and not self._listener_task.done():
            return
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()
            self._listener_task = loop.create_task(self._listener_loop())
        except Exception as e:
            print(f"[tg_radar] start listener failed: {e}", flush=True)

    async def terminate(self):
        try:
            if self.client:
                await self.client.disconnect()
        except Exception:
            pass
        for t in (self._listener_task, self._enroll_task):
            try:
                if t and not t.done():
                    t.cancel()
            except Exception:
                pass

    async def _listener_loop(self):
        from telethon import TelegramClient, events
        api_id, api_hash = self._api_creds()
        if not api_id or not api_hash:
            print("[tg_radar] api_id/api_hash not configured", flush=True)
            return
        self._enroll_status = "starting"
        while True:
            try:
                client = TelegramClient(self.session_path, api_id, api_hash)
                self.client = client
                await client.connect()
                if not await client.is_user_authorized():
                    print("[tg_radar] session not authorized; enroll needed", flush=True)
                    self._enroll_status = "need_enroll"
                    return
                me = await client.get_me()
                self.state["user"] = f"@{me.username}" if me.username else str(me.id)
                self._save_state()
                self._enroll_status = "listening"
                print(f"[tg_radar] listening as {self.state['user']}", flush=True)

                @client.on(events.NewMessage(incoming=True))
                async def handler(event):
                    try:
                        if not (event.is_group or event.is_channel):
                            return
                        text = (event.raw_text or "").strip()
                        if len(text) < 8:
                            return
                        key = f"{event.chat_id}:{event.id}"
                        thash = hashlib.md5(text.encode("utf-8")).hexdigest()
                        seen = self.state.setdefault("seen", [])
                        if key in seen or thash in seen:
                            return
                        seen.append(key)
                        seen.append(thash)
                        if len(seen) > 800:
                            self.state["seen"] = seen[-800:]
                        self._save_state()
                        rules_score, hits = self._rules_score(text)
                        chat = await event.get_chat()
                        title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)
                        username = getattr(chat, "username", None)
                        if username:
                            link = f"https://t.me/{username}/{event.id}"
                        else:
                            cid = str(event.chat_id)
                            if cid.startswith("-100"):
                                cid = cid[4:]
                            link = f"https://t.me/c/{cid}/{event.id}"
                        blocked = self._blocked_word(text)
                        if blocked:
                            self.state["hits"] = int(self.state.get("hits", 0)) + 1
                            hist = self.state.setdefault("recent_hits", [])
                            hist.append({"ts": int(time.time()), "chat": title, "link": link,
                                         "score": 0, "rules": rules_score,
                                         "reason": f"黄赌毒硬过滤（命中「{blocked}」），直接忽略",
                                         "src": "",
                                         "verdict": "dropped", "action": "黄赌毒硬过滤（未处理）",
                                         "text": text[:300]})
                            self.state["recent_hits"] = hist[-20:]
                            self._save_state()
                            return
                        src = ""
                        fr_score = self._freeradar_score(text)
                        if fr_score is not None:
                            sender = await event.get_sender()
                            if not sender or getattr(sender, "username", None) != "treemail_bot":
                                fr_score = None
                        if fr_score is not None:
                            # Merged single entry point: freebie-radar is now just a
                            # source feeder; trust the score it already computed.
                            ai_score, reason, src = fr_score, "老雷达（freebie-radar）评分", "freebie-radar"
                        else:
                            min_score = int(self._cfg("min_score", 3) or 3)
                            if rules_score < min_score:
                                return
                            ai_score, reason = await self._ai_score(text)
                            if ai_score is None:
                                ai_score = 70 if rules_score >= 5 else (60 if rules_score == 4 else 50)
                                reason = "AI 未返回，规则估分"
                        threshold = int(self._cfg("claim_threshold", 65) or 65)
                        if ai_score < threshold:
                            verdict, action, alert = "dropped", "低分忽略（未处理）", None
                        else:
                            action = await self._try_claim(event)
                            verdict = "claimed" if action.startswith("已自动点击") else "manual_needed"
                            desc = self._desc(reason, text)
                            if verdict == "claimed":
                                alert = (
                                    "✅ 福利已领取\n"
                                    f"群：{title}\n"
                                    f"评分：{ai_score}分\n"
                                    f"💡 {desc}\n"
                                    f"处理：{action}\n"
                                    f"链接：{link}\n"
                                )
                            else:
                                alert = (
                                    "⚠️ 福利需要你手动领取（重点提醒）\n"
                                    f"群：{title}\n"
                                    f"评分：{ai_score}分\n"
                                    f"💡 {desc}\n"
                                    f"原因：{action}\n"
                                    f"链接：{link}\n"
                                )
                        self.state["hits"] = int(self.state.get("hits", 0)) + 1
                        hist = self.state.setdefault("recent_hits", [])
                        hist.append({"ts": int(time.time()), "chat": title, "link": link,
                                     "score": ai_score, "rules": rules_score, "reason": reason,
                                     "src": src,
                                     "verdict": verdict, "action": action, "text": text[:300]})
                        self.state["recent_hits"] = hist[-20:]
                        self._save_state()
                        if alert:
                            await self._alert(alert)
                    except Exception as e:
                        print(f"[tg_radar] handler error: {e}", flush=True)

                await client.run_until_disconnected()
            except Exception as e:
                print(f"[tg_radar] listener error: {e}; retry in 15s", flush=True)
                await asyncio.sleep(15)

    # ---------- enrollment (QR login) ----------
    async def _enroll_loop(self):
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
        import qrcode
        api_id, api_hash = self._api_creds()
        if not api_id or not api_hash:
            self._enroll_status = "no_api_config"
            return
        self._enroll_status = "starting"
        client = TelegramClient(self.session_path, api_id, api_hash)
        self.client = client
        await client.connect()
        if await client.is_user_authorized():
            self._enroll_status = "already"
            self._start_listener()
            return
        try:
            while True:
                qr = await client.qr_login()
                self._qr_url = qr.url
                img = qrcode.make(qr.url)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                import base64
                self._qr_png_b64 = base64.b64encode(buf.getvalue()).decode()
                self._enroll_status = "waiting_scan"
                try:
                    await qr.wait(timeout=28)
                    me = await client.get_me()
                    self.state["user"] = f"@{me.username}" if me and me.username else ""
                    self._save_state()
                    self._enroll_status = "login_ok"
                    print(f"[tg_radar] enroll ok: {self.state['user']}", flush=True)
                    break
                except SessionPasswordNeededError:
                    self._enroll_status = "need_2fa_password"
                    return
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    if "expired" in str(e).lower() or "AuthTokenExpired" in type(e).__name__:
                        continue
                    self._enroll_status = f"error:{e}"
                    return
        finally:
            pass
        self._start_listener()

    # ---------- web APIs ----------
    async def _req_args(self):
        args = {}
        req = web_request
        try:
            from quart import request as qreq
            req = qreq
        except Exception:
            pass
        try:
            a = req.args or {}
            for k in a.keys():
                args[k] = a.get(k)
        except Exception:
            pass
        return args

    def _check_token(self, args):
        token = str(self._cfg("page_token", "")).strip()
        return token and args.get("t") == token

    async def api_qr_page(self):
        args = await self._req_args()
        if not self._check_token(args):
            return self._err("forbidden", status=403)
        return json_response({
            "ok": True,
            "enroll_status": self._enroll_status,
            "user": self.state.get("user", ""),
            "qr_png_b64": self._qr_png_b64,
            "qr_url": self._qr_url,
        })

    async def api_enroll(self):
        args = await self._req_args()
        if not self._check_token(args):
            return self._err("forbidden", status=403)
        if not self._enroll_task or self._enroll_task.done():
            try:
                loop = asyncio.get_event_loop()
                self._enroll_task = loop.create_task(self._enroll_loop())
            except Exception as e:
                return self._err(str(e), status=500)
        return json_response({"ok": True, "status": self._enroll_status})

    async def api_status(self):
        return json_response({
            "ok": True,
            "enroll_status": self._enroll_status,
            "user": self.state.get("user", ""),
            "alert_bound": bool(self.state.get("alert_origin")),
            "hits": self.state.get("hits", 0),
            "session_exists": os.path.exists(self.session_path + ".session"),
            "recent_hits": self.state.get("recent_hits", []),
        })

    async def api_qr_html(self):
        args = await self._req_args()
        if not self._check_token(args):
            return self._err("forbidden", status=403)
        from quart import Response as QuartResponse
        if self._enroll_status == "login_ok" or self.state.get("user"):
            body = ("<html><head><meta charset='utf-8'></head><body style='font-family:sans-serif;"
                    "text-align:center;padding:40px'><h2>已登录：" + (self.state.get("user") or "") +
                    "</h2><p>监听已启动，可关闭本页。</p></body></html>")
            return QuartResponse(body, status=200, content_type="text/html; charset=utf-8")
        img = ""
        if self._qr_png_b64:
            img = f"<img src='data:image/png;base64,{self._qr_png_b64}' style='width:300px;height:300px'/>"
        body = (
            "<html><head><meta charset='utf-8'><meta http-equiv='refresh' content='3'>"
            "<title>TG监听登录</title></head>"
            "<body style='font-family:sans-serif;text-align:center;padding:24px'>"
            "<h2>Telegram 监听会话登录（138号）</h2>"
            "<p>用 Telegram 扫码：设置 → 设备 → 连接桌面设备</p>"
            + img +
            f"<p>状态：{self._enroll_status}（页面每3秒自动刷新，二维码始终最新）</p>"
            "</body></html>"
        )
        return QuartResponse(body, status=200, content_type="text/html; charset=utf-8")

    # ---------- chat commands ----------
    @filter.command("radar_bind")
    async def radar_bind(self, event: AstrMessageEvent):
        self.state["alert_origin"] = event.unified_msg_origin
        self._save_state()
        yield event.plain_result("已绑定：TG 福利命中会推送到这个会话。用 /radar_status 看状态，/radar_test 发测试推送。")

    @filter.command("radar_status")
    async def radar_status(self, event: AstrMessageEvent):
        yield event.plain_result(
            f"TG雷达状态：登录状态 {self._enroll_status}，账号 {self.state.get('user') or '未登录'}，"
            f"累计命中 {self.state.get('hits', 0)} 条，本会话推送绑定 {'已绑定' if self.state.get('alert_origin') else '未绑定'}。"
        )

    @filter.command("radar_test")
    async def radar_test(self, event: AstrMessageEvent):
        ok = await self._alert("✅ 福利已领取（测试）\n群：测试\n评分：80分\n福利：测试推送\n处理：测试通过\n")
        yield event.plain_result("测试推送已发送到绑定目标。" if ok else "还没有可用的推送目标：请先在想接收的会话发 /radar_bind，或在目标会话用推送桥 /push_bind wx 绑定。")
