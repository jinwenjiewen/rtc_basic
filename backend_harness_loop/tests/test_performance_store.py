"""Bulk reads preserve the old frozen-material bytes; work no longer scales in SQL calls."""

from dataclasses import replace
import hashlib
import json
import unittest
from unittest.mock import patch

from backend_harness_loop.context import ContextEngine, ContextError
from backend_harness_loop.models import ModelProfile, ModelProfiles
from backend_harness_loop.runtime import AgentRuntime, RuntimeFault
from backend_harness_loop.store import RuntimeStore, StoreError
from backend_harness_loop.tests import test_context_edits as edits_fixture
from backend_harness_loop.tests import test_context_segments as segments_fixture


def reference_material(store, session_id, excluded=None, snapshot_seq=None):
    """v0.13 per-turn reader, deliberately independent of the new bulk scan."""
    through = store.db.execute("SELECT COALESCE(MAX(seq),0) FROM ledger WHERE session_id=?", (session_id,)).fetchone()[0]
    if snapshot_seq is not None:
        through = min(through, snapshot_seq)
    turns = store.db.execute("SELECT t.*,MIN(l.seq) first_seq FROM turns t JOIN ledger l ON l.turn_id=t.turn_id AND l.session_id=t.session_id WHERE t.session_id=? AND l.seq<=? GROUP BY t.turn_id ORDER BY first_seq", (session_id,through)).fetchall()
    groups = []
    for index, turn in enumerate(turns):
        if turn["turn_id"] == excluded:
            continue
        upper = turns[index+1]["first_seq"]-1 if index+1<len(turns) else through
        boundary = store.db.execute("SELECT MAX(seq) FROM ledger WHERE session_id=? AND turn_id=? AND seq<=?", (session_id,turn["turn_id"],upper)).fetchone()[0]
        rows = store.db.execute("SELECT seq,payload_json,created_at FROM ledger WHERE session_id=? AND turn_id=? AND kind='message' AND seq<=? ORDER BY seq", (session_id,turn["turn_id"],through)).fetchall()
        messages = []
        for row in rows:
            message = json.loads(row["payload_json"])
            if message["role"] == "assistant" and not message.get("tool_calls") and turn["delivery_status"] != "completed":
                if message.get("content") != turn["delivery_text"]:
                    message["generated_content"] = message.get("content")
                message["content"] = turn["delivery_text"]
            messages.append({"seq":row["seq"],"reference":"seq:"+str(row["seq"]),"created_at":row["created_at"],
                "turn_status":turn["state"],"delivery_status":turn["delivery_status"],
                "audience":"internal_tool_protocol" if message["role"]=="tool" or message.get("tool_calls") else "conversation",
                "source_type":"user_statement" if message["role"]=="user" else "tool_evidence" if message["role"]=="tool" else "assistant_claim",**message})
        opened = store.db.execute("SELECT COUNT(*) FROM tool_calls WHERE turn_id=? AND state IN ('prepared','dispatched')",(turn["turn_id"],)).fetchone()[0]
        late = [row["seq"] for row in rows if row["seq"]>upper]
        groups.append({"turn_id":turn["turn_id"],"round_id":turn["round_id"],"epoch":turn["epoch"],"state":turn["state"],
            "delivery_status":turn["delivery_status"],"delivery_text":turn["delivery_text"],"assistant_text":turn["assistant_text"],
            "first_seq":turn["first_seq"],"through_seq":boundary,"messages":messages,"open_tools":opened,"late_message_seqs":late,
            "blocked":turn["state"] not in {"generation_completed","cancelled","failed"} or bool(opened or late)})
    digest = hashlib.sha256()
    for fragment in json.JSONEncoder(ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).iterencode(groups):
        digest.update(fragment.encode("utf-8"))
    return {"groups":groups,"ledger_through_seq":through,
        "business_through_seq":max((m["seq"] for g in groups for m in g["messages"]),default=0),
        "blocked_turn_ids":[g["turn_id"] for g in groups if g["blocked"]],"material_fingerprint":digest.hexdigest()}


