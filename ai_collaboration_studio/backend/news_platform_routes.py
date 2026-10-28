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
            if callable(getattr(ai, "market_context", None)):
                context = ai.market_context(item)
                item["market_context"] = context if isinstance(context, dict) else None
            if callable(getattr(ai, "market_result", None)):
                result = ai.market_result(item)
                item["market_impact"] = result if isinstance(result, dict) else None
        return item
    try:
        if method == "GET":
            allowed = {"q", "source", "category", "unread", "importance", "limit", "offset"} if path == "/api/news/items" else {"room_id"} if path == "/api/news/market/options" else set()
            if set(query) - allowed or any(len(v) != 1 for v in query.values()):
                raise ValueError("不支持的查询参数")
            if path == "/api/news/overview":
                return {"ok": True, "overview": overview()}, 200
            if path == "/api/news/ai" and ai is not None:
                return {"ok": True, "ai": ai.status()}, 200
            if path == "/api/news/startup" and startup is not None:
                return {"ok": True, "startup": startup.status()}, 200
            if path == "/api/news/market/options":
                room_id = query.get("room_id", [""])[0]
                if not isinstance(room_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", room_id):
                    raise ValueError("请选择资料房间")
                store = getattr(runtime, "research_store", None)
                if store is None:
                    return {"ok": False, "error": "当前运行方式尚未连接讨论房间资料，已有消息仍可阅读。"}, 503
                snapshot = store.room_snapshot(room_id)
                if not isinstance(snapshot, dict):
                    return {"ok": False, "error": "这个资料房间不存在。"}, 404
                latest = snapshot.get("latest_round") or {}
                checkpoint = store.get_round_checkpoint(room_id, latest["id"]) if latest.get("id") else None
                choices = []
                if (isinstance(checkpoint, dict) and checkpoint.get("protocol_integrity_ok") is True
                        and checkpoint.get("room_id") == room_id and checkpoint.get("round_id") == latest.get("id")):
                    manifest = (checkpoint.get("state") or {}).get("round_evidence_manifest") or {}
                    materials = []
                    for ref in manifest.get("materials", []):
                        if not isinstance(ref, dict) or not ref.get("id") or type(ref.get("version")) is not int:
                            continue
                        material = store.get_material_version(room_id, ref["id"], ref["version"])
                        if isinstance(material, dict):
                            materials.append({"id": ref["id"], "version": ref["version"], "title": str(material.get("title") or "未命名资料")[:120]})
                    choices.append({"id": latest["id"], "created_at": latest.get("created_at"), "materials": materials})
                return {"ok": True, "rounds": choices, "scope": "latest_saved_round", "message": "仅展示最近轮次的已保存证据版本；绑定时还会核对时间与完整性。"}, 200
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
            versions_match = re.fullmatch(r"/api/news/items/([a-zA-Z0-9_-]{1,128})/versions", path)
            if versions_match:
                if runtime.get_item(versions_match.group(1)) is None:
                    return {"ok": False, "error": "这条消息不存在。"}, 404
                versions = runtime.event_versions(versions_match.group(1))
                if ai is not None and callable(getattr(ai, "version_result", None)):
                    current = runtime.get_item(versions_match.group(1))
                    versions = [{**version, "ai_analysis": ai.version_result(current, version)} for version in versions]
                return {"ok": True, "versions": versions}, 200
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
            context_match = re.fullmatch(r"/api/news/items/([a-zA-Z0-9_-]{1,128})/market/context", path)
            if context_match and ai is not None:
                if set(payload) != {"room_id", "round_id", "material_refs"}:
                    raise ValueError("市场资料格式不正确")
                if not isinstance(payload["room_id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", payload["room_id"]):
                    raise ValueError("请选择资料房间")
                if payload["round_id"] is not None and (not isinstance(payload["round_id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", payload["round_id"])):
                    raise ValueError("请选择已保存轮次")
                refs = payload["material_refs"]
                if not isinstance(refs, list) or len(refs) > 12:
                    raise ValueError("资料数量不正确")
                for ref in refs:
                    if (type(ref) is not dict or set(ref) != {"material_id", "version"}
                            or type(ref["material_id"]) is not str or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", ref["material_id"])
                            or type(ref["version"]) is not int or ref["version"] < 1):
                        raise ValueError("资料版本不正确")
                context = ai.prepare_market_context(context_match.group(1), **payload)
                return {"ok": True, "market_context": context, "market_impact": None}, 200
            market_match = re.fullmatch(r"/api/news/items/([a-zA-Z0-9_-]{1,128})/market/analyze", path)
            if market_match and ai is not None:
                if set(payload) != {"context_id"} or type(payload["context_id"]) is not str or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", payload["context_id"]):
                    raise ValueError("请先绑定市场资料")
                return {"ok": True, "market_impact": ai.request_market(market_match.group(1), payload["context_id"])}, 202
            if path == "/api/news/control":
                if set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
                    raise ValueError("请选择开始或暂停")
                runtime.control(payload["enabled"])
                return {"ok": True, "overview": overview()}, 200
            if path == "/api/news/article-reading":
                if set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
                    raise ValueError("正文读取设置格式不正确")
                runtime.configure_article_reading(payload["enabled"])
                return {"ok": True, "overview": overview()}, 200
            if path == "/api/news/sec-contact":
                if set(payload) != {"user_agent"} or type(payload["user_agent"]) is not str:
                    raise ValueError("SEC 联系标识格式不正确")
                runtime.configure_sec_contact(payload["user_agent"])
                return {"ok": True, "overview": overview()}, 200
            if path == "/api/news/refresh":
                if payload:
                    raise ValueError("立即检查不接受额外参数")
                runtime.refresh()
                return {"ok": True, "overview": overview()}, 200
            source_match = re.fullmatch(r"/api/news/sources/([a-zA-Z0-9_-]{1,128})/control", path)
            if source_match:
                if set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
                    raise ValueError("来源设置格式不正确")
                runtime.configure_source(source_match.group(1), payload["enabled"])
                return {"ok": True, "overview": overview()}, 200
            evidence_match = re.fullmatch(r"/api/news/items/([a-zA-Z0-9_-]{1,128})/evidence", path)
            if evidence_match:
                if set(payload) != {"include_attachments"} or type(payload["include_attachments"]) is not bool:
                    raise ValueError("正文读取设置格式不正确")
                item = detail(runtime.read_article(evidence_match.group(1), include_attachments=payload["include_attachments"]))
                return ({"ok": True, "item": item}, 200) if item else ({"ok": False, "error": "这条消息不存在。"}, 404)
            relation_match = re.fullmatch(r"/api/news/items/([a-zA-Z0-9_-]{1,128})/event-relation", path)
            if relation_match:
                if set(payload) != {"origin_item_id", "acknowledge"} or payload["acknowledge"] is not True or type(payload["origin_item_id"]) is not str or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", payload["origin_item_id"]):
                    raise ValueError("请核对所选原稿并确认")
                from .news_event_actions import confirm_relation
                return {"ok": True, "item": detail(confirm_relation(runtime, relation_match.group(1), payload["origin_item_id"]))}, 200
            withdrawal_match = re.fullmatch(r"/api/news/items/([a-zA-Z0-9_-]{1,128})/withdrawal", path)
            if withdrawal_match:
                if (set(payload) != {"evidence_item_id", "quote", "acknowledge"} or payload["acknowledge"] is not True
                        or type(payload["evidence_item_id"]) is not str or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", payload["evidence_item_id"])
                        or type(payload["quote"]) is not str or not 1 <= len(payload["quote"]) <= 2000):
                    raise ValueError("请提供已保存撤回公告中的明确原句并确认")
                from .news_event_actions import confirm_withdrawal
                return {"ok": True, "item": detail(confirm_withdrawal(runtime, withdrawal_match.group(1), payload["evidence_item_id"], payload["quote"]))}, 200
            matched = re.fullmatch(r"/api/news/items/([a-zA-Z0-9_-]{1,128})/read", path)
            if matched:
                if set(payload) != {"read"} or type(payload["read"]) is not bool:
                    raise ValueError("已读状态不正确")
                item = detail(runtime.mark_read(matched.group(1), read=payload["read"]))
                return ({"ok": True, "item": item}, 200) if item else ({"ok": False, "error": "这条消息不存在。"}, 404)
        return {"ok": False, "error": "没有这个消息操作。"}, 404
    except (ValueError, TypeError) as error:
        reasons = {
            "market_checkpoint_unverified": "该轮次的证据记录尚未核验完整，暂不能用于市场分析。",
            "market_material_not_in_manifest": "所选资料版本未保存在该轮次中，请重新选择。",
            "market_context_missing_or_version_changed": "消息已有新版本，请重新绑定材料并核对发送范围。",
            "material_refs_require_saved_round": "请先选择已保存的研究轮次，再选择其中的资料。",
            "article_source_cooldown": "这个来源仍在等待期限内，请到允许时间后再读取。",
            "news_source_cooldown_active": "这个来源仍在等待期限内，请到允许时间后再读取。",
            "article_read_in_progress": "这条消息正在读取正文，请等待当前结果。",
            "sec_contact_required": "请先在来源设置中填写实际的应用名称与联系邮箱，再启用 SEC 来源。",
            "invalid_sec_contact": "请填写应用名称与有效联系邮箱，使用英文字符，不含换行。",
            "withdrawal_requires_same_publisher": "撤回依据必须来自当前消息的同一发布来源，请重新选择已保存公告。",
            "withdrawal_quote_not_supported": "所选原句未能与保存的明确撤回表述对应，请核对原文后再确认。",
            "different_original_required": "请选择另一条已保存的原始公告。",
        }
        if str(error) in reasons:
            return {"ok": False, "error": reasons[str(error)]}, 400
        return {"ok": False, "error": "消息请求参数不正确，请刷新后重试。"}, 400
    except Exception:
        return {"ok": False, "error": "消息服务暂时无法完成操作，请稍后重试。"}, 503
