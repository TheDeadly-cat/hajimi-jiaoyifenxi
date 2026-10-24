"""Small HTTP-independent contract for the local news workspace."""
from __future__ import annotations

import re

from .news_content_quality import inspect_material


def news_response(runtime, path, *, query=None, payload=None, method="GET"):
    if runtime is None:
        return {"ok": False, "error": "消息服务暂未就绪，请查看启动状态后重试。"}, 503
    query = query or {}
    ai = getattr(runtime, "ai", None)
    startup = getattr(runtime, "startup", None)
    def overview():
        value = runtime.overview()
        if ai is not None:
            value["ai"] = ai.status()
        value["startup"] = startup.status() if startup is not None else {"supported": False, "enabled": False, "error": "登录恢复入口尚未配置。"}
        return value
    def reading(item):
        if item is None:
            return None
        return {**item, "material_quality": inspect_material(item)}
    def detail(item):
        item = reading(item)
        if item is not None and ai is not None:
            item["ai_analysis"] = ai.result(item)
        return item
    try:
        if method == "GET":
            allowed = {"q", "source", "category", "unread", "importance", "limit", "offset"} if path == "/api/news/items" else set()
            if set(query) - allowed or any(len(v) != 1 for v in query.values()):
                raise ValueError("不支持的查询参数")
            if path == "/api/news/overview":
                return {"ok": True, "overview": overview()}, 200
            if path == "/api/news/ai" and ai is not None:
                return {"ok": True, "ai": ai.status()}, 200
            if path == "/api/news/startup" and startup is not None:
                return {"ok": True, "startup": startup.status()}, 200
            if path == "/api/news/items":
                def value(key, default=""):
                    return query.get(key, [default])[0]
                if len(value("q")) > 200 or value("unread") not in {"", "0", "1"}:
                    raise ValueError("查询条件不正确")
                limit, offset = int(value("limit", "100")), int(value("offset", "0"))
                if not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
                    raise ValueError("分页范围不正确")
                result = runtime.list_items(query=value("q"), source=value("source"),
                    category=value("category"), unread_only=value("unread") == "1",
                    importance=value("importance"), limit=limit, offset=offset)
                return {"ok": True, **result, "items": [reading(item) for item in result["items"]]}, 200
            matched = re.fullmatch(r"/api/news/items/([a-zA-Z0-9_-]{1,128})", path)
            if matched:
                item = detail(runtime.get_item(matched.group(1)))
                return ({"ok": True, "item": item}, 200) if item else ({"ok": False, "error": "这条消息不存在。"}, 404)
        elif method == "POST":
            if query or type(payload) is not dict:
                raise ValueError("请求格式不正确")
            if path == "/api/news/startup" and startup is not None:
                if set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
                    raise ValueError("登录恢复设置格式不正确")
                return {"ok": True, "startup": startup.set_enabled(payload["enabled"])}, 200
            if path == "/api/news/ai/session" and ai is not None:
                required = {"api_key", "automatic", "acknowledge_budget"}
                optional = {"provider", "model"}
                if (not required.issubset(payload) or set(payload) - required - optional
                        or ("model" in payload and "provider" not in payload)
                        or any(key in payload and type(payload[key]) is not str for key in optional)):
                    raise ValueError("分析配置格式不正确")
                return {"ok": True, "ai": ai.configure(**payload)}, 200
            if path == "/api/news/ai/clear" and ai is not None:
                if payload:
                    raise ValueError("不接受额外参数")
                return {"ok": True, "ai": ai.clear()}, 200
            analyze_match = re.fullmatch(r"/api/news/items/([a-zA-Z0-9_-]{1,128})/analyze", path)
            if analyze_match and ai is not None:
                if payload:
                    raise ValueError("不接受额外参数")
                return {"ok": True, "ai_analysis": ai.request(analyze_match.group(1))}, 202
            if path == "/api/news/control":
                if set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
                    raise ValueError("请选择开始或暂停")
                runtime.control(payload["enabled"])
                return {"ok": True, "overview": overview()}, 200
            if path == "/api/news/refresh":
                if payload:
                    raise ValueError("立即检查不接受额外参数")
                runtime.refresh()
                return {"ok": True, "overview": overview()}, 200
            matched = re.fullmatch(r"/api/news/items/([a-zA-Z0-9_-]{1,128})/read", path)
            if matched:
                if set(payload) != {"read"} or type(payload["read"]) is not bool:
                    raise ValueError("已读状态不正确")
                item = detail(runtime.mark_read(matched.group(1), read=payload["read"]))
                return ({"ok": True, "item": item}, 200) if item else ({"ok": False, "error": "这条消息不存在。"}, 404)
        return {"ok": False, "error": "没有这个消息操作。"}, 404
    except (ValueError, TypeError):
        return {"ok": False, "error": "消息请求参数不正确，请刷新后重试。"}, 400
    except Exception:
        return {"ok": False, "error": "消息服务暂时无法完成操作，请稍后重试。"}, 503
