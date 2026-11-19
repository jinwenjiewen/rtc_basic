"""Model-authored partial context edits; no model calls and no coverage changes."""

from copy import deepcopy
import hashlib
import json
import time

from .context import ContextError, EditPlan, SUMMARY_KEYS, _HIDDEN, _REF, _integer, _json, _refs
from .store import ContextConflict


def edit_schemas() -> list[dict]:
    reference = {"type": "string", "pattern": "^seq:[1-9][0-9]*$"}
    refs = {"type": "array", "items": reference, "maxItems": 64}
    entry = {"type": "object", "properties": {
        "section": {"type": "string", "enum": list(SUMMARY_KEYS)}, "id": {"type": "string", "minLength": 1, "maxLength": 80},
        "kind": {"type": "string", "maxLength": 80}, "text": {"type": "string", "maxLength": 2000}, "value": {},
        "topic": {"type": "string", "maxLength": 200},
        "source_type": {"type": "string", "enum": ["user_statement", "tool_evidence", "assistant_claim", "inference"]},
        "source_refs": {**refs, "minItems": 1, "maxItems": 32},
        "status": {"type": "string", "enum": ["active", "pending", "resolved", "superseded", "uncertain"]},
        "supersedes": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
        "representation": {"type": "string", "enum": ["fact", "pointer", "verbatim"]},
        "retain_for": {"type": "string", "maxLength": 120}},
        "required": ["section", "id", "kind", "source_type", "source_refs", "status"],
        "anyOf": [{"required": ["text"]}, {"required": ["value"]}, {"required": ["topic"]}], "additionalProperties": False}
    version = {"type": "integer", "minimum": 0}

    def tool(name, description, properties):
        return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}}
    return [tool("context_update", "按条目更新工作摘要，不压缩历史。使用当前context_version和可见seq:N。保留未修改条目；修正用supersedes指向已有id，旧条目标为superseded。无id旧条目使用summary_entry_ids。事实取舍由你决定，勿写隐藏推理或把待确认动作写成完成。", {
        "expected_context_version": version, "upserts": {"type": "array", "items": entry, "minItems": 1, "maxItems": 16}}),
        tool("context_select", "调整后续工作视图加载的原始证据。keep_refs新增保留，release_refs仅取消显式保留，不删除账本或受保护近期原文。新增来源必须已在本次输入或真实历史工具中可见；先读后选。使用当前context_version，purpose简写用途。", {
            "expected_context_version": version,
            "keep_refs": {**refs, "description": "新增保留的可见seq:N；只释放时填[]。"},
            "release_refs": {**refs, "description": "取消保留的seq:N，取自当前selected_refs；keep_refs=[]不会代替此操作。"},
            "purpose": {"type": "string", "maxLength": 120}})]


def prepare_edit(engine, target, user_text, system, tools, current_chain, *, request_fields=None, view=None):
    fields, chain = dict(request_fields or {}), list(current_chain)
    before = view or engine.build(target, user_text, system, tools, chain, request_fields=fields)
    visible = {ref for _, ref in before.metrics["history_sources"]} | set(before.metrics["current_input_refs"])
    visible |= _refs(before.context["summary"]) | set(before.context["selected_refs"]) | engine._chain_visible_refs(target, chain)
    frozen = {"expected_context_version": before.context["context_version"],
        "expected_notes_version": before.notes["version"], "ledger_through_seq": before.material["ledger_through_seq"],
        "material_fingerprint": before.material["material_fingerprint"], "owner_user_ref": engine.store.user_reference(target, user_text),
        "visible_refs": sorted(visible, key=lambda ref: int(ref[4:])), "prompt_version": "m2-edit-v1",
        "input_fingerprint": hashlib.sha256(_json({"messages": before.messages, "tools": tools, "fields": fields}).encode()).hexdigest()}
    return EditPlan(target, before, frozen, user_text, system, deepcopy(tools), deepcopy(chain), fields)


def _references(value, maximum=64):
    if (not isinstance(value, list) or len(value) > maximum
        or any(not isinstance(ref, str) or not _REF.fullmatch(ref) for ref in value) or len(set(value)) != len(value)):
        raise ContextError("invalid_context_reference_list")
    return value


