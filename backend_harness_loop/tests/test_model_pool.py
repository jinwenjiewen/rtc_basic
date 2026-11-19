"""Transport reuse never shares per-call state, credentials, or cancellation."""

import asyncio
from dataclasses import replace
import unittest

import httpx

from backend_harness_loop.models import ModelClientPool, ModelConfigurationError
from backend_harness_loop.tests import test_models as fixture


class ModelPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_reuse_with_fresh_model_state_and_explicit_pool_shutdown(self):
        clients=[]
        def create(profile):
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=fixture.payload(fixture.event({"content":"ok"}),fixture.event(finish="stop")))))
            clients.append(client);return client
        pool=ModelClientPool(client_factory=create)
        p=fixture.profile()
        first,second=pool.model(p),pool.model(p)
        self.assertIsNot(first,second)
        self.assertIs(first._client,second._client)
        first.last_usage["prompt_tokens"]=99
        self.assertEqual(second.last_usage,{})
        await first.aclose()
        self.assertFalse(clients[0].is_closed)
        self.assertEqual((await second.complete([{"role":"user","content":"fixture"}])).content,"ok")
        await pool.aclose()
        self.assertTrue(clients[0].is_closed)
        with self.assertRaisesRegex(ModelConfigurationError,"model_client_pool_closed"):pool.model(p)

    async def test_profiles_use_distinct_clients_and_live_profile_changes_are_rejected(self):
        pool=ModelClientPool(client_factory=lambda p:httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200))))
        p=fixture.profile()
        a=pool.model(p)
        b=pool.model(replace(p,profile_id="pro",model="other",api_key="other-unused-key"))
        self.assertIsNot(a._client,b._client)
        with self.assertRaisesRegex(ModelConfigurationError,"model_profile_changed_during_runtime"):
            pool.model(replace(p,api_key="changed-unused-key"))
        await pool.aclose()

    async def test_cancelling_one_stream_does_not_close_another_request_transport(self):
        entered=asyncio.Event()
        class Blocked(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set();await asyncio.Event().wait()
                yield b""
            async def aclose(self):pass
        def handler(request):
            if b"slow" in request.content:return httpx.Response(200,stream=Blocked())
            return httpx.Response(200,content=fixture.payload(fixture.event({"content":"fast"}),fixture.event(finish="stop")))
        pool=ModelClientPool(client_factory=lambda p:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        a,b=pool.model(fixture.profile()),pool.model(fixture.profile())
        slow=asyncio.create_task(a.complete([{"role":"user","content":"slow"}]))
        await entered.wait()
        slow.cancel()
        with self.assertRaises(asyncio.CancelledError):await slow
        self.assertFalse(b._client.is_closed)
        self.assertEqual((await b.complete([{"role":"user","content":"fast"}])).content,"fast")
        await pool.aclose()


if __name__=="__main__":unittest.main()
