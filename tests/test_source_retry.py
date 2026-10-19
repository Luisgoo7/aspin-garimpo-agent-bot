import asyncio
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from app import source_retry


def test_failed_query_is_persisted_without_blocking_main_cursor(monkeypatch):
    state={}
    monkeypatch.setattr(source_retry.storage,'get_state',lambda key:state.get(key,{}))
    monkeypatch.setattr(source_retry.storage,'set_state',lambda key,value:state.update({key:value}))
    now=datetime.now(timezone.utc)
    cursor={'since':'2026-10-10','until':'2026-10-17','date_index':0,'term_index':5,'page':1}
    batch=SimpleNamespace(retry_cursors=[cursor])
    metrics={'djen_documents':0,'djen_pages':0,'limited':False}
    asyncio.run(source_retry.recover_queries(batch,metrics,[],now))
    assert state['djen_retry_queue']['queries'][0]['cursor']==cursor
    assert metrics['djen_queries_pending']==1 and metrics['limited']


def test_due_query_recovers_and_leaves_queue(monkeypatch):
    now=datetime.now(timezone.utc)
    cursor={'since':'2026-10-10','until':'2026-10-17','date_index':0,'term_index':5,'page':1}
    state={'djen_retry_queue':{'queries':[{'cursor':cursor,'attempts':0,'next_at':(now-timedelta(minutes=1)).isoformat()}]}}
    monkeypatch.setattr(source_retry.storage,'get_state',lambda key:state.get(key,{}))
    monkeypatch.setattr(source_retry.storage,'set_state',lambda key,value:state.update({key:value}))
    saved=[]
    monkeypatch.setattr(source_retry.storage,'upsert_documents',saved.extend)
    result=SimpleNamespace(rows=[{'id':'recovered'}],pages=1,errors=[],cursor=None)
    search=AsyncMock(return_value=result)
    monkeypatch.setattr(source_retry,'search_djen',search)
    metrics={'djen_documents':0,'djen_pages':0,'limited':False}
    asyncio.run(source_retry.recover_queries(SimpleNamespace(retry_cursors=[]),metrics,[],now))
    assert state['djen_retry_queue']['queries']==[]
    assert saved==[{'id':'recovered'}]
    assert search.call_args.kwargs['stop_after_query'] is True