def _no_hidden(value, depth=0):
    if depth > 32:
        raise ContextError("invalid_context_value")
    if isinstance(value, dict):
        if set(value) & _HIDDEN:
            raise ContextError("hidden_reasoning_in_context")
        for child in value.values():
            _no_hidden(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _no_hidden(child, depth + 1)


def _update(engine, target, summary, upserts, visible):
    if not isinstance(upserts, list) or not 1 <= len(upserts) <= 16:
        raise ContextError("invalid_context_upserts")
    locations = {}
    for section in SUMMARY_KEYS:
        for index, item in enumerate(summary[section]):
            identifier = item.get("id", f"legacy:{section}:{index}")
            if not isinstance(identifier, str) or identifier in locations:
                raise ContextError("context_ambiguous_item_id")
            locations[identifier] = (section, index)
    original_ids, batch_ids = set(locations), set()
    for entry in upserts:
        if not isinstance(entry, dict):
            raise ContextError("invalid_context_upsert")
        allowed = {"section", "id", "kind", "text", "value", "topic", "source_type", "source_refs", "status", "supersedes", "representation", "retain_for"}
        required = {"section", "id", "kind", "source_type", "source_refs", "status"}
        if set(entry) - allowed or not required <= set(entry) or not {"text", "value", "topic"} & set(entry):
            raise ContextError("invalid_context_upsert")
        _no_hidden(entry)
        section, identifier = entry["section"], entry["id"]
        if (not isinstance(section, str) or section not in SUMMARY_KEYS
            or not isinstance(identifier, str) or not 1 <= len(identifier) <= 80 or any(ord(c) < 32 for c in identifier)
            or identifier in batch_ids or not isinstance(entry["kind"], str) or not 1 <= len(entry["kind"]) <= 80):
            raise ContextError("invalid_context_upsert")
        batch_ids.add(identifier)
        if entry["source_type"] not in ("user_statement", "tool_evidence", "assistant_claim", "inference") or entry["status"] not in ("active", "pending", "resolved", "superseded", "uncertain"):
            raise ContextError("invalid_context_upsert")
        if "representation" in entry and entry["representation"] not in ("fact", "pointer", "verbatim"):
            raise ContextError("invalid_context_upsert")
        for key, maximum in (("text", 2000), ("topic", 200), ("retain_for", 120)):
            if key in entry and (not isinstance(entry[key], str) or not 1 <= len(entry[key]) <= maximum):
                raise ContextError("invalid_context_upsert")
        refs = _references(entry["source_refs"], 32)
        if not refs or not set(refs) <= visible:
            raise ContextError("context_reference_not_visible")
        roles = {engine.store.read_message(target.session_id, int(ref[4:]), max_chars=1)["role"] for ref in refs}
        role = {"user_statement": "user", "tool_evidence": "tool", "assistant_claim": "assistant"}.get(entry["source_type"])
        if role and role not in roles:
            raise ContextError("context_source_type_mismatch")
        replaced = entry.get("supersedes", [])
        if (not isinstance(replaced, list) or len(replaced) > 16 or any(not isinstance(item, str) for item in replaced)
            or len(set(replaced)) != len(replaced) or identifier in replaced or not set(replaced) <= original_ids):
            raise ContextError("invalid_context_supersedes")
        for old_id in replaced:
            old_section, index = locations[old_id]
            summary[old_section][index].update({"id": old_id, "status": "superseded"})
        value = {key: deepcopy(child) for key, child in entry.items() if key != "section"}
        if identifier in locations:
            old_section, index = locations[identifier]
            if old_section != section:
                raise ContextError("context_item_section_conflict")
            summary[section][index] = value
        else:
            locations[identifier] = (section, len(summary[section]))
            summary[section].append(value)
        if len(summary[section]) > 32:
            raise ContextError("context_section_limit")
    # Explicitly updating an old entry to superseded alongside its replacement
    # is valid. Conflicting active status or circular replacement is not.
    entries_by_id = {entry["id"]: entry for entry in upserts}
    if any(entries_by_id[old]["status"] != "superseded" for entry in upserts
           for old in set(entry.get("supersedes", [])) & batch_ids):
        raise ContextError("context_supersedes_batch_conflict")
    graph = {item.get("id", f"legacy:{section}:{index}"): item.get("supersedes", [])
             for section in SUMMARY_KEYS for index, item in enumerate(summary[section])}
    if any(not isinstance(relations, list) or any(not isinstance(old, str) for old in relations) for relations in graph.values()):
        raise ContextError("invalid_context_supersedes")
    active, finished = set(), set()
    def visit(identifier):
        if identifier in active:
            raise ContextError("context_supersedes_cycle")
        if identifier in finished or identifier not in graph:
            return
        active.add(identifier)
        for old in graph[identifier]:
            if isinstance(old, str):
                visit(old)
        active.remove(identifier)
        finished.add(identifier)
    for identifier in graph:
        visit(identifier)
    if len(_json(summary).encode("utf-8")) > 32768:
        raise ContextError("context_summary_too_large")
    return summary


def commit_edit(engine, target, plan, operation, args, *, tool_call_id=None):
    if target != plan.target:
        raise ContextError("stale_context_plan")
    expected_keys = {"expected_context_version", "upserts"} if operation == "context_update" else {"expected_context_version", "keep_refs", "release_refs", "purpose"}
    if operation not in {"context_update", "context_select"} or not isinstance(args, dict) or set(args) != expected_keys or not _integer(args["expected_context_version"]):
        raise ContextError("invalid_context_edit_arguments")
    if args["expected_context_version"] != plan.input_ref["expected_context_version"]:
        raise ContextConflict()
    # Native retries are idempotent, including when their first atomic commit
    # already advanced the live version. Never make a second revision.
    if tool_call_id is not None:
        saved = engine.store.get_tool_result(target, tool_call_id)
        if saved and saved.get("status") == "success":
            action = engine.store.db.execute("SELECT name,arguments FROM tool_calls WHERE turn_id=? AND tool_call_id=?", (target.turn_id, tool_call_id)).fetchone()
            if not action or action["name"] != operation or json.loads(action["arguments"]) != args:
                raise ContextError("context_edit_tool_arguments_conflict")
            if not engine.store.is_current(target):
                raise ContextError("stale_turn")
            return saved["data"]
    if engine.store.get_context(target.session_id)["context_version"] != args["expected_context_version"]:
        raise ContextConflict()
    context = deepcopy(plan.before_view.context)
    visible = set(plan.input_ref["visible_refs"])
    if operation == "context_update":
        context["summary"] = _update(engine, target, context["summary"], args["upserts"], visible)
    else:
        kept, released = _references(args["keep_refs"]), _references(args["release_refs"])
        if set(kept) & set(released) or not isinstance(args["purpose"], str) or len(args["purpose"]) > 120:
            raise ContextError("invalid_context_selection")
        if not set(kept) <= visible:
            raise ContextError("context_reference_not_visible")
        context["selected_refs"] = [ref for ref in dict.fromkeys([*context["selected_refs"], *kept]) if ref not in released]
        if len(context["selected_refs"]) > 64:
            raise ContextError("invalid_context_selection")
        context["selection_purpose"] = args["purpose"] if context["selected_refs"] else ""
    context["context_version"] += 1
    after = engine._assemble(target, plan.user_text, plan.system, plan.tools, plan.current_chain,
        plan.before_view.material, context, plan.before_view.notes, plan.request_fields)
    if after.metrics["hard_limit_exceeded"]:
        raise ContextError("context_edit_over_budget")
    frozen = deepcopy(plan.input_ref)
    frozen["estimate"] = {"before_tokens": plan.before_view.metrics["input_tokens_estimate"],
        "after_tokens": after.metrics["input_tokens_estimate"], "budget_tokens": engine.policy.input_budget_tokens}
    if plan.deadline is not None and time.monotonic() >= plan.deadline:
        raise ContextError("context_edit_timeout")
    return engine.store.commit_context_edit(target, operation, args, context["summary"], context["selected_refs"],
        context.get("selection_purpose", ""), input_ref=frozen, metrics=after.metrics, tool_call_id=tool_call_id, deadline=plan.deadline)