class BulkMaterialTests(unittest.TestCase):
    def setUp(self):
        self.store=RuntimeStore(":memory:")
        self.store.create_session("session","run","fixture")
        self.store.create_session("other","other-run","fixture")
        self.serial=0

    def tearDown(self):
        self.store.close()

    def seed(self,count):
        result=[]
        for _ in range(count):
            self.serial+=1
            owner=self.store.admit_turn("session",str(self.serial),"原文😀"+str(self.serial)+"x"*200,"c"+str(self.serial))
            if self.serial%3==0:
                call={"id":"read","type":"function","function":{"name":"context_read_history","arguments":"{}"}}
                self.store.prepare_tool_calls(owner,{"role":"assistant","content":None,"tool_calls":[call]})
                self.store.mark_tool_dispatched(owner,"read")
                self.store.complete_tool_call(owner,"read",{"status":"success","data":{"value":"known"},"error":None,"truncated":False})
            self.store.record_delivery(owner,"heard","delta")
            self.store.complete_turn(owner,"generation_completed","heard and generated suffix")
            if self.serial%4:
                self.store.record_delivery(owner,"","completed")
            result.append(owner)
        return result

    def test_bulk_scan_is_byte_equivalent_for_native_partial_late_open_and_interleaved_history(self):
        old=self.seed(12)
        foreign=self.store.admit_turn("other","1","foreign","foreign")
        self.store.complete_turn(foreign,"generation_completed","")
        self.seed(3)
        current=self.store.admit_turn("session","current","now","now")
        snap=self.store.db.execute("SELECT MAX(seq) FROM ledger").fetchone()[0]
        self.store.prepare_tool_calls(current,{"role":"assistant","content":None,"tool_calls":[
            {"id":"open","type":"function","function":{"name":"context_read_history","arguments":"{}"}}]})
        self.store._event("session",old[0].turn_id,old[0].epoch,"message",{"role":"assistant","content":"late"},"assistant")
        for excluded in (None,current.turn_id):
            for upper in (None,snap,snap-5):
                self.assertEqual(self.store._context_material("session",excluded,snapshot_seq=upper),reference_material(self.store,"session",excluded,upper))

    def test_query_count_is_constant_and_all_history_is_retained(self):
        counts=[]
        for amount in (20,80):
            self.seed(amount)
            traced=[]
            self.store.db.set_trace_callback(lambda sql: traced.append(1))
            material=self.store.context_material("session")
            self.store.db.set_trace_callback(None)
            counts.append(len(traced))
        self.assertEqual(counts[0],counts[1])
        self.assertLessEqual(counts[1],8)
        self.assertEqual(len(material["groups"]),100)

    def test_single_preflight_reuses_only_its_frozen_read_and_still_measures_exact_request(self):
        self.seed(6)
        owner=self.store.admit_turn("session","current","now","now")
        p=ModelProfile("fixture","fixture","https://fixture.example","fixture","KEY","unused")
        runtime=AgentRuntime(self.store,ModelProfiles({"fixture":p},"fixture"),model_factory=lambda p:None)
        runtime.register_session("session","run","fixture","task","user")
        session=runtime.binding("run")
        with patch.object(self.store,"context_material",wraps=self.store.context_material) as reads:
            view=runtime._view(session,owner,"now",runtime._schemas())
            plan,tools,actual,edit=runtime._decision_context(session,owner,"now",[],view)
            self.assertEqual(reads.call_count,1)
        fresh=runtime._view(session,owner,"now",tools)
        self.assertEqual(actual.messages,fresh.messages)
        self.assertEqual(actual.metrics,fresh.metrics)
        self.assertEqual(edit.input_ref["material_fingerprint"],fresh.material["material_fingerprint"])
        changed=self.store.admit_turn("session","next","new","new")
        with self.assertRaisesRegex(RuntimeFault,"stale_context_view"):
            runtime._decision_context(session,changed,"new",[],view)
        with self.assertRaisesRegex(ContextError,"stale_context_view"):
            runtime._engine(session).prepare(changed,"new","rules",[],force=True,view=view)


class DeadlineTransactionTests(unittest.TestCase):
    def test_late_native_stage_rolls_back_receipt_version_event_and_tool_success(self):
        case=segments_fixture.SegmentTests();case.setUp()
        try:
            plan=replace(case.plan(),deadline=1)
            args=case.args(plan)
            case.store.prepare_tool_calls(case.owner,{"role":"assistant","content":None,"tool_calls":[
                {"id":"stage","type":"function","function":{"name":"context_compact","arguments":json.dumps(args)}}]})
            case.store.mark_tool_dispatched(case.owner,"stage")
            clock=[0]
            original=case.store._finish_tool
            def finish(*args,**kwargs):
                result=original(*args,**kwargs);clock[0]=2;return result
            with patch("backend_harness_loop.store.time.monotonic",side_effect=lambda:clock[0]),patch.object(case.store,"_finish_tool",side_effect=finish):
                with self.assertRaisesRegex(StoreError,"context_compaction_timeout"):
                    case.engine.commit(case.owner,plan,args,tool_call_id="stage")
            self.assertEqual(case.store.snapshot("session")["context_candidate_count"],0)
            self.assertIsNone(case.store.get_tool_result(case.owner,"stage"))
        finally:case.tearDown()

    def test_deadline_expiring_in_material_validation_blocks_context_edit(self):
        case=edits_fixture.ContextEditTests();case.setUp()
        try:
            plan=replace(case.plan(),deadline=1)
            original=case.store._context_material;clock=[0]
            def material(*args,**kwargs):
                result=original(*args,**kwargs);clock[0]=2;return result
            with patch("backend_harness_loop.store.time.monotonic",side_effect=lambda:clock[0]),patch.object(case.store,"_context_material",side_effect=material):
                with self.assertRaisesRegex(StoreError,"context_edit_timeout"):
                    case.update(plan=plan)
            self.assertEqual(case.store.snapshot("session")["context_edit_count"],0)
        finally:case.tearDown()


if __name__=="__main__":unittest.main()
